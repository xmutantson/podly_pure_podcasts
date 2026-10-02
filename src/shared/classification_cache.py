"""Classification receipts and an explicit manifest bind retries to their inputs."""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List

from podcast_processor.transcribe import Segment
from shared.files import atomic_output


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def transcript_fingerprint(segments: List[Segment]) -> str:
    return fingerprint([segment.dict() for segment in segments])


@dataclass
class ClassificationChunk:
    directory: str
    start: int
    end: int
    fingerprint: str


def write_manifest(
    folder: Path, segments: List[Segment], chunks: List[ClassificationChunk]
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with atomic_output(folder / "manifest.json") as temporary:
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "transcript": transcript_fingerprint(segments),
                    "chunks": [asdict(chunk) for chunk in chunks],
                }
            ),
            encoding="utf-8",
        )


def read_manifest(folder: Path, segments: List[Segment]) -> List[ClassificationChunk]:
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if (
        manifest["version"] != 1
        or manifest["transcript"] != transcript_fingerprint(segments)
        or not isinstance(manifest["chunks"], list)
    ):
        raise ValueError("Classification manifest does not match the transcript")
    chunks = [ClassificationChunk(**entry) for entry in manifest["chunks"]]
    expected_start = 0
    for chunk in chunks:
        if not all(
            isinstance(index, int) and not isinstance(index, bool)
            for index in (chunk.start, chunk.end)
        ):
            raise ValueError("Classification chunk indices must be integers")
        if chunk.start != expected_start or not chunk.start < chunk.end <= len(
            segments
        ):
            raise ValueError("Classification manifest has incomplete coverage")
        directory = f"{segments[chunk.start].start}_{segments[chunk.end - 1].end}"
        if chunk.directory != directory or not isinstance(chunk.fingerprint, str):
            raise ValueError("Invalid classification chunk identity")
        expected_start = chunk.end
    if expected_start != len(segments):
        raise ValueError("Classification manifest has incomplete coverage")
    return chunks


def read_classification(folder: Path, expected_fingerprint: str) -> str:
    receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
    identification = (folder / "identification.txt").read_text(encoding="utf-8")
    if receipt["fingerprint"] != expected_fingerprint or receipt[
        "response"
    ] != fingerprint(identification):
        raise ValueError("Classification receipt does not match current inputs/output")
    return identification


def write_classification(
    folder: Path, expected_fingerprint: str, identification: str
) -> None:
    with atomic_output(folder / "identification.txt") as temporary:
        temporary.write_text(identification, encoding="utf-8")
    # Publish the receipt last; interrupted writes cannot validate a new response.
    with atomic_output(folder / "receipt.json") as temporary:
        temporary.write_text(
            json.dumps(
                {
                    "fingerprint": expected_fingerprint,
                    "response": fingerprint(identification),
                }
            ),
            encoding="utf-8",
        )

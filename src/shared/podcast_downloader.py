import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

import requests
import validators
from flask import abort

from app.models import Post
from shared.files import atomic_output

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = "in"


def download_episode(post: Post) -> Optional[str]:
    provenance = download_provenance(post)
    download_path = str(get_and_make_download_path(post))
    if valid_download_receipt(Path(download_path), provenance):
        logger.info("Episode already downloaded with matching storage provenance.")
        return download_path

    # Missing/unverified files are never adopted just because a filename matches.
    audio_link = post.download_url
    if audio_link is None or not validators.url(audio_link):
        abort(404)

    logger.info(f"Downloading {audio_link} into {download_path}...")
    response = requests.get(audio_link, timeout=(10, 60))
    if response.status_code == 200:
        content = response.content
        with atomic_output(Path(download_path)) as temporary:
            temporary.write_bytes(content)
        # Publish provenance last: an interrupted write cannot become a cache hit.
        with atomic_output(Path(download_path).with_suffix(".json")) as temporary:
            temporary.write_text(
                json.dumps(
                    {
                        "provenance": provenance,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
        logger.info("Download complete.")
    else:
        logger.info(
            f"Failed to download the podcast episode, response: {response.status_code}"
        )
        return None

    return download_path


def download_provenance(post: Post) -> Dict[str, Any]:
    namespace = getattr(getattr(post, "feed", None), "storage_namespace", None)
    if isinstance(post, Post) and not namespace:
        raise ValueError("Persist the feed storage namespace before downloading")
    # URL-only callers have no durable database feed; never fall back to reusable IDs.
    identity = (
        ["feed", namespace, post.guid] if namespace else ["url", post.download_url]
    )
    return {"version": 2, "identity": identity}


def valid_download_receipt(path: Path, provenance: Dict[str, Any]) -> bool:
    try:
        receipt = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        if (
            receipt["provenance"] != provenance
            or not path.is_file()
            or path.stat().st_size == 0
        ):
            return False
        digest = hashlib.sha256()
        with path.open("rb") as audio:
            for block in iter(lambda: audio.read(1024 * 1024), b""):
                digest.update(block)
        expected_digest = receipt["sha256"]
        return (
            isinstance(expected_digest, str) and expected_digest == digest.hexdigest()
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def get_and_make_download_path(post: Post) -> Path:
    """Versioned identity excludes reusable database IDs and display metadata."""
    provenance = download_provenance(post)
    digest = hashlib.sha256(
        json.dumps(provenance, sort_keys=True).encode("utf-8")
    ).hexdigest()
    basename = f"episode-v2-{digest}"
    directory = Path(DOWNLOAD_DIR) / "v2" / basename
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{basename}.mp3"


def sanitize_title(title: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\s]", "", title)


def find_audio_link(entry: Any) -> str:
    for link in entry.links:
        if link.type == "audio/mpeg":
            href = link.href
            assert isinstance(href, str)
            return href

    return str(entry.id)

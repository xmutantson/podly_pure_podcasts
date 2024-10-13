import logging
from unittest.mock import MagicMock

import pytest
import yaml
from openai import OpenAI
from openai.types.audio.transcription_segment import TranscriptionSegment
from pytest_mock import MockerFixture


@pytest.fixture(autouse=True)
def mock_whisper_fixture(mocker: MockerFixture) -> None:
    mocker.patch("podcast_processor.transcribe.whisper", new=MagicMock())
    mocker.patch.dict("sys.modules", {"whisper": MagicMock()})


@pytest.mark.skip
def test_remote_transcribe() -> None:
    from podcast_processor.transcribe import RemoteWhisperTranscriber

    logger = logging.getLogger("global_logger")
    with open("config/config.yml", "r") as f:
        config = yaml.safe_load(f)
    client = OpenAI(
        base_url=(
            config["openai_base_url"]
            if "openai_base_url" in config
            else "https://api.openai.com/v1"
        ),
        api_key=config["openai_api_key"],
    )
    transcriber = RemoteWhisperTranscriber(logger, client)

    transcription = transcriber.transcribe("file.mp3")
    assert transcription == []


@pytest.mark.skip
def test_local_transcribe() -> None:
    from podcast_processor.transcribe import LocalWhisperTranscriber

    logger = logging.getLogger("global_logger")
    transcriber = LocalWhisperTranscriber(logger, "base")
    transcription = transcriber.transcribe("src/tests/file.mp3")
    assert transcription == []


def test_offset() -> None:
    from podcast_processor.transcribe import RemoteWhisperTranscriber

    assert RemoteWhisperTranscriber.add_offset_to_segments(
        [
            TranscriptionSegment(
                id=1,
                avg_logprob=2,
                seek=6,
                temperature=7,
                text="hi",
                tokens=[],
                compression_ratio=3,
                no_speech_prob=4,
                start=12.345,
                end=45.678,
            )
        ],
        123,
    ) == [
        TranscriptionSegment(
            id=1,
            avg_logprob=2,
            seek=6,
            temperature=7,
            text="hi",
            tokens=[],
            compression_ratio=3,
            no_speech_prob=4,
            start=12.468,
            end=45.800999999999995,
        )
    ]
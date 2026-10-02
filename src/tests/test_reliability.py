"""Offline regressions using synthetic files and an in-memory database."""

# App imports must occur after the fixture isolates configuration and storage.
# pylint: disable=import-outside-toplevel

import importlib
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Optional, cast
from unittest.mock import MagicMock

import pytest
from flask import Flask, current_app
from jinja2 import Template

from shared.config import get_config
from shared.files import atomic_output


@pytest.fixture(name="runtime")
def runtime_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    config = get_config("config/config.yml.example")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setitem(sys.modules, "whisper", MagicMock())
    monkeypatch.setattr("shared.config.get_config", lambda _: config)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    app_module = importlib.import_module("app")
    models = importlib.import_module("app.models")
    processor_module = importlib.import_module("podcast_processor.podcast_processor")
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    app_module.db.init_app(app)
    with app.app_context():
        app_module.db.create_all()
        feed = models.Feed(
            title="Synthetic feed", rss_url="https://example.invalid/rss"
        )
        post = models.Post(
            feed=feed,
            title="Synthetic episode",
            guid="synthetic-guid",
            download_url="https://example.invalid/episode.mp3",
            whitelisted=True,
        )
        app_module.db.session.add(post)
        app_module.db.session.commit()
        processor = processor_module.PodcastProcessor(config)
        yield app_module, post, processor, processor_module
        app_module.db.session.remove()
        app_module.db.drop_all()


def test_atomic_output_preserves_previous_file(tmp_path: Path) -> None:
    target = tmp_path / "audio.mp3"
    target.write_bytes(b"completed")
    with pytest.raises(RuntimeError):
        with atomic_output(target) as temporary:
            temporary.write_bytes(b"partial")
            raise RuntimeError("encoder failed")
    assert target.read_bytes() == b"completed"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_output_rejects_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "audio.mp3"
    with pytest.raises(ValueError, match="Empty output"):
        with atomic_output(target):
            pass
    assert not list(tmp_path.iterdir())


def test_retry_preserves_download(runtime: Any) -> None:
    _, post, _, _ = runtime
    from app.jobs import clean_download_path

    source = Path("source.mp3")
    source.write_bytes(b"completed source")
    post.unprocessed_audio_path = str(source)
    clean_download_path(post)
    assert source.read_bytes() == b"completed source"
    assert post.unprocessed_audio_path == str(source)


def test_scheduled_retry_does_not_delete_completed_files(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, post, _, _ = runtime
    from app import jobs

    source = Path("source.mp3")
    source.write_bytes(b"completed source")
    post.unprocessed_audio_path = str(source)
    monkeypatch.setattr(
        jobs,
        "scheduler",
        SimpleNamespace(app=current_app),
    )

    def process(*_args: Any, **_kwargs: Any) -> None:
        assert source.read_bytes() == b"completed source"
        raise RuntimeError("retryable classification failure")

    monkeypatch.setattr(jobs, "download_and_process_post", process)
    jobs.process_post(post)
    assert source.read_bytes() == b"completed source"


@pytest.mark.parametrize("state", ["missing", "empty", "directory"])
def test_reconcile_unusable_paths(runtime: Any, state: str) -> None:
    _, post, _, _ = runtime
    from app.jobs import clean_post

    source = Path("source.mp3")
    if state == "empty":
        source.touch()
    elif state == "directory":
        source.mkdir()
    post.unprocessed_audio_path = post.processed_audio_path = str(source)
    clean_post(post)
    assert post.unprocessed_audio_path is None
    assert post.processed_audio_path is None


def test_download_recovers_stale_database_path(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, post, _, module = runtime
    from app import posts
    from shared.podcast_downloader import get_and_make_download_path

    source = get_and_make_download_path(post)
    source.write_bytes(b"completed source")
    post.unprocessed_audio_path = "missing.mp3"
    post.processed_audio_path = "missing-result.mp3"
    process = MagicMock(return_value="result.mp3")
    monkeypatch.setattr(module.PodcastProcessor, "process", process)
    assert posts.download_and_process_post(post.guid) == "result.mp3"
    assert post.unprocessed_audio_path == str(source)
    assert source.read_bytes() == b"completed source"
    process.assert_called_once()


def test_completed_audio_does_not_require_redownload(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, post, _, _ = runtime
    from app import posts

    result = Path("completed.mp3")
    result.write_bytes(b"completed audio")
    post.processed_audio_path = str(result)
    download = MagicMock(side_effect=AssertionError("unexpected download"))
    monkeypatch.setattr(posts, "download_episode", download)
    assert posts.download_and_process_post(post.guid) == str(result)
    download.assert_not_called()


@pytest.mark.parametrize("content", [b"synthetic audio", b""])
def test_download_publishes_only_nonempty_results(
    runtime: Any, monkeypatch: pytest.MonkeyPatch, content: Any
) -> None:
    _, post, _, _ = runtime
    from shared import podcast_downloader as downloader

    request = MagicMock(return_value=MagicMock(status_code=200, content=content))
    monkeypatch.setattr("shared.podcast_downloader.requests.get", request)
    if content:
        path = downloader.download_episode(post)
        assert path is not None
        assert Path(path).read_bytes() == content
    else:
        with pytest.raises(ValueError):
            downloader.download_episode(post)
        assert not list(Path("in").rglob("*.mp3"))
    request.assert_called_once_with(post.download_url, timeout=(10, 60))


def prepare_processing(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    _, post, processor, module = runtime
    source = Path("source.mp3")
    source.write_bytes(b"synthetic source")
    post.unprocessed_audio_path = str(source)
    monkeypatch.setattr(processor, "transcribe", lambda _: [])
    monkeypatch.setattr(processor, "get_user_prompt_template", lambda _: Template(""))
    monkeypatch.setattr(processor, "get_system_prompt", lambda _: "")
    monkeypatch.setattr(module, "get_audio_duration_ms", lambda _: 10000)
    return post, processor, module


def test_encoding_failure_can_be_retried(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    post, processor, module = prepare_processing(runtime, monkeypatch)
    calls = []

    def encode(**kwargs: Any) -> None:
        calls.append(kwargs)
        Path(kwargs["out_path"]).write_bytes(b"synthetic encoded audio")
        if len(calls) == 1:
            raise RuntimeError("encoder interrupted")

    monkeypatch.setattr(module, "clip_segments_with_fade", encode)
    with pytest.raises(RuntimeError):
        processor.process(post, blocking=False)
    assert post.processed_audio_path is None
    assert not list(Path("srv").rglob("*.mp3"))
    result = processor.process(post, blocking=False)
    assert Path(result).read_bytes() == b"synthetic encoded audio"
    assert post.processed_audio_path == result
    assert len(calls) == 2


def test_processor_recovers_db_after_completed_file(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    post, processor, module = prepare_processing(runtime, monkeypatch)
    paths = module.get_post_processed_audio_path(post)
    processor.make_dirs(paths)
    paths.post_processed_audio_path.write_bytes(b"completed result")
    result = processor.process(post, blocking=False)
    assert post.processed_audio_path == result


@pytest.mark.parametrize(
    "cached", [None, "truncated", '{"ad_segments": [0], "confidence": 1}']
)
def test_classification_validates_cache(
    runtime: Any, monkeypatch: pytest.MonkeyPatch, cached: Optional[str]
) -> None:
    _, post, processor, module = runtime
    segment = module.Segment(start=0, end=1, text="synthetic")
    folder = Path("classification/0.0_1.0")
    folder.mkdir(parents=True)
    cache = folder / "identification.txt"
    if cached is not None:
        cache.write_text(cached)
    valid = '{"ad_segments": [0], "confidence": 1}'
    call = MagicMock(return_value=valid)
    monkeypatch.setattr(processor, "call_model", call)
    processor.classify(
        transcript_segments=[segment],
        model="offline",
        system_prompt="",
        user_prompt_template=Template("synthetic"),
        num_segments_per_prompt=30,
        post=post,
        classification_path=folder.parent,
    )
    assert cache.read_text() == valid
    # Legacy responses lack provenance, even if syntactically valid.
    assert call.call_count == 1
    processor.classify(
        transcript_segments=[segment],
        model="offline",
        system_prompt="",
        user_prompt_template=Template("synthetic"),
        num_segments_per_prompt=30,
        post=post,
        classification_path=folder.parent,
    )
    assert call.call_count == 1
    assert not (folder / ".in_progress").exists()


@pytest.mark.parametrize("response", [None, "", "truncated", '{"confidence": 1}'])
def test_bad_classification_never_becomes_success(
    runtime: Any, monkeypatch: pytest.MonkeyPatch, response: Optional[str]
) -> None:
    _, post, processor, module = runtime
    folder = Path("classification")
    folder.mkdir()
    monkeypatch.setattr(processor, "call_model", lambda *args: response)
    with pytest.raises((module.ProcessorException, ValueError, AssertionError)):
        processor.classify(
            transcript_segments=[module.Segment(start=0, end=1, text="synthetic")],
            model="offline",
            system_prompt="",
            user_prompt_template=Template(""),
            num_segments_per_prompt=30,
            post=post,
            classification_path=folder,
        )
    assert not list(folder.rglob("identification.txt"))
    assert not list(folder.rglob(".in_progress"))


def test_retry_reuses_successful_classification_chunks(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, post, processor, module = runtime
    folder = Path("classification")
    folder.mkdir()
    valid = '{"ad_segments": [], "confidence": 1}'
    call = MagicMock(side_effect=[valid, RuntimeError("temporary failure"), valid])
    monkeypatch.setattr(processor, "call_model", call)
    kwargs = {
        "transcript_segments": [
            module.Segment(start=0, end=1, text="first"),
            module.Segment(start=1, end=2, text="second"),
        ],
        "model": "offline",
        "system_prompt": "",
        "user_prompt_template": Template(""),
        "num_segments_per_prompt": 1,
        "post": post,
        "classification_path": folder,
    }
    with pytest.raises(RuntimeError):
        processor.classify(**kwargs)
    assert len(list(folder.rglob("identification.txt"))) == 1
    processor.classify(**kwargs)
    assert call.call_count == 3
    assert len(list(folder.rglob("identification.txt"))) == 2


@pytest.mark.parametrize("content", [None, "truncated"])
def test_missing_or_bad_classification_stops_processing(
    runtime: Any, content: Any
) -> None:
    _, _, processor, module = runtime
    folder = Path("classification/0.0_1.0")
    folder.mkdir(parents=True)
    if content is not None:
        (folder / "identification.txt").write_text(content)
    with pytest.raises(module.ProcessorException):
        processor.get_ad_segments([], folder.parent)


def test_merge_preserves_nested_intervals_and_audio_units(runtime: Any) -> None:
    _, _, processor, _ = runtime
    assert processor.merge_ad_segments(
        duration_ms=100000,
        min_ad_segment_length_seconds=0.0,
        min_ad_segment_separation_seconds=5.0,
        ad_segments=[(0.0, 40.0), (10.0, 20.0), (90.0, 98.0)],
    ) == [(0, 40000), (90000, 100000)]


@pytest.mark.usefixtures("runtime")
def test_timeout_retains_caller_and_waits_for_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import timeout_decorator as timeout

    clock = iter([0, 2])
    monkeypatch.setattr("app.timeout_decorator.time.monotonic", lambda: next(clock))
    caller = threading.get_ident()
    completed = []

    @timeout.timeout_decorator(1)
    def work() -> Any:
        assert threading.get_ident() == caller
        completed.append(True)

    with pytest.raises(timeout.TimeoutException, match="completed"):
        work()
    assert completed == [True]


@pytest.mark.usefixtures("runtime")
def test_timeout_propagates_original_exception() -> None:
    from app.timeout_decorator import timeout_decorator

    error = RuntimeError("original failure")

    @timeout_decorator(10)
    def work() -> Any:
        raise error

    with pytest.raises(RuntimeError) as caught:
        work()
    assert caught.value is error


@pytest.mark.usefixtures("runtime")
def test_timeout_returns_successful_result() -> None:
    from app.timeout_decorator import timeout_decorator

    @timeout_decorator(10)
    def work() -> Any:
        return "completed"

    assert work() == "completed"


@pytest.mark.parametrize(
    "changed", ["transcript", "model", "system", "prompt", "chunk_size"]
)
def test_classification_cache_is_bound_to_current_inputs(
    runtime: Any, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    _, post, processor, module = runtime
    segments = [
        module.Segment(start=0, end=1, text="first"),
        module.Segment(start=1, end=2, text="second"),
    ]
    call = MagicMock(return_value='{"ad_segments": [], "confidence": 1}')
    monkeypatch.setattr(processor, "call_model", call)
    kwargs: dict[str, Any] = {
        "transcript_segments": segments,
        "model": "offline",
        "system_prompt": "",
        "user_prompt_template": Template("{{ transcript }}"),
        "num_segments_per_prompt": 2,
        "post": post,
        "classification_path": Path("classification"),
    }
    processor.classify(**kwargs)
    processor.classify(**kwargs)
    assert call.call_count == 1
    if changed == "transcript":
        segments[0].text = "corrected transcript"
    elif changed == "model":
        kwargs["model"] = "different-model"
    elif changed == "system":
        kwargs["system_prompt"] = "different instructions"
    elif changed == "prompt":
        kwargs["user_prompt_template"] = Template("new prompt {{ transcript }}")
    else:
        kwargs["num_segments_per_prompt"] = 1
    processor.classify(**kwargs)
    assert call.call_count == (3 if changed == "chunk_size" else 2)
    assert processor.get_ad_segments(segments, Path("classification")) == []


def test_current_manifest_rejects_incomplete_and_altered_results(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, post, processor, module = runtime
    segments = [
        module.Segment(start=0, end=1, text="first"),
        module.Segment(start=1, end=2, text="second"),
    ]
    call = MagicMock(
        side_effect=[
            '{"ad_segments": [], "confidence": 1}',
            RuntimeError("interrupted"),
        ]
    )
    monkeypatch.setattr(processor, "call_model", call)
    kwargs: dict[str, Any] = {
        "transcript_segments": segments,
        "model": "offline",
        "system_prompt": "",
        "user_prompt_template": Template(""),
        "num_segments_per_prompt": 1,
        "post": post,
        "classification_path": Path("classification"),
    }
    with pytest.raises(RuntimeError):
        processor.classify(**kwargs)
    with pytest.raises(module.ProcessorException):
        processor.get_ad_segments(segments, Path("classification"))
    call.side_effect = None
    call.return_value = '{"ad_segments": [], "confidence": 1}'
    processor.classify(**kwargs)
    assert call.call_count == 3  # The completed current chunk was reused.
    assert processor.get_ad_segments(segments, Path("classification")) == []
    Path("classification/0.0_1.0/identification.txt").write_text(
        '{"ad_segments": [0], "confidence": 1}'
    )
    with pytest.raises(module.ProcessorException):
        processor.get_ad_segments(segments, Path("classification"))
    processor.classify(**kwargs)
    assert call.call_count == 4
    segments[0].text = "changed"
    with pytest.raises(module.ProcessorException):
        processor.get_ad_segments(segments, Path("classification"))


def test_download_identity_is_stable_and_ignores_legacy_title_cache(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, post, _, _ = runtime
    from shared import podcast_downloader as downloader
    from shared.processing_paths import paths_from_unprocessed_path

    legacy = Path("in/Synthetic episode/Synthetic episode.mp3")
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"unowned old bytes")
    request = MagicMock(
        side_effect=[
            SimpleNamespace(status_code=200, content=b"first"),
            SimpleNamespace(status_code=200, content=b"second"),
        ]
    )
    monkeypatch.setattr("shared.podcast_downloader.requests.get", request)
    first = downloader.download_episode(post)
    post.title = "renamed episode"
    assert downloader.download_episode(post) == first
    other = SimpleNamespace(
        feed_id=post.feed_id + 1,
        guid=post.guid,
        title=post.title,
        download_url="https://example.invalid/second.mp3",
    )
    second = downloader.download_episode(cast(Any, other))
    assert first is not None and second is not None and first != second
    assert Path(first).read_bytes() == b"first"
    assert Path(second).read_bytes() == b"second"
    assert paths_from_unprocessed_path(
        first, "Same feed title"
    ) != paths_from_unprocessed_path(second, "Same feed title")
    assert request.call_count == 2
    assert legacy.read_bytes() == b"unowned old bytes"


def test_http_recovery_preserves_individual_and_bulk_whitelist_policy(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_module, post, _, _ = runtime
    monkeypatch.setitem(sys.modules, "bleach", SimpleNamespace(clean=MagicMock()))
    from app import routes

    output = Path("completed.mp3")
    output.write_bytes(b"completed audio")
    post.processed_audio_path = str(output)
    post.whitelisted = False
    app_module.db.session.commit()
    download = MagicMock(side_effect=AssertionError("unexpected download"))
    monkeypatch.setattr(routes, "download_episode", download)
    # pylint: disable=protected-access
    app = cast(Any, current_app)._get_current_object()
    assert routes.download_and_process(post, app)["status"] == "error"
    assert (
        routes.download_and_process(post, app, require_whitelisted=False)["status"]
        == "success"
    )
    assert post.whitelisted is False
    post.whitelisted = True
    app_module.db.session.commit()
    assert routes.download_and_process(post, app)["status"] == "success"
    assert post.whitelisted is True
    download.assert_not_called()

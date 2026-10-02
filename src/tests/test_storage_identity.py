"""Offline storage ownership, SQLite ID reuse, and migration regressions."""

# Application imports must follow the isolated runtime fixture.
# pylint: disable=import-outside-toplevel
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, cast
from unittest.mock import MagicMock

import pytest
from flask import Flask
from flask_migrate import Migrate, upgrade
from sqlalchemy import inspect, text

# Expose the shared isolated fixture to pytest in this module.
from test_reliability import runtime_fixture  # pylint: disable=unused-import

from shared.files import atomic_output

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@pytest.mark.parametrize("same_feed_url", [False, True])
def test_sqlite_id_reuse_cannot_reclaim_deleted_feed_audio(
    runtime: Any, monkeypatch: pytest.MonkeyPatch, same_feed_url: bool
) -> None:
    app, post, _, _ = runtime
    from app.models import Feed, Post
    from shared import podcast_downloader as downloader

    request = MagicMock(
        side_effect=[
            SimpleNamespace(status_code=200, content=b"old feed audio"),
            SimpleNamespace(status_code=200, content=b"new feed audio"),
        ]
    )
    monkeypatch.setattr("shared.podcast_downloader.requests.get", request)
    first = Path(cast(str, downloader.download_episode(post)))
    old_feed = post.feed
    old_id, old_guid, old_url = old_feed.id, post.guid, old_feed.rss_url
    old_namespace, old_download = old_feed.storage_namespace, post.download_url
    app.db.session.delete(post)
    app.db.session.delete(old_feed)
    app.db.session.commit()
    replacement_feed = Feed(
        title="Synthetic feed",
        rss_url=(old_url if same_feed_url else "https://example.invalid/other-feed"),
    )
    replacement = Post(
        feed=replacement_feed,
        guid=old_guid,
        title="Synthetic episode",
        download_url=old_download,
        whitelisted=True,
    )
    app.db.session.add(replacement)
    app.db.session.commit()
    assert replacement.feed_id == old_id  # Exercise actual SQLite primary-key reuse.
    assert replacement_feed.storage_namespace != old_namespace
    second = Path(cast(str, downloader.download_episode(replacement)))
    assert second != first
    assert first.read_bytes() == b"old feed audio"
    assert second.read_bytes() == b"new feed audio"
    app.db.session.expire_all()
    replacement.title = "Renamed episode"
    replacement.feed.title = "Renamed feed"
    replacement.feed.rss_url = "https://example.invalid/renamed-feed"
    app.db.session.commit()
    assert downloader.download_episode(replacement) == str(second)
    assert request.call_count == 2


@pytest.mark.parametrize("receipt_state", ["missing", "version", "owner", "content"])
def test_unverified_v2_file_is_not_a_cache_hit(
    runtime: Any, monkeypatch: pytest.MonkeyPatch, receipt_state: str
) -> None:
    _, post, _, _ = runtime
    from shared import podcast_downloader as downloader

    path = downloader.get_and_make_download_path(post)
    path.write_bytes(b"orphaned bytes")
    provenance = downloader.download_provenance(post)
    receipt = {
        "provenance": provenance,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    if receipt_state == "version":
        provenance["version"] = 1
    elif receipt_state == "owner":
        provenance["identity"] = ["feed", "deleted namespace", post.guid]
    elif receipt_state == "content":
        receipt["sha256"] = "wrong digest"
    if receipt_state != "missing":
        path.with_suffix(".json").write_text(json.dumps(receipt), encoding="utf-8")
    request = MagicMock(
        return_value=SimpleNamespace(status_code=200, content=b"owned bytes")
    )
    monkeypatch.setattr("shared.podcast_downloader.requests.get", request)
    assert downloader.download_episode(post) == str(path)
    assert path.read_bytes() == b"owned bytes"
    assert downloader.download_episode(post) == str(path)
    request.assert_called_once()


def test_old_id_based_orphan_is_never_discovered(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, post, _, _ = runtime
    from shared import podcast_downloader as downloader

    old_digest = hashlib.sha256(
        json.dumps([post.feed_id, post.guid]).encode()
    ).hexdigest()
    legacy = Path("in") / f"episode-{old_digest}" / f"episode-{old_digest}.mp3"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"v1 orphan")
    request = MagicMock(
        return_value=SimpleNamespace(status_code=200, content=b"current audio")
    )
    monkeypatch.setattr("shared.podcast_downloader.requests.get", request)
    current = Path(cast(str, downloader.download_episode(post)))
    assert current != legacy and "v2" in current.parts
    assert legacy.read_bytes() == b"v1 orphan"
    assert current.read_bytes() == b"current audio"
    request.assert_called_once()


def test_interrupted_download_receipt_is_retried(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, post, _, _ = runtime
    from shared import podcast_downloader as downloader

    request = MagicMock(
        side_effect=[
            SimpleNamespace(status_code=200, content=b"first"),
            SimpleNamespace(status_code=200, content=b"replacement"),
            SimpleNamespace(status_code=200, content=b"replacement"),
        ]
    )
    monkeypatch.setattr("shared.podcast_downloader.requests.get", request)
    path = Path(cast(str, downloader.download_episode(post)))
    old_receipt = path.with_suffix(".json").read_bytes()
    path.write_bytes(b"changed after receipt")

    @contextmanager
    def interrupted(target: Path) -> Iterator[Path]:
        with atomic_output(target) as temporary:
            yield temporary
            if target.suffix == ".json":
                raise OSError("receipt publication interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(downloader, "atomic_output", interrupted)
        with pytest.raises(OSError):
            downloader.download_episode(post)
    assert path.with_suffix(".json").read_bytes() == old_receipt
    assert not downloader.valid_download_receipt(
        path, downloader.download_provenance(post)
    )
    assert downloader.download_episode(post) == str(path)
    assert downloader.download_episode(post) == str(path)
    assert path.read_bytes() == b"replacement" and request.call_count == 3
    assert not list(path.parent.glob(".*"))


def test_legacy_database_owned_source_remains_usable(
    runtime: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, post, processor, _ = runtime
    from app.posts import download_and_process_post

    legacy = Path("legacy-title.mp3")
    legacy.write_bytes(b"database-owned legacy audio")
    post.unprocessed_audio_path = str(legacy)
    process = MagicMock(return_value="processed.mp3")
    monkeypatch.setattr(processor, "process", process)
    download = MagicMock(side_effect=AssertionError("unexpected download"))
    assert (
        download_and_process_post(post.guid, processor=processor, downloader=download)
        == "processed.mp3"
    )
    download.assert_not_called()
    process.assert_called_once()
    assert legacy.read_bytes() == b"database-owned legacy audio"


def test_real_migration_backfills_namespaces_preserving_existing_posts(
    runtime: Any, tmp_path: Path
) -> None:
    app_module, _, _, _ = runtime
    from app.models import Feed, Post, Transcript

    migration_app = Flask("migration-test")
    migration_app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///{(tmp_path / 'legacy.sqlite').as_posix()}"
    )
    app_module.db.init_app(migration_app)
    Migrate(migration_app, app_module.db, directory=str(MIGRATIONS))
    with migration_app.app_context():
        upgrade(directory=str(MIGRATIONS), revision="ded4b70feadb")
        app_module.db.session.execute(
            text(
                "INSERT INTO feed (id, title, rss_url) VALUES "
                "(1, 'Old feed', 'https://example.invalid/old'), "
                "(2, 'Other feed', 'https://example.invalid/other')"
            )
        )
        app_module.db.session.execute(
            text(
                "INSERT INTO post (id, feed_id, guid, title, download_url, whitelisted, "
                "unprocessed_audio_path, processed_audio_path) VALUES "
                "(1, 1, 'old-guid', 'Old episode', 'https://example.invalid/old.mp3', "
                "1, 'legacy-source.mp3', 'legacy-completed.mp3')"
            )
        )
        app_module.db.session.execute(
            text("INSERT INTO transcript (id, post_id, content) VALUES (1, 1, '[]')")
        )
        app_module.db.session.commit()
        upgrade(directory=str(MIGRATIONS))
        feeds = Feed.query.order_by(Feed.id).all()
        namespaces = [feed.storage_namespace for feed in feeds]
        assert len(set(namespaces)) == 2 and all(
            len(value) == 32 for value in namespaces
        )
        schema = inspect(app_module.db.engine)
        column = next(
            c for c in schema.get_columns("feed") if c["name"] == "storage_namespace"
        )
        assert not column["nullable"]
        assert any(
            c["column_names"] == ["storage_namespace"]
            for c in schema.get_unique_constraints("feed")
        )
        post = Post.query.one()
        assert (
            post.feed_id,
            post.guid,
            post.unprocessed_audio_path,
            post.processed_audio_path,
            post.whitelisted,
        ) == (1, "old-guid", "legacy-source.mp3", "legacy-completed.mp3", True)
        assert Transcript.query.one().content == "[]"
        app_module.db.session.remove()
        upgrade(
            directory=str(MIGRATIONS)
        )  # Normal repeat startup does not rotate namespaces.
        assert [
            feed.storage_namespace for feed in Feed.query.order_by(Feed.id).all()
        ] == namespaces

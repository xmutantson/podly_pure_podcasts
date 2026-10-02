"""Real SQLite/Alembic interruption and partial-state recovery, with no services."""

from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask_migrate import Migrate, upgrade
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import OperationalError

# Register the existing isolated runtime fixture in this module.
from test_reliability import runtime_fixture  # pylint: disable=unused-import

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
OLD_REVISION = "ded4b70feadb"
NEW_REVISION = "f72b62c9d310"


@pytest.fixture(name="legacy_database")
def legacy_database_fixture(runtime: Any, tmp_path: Path) -> Any:
    app_module, _, _, _ = runtime
    app = Flask("migration-retry")
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///{(tmp_path / 'retry.sqlite').as_posix()}"
    )
    db = app_module.db
    db.init_app(app)
    Migrate(app, db, directory=str(MIGRATIONS))
    with app.app_context():
        upgrade(directory=str(MIGRATIONS), revision=OLD_REVISION)
        for number in (1, 2):
            db.session.execute(
                text(
                    "INSERT INTO feed (id, title, description, author, rss_url, alt_id, image_url) "
                    "VALUES (:id, 'Legacy', 'Description', 'Author', :url, :alt, :image)"
                ),
                {
                    "id": number,
                    "url": f"https://example.invalid/feed-{number}",
                    "alt": f"alternate-{number}",
                    "image": f"https://example.invalid/image-{number}",
                },
            )
            db.session.execute(
                text(
                    "INSERT INTO post (id, feed_id, guid, title, description, "
                    "download_url, whitelisted, "
                    "unprocessed_audio_path, processed_audio_path, duration, release_date) "
                    "VALUES (:id, :id, :guid, 'Episode', 'Episode description', "
                    ":url, :flag, :source, :output, 123, '2026-10-02')"
                ),
                {
                    "id": number,
                    "guid": f"guid-{number}",
                    "url": f"https://example.invalid/audio-{number}",
                    "flag": number - 1,
                    "source": f"source-{number}.mp3",
                    "output": f"result-{number}.mp3",
                },
            )
            db.session.execute(
                text(
                    "INSERT INTO transcript (id, post_id, content) VALUES (:id, :id, '[]')"
                ),
                {"id": number},
            )
        db.session.commit()
    return app, db


def snapshot(db: Any) -> Any:
    result = {}
    for table in ("feed", "post", "transcript"):
        rows = [
            dict(row)
            for row in db.session.execute(
                text(f"SELECT * FROM {table} ORDER BY id")
            ).mappings()
        ]
        for row in rows:
            row.pop("storage_namespace", None)
        result[table] = rows
    db.session.remove()
    return result


def namespaces(db: Any) -> dict[int, str]:
    if "storage_namespace" not in {
        c["name"] for c in inspect(db.engine).get_columns("feed")
    }:
        return {}
    result = dict(
        db.session.execute(
            text(
                "SELECT id, storage_namespace FROM feed WHERE storage_namespace IS NOT NULL"
            )
        ).all()
    )
    db.session.remove()
    return result


def assert_complete(db: Any, before: Any, assigned: dict[int, str]) -> None:
    assert snapshot(db) == before
    values = namespaces(db)
    assert set(values) == {1, 2} and len(set(values.values())) == 2
    assert all(len(value) == 32 for value in values.values())
    assert all(values[feed_id] == namespace for feed_id, namespace in assigned.items())
    schema = inspect(db.engine)
    assert "_alembic_tmp_feed" not in schema.get_table_names()
    column = next(
        c for c in schema.get_columns("feed") if c["name"] == "storage_namespace"
    )
    assert not column["nullable"]
    assert any(
        c["column_names"] == ["storage_namespace"]
        for c in schema.get_unique_constraints("feed")
    )
    assert (
        db.session.execute(text("SELECT version_num FROM alembic_version")).scalar()
        == NEW_REVISION
    )
    db.session.remove()
    upgrade(directory=str(MIGRATIONS))
    assert namespaces(db) == values


@pytest.mark.parametrize(
    "stage", ["first_backfill", "second_backfill", "copy", "rename", "revision"]
)
def test_interrupted_upgrade_then_clean_retry_preserves_data(
    legacy_database: Any, stage: str
) -> None:
    app, db = legacy_database
    with app.app_context():
        before = snapshot(db)
        engine = db.engine
        updates = 0
        injected = False

        def fail_once(
            _connection: Any,
            _cursor: Any,
            statement: str,
            parameters: Any,
            _context: Any,
            _executemany: Any,
        ) -> None:
            nonlocal updates, injected
            normalized = " ".join(statement.replace('"', "").split()).upper()
            if normalized.startswith("UPDATE FEED SET STORAGE_NAMESPACE="):
                updates += 1
                current = "first_backfill" if updates == 1 else "second_backfill"
            elif normalized.startswith("INSERT INTO _ALEMBIC_TMP_FEED"):
                current = "copy"
            elif normalized.startswith("ALTER TABLE _ALEMBIC_TMP_FEED RENAME TO FEED"):
                current = "rename"
            elif normalized.startswith("UPDATE ALEMBIC_VERSION SET"):
                current = "revision"
            else:
                return
            if current == stage and not injected:
                injected = True
                raise OperationalError(
                    statement,
                    parameters,
                    RuntimeError("synthetic transient write failure"),
                )

        event.listen(engine, "before_cursor_execute", fail_once)
        try:
            with pytest.raises(OperationalError):
                upgrade(directory=str(MIGRATIONS))
        finally:
            event.remove(engine, "before_cursor_execute", fail_once)
            db.session.remove()
        assert injected
        assert snapshot(db) == before
        assert (
            db.session.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == OLD_REVISION
        )
        db.session.remove()
        assigned = namespaces(db)
        assert "_alembic_tmp_feed" not in inspect(engine).get_table_names()
        upgrade(directory=str(MIGRATIONS))
        assert_complete(db, before, assigned)


@pytest.mark.parametrize(
    "progress", ["partial_backfill", "backfill_done", "constraints_done"]
)
def test_resume_previously_committed_progress_keeps_assigned_namespaces(
    legacy_database: Any, progress: str
) -> None:
    app, db = legacy_database
    with app.app_context():
        before = snapshot(db)
        if progress == "constraints_done":
            upgrade(directory=str(MIGRATIONS))
            db.session.execute(
                text("UPDATE alembic_version SET version_num=:old"),
                {"old": OLD_REVISION},
            )
        else:
            db.session.execute(
                text("ALTER TABLE feed ADD COLUMN storage_namespace VARCHAR(32)")
            )
            db.session.execute(
                text("UPDATE feed SET storage_namespace=:value WHERE id=1"),
                {"value": "a" * 32},
            )
            if progress == "backfill_done":
                db.session.execute(
                    text("UPDATE feed SET storage_namespace=:value WHERE id=2"),
                    {"value": "b" * 32},
                )
        db.session.commit()
        assigned = namespaces(db)
        upgrade(directory=str(MIGRATIONS))
        assert_complete(db, before, assigned)

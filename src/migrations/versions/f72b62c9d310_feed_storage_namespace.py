"""Assign persistent, non-reusable feed storage namespaces.

Revision ID: f72b62c9d310
Revises: ded4b70feadb
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "f72b62c9d310"
down_revision = "ded4b70feadb"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    # A real SAVEPOINT includes SQLite DDL even when the driver's outer BEGIN
    # is deferred. Failure during backfill/table recreation rolls it all back.
    # Schema introspection also resumes partial states left by the old migration
    # or a completed schema change whose Alembic version update did not commit.
    with connection.begin_nested():
        columns = {
            column["name"]: column
            for column in sa.inspect(connection).get_columns("feed")
        }
        if "storage_namespace" not in columns:
            op.add_column(
                "feed", sa.Column("storage_namespace", sa.String(32), nullable=True)
            )
        feeds = sa.table(
            "feed",
            sa.column("id", sa.Integer),
            sa.column("storage_namespace", sa.String(32)),
        )
        missing = sa.select(feeds.c.id).where(feeds.c.storage_namespace.is_(None))
        for feed_id in connection.execute(missing).scalars().all():
            connection.execute(
                feeds.update()
                .where(feeds.c.id == feed_id, feeds.c.storage_namespace.is_(None))
                .values(storage_namespace=uuid.uuid4().hex)
            )
        schema = sa.inspect(connection)
        column = next(
            c for c in schema.get_columns("feed") if c["name"] == "storage_namespace"
        )
        has_unique = any(
            constraint["column_names"] == ["storage_namespace"]
            for constraint in schema.get_unique_constraints("feed")
        ) or any(
            index["unique"] and index["column_names"] == ["storage_namespace"]
            for index in schema.get_indexes("feed")
        )
        if column["nullable"] or not has_unique:
            with op.batch_alter_table("feed") as batch_op:
                if column["nullable"]:
                    batch_op.alter_column(
                        "storage_namespace", existing_type=sa.String(32), nullable=False
                    )
                if not has_unique:
                    batch_op.create_unique_constraint(
                        "uq_feed_storage_namespace", ["storage_namespace"]
                    )


def downgrade():
    with op.batch_alter_table("feed") as batch_op:
        batch_op.drop_constraint("uq_feed_storage_namespace", type_="unique")
        batch_op.drop_column("storage_namespace")

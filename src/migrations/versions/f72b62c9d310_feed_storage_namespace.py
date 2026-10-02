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
    op.add_column("feed", sa.Column("storage_namespace", sa.String(32), nullable=True))
    feeds = sa.table(
        "feed",
        sa.column("id", sa.Integer),
        sa.column("storage_namespace", sa.String(32)),
    )
    connection = op.get_bind()
    for feed_id in connection.execute(sa.select(feeds.c.id)).scalars().all():
        connection.execute(
            feeds.update()
            .where(feeds.c.id == feed_id)
            .values(storage_namespace=uuid.uuid4().hex)
        )
    with op.batch_alter_table("feed") as batch_op:
        batch_op.alter_column(
            "storage_namespace", existing_type=sa.String(32), nullable=False
        )
        batch_op.create_unique_constraint(
            "uq_feed_storage_namespace", ["storage_namespace"]
        )


def downgrade():
    with op.batch_alter_table("feed") as batch_op:
        batch_op.drop_constraint("uq_feed_storage_namespace", type_="unique")
        batch_op.drop_column("storage_namespace")

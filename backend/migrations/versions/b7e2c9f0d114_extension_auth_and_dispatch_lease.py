"""extension auth session + dispatch lease

Revision ID: b7e2c9f0d114
Revises: a1f4d8e9b201
Create Date: 2026-04-21
"""

from alembic import op

revision = "b7e2c9f0d114"
down_revision = "a1f4d8e9b201"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE extension_sessions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
    op.execute("ALTER TABLE extension_sessions ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ")
    op.execute("ALTER TABLE extension_sessions ADD COLUMN IF NOT EXISTS session_secret_hash TEXT")

    op.execute("ALTER TABLE execution_commands ADD COLUMN IF NOT EXISTS dispatch_lease_owner TEXT")
    op.execute("ALTER TABLE execution_commands ADD COLUMN IF NOT EXISTS dispatch_lease_expires_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE execution_commands DROP COLUMN IF EXISTS dispatch_lease_expires_at")
    op.execute("ALTER TABLE execution_commands DROP COLUMN IF EXISTS dispatch_lease_owner")
    op.execute("ALTER TABLE extension_sessions DROP COLUMN IF EXISTS session_secret_hash")
    op.execute("ALTER TABLE extension_sessions DROP COLUMN IF EXISTS revoked_at")
    op.execute("ALTER TABLE extension_sessions DROP COLUMN IF EXISTS expires_at")

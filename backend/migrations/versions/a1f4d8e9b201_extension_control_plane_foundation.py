"""extension control plane foundation

Revision ID: a1f4d8e9b201
Revises: 9f43dce5905d
Create Date: 2026-04-21
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "a1f4d8e9b201"
down_revision = "9f43dce5905d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE trading_accounts ADD COLUMN IF NOT EXISTS platform_key TEXT")
    op.execute("ALTER TABLE trading_accounts ADD COLUMN IF NOT EXISTS platform_account_ref TEXT")
    op.execute("ALTER TABLE trading_accounts ADD COLUMN IF NOT EXISTS extension_device_id BIGINT")
    op.execute("ALTER TABLE trading_accounts ADD COLUMN IF NOT EXISTS platform_session_id BIGINT")
    op.execute("ALTER TABLE trading_accounts ADD COLUMN IF NOT EXISTS execution_enabled BOOLEAN NOT NULL DEFAULT FALSE")

    op.execute("""
    CREATE TABLE IF NOT EXISTS extension_pairing_tokens (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        pair_code TEXT NOT NULL UNIQUE,
        pair_secret_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        expires_at TIMESTAMPTZ NOT NULL,
        consumed_at TIMESTAMPTZ,
        metadata JSONB DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS extension_devices (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        device_fingerprint TEXT NOT NULL,
        label TEXT,
        platform TEXT,
        browser TEXT,
        extension_version TEXT,
        status TEXT NOT NULL DEFAULT 'offline',
        last_seen_at TIMESTAMPTZ,
        metadata JSONB DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (user_id, device_fingerprint)
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS extension_sessions (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        extension_device_id BIGINT NOT NULL REFERENCES extension_devices(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'active',
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_heartbeat_at TIMESTAMPTZ,
        ended_at TIMESTAMPTZ,
        metadata JSONB DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS platform_sessions (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        extension_device_id BIGINT NOT NULL REFERENCES extension_devices(id) ON DELETE CASCADE,
        adapter_key TEXT NOT NULL,
        platform_key TEXT NOT NULL,
        tab_id TEXT NOT NULL,
        tab_url TEXT,
        platform_account_ref TEXT,
        session_ref TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        capabilities JSONB DEFAULT '{}'::jsonb,
        metadata JSONB DEFAULT '{}'::jsonb,
        last_seen_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (extension_device_id, adapter_key, tab_id)
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS execution_batches (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        request_id TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        requested_by TEXT,
        metadata JSONB DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS execution_commands (
        id BIGSERIAL PRIMARY KEY,
        execution_batch_id BIGINT NOT NULL REFERENCES execution_batches(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        trading_account_id INTEGER REFERENCES trading_accounts(id) ON DELETE SET NULL,
        extension_device_id BIGINT REFERENCES extension_devices(id) ON DELETE SET NULL,
        platform_session_id BIGINT REFERENCES platform_sessions(id) ON DELETE SET NULL,
        adapter_key TEXT NOT NULL,
        command_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        payload JSONB DEFAULT '{}'::jsonb,
        metadata JSONB DEFAULT '{}'::jsonb,
        dispatched_at TIMESTAMPTZ,
        acked_at TIMESTAMPTZ,
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS execution_results (
        id BIGSERIAL PRIMARY KEY,
        execution_command_id BIGINT NOT NULL REFERENCES execution_commands(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        status TEXT NOT NULL,
        result_payload JSONB DEFAULT '{}'::jsonb,
        adapter_error_code TEXT,
        adapter_error_message TEXT,
        received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS canonical_orders (
        id BIGSERIAL PRIMARY KEY,
        trading_account_id INTEGER NOT NULL REFERENCES trading_accounts(id) ON DELETE CASCADE,
        platform_order_ref TEXT NOT NULL,
        symbol TEXT,
        side TEXT,
        order_type TEXT,
        status TEXT,
        quantity DOUBLE PRECISION,
        filled_quantity DOUBLE PRECISION,
        price DOUBLE PRECISION,
        stop_price DOUBLE PRECISION,
        submitted_at TIMESTAMPTZ,
        source_metadata JSONB DEFAULT '{}'::jsonb,
        last_seen_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (trading_account_id, platform_order_ref)
    )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS execution_results")
    op.execute("DROP TABLE IF EXISTS execution_commands")
    op.execute("DROP TABLE IF EXISTS execution_batches")
    op.execute("DROP TABLE IF EXISTS platform_sessions")
    op.execute("DROP TABLE IF EXISTS extension_sessions")
    op.execute("DROP TABLE IF EXISTS extension_devices")
    op.execute("DROP TABLE IF EXISTS extension_pairing_tokens")
    op.execute("DROP TABLE IF EXISTS canonical_orders")

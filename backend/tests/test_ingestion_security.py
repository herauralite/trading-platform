"""P0 security regression tests: ingestion, extension and journal routes must be
authenticated and scoped to accounts the caller owns."""
import asyncio
import os

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import ingest_auth  # noqa: E402
from app.core.auth_session import create_session_token  # noqa: E402
import app.main as main_mod  # noqa: E402
from app.main import app  # noqa: E402
from app.services.connector_ingest import AccountOwnershipError  # noqa: E402

ALICE, BOB = "1001", "2002"
ALICE_ACCT, BOB_ACCT, FREE_ACCT = "ACCT-ALICE", "ACCT-BOB", "ACCT-FREE"


def session(user):
    return {"Authorization": f"Bearer {create_session_token(user)}"}


@pytest.fixture
def world(monkeypatch):
    """In-memory stand-in for the database. Every mutation is recorded in `writes`."""
    w = {
        "owners": {ALICE_ACCT: {ALICE}, BOB_ACCT: {BOB}},
        "writes": [],
        "telegram": [],
        "tokens": {},  # token_hash -> {"user": str, "revoked": bool}
        "trades": {
            ALICE_ACCT: [{"id": 1, "account_id": ALICE_ACCT, "symbol": "US30", "direction": "buy", "pnl": 25.0}],
            BOB_ACCT: [{"id": 2, "account_id": BOB_ACCT, "symbol": "XAUUSD", "direction": "sell", "pnl": -40.0}],
        },
    }
    main_mod.account_data_store.clear()

    async def owners(account_id):
        return set(w["owners"].get(account_id, set()))

    async def owned(user_id):
        return sorted(a for a, users in w["owners"].items() if user_id in users)

    def claim(user_id, account_id):
        if user_id and account_id:
            existing = w["owners"].setdefault(account_id, set())
            if existing and user_id not in existing:
                raise AccountOwnershipError("owned by someone else")
            existing.add(user_id)

    async def upsert(payload):
        claim(payload.get("user_id"), payload.get("external_account_id"))
        w["writes"].append(("account", payload.get("user_id"), payload.get("external_account_id")))
        return {"id": 7, "external_account_id": payload.get("external_account_id"), "user_id": payload.get("user_id")}

    async def snapshot(payload):
        w["writes"].append(("snapshot", payload.get("user_id"), payload.get("external_account_id")))
        return True

    async def position(payload):
        w["writes"].append(("position", payload.get("user_id"), payload.get("external_account_id")))
        return "US30|buy|na"

    async def deactivate(**kwargs):
        return 0

    async def link(telegram_user_id, account_id, *args, **kwargs):
        claim(telegram_user_id, account_id)
        w["writes"].append(("link", telegram_user_id, account_id))

    async def trade(payload):
        key = (payload.get("external_account_id"), payload.get("symbol"), payload.get("side"), str(payload.get("close_time")), payload.get("pnl"))
        duplicate = any(wr[0] == "trade" and wr[3] == key for wr in w["writes"])
        if not duplicate:  # mirrors the UNIQUE(account_id, symbol, direction, closed_at, pnl) upsert
            w["writes"].append(("trade", payload.get("user_id"), payload.get("external_account_id"), key, payload.get("source")))
        return True

    async def event(payload):
        w["writes"].append(("event", payload.get("user_id"), payload.get("external_account_id")))

    async def get_trades(account_id=None, limit=50, offset=0, order="desc", source="scraper", account_ids=None):
        assert account_ids is not None, "private reads must always be scoped to an account list"
        return [t for a in account_ids for t in w["trades"].get(a, [])]

    async def get_stats(account_id=None, account_ids=None):
        assert account_ids is not None
        return {"total": sum(len(w["trades"].get(a, [])) for a in account_ids), "oldest_trade_date": None}

    async def user_accounts(uid):
        return [{"account_id": a} for a in await owned(uid)]

    async def tg(message, chat_id=None):
        w["telegram"].append((chat_id, message))

    async def mint(user_id):
        token = ingest_auth.EXTENSION_TOKEN_PREFIX + f"test-{len(w['tokens'])}-{user_id}"
        w["tokens"][ingest_auth._hash_token(token)] = {"user": user_id, "revoked": False}
        return {"token": token, "expires_at": "2099-01-01T00:00:00+00:00"}

    async def revoke(user_id):
        hit = [t for t in w["tokens"].values() if t["user"] == user_id and not t["revoked"]]
        for t in hit:
            t["revoked"] = True
        return len(hit)

    async def resolve(token):
        row = w["tokens"].get(ingest_auth._hash_token(token))
        return row["user"] if row and not row["revoked"] else None

    async def has_trades(account_id):
        return bool(w["trades"].get(account_id))

    monkeypatch.setattr(ingest_auth, "get_account_owners", owners)
    monkeypatch.setattr(ingest_auth, "account_has_trades", has_trades)
    monkeypatch.setattr(ingest_auth, "get_owned_account_ids", owned)
    monkeypatch.setattr(ingest_auth, "mint_extension_token", mint)
    monkeypatch.setattr(ingest_auth, "revoke_extension_tokens", revoke)
    monkeypatch.setattr(ingest_auth, "resolve_extension_token", resolve)
    for target in ("app.main", "app.routers.ingest"):
        monkeypatch.setattr(f"{target}.upsert_trading_account", upsert)
        monkeypatch.setattr(f"{target}.ingest_account_snapshot", snapshot)
        monkeypatch.setattr(f"{target}.ingest_position", position)
        monkeypatch.setattr(f"{target}.ingest_trade", trade)
    monkeypatch.setattr("app.routers.ingest.ingest_event", event)
    monkeypatch.setattr(main_mod, "deactivate_missing_positions", deactivate)
    monkeypatch.setattr(main_mod, "db_link_account", link)
    monkeypatch.setattr(main_mod, "db_get_trades", get_trades)
    monkeypatch.setattr(main_mod, "db_get_trade_stats", get_stats)
    monkeypatch.setattr(main_mod, "db_get_user_accounts", user_accounts)
    async def payout(acct_id, acct):
        return {"account_id": acct_id, "eligible": False, "balance": acct.get("balance")}

    monkeypatch.setattr(main_mod, "send_telegram", tg)

    async def persist_hb(**kwargs):
        w["writes"].append(("heartbeat", kwargs["user_id"], kwargs["trading_account_id"]))

    async def no_heartbeats(user_id, account_ids=None):
        return {}
    monkeypatch.setattr(main_mod.live_status, "persist_heartbeat", persist_hb)
    monkeypatch.setattr(main_mod.live_status, "load_heartbeats", no_heartbeats)
    monkeypatch.setattr(main_mod, "evaluate_payout_eligibility", payout)
    yield w
    main_mod.account_data_store.clear()


def call(method, path, *, headers=None, json=None, content=None):
    async def _run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            return await http.request(method, path, headers=headers, json=json, content=content)

    return asyncio.run(_run())


def heartbeat(account=ALICE_ACCT, **extra):
    return {"accountId": account, "accountType": "2_step_master", "accountSize": 10000, "balance": 10100, "equity": 10090,
            "positions": [{"symbol": "US30", "side": "buy"}], "hasPositions": True, "alerts": [], "closedTrades": [], **extra}


def trade(account=ALICE_ACCT, **extra):
    return {"accountId": account, "accountSize": 10000, "symbol": "US30", "direction": "buy", "pnl": 25,
            "closedAt": "2026-04-16T10:00:00Z", "source": "scraper", **extra}


WRITE_ROUTES = [
    ("/extension/data", heartbeat()),
    ("/extension/trade", trade()),
    ("/journal/trade", trade()),
    ("/ingest/accounts", {"connector_type": "manual", "external_account_id": ALICE_ACCT}),
    ("/ingest/account-snapshots", {"connector_type": "manual", "external_account_id": ALICE_ACCT, "balance": 1}),
    ("/ingest/positions", {"connector_type": "manual", "external_account_id": ALICE_ACCT, "symbol": "US30"}),
    ("/ingest/trades", {"connector_type": "manual", "external_account_id": ALICE_ACCT, "symbol": "US30", "side": "buy", "pnl": 5}),
    ("/ingest/events", {"connector_type": "manual", "external_account_id": ALICE_ACCT, "event_type": "sync"}),
    ("/ingest/csv/trades", {"external_account_id": ALICE_ACCT, "rows": [{"symbol": "US30", "side": "buy", "pnl": 5}]}),
]
PRIVATE_READS = [
    f"/extension/journal?account_id={ALICE_ACCT}", "/extension/journal", f"/extension/journal/stats?account_id={ALICE_ACCT}",
    "/extension/journal/stats", "/extension/status", f"/extension/payout?account_id={ALICE_ACCT}", "/extension/payout",
]


# 1, 16 ── anonymous writes ─────────────────────────────────────────────────────
@pytest.mark.parametrize("path,body", WRITE_ROUTES)
def test_anonymous_writes_are_rejected_and_change_nothing(world, path, body):
    res = call("POST", path, json=body)
    assert res.status_code == 401
    assert world["writes"] == [] and world["telegram"] == [] and main_mod.account_data_store == {}


@pytest.mark.parametrize("path,body", WRITE_ROUTES)
def test_garbage_credentials_are_rejected(world, path, body):
    for bad in ("Bearer not-a-token", "Bearer a.b", f"Bearer {create_session_token(ALICE)}x", "Basic abc", "Bearer tti_unknown"):
        res = call("POST", path, json=body, headers={"Authorization": bad})
        assert res.status_code == 401
    assert world["writes"] == []


# 2 ── anonymous reads ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", PRIVATE_READS)
def test_anonymous_private_reads_are_rejected(world, path):
    main_mod.account_data_store[ALICE_ACCT] = {"balance": 123456, "_owner": ALICE, "last_updated": None}
    res = call("GET", path)
    assert res.status_code == 401
    assert "US30" not in res.text and "123456" not in res.text and ALICE_ACCT not in res.text


# 3 ── owners can use their own data ────────────────────────────────────────────
def test_owner_can_ingest_and_read_their_own_account(world):
    assert call("POST", "/extension/data", json=heartbeat(), headers=session(ALICE)).status_code == 200
    assert call("POST", "/extension/trade", json=trade(), headers=session(ALICE)).json() == {"status": "ok", "persisted": True}
    kinds = [wr[0] for wr in world["writes"]]
    assert kinds.count("account") == 1 and "snapshot" in kinds and "position" in kinds and "link" in kinds and "trade" in kinds
    assert {wr[1] for wr in world["writes"]} == {ALICE}

    journal = call("GET", f"/extension/journal?account_id={ALICE_ACCT}", headers=session(ALICE))
    assert journal.status_code == 200 and [t["symbol"] for t in journal.json()["trades"]] == ["US30"]
    assert call("GET", "/extension/journal/stats", headers=session(ALICE)).json()["total"] == 1
    status = call("GET", "/extension/status", headers=session(ALICE)).json()
    assert list(status["accounts"]) == [ALICE_ACCT] and "_owner" not in status["accounts"][ALICE_ACCT]
    payout = call("GET", f"/extension/payout?account_id={ALICE_ACCT}", headers=session(ALICE))
    assert payout.status_code == 200 and payout.json()["account_id"] == ALICE_ACCT and payout.json()["balance"] == 10100


def test_user_can_claim_an_unowned_account_then_it_is_theirs(world):
    assert call("POST", "/extension/data", json=heartbeat(FREE_ACCT), headers=session(ALICE)).status_code == 200
    assert world["owners"][FREE_ACCT] == {ALICE}
    res = call("POST", "/extension/data", json=heartbeat(FREE_ACCT), headers=session(BOB))
    assert res.status_code == 403


def test_unowned_account_with_existing_history_cannot_be_adopted(world):
    world["trades"]["ACCT-ORPHAN"] = [{"id": 9, "account_id": "ACCT-ORPHAN", "symbol": "NAS100", "direction": "buy", "pnl": 300.0}]
    attempts = [
        ("POST", "/auth/link-account?account_id=ACCT-ORPHAN", None),
        ("POST", "/extension/data", heartbeat("ACCT-ORPHAN")),
        ("POST", "/extension/trade", trade("ACCT-ORPHAN")),
        ("POST", "/ingest/accounts", {"connector_type": "manual", "external_account_id": "ACCT-ORPHAN"}),
    ]
    for method, path, body in attempts:
        assert call(method, path, json=body, headers=session(ALICE)).status_code == 403, path
    assert world["writes"] == [] and "ACCT-ORPHAN" not in world["owners"]
    res = call("GET", "/extension/journal?account_id=ACCT-ORPHAN", headers=session(ALICE))
    assert res.status_code == 403 and "NAS100" not in res.text


# 4 ── cross-user reads ─────────────────────────────────────────────────────────
def test_user_cannot_read_another_users_private_data(world):
    call("POST", "/extension/data", json=heartbeat(BOB_ACCT, balance=987654), headers=session(BOB))
    for path in (f"/extension/journal?account_id={BOB_ACCT}", f"/extension/journal/stats?account_id={BOB_ACCT}", f"/extension/payout?account_id={BOB_ACCT}"):
        res = call("GET", path, headers=session(ALICE))
        assert res.status_code == 403
        assert "XAUUSD" not in res.text and "987654" not in res.text
    # Unscoped reads return only the caller's own accounts, never everyone's.
    mine = call("GET", "/extension/journal", headers=session(ALICE)).json()["trades"]
    assert [t["symbol"] for t in mine] == ["US30"]
    status = call("GET", "/extension/status", headers=session(ALICE)).json()
    assert BOB_ACCT not in status["accounts"] and "987654" not in str(status)
    # Same response for an account that does not exist: no existence oracle.
    assert call("GET", "/extension/journal?account_id=NO-SUCH", headers=session(ALICE)).status_code == 403


def test_user_with_no_accounts_sees_nothing(world):
    carol = session("3003")
    assert call("GET", "/extension/journal", headers=carol).json()["trades"] == []
    assert call("GET", "/extension/journal/stats", headers=carol).json()["total"] == 0
    assert call("GET", "/extension/status", headers=carol).json() == {"accounts": {}, "count": 0, "stale_accounts": {}}
    assert call("GET", "/extension/payout", headers=carol).status_code == 404


# 5, 7, 16 ── cross-account writes ──────────────────────────────────────────────
@pytest.mark.parametrize("path,body", [
    ("/extension/data", heartbeat(BOB_ACCT)),
    ("/extension/trade", trade(BOB_ACCT)),
    ("/journal/trade", trade(BOB_ACCT)),
    ("/ingest/accounts", {"connector_type": "fundingpips_extension", "external_account_id": BOB_ACCT}),
    ("/ingest/account-snapshots", {"connector_type": "manual", "external_account_id": BOB_ACCT, "balance": 1}),
    ("/ingest/positions", {"connector_type": "manual", "external_account_id": BOB_ACCT, "symbol": "US30"}),
    ("/ingest/trades", {"connector_type": "manual", "external_account_id": BOB_ACCT, "symbol": "US30", "side": "buy", "pnl": 5}),
    ("/ingest/events", {"connector_type": "manual", "external_account_id": BOB_ACCT, "event_type": "sync"}),
    ("/ingest/csv/trades", {"external_account_id": BOB_ACCT, "rows": [{"symbol": "US30", "side": "buy", "pnl": 5}]}),
])
def test_user_cannot_write_into_another_users_account(world, path, body):
    res = call("POST", path, json=body, headers=session(ALICE))
    assert res.status_code == 403
    assert world["writes"] == [] and world["telegram"] == []
    assert world["owners"][BOB_ACCT] == {BOB}
    assert BOB_ACCT not in main_mod.account_data_store


def test_user_cannot_link_an_account_owned_by_someone_else(world):
    res = call("POST", f"/auth/link-account?account_id={BOB_ACCT}", headers=session(ALICE))
    assert res.status_code == 403 and world["writes"] == []
    assert call("POST", f"/auth/link-account?account_id={ALICE_ACCT}", headers=session(ALICE)).status_code == 200


# 6 ── forged identities in the body ────────────────────────────────────────────
def test_forged_user_id_in_extension_payload_is_ignored(world):
    alerts = [{"type": "daily", "level": "warn", "message": "Send your seed phrase to..."}]
    res = call("POST", "/extension/data", json=heartbeat(telegramUserId=BOB, alerts=alerts), headers=session(ALICE))
    assert res.status_code == 200
    assert {wr[1] for wr in world["writes"]} == {ALICE}  # nothing is attributed to Bob
    assert {chat for chat, _ in world["telegram"]} == {ALICE}  # Bob receives no bot message
    assert main_mod.account_data_store[ALICE_ACCT]["telegramUserId"] == ALICE
    # Bob's forged claim over Alice's account id does not make it readable to Bob.
    assert call("GET", f"/extension/journal?account_id={ALICE_ACCT}", headers=session(BOB)).status_code == 403


def test_forged_user_id_on_ingest_routes_is_refused(world):
    for path, body in WRITE_ROUTES[3:]:
        res = call("POST", path, json={**body, "user_id": BOB}, headers=session(ALICE))
        assert res.status_code == 400, path
    assert world["writes"] == []


def test_extension_trade_cannot_set_arbitrary_source_or_owner(world):
    body = trade(source="admin_override", telegramUserId=BOB, user_id=BOB, account_key="x", id=999)
    assert call("POST", "/extension/trade", json=body, headers=session(ALICE)).status_code == 200
    (kind, user, account, _key, source), = [wr for wr in world["writes"] if wr[0] == "trade"]
    assert (user, account, source) == (ALICE, ALICE_ACCT, "scraper")


def test_storage_layer_ownership_error_becomes_403(world, monkeypatch):
    async def stale_owner_lookup(_account_id):
        return set()  # simulate a race: the pre-check saw no owner

    monkeypatch.setattr(ingest_auth, "get_account_owners", stale_owner_lookup)
    res = call("POST", "/ingest/accounts", json={"connector_type": "manual", "external_account_id": BOB_ACCT}, headers=session(ALICE))
    assert res.status_code == 403
    assert world["writes"] == []


# 8, 15 ── extension tokens ─────────────────────────────────────────────────────
def test_extension_token_lifecycle(world):
    assert call("POST", "/auth/extension-token").status_code == 401
    minted = call("POST", "/auth/extension-token", headers=session(ALICE)).json()
    token = minted["token"]
    assert token.startswith("tti_") and minted["token_type"] == "extension_ingest"
    ext = {"Authorization": f"Bearer {token}"}

    # Legitimate extension ingestion works, attributed to the token's user.
    assert call("POST", "/extension/data", json=heartbeat(), headers=ext).status_code == 200
    assert call("POST", "/extension/trade", json=trade(), headers=ext).status_code == 200
    assert {wr[1] for wr in world["writes"]} == {ALICE}
    assert call("GET", "/extension/status", headers=ext).status_code == 200
    # ...but only for accounts that user owns.
    assert call("POST", "/extension/trade", json=trade(BOB_ACCT), headers=ext).status_code == 403

    # It is not a session: no dashboard, account, connector, token-minting or admin access.
    for method, path in [("GET", "/auth/me"), ("GET", "/connectors/overview"), ("GET", "/accounts/workspaces"),
                         ("POST", "/auth/extension-token"), ("DELETE", "/auth/extension-token"),
                         ("POST", f"/auth/link-account?account_id={FREE_ACCT}"), ("POST", "/connectors/manual/disconnect")]:
        assert call(method, path, headers=ext).status_code == 401, path
    assert call("POST", "/ingest/trades", json=WRITE_ROUTES[6][1], headers=ext).status_code == 401
    assert call("GET", "/admin/dedup-trades/preview", headers=ext).status_code == 404

    # Revocation takes effect immediately.
    before = len(world["writes"])
    assert call("DELETE", "/auth/extension-token", headers=session(ALICE)).json()["revoked"] == 1
    assert call("POST", "/extension/data", json=heartbeat(), headers=ext).status_code == 401
    assert call("POST", "/extension/trade", json=trade(), headers=ext).status_code == 401
    assert len(world["writes"]) == before


def test_admin_token_is_not_an_ingestion_credential(world, monkeypatch):
    admin = "unit-test-admin-token-0123456789abcdef-XYZ"
    monkeypatch.setenv("TALITRADE_ADMIN_MAINTENANCE_MODE", "true")
    monkeypatch.setenv("TALITRADE_ADMIN_TOKEN", admin)
    for headers in ({"X-TaliTrade-Admin-Token": admin}, {"Authorization": f"Bearer {admin}"}):
        assert call("POST", "/extension/trade", json=trade(), headers=headers).status_code == 401
        assert call("GET", "/extension/journal", headers=headers).status_code == 401
    assert world["writes"] == []


# 9 ── replay / duplicates ──────────────────────────────────────────────────────
def test_replayed_trade_does_not_create_a_duplicate(world):
    for _ in range(3):
        assert call("POST", "/extension/trade", json=trade(), headers=session(ALICE)).status_code == 200
    assert len([wr for wr in world["writes"] if wr[0] == "trade"]) == 1


def test_trade_upsert_keeps_its_database_dedupe_and_owner_guard():
    import inspect
    from app.services import connector_ingest as ci

    assert "ON CONFLICT (account_id, symbol, direction, closed_at, pnl)" in inspect.getsource(ci.ingest_trade)
    upsert_sql = inspect.getsource(ci.upsert_trading_account)
    assert "user_id = COALESCE(trading_accounts.user_id, EXCLUDED.user_id)" in upsert_sql  # owner never overwritten
    assert "AccountOwnershipError" in upsert_sql


# 10 ── invalid / oversized payloads ────────────────────────────────────────────
def test_invalid_and_oversized_payloads_are_rejected_safely(world):
    alice = session(ALICE)
    big = b'{"accountId":"' + ALICE_ACCT.encode() + b'","pad":"' + b"x" * (600 * 1024) + b'"}'
    for path in ("/extension/data", "/extension/trade", "/journal/trade", "/ingest/trades"):
        res = call("POST", path, content=big, headers={**alice, "Content-Type": "application/json"})
        assert res.status_code == 413, path
    assert call("POST", "/extension/data", json=heartbeat(positions=[{}] * 501), headers=alice).status_code == 422
    assert call("POST", "/extension/data", json=heartbeat(closedTrades=[{}] * 201), headers=alice).status_code == 422
    assert call("POST", "/extension/data", json=heartbeat(alerts=[{}] * 51), headers=alice).status_code == 422
    assert call("POST", "/extension/data", json=heartbeat("A" * 65), headers=alice).status_code == 422
    assert call("POST", "/extension/trade", json=trade(""), headers=alice).status_code == 422
    assert call("POST", "/extension/trade", json={"symbol": "US30"}, headers=alice).status_code == 422
    assert call("POST", "/extension/trade", content=b"{not json", headers={**alice, "Content-Type": "application/json"}).status_code == 422
    assert call("POST", "/ingest/csv/trades", json={"external_account_id": ALICE_ACCT, "rows": [{}] * 5001}, headers=alice).status_code == 422
    assert call("GET", "/extension/journal?limit=999999&offset=-5", headers=alice).json()["limit"] == 500
    assert world["writes"] == []


# 11 ── public leaderboard ──────────────────────────────────────────────────────
def test_public_leaderboard_excludes_private_identifiers(world, monkeypatch):
    rows = [
        {"telegram_user_id": ALICE, "display_name": "Alice", "account_id": ALICE_ACCT, "account_type": "2_step", "account_size": 10000, "trade_count": 4, "total_pnl": 500, "wins": 3, "losses": 1},
        {"telegram_user_id": BOB, "display_name": "Bob", "account_id": BOB_ACCT, "account_type": "2_step", "account_size": 10000, "trade_count": 2, "total_pnl": 100, "wins": 1, "losses": 1},
    ]
    seen = {}

    class Result:
        def mappings(self):
            return self

        def all(self):
            return rows

    class Conn:
        async def execute(self, _stmt, params=None):
            seen.update(params or {})
            return Result()

    class Ctx:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *exc):
            return False

    class Engine:
        def connect(self):
            return Ctx()

    monkeypatch.setattr("app.core.database.engine", Engine())

    public = call("GET", "/leaderboard?limit=100000")
    assert public.status_code == 200 and seen["lim"] == 50
    for leaked in (ALICE_ACCT, BOB_ACCT, ALICE, BOB, "accountId", "telegram"):
        assert leaked not in public.text
    assert set(public.json()["leaderboard"][0]) == {"rank", "displayName", "isYou", "accountType", "accountSize", "totalPnl", "pnlPct", "tradeCount", "winRate"}
    assert [r["isYou"] for r in public.json()["leaderboard"]] == [False, False]
    # A signed-in viewer learns only which row is their own.
    assert [r["isYou"] for r in call("GET", "/leaderboard", headers=session(BOB)).json()["leaderboard"]] == [False, True]
    # A stale/invalid token must not break the public board.
    assert call("GET", "/leaderboard", headers={"Authorization": "Bearer junk"}).status_code == 200


# 12, 14 ── neighbours keep working ─────────────────────────────────────────────
def test_public_and_session_routes_are_unaffected(world):
    assert call("GET", "/health").status_code == 200
    assert call("GET", "/auth/telegram/config").status_code == 200
    assert call("GET", "/connectors/catalog").status_code == 200
    assert call("POST", "/auth/telegram", json={"id": 1, "auth_date": 1, "hash": "x"}).status_code == 401
    for path in ("/admin/dedup-trades/preview", "/test/db"):
        assert call("GET", path, headers=session(ALICE)).status_code == 404
    assert call("DELETE", "/admin/dedup-trades", headers=session(ALICE)).status_code == 404


def test_every_ingestion_and_private_route_declares_an_auth_dependency():
    """Fails if someone later adds an /extension, /journal or /ingest route without auth."""
    public = {"/extension/news"}
    guards = {ingest_auth.require_ingest_principal, ingest_auth.require_session_principal}
    checked = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith(("/extension/", "/journal/", "/ingest/")) and path not in public:
            deps = {d.call for d in route.dependant.dependencies}
            assert deps & guards, f"{path} has no authentication dependency"
            checked.append(path)
    assert len(checked) == 13

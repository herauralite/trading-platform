import asyncio
import hashlib
import hmac
import json
import logging
from pathlib import Path
from io import BytesIO
from PIL import Image, ImageChops, ImageStat
import os
from app.services.bot_banner import banner
from app.services.bot_reports import COMMANDS
from app.services.bot_releases import published
from datetime import date, timedelta, timezone

import httpx
from sqlalchemy import text

from app.core.database import engine

log = logging.getLogger(__name__)
# Telegram request URLs contain the bot credential.
logging.getLogger("httpx").setLevel(logging.WARNING)
CHANNEL = "https://t.me/talitrade"
PROFILE_TEXT = "Automated trading alerts and account tools from TaliTrade."
profile_verified = False


def webhook_secret(token):
    return hmac.new(token.encode(), b"talitrade-webhook-v1", hashlib.sha256).hexdigest()


async def api(client, method, *, data=None, files=None):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    res = await client.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files)
    if not res.is_success or not res.json().get("ok"):
        raise RuntimeError(f"Telegram {method} returned {res.status_code}")
    return res.json()["result"]


async def matches_avatar(client, user_id, target):
    photos = await api(client, "getUserProfilePhotos", data={"user_id":user_id,"limit":1})
    if not photos["photos"]:
        return False
    largest = max(photos["photos"][0], key=lambda p: p["width"])
    file = await api(client, "getFile", data={"file_id":largest["file_id"]})
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    response = await client.get(f"https://api.telegram.org/file/bot{token}/{file['file_path']}")
    response.raise_for_status()
    with Image.open(BytesIO(response.content)) as remote, Image.open(BytesIO(target)) as local:
        def thumbnail(img):
            return img.convert("RGB").resize((64,64), Image.Resampling.LANCZOS)
        difference = ImageChops.difference(thumbnail(remote), thumbnail(local))
        return max(ImageStat.Stat(difference).rms) < 8


async def restore_profile():
    global profile_verified
    async with httpx.AsyncClient(timeout=10) as client:
        me = await api(client, "getMe")
        if me["username"].lower() != "talitradebot":
            return
        for lang in ("", "en"):
            for method, field, value in (
                ("Description", "description", PROFILE_TEXT),
                ("ShortDescription", "short_description", PROFILE_TEXT),
            ):
                args = {"language_code": lang}
                current = await api(client, "getMy" + method, data=args)
                if current[field] != value:
                    await api(client, "setMy" + method, data={**args, field: value})
                    verified = await api(client, "getMy" + method, data=args)
                    if verified[field] != value:
                        raise RuntimeError("Profile verification failed")
        commands = [{"command": name, "description": desc} for name, desc in COMMANDS.items()]
        for language in ("", "en"):
            args = {"language_code": language, "scope": json.dumps({"type":"all_private_chats"})}
            if await api(client, "getMyCommands", data=args) != commands:
                await api(client, "setMyCommands", data={**args, "commands":json.dumps(commands)})
                if await api(client, "getMyCommands", data=args) != commands:
                    raise RuntimeError("Private menu verification failed")
        target = Path(__file__).with_name("midnight-avatar.jpg").read_bytes()
        if not await matches_avatar(client, me["id"], target):
            await api(client, "setMyProfilePhoto",
                      data={"photo":json.dumps({"type":"static","photo":"attach://avatar"})},
                      files={"avatar":("midnight.jpg",target,"image/jpeg")})
            if not await matches_avatar(client, me["id"], target):
                raise RuntimeError("Avatar verification pending")
        profile_verified = True


async def setup():
    async with engine.begin() as conn:
        await conn.execute(text("""CREATE TABLE IF NOT EXISTS telegram_alert_preferences (
            user_id TEXT PRIMARY KEY, enabled_since TIMESTAMPTZ NOT NULL DEFAULT NOW())"""))
        await conn.execute(text("""CREATE TABLE IF NOT EXISTS telegram_alert_receipts (
            user_id TEXT NOT NULL, event_key TEXT NOT NULL, PRIMARY KEY(user_id,event_key))"""))


async def subscribe(user_id, enabled):
    async with engine.begin() as conn:
        sql = ("INSERT INTO telegram_alert_preferences(user_id) VALUES (:u) ON CONFLICT DO NOTHING"
               if enabled else "DELETE FROM telegram_alert_preferences WHERE user_id=:u")
        await conn.execute(text(sql), {"u": user_id})


async def deliver(user_id, key, message, photo=None, *, require_subscription=True, parse_mode=None):
    """Send `message` to `user_id`; after one successful send, later attempts for (user_id, key) do nothing.

    The receipt is inserted and the message sent inside ONE transaction:
    * a failed send raises (api() raises on any non-OK Telegram response, httpx raises on transport errors),
      the transaction rolls back and no receipt remains, so the same event can be delivered later;
    * a concurrent attempt for the same key, in any process, blocks on the receipt's primary key until the
      first attempt finishes: after its commit the second inserts nothing and does not send; after its
      rollback the second sends.
    Remaining ambiguity (at-least-once, not exactly-once): if Telegram accepts the message but the response
    is lost (timeout), or the process dies after Telegram accepted it and before the commit, no receipt is
    recorded and a retry sends the message again. The Bot API offers no idempotency key that would let us
    detect that. The transaction stays open for at most the HTTP timeout (8 s).

    require_subscription=True (default) delivers only to users in telegram_alert_preferences.
    Returns True if this call sent the message, False if it was already delivered or not subscribed."""
    async with engine.begin() as conn:
        if require_subscription:
            claimed = await conn.scalar(text("""INSERT INTO telegram_alert_receipts(user_id,event_key)
                SELECT user_id,:key FROM telegram_alert_preferences WHERE user_id=:u
                ON CONFLICT DO NOTHING RETURNING 1"""), {"u": user_id, "key": key})
        else:
            claimed = await conn.scalar(text("""INSERT INTO telegram_alert_receipts(user_id,event_key)
                VALUES (:u,:key) ON CONFLICT DO NOTHING RETURNING 1"""), {"u": user_id, "key": key})
        if not claimed:
            return False
        async with httpx.AsyncClient(timeout=8) as client:
            if photo:
                data = {"chat_id": user_id, "caption": message}
                if parse_mode:
                    data["parse_mode"] = parse_mode
                await api(client, "sendPhoto", data=data, files={"photo": ("talitrade.png", photo, "image/png")})
            else:
                data = {"chat_id": user_id, "text": message}
                if parse_mode:
                    data["parse_mode"] = parse_mode
                await api(client, "sendMessage", data=data)
    return True


async def alert_image(section, title, detail):
    try:
        return await asyncio.to_thread(banner, section, title, detail)
    except Exception:
        log.warning("Banner unavailable; delivering text")
        return None


async def account_pass():
    queries = ["""SELECT a.user_id, 'account:' || a.id AS key, 'Connected account: ' ||
        COALESCE(a.display_label,a.broker_name,'Trading account') AS message, NULL::text AS fill_detail
        FROM trading_accounts a JOIN telegram_alert_preferences p ON p.user_id=a.user_id
        WHERE a.is_active AND a.created_at >= p.enabled_since"""]
    async with engine.connect() as conn:
        if await conn.scalar(text("SELECT to_regclass('mtapi_connections') IS NOT NULL")):
            queries.append("""SELECT c.user_id, 'mt:' || c.id AS key, 'Connected Match-Trader account: ' ||
                COALESCE(c.broker_name,'Match-Trader') || ' · ' || right(c.external_login,4) AS message, NULL::text AS fill_detail
                FROM mtapi_connections c JOIN telegram_alert_preferences p ON p.user_id=c.user_id
                WHERE c.disconnected_at IS NULL AND c.created_at>=p.enabled_since""")
            queries.append("""SELECT c.user_id, 'close:' || c.id || ':' || t.uid AS key,
                'Trade closed · ' || t.symbol || ' · Account ending ' || right(c.external_login,4) ||
                COALESCE(' · Net ' || t.net_profit || ' ' || c.account_currency,'') AS message, concat_ws(' · ', 'Closed ' || to_char(t.close_time AT TIME ZONE 'UTC','HH24:MI') || ' UTC',
                    'Entry ' || t.open_price, 'Exit ' || t.close_price) AS fill_detail
                FROM mtapi_closed_positions t JOIN mtapi_connections c ON c.id=t.connection_id
                JOIN telegram_alert_preferences p ON p.user_id=c.user_id
                WHERE c.disconnected_at IS NULL AND t.close_time>=p.enabled_since""")
        rows = (await conn.execute(text("SELECT e.* FROM (" + " UNION ALL ".join(queries) +
            ") e WHERE NOT EXISTS (SELECT 1 FROM telegram_alert_receipts r WHERE r.user_id=e.user_id AND r.event_key=e.key) LIMIT 100"))).all()
    for uid, key, message, fill_detail in rows:
        try:
            parts = message.split(" · ")
            if key.startswith("close:"):
                image = await alert_image("Trade closed", " · ".join(parts[1:2] + parts[3:]), parts[2] if len(parts)>2 else "Trading account")
            else:
                image = await alert_image("Account connected", message.split(": ",1)[-1], "Telegram account alerts")
            await deliver(uid, key, fill_detail if image and fill_detail else message, image)
        except Exception:
            log.warning("Account alert delivery deferred")


async def accounts(user_id):
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""SELECT COALESCE(display_label,broker_name,connector_type)
            FROM trading_accounts WHERE user_id=:u AND is_active ORDER BY id"""), {"u":user_id})).scalars().all()
        if await conn.scalar(text("SELECT to_regclass('mtapi_connections') IS NOT NULL")):
            summaries = await conn.scalar(text("SELECT to_regclass('mtapi_account_summary') IS NOT NULL"))
            query = """SELECT COALESCE(c.broker_name,'Match-Trader') || ' · ending ' ||
                right(c.external_login,4) AS label, c.account_currency AS currency, """
            query += ("s.equity, s.fetched_at, NOW() - s.fetched_at AS age FROM mtapi_connections c "
                      "LEFT JOIN mtapi_account_summary s ON s.connection_id=c.id " if summaries else
                      "NULL AS equity, NULL AS fetched_at, NULL AS age FROM mtapi_connections c ")
            query += "WHERE c.user_id=:u AND c.disconnected_at IS NULL ORDER BY c.id"
            for row in (await conn.execute(text(query), {"u":user_id})).mappings():
                label = row["label"]
                if row["equity"] is None or not row["fetched_at"] or not row["currency"]:
                    rows.append(label + " · Waiting for account data")
                    continue
                fresh = 0 <= row["age"].total_seconds() <= 90
                stamp = row["fetched_at"].astimezone(timezone.utc).strftime("%d %b %H:%M UTC")
                prefix = "Equity" if fresh else "Last recorded equity"
                rows.append(f"{label}\n{prefix}: {row['equity']:,.2f} {row['currency']} · {stamp}")
    return ("Your connected accounts\n" + "\n".join(rows))[:4000] if rows else "Connect an account on talitrade.com/app using this Telegram login."


async def market_alert(key, title, detail, caption=None, section="Market update"):
    async with engine.connect() as conn:
        users = (await conn.execute(text("SELECT user_id FROM telegram_alert_preferences"))).scalars().all()
    if users:
        photo = await alert_image(section, title, detail)
        for uid in users:
            try:
                await deliver(uid, key, caption or f"{title}\n{detail}", photo)
            except Exception:
                log.warning("Market alert delivery failed")


async def run():
    next_release_check = 0
    next_gold_check = 0
    while True:
        jobs = [account_pass]
        if not profile_verified:
            jobs.append(restore_profile)
        now = asyncio.get_running_loop().time()
        if now >= next_release_check:
            jobs.append(release_pass)
            next_release_check = now + 600
        if now >= next_gold_check:
            jobs.append(gold_pass)
            next_gold_check = now + 3600
        for job in jobs:
            try:
                await job()
            except Exception as exc:
                log.warning("Community bot %s failed (%s)", job.__name__, type(exc).__name__)
        await asyncio.sleep(60)


async def gold_pass():
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get("https://api.nbp.pl/api/cenyzlota/last/2/?format=json")
        response.raise_for_status()
        older, latest = sorted(response.json(), key=lambda row: row["data"])
    if date.fromisoformat(latest["data"]) < date.today() - timedelta(days=1):
        return
    change = (latest["cena"] / older["cena"] - 1) * 100
    if abs(change) >= 1:
        await market_alert("nbp-gold:" + latest["data"], f"Gold fixing {change:+.2f}%",
            f"NBP | {latest['cena']:.2f} PLN/g | {older['data']} to {latest['data']}")


async def release_pass():
    for url, title, period, summary in await published():
        await market_alert("official:" + url, title, period + " · Statistics Canada",
                           summary, "Economic release")

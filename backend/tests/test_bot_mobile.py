import asyncio
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import AsyncMock
import httpx
import pytest
from PIL import Image
from app import main
from app.services import community_bot, bot_reports
from app.services.bot_banner import banner
from app.services.bot_releases import releases

@pytest.mark.parametrize("name", list(bot_reports.COMMANDS))
def test_private_commands(monkeypatch, name):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit")
    from app.services import bot_releases
    monkeypatch.setattr(bot_releases, "published", AsyncMock(return_value=[]))
    report = AsyncMock(return_value="Private account report")
    monkeypatch.setattr(bot_reports, "api_report", report)
    monkeypatch.setattr(community_bot, "accounts", AsyncMock(return_value="Connected"))
    monkeypatch.setattr(community_bot, "subscribe", AsyncMock())
    monkeypatch.setattr(main, "db_upsert_user", AsyncMock())
    monkeypatch.setattr(main, "fetch_news_calendar", AsyncMock(return_value=[]))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token":community_bot.webhook_secret("unit")},
                json={"message":{"chat":{"id":1234,"type":"private"},"from":{"id":1234},"text":"/"+name}})
            reply = response.json()
            assert reply["method"] == "sendMessage" and reply["chat_id"] == "1234" and reply["text"]
    asyncio.run(run())
    if name in ("status","today","week","summary","journal","payout"):
        report.assert_awaited_once_with("1234",name)

def test_banner_size():
    assert Image.open(BytesIO(banner("Trade closed","EUR/USD +25 pips","Account ending 0000"))).size == (1080,336)

def test_published_feed():
    xml = """<feed><entry><title><div>Consumer Price Index, <span>October 2026</span></div></title>
    <link href="https://www.statcan.gc.ca/daily-quotidien/test"/><updated>2026-11-16T08:30:00-05:00</updated>
    <summary><div>Reported result.</div></summary></entry></feed>"""
    assert releases(xml, datetime(2026,11,16,14,tzinfo=timezone.utc))[0][2:] == ("October 2026","Reported result.")
    assert not releases(xml, datetime(2026,11,18,tzinfo=timezone.utc))

def test_real_account_reports(monkeypatch):
    import os
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    url = os.getenv("TALITRADE_TEST_DATABASE_URL", "")
    if not url.endswith("/bot_commands_test"):
        pytest.skip("requires the private bot_commands_test database")
    async def run():
        db = create_async_engine(url)
        monkeypatch.setattr(bot_reports,"engine",db)
        from app.services.matchtrader_api.store import SqlStore
        await SqlStore(lambda: db).ensure_schema()
        async with db.begin() as conn:
            for uid,login in (("100","1234"),("200","9876")):
                cid = await conn.scalar(text("""INSERT INTO mtapi_connections(user_id,platform_host,external_login,account_currency)
                    VALUES(:u,'example.test',:l,'USD') RETURNING id"""),{"u":uid,"l":login})
                await conn.execute(text("""INSERT INTO mtapi_closed_positions(connection_id,uid,position_id,symbol,net_profit,close_time,raw)
                    VALUES(:id,'entry','position',:s,12.5,now(),'{}')"""),{"id":cid,"s":"EURUSD" if uid=="100" else "OTHER"})
        for name in ("today","week","summary","journal","status","payout"):
            reply = await bot_reports.api_report("100",name)
            assert reply and "9876" not in reply and "OTHER" not in reply
            if name in ("today","week","summary","journal"):
                assert "12.50" in reply
        assert await bot_reports.api_report("no_accounts","journal") is None
        await db.dispose()
    asyncio.run(run())

def test_automatic_release_caption(monkeypatch):
    url = "https://www.statcan.gc.ca/daily-quotidien/example"
    monkeypatch.setattr(community_bot,"published",AsyncMock(return_value=[(url,"Canadian inflation","October","Prices rose.")]))
    send = AsyncMock()
    monkeypatch.setattr(community_bot,"market_alert",send)
    asyncio.run(community_bot.release_pass())
    assert send.await_args.args[3] == "Prices rose."
    assert url in send.await_args.args[0]

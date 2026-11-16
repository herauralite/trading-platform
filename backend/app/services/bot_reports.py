from sqlalchemy import text
from app.core.database import engine

COMMANDS = {
    "start": "Connect your Telegram and trading accounts",
    "accounts": "List your linked accounts",
    "status": "Check account equity and last update",
    "today": "Today's imported trading results",
    "journal": "Your 10 latest closing entries",
    "summary": "Daily trading recap",
    "week": "Seven-day trading recap",
    "news": "Upcoming economic releases",
    "payout": "Check available payout information",
    "alerts": "Enable private account and market alerts",
    "stop": "Turn off subscribed alerts",
    "help": "Show all commands",
}

async def api_report(user, command):
    async with engine.connect() as conn:
        if not await conn.scalar(text("SELECT to_regclass('mtapi_connections') IS NOT NULL")):
            return None
        accounts = (await conn.execute(text("""SELECT c.id, coalesce(c.broker_name,'Match-Trader') AS broker,
            right(c.external_login,4) AS ending, c.account_currency AS currency, s.equity, s.fetched_at
            FROM mtapi_connections c LEFT JOIN mtapi_account_summary s ON s.connection_id=c.id
            WHERE c.user_id=:u AND c.disconnected_at IS NULL ORDER BY c.id"""), {"u":user})).mappings().all()
        if not accounts:
            return None
        if command == "payout":
            return "Match-Trader supplies balances and trade history. Payout eligibility requires the firm’s rules and payout records. Check the payout page in your firm’s portal."
        labels = {a["id"]:f"{a['broker']} · ending {a['ending']}" for a in accounts}
        if command == "status":
            return "\n\n".join(labels[a["id"]] + ("\nWaiting for equity snapshot" if a["equity"] is None else
                f"\nRecorded equity {a['equity']:,.2f} {a['currency'] or ''} · {a['fetched_at']:%d %b %H:%M %Z}") for a in accounts)
        owned = """ FROM mtapi_closed_positions t JOIN mtapi_connections c ON c.id=t.connection_id
            WHERE c.user_id=:u AND c.disconnected_at IS NULL """
        if command == "journal":
            rows = (await conn.execute(text("SELECT t.connection_id,t.symbol,t.close_time,t.net_profit,c.account_currency" + owned +
                " ORDER BY t.close_time DESC,t.connection_id,t.uid LIMIT 10"), {"u":user})).mappings().all()
            lines = [f"{r['close_time']:%d %b %H:%M %Z} · {labels[r['connection_id']]}\n{r['symbol']} · " +
                     (f"{r['net_profit']:+,.2f} {r['account_currency'] or ''}" if r['net_profit'] is not None else "Net unavailable") for r in rows]
            return "Latest imported closes\n" + ("\n".join(lines) or "No imported closes yet.")
        days = 6 if command == "week" else 0
        rows = (await conn.execute(text("SELECT t.connection_id,count(*) AS entries,count(t.net_profit) AS priced,"
            "sum(t.net_profit) AS net,count(*) FILTER (WHERE t.net_profit>0) AS wins" + owned +
            "AND t.close_time >= (date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') - :days * interval '1 day' "
            "AND t.close_time <= now() GROUP BY t.connection_id"), {"u":user,"days":days})).mappings().all()
        totals = {r["connection_id"]:r for r in rows}
        lines = ["Last 7 UTC dates" if days else "Today · UTC"]
        for a in accounts:
            row = totals.get(a["id"])
            result = "No imported closes." if not row else f"{row['entries']} closing entries · {row['wins']} wins · " + (
                f"Net {row['net']:+,.2f} {a['currency'] or ''}" if row['priced']==row['entries'] else "Net incomplete")
            lines.append(labels[a["id"]] + "\n" + result)
        return "\n\n".join(lines)

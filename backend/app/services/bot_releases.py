from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree
from urllib.parse import urlsplit
import httpx

URL = "https://www150.statcan.gc.ca/n1/rss/dai-quo/0-eng.atom"
TOPICS = {
    "Consumer Price Index": "Canadian inflation",
    "Labour Force Survey": "Canadian jobs report",
    "Gross domestic product by industry": "Canadian economic growth",
    "Retail trade": "Canadian retail sales",
}

def releases(xml, now):
    rows = []
    for entry in ElementTree.fromstring(xml).findall("{*}entry"):
        def field(key):
            node = entry.find("{*}" + key)
            return " ".join("".join(node.itertext()).split()) if node is not None else ""
        topic, _, period = field("title").partition(", ")
        link = entry.find("{*}link")
        if topic not in TOPICS or link is None or not field("summary"):
            continue
        url = link.get("href", "")
        if urlsplit(url).scheme != "https" or urlsplit(url).hostname not in {"www.statcan.gc.ca", "www150.statcan.gc.ca"}:
            continue
        stamp = datetime.fromisoformat(field("updated"))
        if timedelta(0) <= now - stamp <= timedelta(hours=12):
            rows.append((url, TOPICS[topic], period, field("summary")[:620]))
    return rows

async def published():
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        response = await client.get(URL)
        response.raise_for_status()
        return releases(response.content, datetime.now(timezone.utc))

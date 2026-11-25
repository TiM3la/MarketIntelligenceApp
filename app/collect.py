import asyncio
import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from .store import canonical, clean

AGENT = "MarketIntelligence/1.0"


def request(url, headers=None, data=None):
    canonical(url)
    req = urllib.request.Request(url, data=data, headers={"User-Agent": AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as response:
        payload = response.read(16*1024*1024+1)
        if len(payload) > 16*1024*1024:
            raise ValueError("Ответ больше 16 МБ")
        return payload, response.headers


def feed(payload, base):
    root = ET.fromstring(payload)
    for node in root.iter():
        node.tag = node.tag.split("}")[-1]
    if root.tag not in ("rss", "feed", "RDF"):
        raise ValueError("Документ не является RSS/Atom")
    rows = []
    for node in root.iter():
        if node.tag not in ("item", "entry"):
            continue
        link = next((l.get("href") or l.text for l in node.findall("link")
                     if l.get("rel","alternate") == "alternate"), None)
        if not link:
            continue
        url = urljoin(base, link.strip())
        rows.append((node.findtext("guid") or node.findtext("id") or url, node.findtext("title"),
                     node.findtext("encoded") or node.findtext("content") or node.findtext("description") or node.findtext("summary") or "",
                     url, node.findtext("published") or node.findtext("pubDate") or node.findtext("updated")))
    return rows


def web_source(s):
    url = s["url"]
    if s["kind"] == "youtube":
        if not re.fullmatch(r"UC[\w-]{22}", url):
            raise ValueError("Нужен идентификатор YouTube-канала UC…")
        url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + url
    if s["kind"] == "site":
        robots = RobotFileParser()
        try:
            data, _ = request(urljoin(url, "/robots.txt"))
            robots.parse(data.decode(errors="replace").splitlines())
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        else:
            if not robots.can_fetch(AGENT, url):
                raise ValueError("Доступ ограничен robots.txt")
    headers = {key:s[col] for key,col in (("If-None-Match","etag"),("If-Modified-Since","modified")) if s[col]}
    try:
        payload, meta = request(url, headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return [], s["etag"], s["modified"], s["cursor"]
        raise
    if s["kind"] == "site":
        from bs4 import BeautifulSoup
        doc = BeautifulSoup(payload, "html.parser")
        nodes = doc.select(s["selector"] or "article") or ([] if s["selector"] else [doc.body or doc])
        rows = []
        for node in nodes:
            heading = node.select_one("h1,h2,h3") or doc.title
            link = node if node.name == "a" else (heading.find("a") if heading else None)
            if link is None and node.name == "article":
                link = node.find("a",href=True)
            target = urljoin(url, link.get("href",url)) if link else url
            date = node.find("time")
            rows.append((target, heading.get_text(" ",strip=True) if heading else "",node.get_text(" ",strip=True),
                         target,date.get("datetime") if date else None))
    else:
        rows = feed(payload, url)
    return rows, meta.get("ETag"), meta.get("Last-Modified"), s["cursor"]


async def telegram_source(client, s):
    if client is None:
        raise ValueError("Требуется авторизация Telethon")
    channel = s["url"].rstrip("/").split("/")[-1].lstrip("@")
    rows = []
    async for msg in client.iter_messages(channel, limit=None if s["cursor"] else 50,
                                          min_id=max(0,s["cursor"]-20), reverse=bool(s["cursor"])):
        body = msg.message or ""
        if body:
            rows.append((str(msg.id), clean(body)[:120], body, f"https://t.me/{channel}/{msg.id}",msg.date.isoformat()))
    return rows, None, None, max([int(r[0]) for r in rows]+[s["cursor"]])


async def telethon(login=False):
    if not os.getenv("TELEGRAM_API_ID") or not os.getenv("TELEGRAM_API_HASH"):
        return None
    from telethon import TelegramClient
    client = TelegramClient(os.getenv("SESSION_PATH","data/telegram"),int(os.environ["TELEGRAM_API_ID"]),os.environ["TELEGRAM_API_HASH"])
    if login:
        await client.start()
    else:
        await client.connect()
    if await client.is_user_authorized():
        return client
    await client.disconnect()


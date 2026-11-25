import hashlib
import html
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LEVELS = ("LOW", "NORMAL", "HIGH", "CRITICAL")
ROOT = Path(__file__).resolve().parent.parent


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=WAL")
    db.execute("pragma foreign_keys=ON")
    if not db.execute("pragma user_version").fetchone()[0]:
        db.executescript((ROOT / "app/schema.sql").read_text("utf-8"))
        with db:
            for name in json.loads((ROOT / "config/sources.json").read_text("utf-8")):
                if isinstance(name, dict):
                    db.execute("insert into sources(name,url,category,trust,enabled) values(:name,:url,:category,:trust,0)", name)
                else:
                    db.execute("insert into sources(name,enabled) values(?,0)", (name,))
            for name, words in json.loads((ROOT / "config/categories.json").read_text("utf-8")).items():
                db.execute("insert into categories values(?,?)", (name, words))
            db.execute("pragma user_version=1")
    if db.execute("pragma user_version").fetchone()[0] == 1:
        with db:
            db.execute("""create table if not exists segment_analysis(
                event integer references videos(event), start real, analysis text not null,
                primary key(event,start))""")
            db.execute("pragma user_version=2")
    if db.execute("pragma user_version").fetchone()[0] == 2:
        with db:
            db.execute("alter table originals add column original_url text")
            db.execute("update originals set original_url=url")
            db.execute("pragma user_version=3")
    if db.execute("pragma user_version").fetchone()[0] == 3:
        with db:
            db.execute("""create table if not exists receipts(
                kind text, ref integer, chat_id integer, cursor integer default 0,
                sent real, next real default 0, primary key(kind,ref,chat_id),
                foreign key(kind,ref) references deliveries(kind,ref))""")
            db.execute("pragma user_version=4")
    return db


def setting(db, key, default=""):
    row = db.execute("select value from settings where key=?", (key,)).fetchone()
    return row[0] if row else default


def clean(text):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def canonical(url):
    p = urlsplit(url)
    if p.scheme not in ("https", "http") or not p.hostname:
        raise ValueError("Нужна ссылка HTTP(S)")
    args = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k != "fbclid"]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", urlencode(args), ""))


def words(value):
    return [x.strip().casefold() for x in value.split(",") if x.strip()]


def ingest(db, source, item):
    uid, title, raw, original_url, published = item
    url = canonical(original_url)
    title, body = clean(title), clean(raw)
    if not title and not body:
        return
    title = title or body[:150]
    content = (title + " " + body).casefold()
    digest = hashlib.sha256(content.encode()).hexdigest()
    old = db.execute("select event from originals where source=? and uid=? order by id desc limit 1",
                     (source["id"], uid)).fetchone()
    match = old or db.execute("""select event from originals where url=? or hash=?
        or (lower(title)=lower(?) and length(?)>=35 and detected>?) limit 1""",
        (url, digest, title, title, time.time()-86400)).fetchone()
    hits = [(sum(w in content for w in words(r[1])), r[0]) for r in db.execute("select * from categories")]
    score, cat = max(hits, default=(0, "Other"))
    if not score:
        cat = source["category"] or "Other"
    instruments = [r[0] for r in db.execute("select ticker,keywords,category from watchlist")
                   if any(w in content for w in words(r[0]+","+r[1])) or cat in r[2].split(",")]
    points = bool(score) + bool(re.search(r"\d", content)) + (source["trust"] >= 2) + bool(instruments)
    level = LEVELS[min(points, 3)]
    now = time.time()
    if match:
        eid = match[0]
    else:
        eid = db.execute("insert into events(title,category,importance,detected,instruments) values(?,?,?,?,?)",
                         (title, cat, level, now, ", ".join(instruments))).lastrowid
        selected = words(setting(db, "filter"))
        if LEVELS.index(level) >= LEVELS.index(setting(db, "minimum", "NORMAL")) and (not selected or cat.casefold() in selected):
            db.execute("insert into deliveries(kind,ref) values('news',?)", (eid,))
    db.execute("""insert or ignore into originals(source,uid,event,title,raw,url,published,hash,detected,original_url)
                  values(?,?,?,?,?,?,?,?,?,?)""", (source["id"], uid, eid, title, raw, url, published, digest, now, original_url))
    if source["kind"] == "youtube":
        db.execute("insert or ignore into videos(event,url) values(?,?)", (eid,url))
    return eid


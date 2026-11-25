import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import time
import urllib.error
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import analysis, bot, collect, video
from .store import connect, ingest, setting

log = logging.getLogger("market")


def configure():
    if Path(".env").exists():
        for line in Path(".env").read_text("utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key,value = line.split("=",1)
                os.environ.setdefault(key.strip(),value.strip().strip("\"'"))
    os.umask(0o077)
    Path("data").mkdir(exist_ok=True,mode=0o700)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),RotatingFileHandler("data/market.log",maxBytes=1000000,backupCount=3,encoding="utf-8")])


def error(exc):
    # HTTP and library exceptions can contain signed URLs or credentials.
    if isinstance(exc,urllib.error.HTTPError):
        return "HTTP "+str(exc.code)
    if isinstance(exc,ValueError) and exc.__class__ is ValueError:
        return str(exc)[:160]
    return type(exc).__name__


async def poll(db,s,client):
    now = time.time()
    try:
        if s["kind"] == "telegram":
            rows,etag,modified,cursor = await collect.telegram_source(client,s)
        else:
            rows,etag,modified,cursor = await asyncio.to_thread(collect.web_source,s)
        with db:
            current = db.execute("select * from sources where id=?",(s["id"],)).fetchone()
            if tuple(current) != tuple(s):
                return
            for item in rows:
                try:
                    ingest(db,s,item)
                except ValueError as exc:
                    log.warning("Источник %s: %s",s["id"],error(exc))
            db.execute("""update sources set checked=?,success=?,error=null,next=?,etag=?,modified=?,cursor=?
                          where id=?""",(now,now,time.time()+s["interval"],etag,modified,cursor,s["id"]))
    except Exception as exc:
        log.warning("Источник %s: %s",s["id"],error(exc))
        delay = max(s["interval"],60)
        if isinstance(exc,urllib.error.HTTPError) and exc.headers:
            retry = exc.headers.get("Retry-After","")
            if retry.isdigit():
                delay = max(delay,int(retry))
        with db:
            db.execute("update sources set checked=?,error=?,next=? where id=?",(now,error(exc),now+delay,s["id"]))


async def monitor(db,client,once=False):
    tasks = {}
    try:
        while True:
            if once:
                for s in db.execute("select * from sources where enabled=1 and next<=? order by next",(time.time(),)).fetchall():
                    await poll(db,s,client)
                return
            for s in db.execute("select * from sources where enabled=1 and next<=? order by next",(time.time(),)).fetchall():
                if s["id"] not in tasks and len(tasks)<8:
                    tasks[s["id"]] = asyncio.create_task(poll(db,s,client))
            await asyncio.sleep(1)
            tasks = {k:v for k,v in tasks.items() if not v.done()}
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(),return_exceptions=True)


async def ai_job(db):
    row = db.execute("""select e.id,o.title,o.raw,o.url from events e join originals o on o.event=e.id
        where e.importance in ('HIGH','CRITICAL') and analysis is null and ai_next<=? order by e.id limit 1""",(time.time(),)).fetchone()
    if row:
        try:
            result = await asyncio.to_thread(analysis.analyze,"\n".join(row[1:]))
            with db:
                db.execute("update events set analysis=? where id=?",(json.dumps(result,ensure_ascii=False),row[0]))
                db.execute("insert or replace into settings values('ai','подключён')")
        except Exception as exc:
            with db:
                db.execute("update events set ai_next=? where id=?",(time.time()+300,row[0]))
                db.execute("insert or replace into settings values('ai',?)",(error(exc),))
            raise


async def video_job(db):
    row = db.execute("""select v.*,e.title from videos v join events e on e.id=v.event
                        where v.report is null and v.next<=? order by v.event limit 1""",(time.time(),)).fetchone()
    if not row:
        return
    eid = row["event"]
    try:
        if row["transcript"]:
            lang,lines = row["language"],json.loads(row["transcript"])
        else:
            lang,lines = await asyncio.to_thread(video.transcript,row["url"])
        groups = video.chunks(lines)
        if not groups:
            raise ValueError("Пустая расшифровка")
        result = video.report(row["title"],row["url"],groups)
        with db:
            db.executemany("insert or replace into segments values(?,?,?,?)",[(eid,*g) for g in groups])
            db.execute("update videos set language=?,transcript=? where event=?",
                       (lang,json.dumps(lines,ensure_ascii=False),eid))
        if all(os.getenv(k) for k in ("LLM_API_KEY","LLM_MODEL","LLM_BASE_URL")):
            annotations = []
            for start,end,body in groups:
                saved = db.execute("select analysis from segment_analysis where event=? and start=?",(eid,start)).fetchone()
                if saved:
                    note = json.loads(saved[0])
                else:
                    note = await asyncio.to_thread(analysis.analyze,row["title"]+"\n"+body)
                    with db:
                        db.execute("insert into segment_analysis values(?,?,?)",(eid,start,json.dumps(note,ensure_ascii=False)))
                    await asyncio.sleep(2)
                annotations.append(f"{row['url']}&t={int(start)}s\n{note['summary']}\n"
                                   +"Факты: "+" · ".join(note["facts"])+"\n"
                                   +"Возможное влияние: "+" · ".join(note["potential_implications"])+"\n"
                                   +"Неопределённости: "+" · ".join(note["uncertainties"]))
            result += "\n\nAI-анализ фрагментов\n"+"\n\n".join(annotations)
        with db:
            db.execute("update videos set status='ready',report=?,error=null where event=?",(result,eid))
            db.execute("insert or ignore into deliveries(kind,ref) values('video',?)",(eid,))
    except Exception as exc:
        with db:
            db.execute("update videos set status='retry',error=?,next=? where event=?",(error(exc),time.time()+3600,eid))
        raise


async def delivery(db):
    now = time.time()
    with db:
        db.execute("""insert or ignore into deliveries(kind,ref) select 'reminder',id from calendar
                      where due between ? and ? and result is null""",(now,now+900))
    if setting(db,"paused")=="1":
        return
    with db:
        for recipient in bot.MEMBERS:
            db.execute("""insert or ignore into receipts(kind,ref,chat_id)
                          select kind,ref,? from deliveries where sent is null""",(recipient,))
    row = db.execute("""select kind,ref,chat_id,cursor from receipts
                        where sent is null and next<=? order by rowid limit 1""",(now,)).fetchone()
    if not row:
        return
    kind,eid,recipient,cursor = row
    markup = {}
    if kind == "news":
        text = bot.event_text(db,eid)
        markup = {"reply_markup":{"inline_keyboard":[[{"text":"AI-анализ","callback_data":f"/analysis {eid}"},
                                                         {"text":"Сохранить","callback_data":f"/save {eid}"}]]}}
    elif kind == "video":
        text = db.execute("select report from videos where event=?",(eid,)).fetchone()[0]
    else:
        e = db.execute("select * from calendar where id=?",(eid,)).fetchone()
        text = e["title"]+"\n"+(e["result"] if kind=="result" else "Начало: "+bot.stamp(e["due"],setting(db,"timezone","UTC")))+"\n"+e["source"]
    try:
        while cursor<len(text):
            block = text[cursor:cursor+2000]
            await bot.api("sendMessage",chat_id=recipient,text=block,**markup)
            cursor += len(block)
            with db:
                db.execute("update receipts set cursor=? where kind=? and ref=? and chat_id=?",(cursor,kind,eid,recipient))
        with db:
            db.execute("update receipts set sent=? where kind=? and ref=? and chat_id=?",(time.time(),kind,eid,recipient))
            db.execute("""update deliveries set sent=? where kind=? and ref=? and not exists
                          (select 1 from receipts where kind=? and ref=? and sent is null)""",
                       (time.time(),kind,eid,kind,eid))
    except Exception:
        with db:
            db.execute("update receipts set next=? where kind=? and ref=? and chat_id=?",(time.time()+60,kind,eid,recipient))
        raise


async def repeat(fn,db,delay):
    while True:
        try:
            await fn(db)
        except Exception as exc:
            log.warning("%s: %s",fn.__name__,error(exc))
            await asyncio.sleep(5)
        await asyncio.sleep(delay)


async def run(mode):
    configure()
    db = connect(os.getenv("DATABASE_PATH","data/market.sqlite3"))
    client = None
    tasks = []
    try:
        if mode == "login":
            client = await collect.telethon(True)
            if client is None:
                raise SystemExit("Укажите TELEGRAM_API_ID и TELEGRAM_API_HASH в .env")
            return
        try:
            client = await collect.telethon()
        except Exception as exc:
            log.warning("Telethon: %s",error(exc))
        with db:
            db.execute("insert or replace into settings values('telethon',?)",("подключён" if client else "отключён",))
            configured = all(os.getenv(k) for k in ("LLM_API_KEY","LLM_MODEL","LLM_BASE_URL"))
            db.execute("insert or replace into settings values('ai',?)",("ожидает публикацию" if configured else "отключён",))
        if mode == "once":
            await monitor(db,client,True)
            print("Проверка завершена. Событий:",db.execute("select count(*) from events").fetchone()[0])
            return
        tasks.append(asyncio.create_task(monitor(db,client)))
        if os.getenv("TELEGRAM_BOT_TOKEN"):
            tasks.extend(asyncio.create_task(repeat(fn,db,delay)) for fn,delay in [(bot.messages,0.3),(delivery,1)])
        if configured:
            tasks.append(asyncio.create_task(repeat(ai_job,db,10)))
        if os.getenv("VIDEO_ENABLED","1")=="1":
            tasks.append(asyncio.create_task(repeat(video_job,db,5)))
        log.info("Market Intelligence запущен")
        stop = asyncio.Event()
        for sig in (signal.SIGINT,signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                asyncio.get_running_loop().add_signal_handler(sig,stop.set)
        stopper = asyncio.create_task(stop.wait())
        tasks.append(stopper)
        finished,_ = await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for task in finished:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if client:
            await client.disconnect()
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode",nargs="?",default="run",choices=["run","once","login"])
    try:
        asyncio.run(run(parser.parse_args().mode))
    except KeyboardInterrupt:
        pass


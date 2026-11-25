import asyncio
import json
import os
import shlex
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from .access import MEMBERS
from .collect import request
from .store import LEVELS, canonical, clean, setting

MENU = {"Новости":"/recent","Источники":"/sources","Инструменты":"/watchlist","Категории":"/categories",
        "Настройки уведомлений":"/settings","Архив":"/search","Аналитика":"/digest","Статус системы":"/status"}
HELP = """Общие настройки Руслана и Яны
/sources — список и состояние
/add_source rss|site|telegram|youtube адрес название
/source ID поле значение — name, kind, url, category, interval (сек.), trust (0–3), enabled (0/1), selector
/remove_source ID — убрать из мониторинга
/categories [название | ключевые слова через запятую]
/watchlist [тикер | название | тип | слова | категории | активы]
/unwatch тикер
/settings minimum NORMAL или /settings filter категории через запятую
/settings timezone Europe/Moscow
/settings language ru|en · /settings digest_minimum LOW
/saved — избранное · /report ID — отчёт по видео
/pause · /resume · /test
/recent · /search запрос · /digest · /event ID · /analysis ID
/calendar · /schedule дата_ISO_со_смещением | название | источник | категория
/result ID текст · /status"""




async def api(method, **args):
    raw,_ = await asyncio.to_thread(request, "https://api.telegram.org/bot"+os.environ["TELEGRAM_BOT_TOKEN"]+"/"+method,
                                    {"Content-Type":"application/json"},json.dumps(args).encode())
    result = json.loads(raw)
    if not result.get("ok"):
        raise RuntimeError("Ошибка Telegram API")
    return result["result"]


def stamp(ts, zone):
    tz = timezone.utc if zone == "UTC" else ZoneInfo(zone)
    return datetime.fromtimestamp(ts,timezone.utc).astimezone(tz).strftime("%d.%m.%Y %H:%M %Z")


def event_text(db,eid):
    e = db.execute("select * from events where id=?",(eid,)).fetchone()
    if not e:
        return "Событие не найдено"
    rows = db.execute("""select o.*,s.name from originals o join sources s on s.id=o.source
                         where event=? order by o.id desc""",(eid,)).fetchall()
    r = rows[0]
    links = "\n".join(dict.fromkeys(x["name"]+" · "+x["original_url"] for x in rows))
    if setting(db,"language","ru") == "en":
        return (f"#{eid} · {e['importance']} · {e['category']}\n{r['title'][:350]}\n\n{clean(r['raw'])[:900]}"
                f"\nInstruments: {e['instruments'] or '—'}\nPublished: {r['published'] or 'unknown'}"
                f"\nDetected: {stamp(e['detected'],setting(db,'timezone','UTC'))}\n{links[:1300]}")
    return (f"#{eid} · {e['importance']} · {e['category']}\n{r['title'][:350]}\n\n{clean(r['raw'])[:900]}"
            f"\nИнструменты: {e['instruments'] or '—'}\nОпубликовано: {r['published'] or 'время не указано'}"
            f"\nОбнаружено: {stamp(e['detected'],setting(db,'timezone','UTC'))}\n{links[:1300]}")


def command(db,message):
    text = MENU.get(message,message).strip()
    cmd,_,arg = text.partition(" ")
    arg = arg.strip()
    if cmd in ("/start","/help"):
        return HELP
    if cmd == "/test":
        return "Тестовое уведомление доставлено."
    if cmd == "/status":
        n,errors,last = db.execute("select count(*),count(error),max(success) from sources where enabled=1").fetchone()
        return (f"Источники: {n} · с ошибками: {errors}\nПоследний успех: {stamp(last,'UTC') if last else '—'}"
                f"\nTelegram Bot: подключён\nTelethon: {setting(db,'telethon','выключен')}"
                f"\nAI: {setting(db,'ai','не настроен')}\nВидео: {dict(db.execute('select status,count(*) from videos group by status'))}")
    if cmd == "/sources":
        return "\n".join(f"{r['id']} · {r['name']} · {r['kind']} · {r['interval']}с · "
                         + (r["error"] or ("проверен" if r["success"] else "ожидает проверки") if r["enabled"] else "выкл.")
                         for r in db.execute("select * from sources where enabled>=0"))+"\n/add_source · /source · /remove_source"
    if cmd == "/add_source":
        kind,url,*name = shlex.split(arg)
        if kind not in ("rss","site","telegram","youtube"):
            raise ValueError("Тип: rss, site, telegram или youtube")
        if kind in ("rss","site"):
            canonical(url)
        sid = db.execute("insert into sources(name,kind,url) values(?,?,?)",(" ".join(name) or url,kind,url)).lastrowid
        return f"Источник #{sid} добавлен. Результат проверки — /sources."
    if cmd == "/source":
        sid,field,value = arg.split(maxsplit=2)
        if field not in ("name","kind","url","category","interval","trust","enabled","selector"):
            raise ValueError("Неизвестное поле")
        if field == "kind" and value not in ("rss","site","telegram","youtube"):
            raise ValueError("Тип: rss, site, telegram или youtube")
        if field in ("interval","trust","enabled"):
            value = int(value)
            if not {"interval":value>=15,"trust":0<=value<=3,"enabled":value in (0,1)}[field]:
                raise ValueError("Проверьте диапазон значения")
        reset = ",etag=null,modified=null,cursor=0,success=null,error=null" if field in ("kind","url","selector") else ""
        cur = db.execute(f"update sources set {field}=?,next=0{reset} where id=? and enabled>=0",(value,int(sid)))
        return "Сохранено" if cur.rowcount else "Источник не найден"
    if cmd == "/remove_source":
        db.execute("update sources set enabled=-1 where id=?",(int(arg),))
        return "Источник снят с мониторинга"
    if cmd in ("/pause","/resume"):
        db.execute("insert or replace into settings values('paused',?)",(str(int(cmd=="/pause")),))
        return "Уведомления приостановлены" if cmd=="/pause" else "Уведомления возобновлены"
    if cmd == "/settings":
        if not arg:
            return (f"Минимум: {setting(db,'minimum','NORMAL')}\nКатегории: {setting(db,'filter') or 'все'}"
                    f"\nЧасовой пояс: {setting(db,'timezone','UTC')}\nЯзык: {setting(db,'language','ru')}"
                    f"\nМинимум дайджеста: {setting(db,'digest_minimum','LOW')}"
                    "\n/settings minimum HIGH\n/settings filter Inflation,Central Banks\n/settings timezone Europe/Moscow")
        key,_,val = arg.partition(" ")
        if key in ("minimum","digest_minimum") and val.upper() in LEVELS:
            val = val.upper()
        elif key == "language" and val in ("ru","en"):
            pass
        elif key == "timezone":
            ZoneInfo(val)
        elif key != "filter":
            raise ValueError("Проверьте параметр и значение")
        db.execute("insert or replace into settings values(?,?)",(key,val))
        return "Настройки сохранены"
    if cmd in ("/categories","/watchlist"):
        if arg:
            parts = [p.strip() for p in arg.split("|")]
            if not parts[0] or len(parts) != (2 if cmd=="/categories" else 6):
                raise ValueError("Формат указан в /help")
            if cmd == "/categories":
                db.execute("insert or replace into categories values(?,?)",parts)
            else:
                db.execute("insert or replace into watchlist values(?,?,?,?,?,?)",parts)
        sql = "select * from categories" if cmd=="/categories" else "select * from watchlist"
        return "\n".join(" · ".join(r) for r in db.execute(sql)) or "Список пуст. Формат добавления — /help."
    if cmd == "/unwatch":
        db.execute("delete from watchlist where ticker=?",(arg,))
        return "Список обновлён"
    if cmd == "/digest":
        threshold = LEVELS.index(setting(db,"digest_minimum","LOW"))
        selected = [v.strip().casefold() for v in setting(db,"filter").split(",") if v.strip()]
        entries = []
        for r in db.execute("select * from events where detected>=? order by detected desc",(time.time()-86400,)):
            if LEVELS.index(r["importance"]) >= threshold and (not selected or r["category"].casefold() in selected):
                entries.append(f"{r['category']} · {r['importance']} · {r['title'][:120]} · /event {r['id']}")
            if len(entries)==20:
                break
        return "Дайджест за 24 часа\n"+("\n".join(entries) or "Нет событий по вашим фильтрам")
    if cmd in ("/recent","/search"):
        if cmd == "/search" and not arg:
            return "Введите /search и слово для поиска"
        pattern = "%"+arg+"%"
        rows = db.execute("""select distinct e.id,e.title,e.importance from events e join originals o on o.event=e.id
                             where o.title like ? or o.raw like ? order by e.id desc limit 20""",(pattern,pattern))
        return "\n".join(f"/event {r[0]} · {r[2]} · {r[1][:120]}" for r in rows) or "Публикаций пока нет"
    if cmd == "/event":
        return event_text(db,int(arg))
    if cmd == "/report":
        row = db.execute("select report,status,error from videos where event=?",(int(arg),)).fetchone()
        return (row["report"] or row["error"] or row["status"]) if row else "Видео не найдено"
    if cmd == "/save":
        db.execute("update events set saved=1 where id=?",(int(arg),))
        return "Добавлено в избранное · /saved"
    if cmd == "/saved":
        return "\n".join(f"/event {r[0]} · {r[1]}" for r in db.execute("select id,title from events where saved=1 order by id desc limit 30")) or "Избранное пусто"
    if cmd == "/analysis":
        row = db.execute("select analysis from events where id=?",(int(arg),)).fetchone()
        return json.dumps(json.loads(row[0]),ensure_ascii=False,indent=2) if row and row[0] else "AI-анализ пока отсутствует"
    if cmd == "/schedule":
        date,title,source,cat = [p.strip() for p in arg.split("|")]
        dt = datetime.fromisoformat(date)
        if dt.tzinfo is None:
            raise ValueError("Укажите смещение часового пояса")
        db.execute("insert into calendar(title,due,source,category) values(?,?,?,?)",(title,dt.timestamp(),source,cat))
        return "Событие добавлено. Напоминание за 15 минут."
    if cmd == "/calendar":
        return "\n".join(f"#{r['id']} {stamp(r['due'],setting(db,'timezone','UTC'))} · {r['title']} · {r['result'] or 'ожидается'}"
                         for r in db.execute("select * from calendar order by due desc limit 30")) or "Календарь пуст"
    if cmd == "/result":
        sid,result = arg.split(maxsplit=1)
        if db.execute("update calendar set result=? where id=?",(result,int(sid))).rowcount:
            db.execute("insert or ignore into deliveries(kind,ref) values('result',?)",(int(sid),))
            return "Результат сохранён"
        return "Событие не найдено"
    return HELP


async def messages(db):
    updates = await api("getUpdates",offset=int(setting(db,"offset","0")),timeout=8,allowed_updates=["message","callback_query"])
    for upd in updates:
        cb = upd.get("callback_query")
        msg = cb["message"] if cb else upd.get("message",{})
        user = cb["from"] if cb else msg.get("from",{})
        chat_id = msg.get("chat",{}).get("id")
        if user.get("id") in MEMBERS and chat_id == user["id"] and msg["chat"].get("type") == "private":
            text = cb["data"] if cb else msg.get("text","")
            try:
                with db:
                    reply = command(db,text)
            except (ValueError,KeyError,TypeError) as exc:
                reply = f"Проверьте ввод: {exc}\n/help"
            if cb:
                await api("answerCallbackQuery",callback_query_id=cb["id"])
            keys = list(MENU.items())
            keyboard = [[{"text":a,"callback_data":b} for a,b in keys[i:i+2]] for i in range(0,len(keys),2)]
            for offset in range(0,len(reply),2000):
                await api("sendMessage",chat_id=chat_id,text=reply[offset:offset+2000],reply_markup={"inline_keyboard":keyboard})
        with db:
            db.execute("insert or replace into settings values('offset',?)",(str(upd["update_id"]+1),))


import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from .collect import request


def chunks(lines):
    groups = []
    for start,end,text in lines:
        if not groups or start-groups[-1][0] >= 360:
            groups.append([start,end,text])
        else:
            groups[-1][1] = max(end,groups[-1][1])
            groups[-1][2] += " " + text
    return groups


def transcript(url):
    import yt_dlp
    parsed = urlsplit(url)
    code = parse_qs(parsed.query).get("v",[""])[0]
    if parsed.hostname not in ("www.youtube.com","youtube.com") or not re.fullmatch(r"[\w-]{11}",code):
        raise ValueError("Нужна ссылка на видео YouTube")
    url = "https://www.youtube.com/watch?v="+code
    with tempfile.TemporaryDirectory() as temp:
        opts = {"quiet":True,"noplaylist":True,"socket_timeout":20,"format":"bestaudio/best",
                "outtmpl":str(Path(temp)/"audio.%(ext)s")}
        with yt_dlp.YoutubeDL(opts) as ydl:
            meta = ydl.extract_info(url,download=False)
            if meta.get("live_status") in ("is_live","is_upcoming","post_live"):
                raise ValueError("Трансляция ещё не обработана")
            for tracks in (meta.get("subtitles") or {},meta.get("automatic_captions") or {}):
                for lang in sorted(tracks,key=lambda x: not x.startswith("ru")):
                    if not lang.startswith(("ru","en")):
                        continue
                    for track in tracks[lang]:
                        if track["ext"] == "json3":
                            raw,_ = request(track["url"])
                            lines = []
                            for e in json.loads(raw).get("events",[]):
                                txt = "".join(seg.get("utf8","") for seg in e.get("segs",[])).strip()
                                if txt:
                                    start = e.get("tStartMs",0)/1000
                                    lines.append((start,start+e.get("dDurationMs",0)/1000,txt))
                            if lines:
                                return lang,lines
            if os.getenv("WHISPER_ENABLED") != "1":
                raise ValueError("Нет субтитров RU/EN, Whisper отключён")
            from faster_whisper import WhisperModel
            ydl.download([url])
            audio = next(Path(temp).glob("audio.*"))
            segments,info = WhisperModel(os.getenv("WHISPER_MODEL","small"),device="cpu",compute_type="int8").transcribe(str(audio))
            return info.language,[(s.start,s.end,s.text.strip()) for s in segments]


def report(title,url,groups):
    statements, changes, figures, timestamps = [], [], [], []
    for start,end,body in groups:
        link = f"{int(start)//60}:{int(start)%60:02d} · {url}&t={int(start)}s"
        timestamps.append(link)
        statements.append(f"{link}\n{body[:350]}")
        sentences = re.split(r"(?<=[.!?])\s+",body)
        for target,pattern in ((changes,r"решил|решени|измен|decid|chang"),(figures,r"\d")):
            sentence = next((s for s in sentences if re.search(pattern,s,re.I)),None)
            if sentence:
                target.append(f"{link}\n{sentence[:350]}")
    sections = [
        ("Краткое содержание (начало записи)", statements[:1]),
        ("Заявления в расшифровке",statements),
        ("Упоминания решений и изменений",changes),
        ("Числа в расшифровке",figures),
        ("Возможное влияние на рынки",["Требуется интерпретация в контексте события."]),
        ("Неопределённости",["Точность цитат и чисел зависит от распознавания речи. Сверьте их с записью."]),
        ("Сравнение с предыдущими заявлениями",["Сопоставление с предыдущими материалами не проводилось."]),
        ("Таймкоды начала фрагментов",timestamps),
    ]
    return title+"\n\n"+"\n\n".join(name+"\n"+"\n".join(items or ["Не обнаружено"]) for name,items in sections)


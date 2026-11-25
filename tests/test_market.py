import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from xml.etree.ElementTree import ParseError

from app import analysis, bot, collect, main, store, video

RSS = b"""<rss><channel><item><guid>a</guid><title>CPI 4%</title><description>Inflation</description>
<link>https://news.example/a?utm_source=test</link></item></channel></rss>"""


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"db"
        self.db = store.connect(self.path)
        bot.command(self.db,"/add_source rss https://news.example/feed Test")
        self.source = self.db.execute("select * from sources where enabled=1").fetchone()
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def first(self,sql):
        return self.db.execute(sql).fetchone()[0]

    def poll(self):
        with patch("app.collect.request",return_value=(RSS,{"ETag":"123"})):
            asyncio.run(main.poll(self.db,self.source,None))

    def test_feed(self):
        self.assertEqual(collect.feed(RSS,"https://news.example")[0][0],"a")
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>1</id><title>x</title><link href="/1"/></entry></feed>'
        self.assertEqual(collect.feed(atom,"https://ex.org/feed")[0][3],"https://ex.org/1")

    def test_bad_feed(self):
        with self.assertRaises(ParseError):
            collect.feed(b"<rss","https://ex.org")
        with self.assertRaises(ValueError):
            collect.feed(b"<html/>","https://ex.org")

    def test_site_down(self):
        self.db.execute("update sources set kind='site' where enabled=1")
        source = self.db.execute("select * from sources where enabled=1").fetchone()
        with patch("app.collect.request",side_effect=TimeoutError):
            asyncio.run(main.poll(self.db,source,None))
        self.assertEqual(self.first("select error from sources where enabled=1"),"TimeoutError")

    def test_clean(self):
        self.assertEqual(store.clean(" A  <i>B</i> &amp;   C"),"A B & C")

    def test_pipeline_restart_and_delivery(self):
        self.poll()
        self.db.close()
        self.db = store.connect(self.path)
        self.source = self.db.execute("select * from sources where enabled=1").fetchone()
        self.poll()
        self.assertEqual(self.first("select count(*) from events"),1)
        self.assertEqual(self.first("select count(*) from originals"),1)
        self.assertEqual(self.first("select category from events"),"Inflation")
        self.assertEqual(self.first("select importance from events"),"HIGH")
        with patch("app.bot.api",new_callable=AsyncMock) as api:
            asyncio.run(main.delivery(self.db))
            asyncio.run(main.delivery(self.db))
            asyncio.run(main.delivery(self.db))
            self.assertEqual([call.kwargs["chat_id"] for call in api.await_args_list], list(bot.MEMBERS))
            self.assertIn("https://news.example/a",api.await_args.kwargs["text"])

    def test_edits_and_cross_source(self):
        self.poll()
        item = collect.feed(RSS,"https://news.example")[0]
        other = self.db.execute("select * from sources where enabled=0 limit 1").fetchone()
        with self.db:
            store.ingest(self.db,other,item)
            store.ingest(self.db,self.source,("a","Revised CPI 4%","Text",item[3],None))
        self.assertEqual(self.first("select count(*) from events"),1)
        self.assertEqual(self.first("select count(*) from originals"),3)

    def test_pending_after_telegram_error(self):
        self.poll()
        with patch("app.bot.api",new_callable=AsyncMock,side_effect=TimeoutError), self.assertRaises(TimeoutError):
            asyncio.run(main.delivery(self.db))
        self.assertIsNone(self.first("select sent from deliveries"))
        self.assertGreater(self.first("select max(next) from receipts"),0)

    def test_ai(self):
        obj = dict(category="Inflation",summary="CPI",verification_status="media",confidence=.8,importance="high",
                   **{k:[] for k in analysis.ARRAYS})
        payload = json.dumps({"choices":[{"message":{"content":json.dumps(obj)}}]}).encode()
        with patch.dict(os.environ,LLM_BASE_URL="https://mock.example",LLM_MODEL="test",LLM_API_KEY="test"), patch("app.analysis.request",return_value=(payload,{})):
            self.poll()
            asyncio.run(main.ai_job(self.db))
        self.assertEqual(json.loads(self.first("select analysis from events")),obj)
        with self.assertRaises(ValueError):
            analysis.validate({**obj,"facts":"not an array"})

    def test_free_analysis_on_demand(self):
        self.poll()
        self.db.execute("update events set importance='LOW'")
        obj = dict(category="Inflation",summary="Краткий разбор",verification_status="media",confidence=.8,
                   importance="low",**{k:[] for k in analysis.ARRAYS})
        payload = json.dumps({"choices":[{"message":{"content":json.dumps(obj)}}]}).encode()
        with patch.dict(os.environ,OPENROUTER_API_KEY="test"),patch("app.analysis.request",return_value=(payload,{})) as api:
            self.assertIn("очереди",bot.command(self.db,"/analysis 1"))
            asyncio.run(main.ai_job(self.db))
            self.assertEqual(bot.command(self.db,"/analysis 1"),"Краткий разбор")
            asyncio.run(main.ai_job(self.db))
            api.assert_called_once()
            url,headers,data = api.call_args.args
            self.assertEqual(url,"https://openrouter.ai/api/v1/chat/completions")
            self.assertEqual(json.loads(data)["model"],"openrouter/free")

    def test_transcript(self):
        self.poll()
        self.db.execute("insert into videos(event,url) values(1,'https://www.youtube.com/watch?v=abcdefghijk')")
        lines = [(0,1,"First"),(10,20,"Second"),(400,450,"Third")]
        with patch("app.video.transcript",return_value=("en",lines)):
            asyncio.run(main.video_job(self.db))
        self.assertEqual(self.first("select count(*) from segments"),2)
        self.assertEqual(json.loads(self.first("select transcript from videos"))[1][2],"Second")
        self.assertIn("t=400s",self.first("select report from videos"))

    def test_owner_guard(self):
        update = {"update_id":2,"message":{"from":{"id":10},"chat":{"id":10},"text":"/pause"}}
        with patch("app.bot.api",new_callable=AsyncMock,return_value=[update]) as api:
            asyncio.run(bot.messages(self.db))
            self.assertEqual(api.await_count,1)
        self.assertEqual(store.setting(self.db,"paused"),"")


    def test_both_members_can_manage(self):
        for member in bot.MEMBERS:
            update = {"update_id":3,"message":{"from":{"id":member},"chat":{"id":member,"type":"private"},"text":"/pause"}}
            with patch("app.bot.api",new_callable=AsyncMock,side_effect=[[update],{}]) as api:
                asyncio.run(bot.messages(self.db))
                self.assertEqual(api.await_args.kwargs["chat_id"],member)
                self.assertEqual(store.setting(self.db,"paused"),"1")

    def test_failed_recipient_does_not_block_other(self):
        self.poll()
        with patch("app.bot.api",new_callable=AsyncMock,side_effect=[TimeoutError(),{},{}]) as api:
            with self.assertRaises(TimeoutError):
                asyncio.run(main.delivery(self.db))
            asyncio.run(main.delivery(self.db))
            self.db.close()
            self.db = store.connect(self.path)
            self.db.execute("update receipts set next=0")
            asyncio.run(main.delivery(self.db))
            asyncio.run(main.delivery(self.db))
            ids = list(bot.MEMBERS)
            self.assertEqual([call.kwargs["chat_id"] for call in api.await_args_list],[ids[0],ids[1],ids[0]])

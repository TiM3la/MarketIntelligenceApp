import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import collect,video


class AdapterTests(unittest.TestCase):
    def test_captions_before_audio(self):
        package = MagicMock()
        ydl = package.YoutubeDL.return_value.__enter__.return_value
        ydl.extract_info.return_value = {"subtitles":{"ru":[{"ext":"json3","url":"https://example.com/captions"}]}}
        body = {"events":[{"tStartMs":5000,"dDurationMs":2000,"segs":[{"utf8":"Заявление"}]}]}
        with patch.dict("sys.modules",{"yt_dlp":package}),patch("app.video.request",return_value=(json.dumps(body).encode(),{})):
            lang,lines = video.transcript("https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual((lang,lines),("ru",[(5,7,"Заявление")]))
        ydl.download.assert_not_called()

    def test_youtube_discovery(self):
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>yt:video:abcdefghijk</id><title>Video</title><link rel="alternate" href="https://www.youtube.com/watch?v=abcdefghijk"/></entry></feed>'
        source = dict(kind="youtube",url="UC"+"0"*22,etag=None,modified=None,cursor=0)
        with patch("app.collect.request",return_value=(atom,{})) as fetch:
            rows,*_ = collect.web_source(source)
        self.assertIn("channel_id=UC",fetch.call_args.args[0])
        self.assertEqual(rows[0][1],"Video")

    def test_telegram_cursor_and_edit_window(self):
        async def messages(*args,**kwargs):
            for mid in (90,101):
                yield SimpleNamespace(id=mid,message=f"Новость {mid}",date=datetime.now(timezone.utc))
        client = MagicMock()
        client.iter_messages.side_effect = messages
        rows,_,_,cursor = asyncio.run(collect.telegram_source(client,dict(url="@channel",cursor=100)))
        self.assertEqual(cursor,101)
        self.assertEqual(rows[0][3],"https://t.me/channel/90")
        self.assertEqual(client.iter_messages.call_args.kwargs["min_id"],80)


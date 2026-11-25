import asyncio
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from app import bot, main, store
from tests.test_market import RSS


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/broken":
            self.send_error(503)
        elif self.headers.get("If-None-Match") == '"local"':
            self.send_response(304)
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("ETag",'"local"')
            self.end_headers()
            self.wfile.write(RSS)

    def log_message(self,*args):
        pass


class SmokeTest(unittest.TestCase):
    def test_two_real_http_sources_with_restart(self):
        server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp)/"smoke.db"
                for index in range(2):
                    db = store.connect(path)
                    try:
                        with db:
                            if index == 0:
                                for route in ("broken","feed"):
                                    bot.command(db,f"/add_source rss http://127.0.0.1:{server.server_port}/{route} {route}")
                            else:
                                db.execute("update sources set next=0")
                        asyncio.run(main.monitor(db,None,once=True))
                        self.assertEqual(db.execute("select count(*) from events").fetchone()[0],1)
                        self.assertEqual(db.execute("select count(*) from deliveries").fetchone()[0],1)
                        self.assertEqual(db.execute("select error from sources where name='broken'").fetchone()[0],"HTTP 503")
                        self.assertEqual(db.execute("select etag from sources where name='feed'").fetchone()[0],'"local"')
                        self.assertIn("Inflation",bot.event_text(db,1))
                    finally:
                        db.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


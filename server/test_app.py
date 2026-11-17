import hashlib
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import app as cc

NATE_TOK, TAYLOR_TOK = "nate-secret", "taylor-secret"


def h(t):
    return hashlib.sha256(t.encode()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.pushed = []
        self.app = cc.App(
            tokens=cc.parse_tokens(f"nate:{h(NATE_TOK)},taylor:{h(TAYLOR_TOK)}"),
            store=cc.Store(":memory:"),
            notify_for={"nate"},
            ntfy_topic="t",
            notifier=lambda topic, sender: self.pushed.append(sender),
        )


class TestApp(Base):
    def test_sender_is_stamped_from_token_not_body(self):
        self.app.send("taylor", {"to": "nate", "body": "hi", "from": "nate"})
        msgs = self.app.inbox("nate", "0")[1]["messages"]
        self.assertEqual(msgs[0]["from"], "taylor")

    def test_inbox_only_shows_own_messages(self):
        self.app.send("taylor", {"to": "nate", "body": "for nate"})
        self.assertEqual(self.app.inbox("taylor", "0")[1]["messages"], [])

    def test_after_cursor_skips_seen(self):
        a = self.app.send("taylor", {"to": "nate", "body": "one"})[1]["id"]
        self.app.send("taylor", {"to": "nate", "body": "two"})
        msgs = self.app.inbox("nate", str(a))[1]["messages"]
        self.assertEqual([m["body"] for m in msgs], ["two"])

    def test_rejects_unknown_self_and_empty(self):
        self.assertEqual(self.app.send("taylor", {"to": "cole", "body": "x"})[0], 400)
        self.assertEqual(self.app.send("taylor", {"to": "taylor", "body": "x"})[0], 400)
        self.assertEqual(self.app.send("taylor", {"to": "nate", "body": "  "})[0], 400)
        self.assertEqual(self.app.send("taylor", {"to": "nate", "body": 5})[0], 400)

    def test_rejects_oversize_body(self):
        self.assertEqual(self.app.send("taylor", {"to": "nate", "body": "x" * (cc.MAX_BODY_CHARS + 1)})[0], 413)

    def test_strips_ansi_and_control_chars(self):
        self.app.send("taylor", {"to": "nate", "body": "a\x1b[2Jb\x07c\nd"})
        self.assertEqual(self.app.inbox("nate", "0")[1]["messages"][0]["body"], "abc\nd")

    def test_rate_limit(self):
        lim = cc.RateLimiter(2)
        self.assertTrue(lim.allow("x", 0) and lim.allow("x", 1))
        self.assertFalse(lim.allow("x", 2))
        self.assertTrue(lim.allow("x", 3700))

    def test_push_is_pointer_only_and_only_for_notify_names(self):
        self.app.send("taylor", {"to": "nate", "body": "secret body"})
        self.app.send("nate", {"to": "taylor", "body": "reply"})
        import time
        time.sleep(0.05)
        self.assertEqual(self.pushed, ["taylor"])

    def test_bad_after_rejected(self):
        self.assertEqual(self.app.inbox("nate", "1;drop")[0], 400)

    def test_parse_tokens_rejects_garbage(self):
        for bad in ["", "nate", "nate:abc", "Bad Name:" + "0" * 64]:
            with self.assertRaises(ValueError):
                cc.parse_tokens(bad)


class TestHTTP(Base):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), cc.make_handler(self.app))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()

    def req(self, path, tok=None, body=None):
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                   method="POST" if body is not None else "GET")
        if tok:
            r.add_header("Authorization", f"Bearer {tok}")
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_no_token_is_401(self):
        self.assertEqual(self.req("/inbox")[0], 401)
        self.assertEqual(self.req("/inbox", "wrong")[0], 401)
        self.assertEqual(self.req("/send", None, {"to": "nate", "body": "x"})[0], 401)

    def test_round_trip(self):
        self.assertEqual(self.req("/send", TAYLOR_TOK, {"to": "nate", "body": "hello"})[0], 200)
        code, data = self.req("/inbox?after=0", NATE_TOK)
        self.assertEqual((code, data["messages"][0]["body"], data["messages"][0]["from"]), (200, "hello", "taylor"))

    def test_whoami(self):
        self.assertEqual(self.req("/whoami", TAYLOR_TOK)[1], {"me": "taylor", "peers": ["nate"]})

    def test_health_open(self):
        self.assertEqual(self.req("/health")[0], 200)


if __name__ == "__main__":
    unittest.main()

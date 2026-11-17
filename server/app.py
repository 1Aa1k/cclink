"""cclink relay: a tiny mailbox so two people's Claude Code sessions can pass text.

Design (see ../README.md):
- The sender is stamped from the bearer token, never from the request body, so nobody can spoof `from`.
- Tokens are stored only as sha256 hashes (TOKENS env: "name:hexhash,name:hexhash").
- Text only, size-capped, rate-limited, control characters stripped. Nothing is executed and no files are kept.
- Phone push is pointer-only ("new message from X"), never the body, because ntfy topics are public by name.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_CHARS = 8000
MAX_REQUEST_BYTES = 32 * 1024
RATE_LIMIT_PER_HOUR = 60
INBOX_PAGE = 50
# Strip ANSI escapes and control chars (keep \n and \t) so a message cannot repaint the reader's terminal.
_CTRL = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]")


def parse_tokens(raw: str) -> dict[str, str]:
    """Map sha256(token) hex -> name. Raises on malformed input rather than guessing."""
    out: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        name, sep, digest = part.partition(":")
        if not sep or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"bad TOKENS entry for {name!r}")
        out[digest] = name
    if not out:
        raise ValueError("TOKENS is empty")
    return out


def clean(text: str) -> str:
    return _CTRL.sub("", text)


class Store:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS msg (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " sender TEXT NOT NULL, recipient TEXT NOT NULL, at TEXT NOT NULL, body TEXT NOT NULL)"
        )
        self._db.commit()

    def add(self, sender: str, recipient: str, body: str) -> int:
        at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO msg (sender, recipient, at, body) VALUES (?,?,?,?)", (sender, recipient, at, body)
            )
            self._db.commit()
            return int(cur.lastrowid)

    def inbox(self, recipient: str, after: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, sender, at, body FROM msg WHERE recipient=? AND id>? ORDER BY id LIMIT ?",
                (recipient, after, INBOX_PAGE),
            ).fetchall()
        return [{"id": r[0], "from": r[1], "at": r[2], "body": r[3]} for r in rows]


class RateLimiter:
    def __init__(self, per_hour: int):
        self.per_hour = per_hour
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, who: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            hits = [t for t in self._hits.get(who, []) if now - t < 3600]
            if len(hits) >= self.per_hour:
                self._hits[who] = hits
                return False
            hits.append(now)
            self._hits[who] = hits
            return True


def notify_pointer(topic: str, sender: str) -> None:
    """Doorbell only: never the body. Failure must not break delivery."""
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=f"cclink: new message from {sender}. Run cclink read.".encode(),
            headers={"Title": "cclink"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).close()
    except Exception:
        pass


class App:
    def __init__(self, tokens: dict[str, str], store: Store, notify_for: set[str], ntfy_topic: str,
                 limiter: RateLimiter | None = None, notifier=notify_pointer):
        self.tokens = tokens
        self.names = set(tokens.values())
        self.store = store
        self.notify_for = notify_for
        self.ntfy_topic = ntfy_topic
        self.limiter = limiter or RateLimiter(RATE_LIMIT_PER_HOUR)
        self.notifier = notifier

    def who(self, auth_header: str | None) -> str | None:
        if not auth_header or not auth_header.startswith("Bearer "):
            return None
        digest = hashlib.sha256(auth_header[7:].strip().encode()).hexdigest()
        return self.tokens.get(digest)

    def send(self, sender: str, payload: dict) -> tuple[int, dict]:
        to, body = payload.get("to"), payload.get("body")
        if not isinstance(to, str) or to not in self.names or to == sender:
            return 400, {"error": "unknown recipient"}
        if not isinstance(body, str) or not body.strip():
            return 400, {"error": "empty body"}
        body = clean(body)
        if len(body) > MAX_BODY_CHARS:
            return 413, {"error": f"body over {MAX_BODY_CHARS} chars"}
        if not self.limiter.allow(sender):
            return 429, {"error": "rate limit"}
        mid = self.store.add(sender, to, body)
        if to in self.notify_for:
            threading.Thread(target=self.notifier, args=(self.ntfy_topic, sender), daemon=True).start()
        return 200, {"id": mid}

    def inbox(self, me: str, after_raw: str) -> tuple[int, dict]:
        if not re.fullmatch(r"\d{1,12}", after_raw or "0"):
            return 400, {"error": "bad after"}
        return 200, {"me": me, "messages": self.store.inbox(me, int(after_raw or 0))}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cclink"

        def _reply(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):  # no bodies or tokens in logs
            return

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/health":
                return self._reply(200, {"ok": True})
            me = app.who(self.headers.get("Authorization"))
            if not me:
                return self._reply(401, {"error": "unauthorized"})
            if path == "/inbox":
                params = dict(p.partition("=")[::2] for p in query.split("&") if p)
                return self._reply(*app.inbox(me, params.get("after", "0")))
            if path == "/whoami":
                return self._reply(200, {"me": me, "peers": sorted(app.names - {me})})
            return self._reply(404, {"error": "not found"})

        def do_POST(self):
            me = app.who(self.headers.get("Authorization"))
            if not me:
                return self._reply(401, {"error": "unauthorized"})
            if self.path != "/send":
                return self._reply(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._reply(400, {"error": "bad length"})
            if length <= 0 or length > MAX_REQUEST_BYTES:
                return self._reply(413, {"error": "request too large"})
            try:
                payload = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                return self._reply(400, {"error": "bad json"})
            if not isinstance(payload, dict):
                return self._reply(400, {"error": "bad json"})
            return self._reply(*app.send(me, payload))

    return Handler


def main() -> None:
    app = App(
        tokens=parse_tokens(os.environ["TOKENS"]),
        store=Store(os.environ.get("DB_PATH", "/data/cclink.db")),
        notify_for={n for n in os.environ.get("NOTIFY_FOR", "").split(",") if n},
        ntfy_topic=os.environ.get("NTFY_TOPIC", ""),
    )
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), make_handler(app)).serve_forever()


if __name__ == "__main__":
    main()

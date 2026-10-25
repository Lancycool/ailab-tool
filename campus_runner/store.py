import json
import os
import secrets
import sqlite3
from pathlib import Path

from .models import utcnow


class Store:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "state.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records(kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id))")
            db.execute("CREATE TABLE IF NOT EXISTS events(time TEXT, kind TEXT, id TEXT, detail TEXT)")
        for name in ("service", "panel"):
            p = self.home / (name + ".token")
            try:
                fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w") as f:
                    f.write(secrets.token_urlsafe(32))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def token(self, kind="service"):
        return (self.home / (kind + ".token")).read_text().strip()

    def put(self, kind, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)", (kind, key, json.dumps(value, ensure_ascii=False)))
        return value

    def get(self, kind, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if not row:
            raise KeyError(f"{kind}/{key} 不存在")
        return json.loads(row[0])

    def all(self, kind):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT value FROM records WHERE kind=? ORDER BY rowid DESC", (kind,))]

    def event(self, kind, key, detail):
        with self.connect() as db:
            db.execute("INSERT INTO events VALUES(?,?,?,?)", (utcnow(), kind, key, json.dumps(detail, ensure_ascii=False)))

"""SQLite tracking store (stdlib ``sqlite3`` — no ORM for 3 tables).

Tables:
  sync_runs   one row per space sync; dashboard + resume bookkeeping.
  page_states last seen version per page; drives update-vs-skip and delete detection.
  app_state   kv store; currently ``lastSync/<SPACE>`` for incremental polls.
Threading: single shared connection + lock (writes come from request handlers,
background syncs and the scheduler thread).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from .models import PageState, SyncRun

_DDL = """
CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  space TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'full',
  status TEXT NOT NULL DEFAULT 'queued',
  fetched INTEGER NOT NULL DEFAULT 0,
  written INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  since TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_space ON sync_runs(space, id DESC);
CREATE TABLE IF NOT EXISTS page_states (
  space TEXT NOT NULL,
  page_id TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  last_seen TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (space, page_id)
);
CREATE TABLE IF NOT EXISTS app_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class Database:
    def __init__(self, path: str) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()  # re-entrant: create_run -> get_run
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.executescript(_DDL)
    def close(self) -> None:
        """Release the sqlite file handle (required on Windows for cleanup)."""
        with self._lock:
            self._db.close()

    def update_run(self, run_id: int, **fields) -> None:
        if fields.get('status') in ('done', 'failed', 'skipped') and 'finished_at' not in fields:
            fields['finished_at'] = _now()
        cols = ', '.join(f'{k}=?' for k in fields)
        with self._lock, self._db as c:
            c.execute(f'UPDATE sync_runs SET {cols} WHERE id=?', (*fields.values(), run_id))

    # -- sync_runs ------------------------------------------------------
    def create_run(self, space: str, mode: str, since: str = '') -> SyncRun:
        with self._lock, self._db as c:
            cur = c.execute(
                'INSERT INTO sync_runs(space, mode, status, since, started_at) VALUES(?,?,?,?,?)',
                (space, mode, 'running', since, _now()),
            )
            return self.get_run(cur.lastrowid)

    def get_run(self, run_id: int) -> SyncRun | None:
        with self._lock:
            row = self._db.execute('SELECT * FROM sync_runs WHERE id=?', (run_id,)).fetchone()
        return SyncRun(**dict(row)) if row else None

    def list_runs(self, space: str = '', limit: int = 50) -> list[SyncRun]:
        q = 'SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?'
        args: tuple = (limit,)
        if space:
            q = 'SELECT * FROM sync_runs WHERE space=? ORDER BY id DESC LIMIT ?'
            args = (space, limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [SyncRun(**dict(r)) for r in rows]

    def running_run(self, space: str) -> SyncRun | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sync_runs WHERE space=? AND status IN ('queued','running') "
                'ORDER BY id DESC LIMIT 1', (space,)).fetchone()
        return SyncRun(**dict(row)) if row else None

    def last_run(self, space: str, ok_only: bool = False) -> SyncRun | None:
        q = 'SELECT * FROM sync_runs WHERE space=? ORDER BY id DESC LIMIT 1'
        if ok_only:
            q = "SELECT * FROM sync_runs WHERE space=? AND status='done' ORDER BY id DESC LIMIT 1"
        with self._lock:
            row = self._db.execute(q, (space,)).fetchone()
        return SyncRun(**dict(row)) if row else None

    # -- page_states ----------------------------------------------------
    def upsert_page(self, p: PageState) -> None:
        with self._lock, self._db as c:
            c.execute(
                'INSERT INTO page_states(space, page_id, version, title, path, updated_at, last_seen)'
                ' VALUES(?,?,?,?,?,?,?) ON CONFLICT(space, page_id) DO UPDATE SET'
                ' version=excluded.version, title=excluded.title, path=excluded.path,'
                ' updated_at=excluded.updated_at, last_seen=excluded.last_seen',
                (p.space, p.page_id, p.version, p.title, p.path, p.updated_at, _now()),
            )

    def count_pages(self, space: str) -> int:
        with self._lock:
            return self._db.execute(
                'SELECT COUNT(*) c FROM page_states WHERE space=?', (space,)).fetchone()['c']

    # -- app_state ------------------------------------------------------
    def get_state(self, key: str, default: str = '') -> str:
        with self._lock:
            row = self._db.execute('SELECT value FROM app_state WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock, self._db as c:
            c.execute('INSERT INTO app_state(key, value) VALUES(?,?) '
                      'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

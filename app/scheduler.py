"""In-process interval scheduler. Threading only — no cron/APScheduler dependency.

Due rule per space: no running run AND (never synced OR last run finished
before ``interval_min`` ago). Incremental runs reuse ``lastSync/<space>``.
Enabled via ``SCHEDULER_ENABLED``; interval via ``INTERVAL_MIN``.
"""
from __future__ import annotations

import calendar
import threading
import time

_CHECK_EVERY = 30  # scheduler heartbeat, seconds


def _age_sec(ts: str) -> float:
    try:
        return time.time() - calendar.timegm(time.strptime(ts, '%Y-%m-%dT%H:%M:%SZ'))
    except (ValueError, TypeError):
        return float('inf')  # unparseable/missing → due


class SchedulerService:
    def __init__(self, settings, db, sync, pages) -> None:
        self.s = settings
        self.db = db
        self.sync = sync
        self.pages = pages
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.s.scheduler_enabled or self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _spaces(self) -> list[str]:
        if self.s.spaces:
            return list(self.s.spaces)
        return [s['key'] for s in self.pages.list_spaces()]

    def _due(self, space: str) -> bool:
        if self.db.running_run(space):
            return False
        last = self.db.last_run(space)
        if not last:
            return True
        stamp = last.finished_at or last.started_at
        return _age_sec(stamp) > self.s.interval_min * 60

    def tick_once(self) -> list[int]:
        """One pass over spaces. Split out for tests (no sleeping)."""
        ids: list[int] = []
        for space in self._spaces():
            if self._due(space):
                since = self.db.get_state(f'lastSync/{space}')
                ids += self.sync.start_sync([space], since)
        return ids

    def _loop(self) -> None:
        while not self._stop.wait(_CHECK_EVERY):
            try:
                self.tick_once()
            except Exception:
                continue  # scheduler never dies on a bad space; run row holds the error

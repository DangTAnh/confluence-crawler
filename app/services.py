"""Orchestration: ``SyncService`` (background runs) + ``PagesService`` (disk reads).

Sync runs the existing ``crawl_confluence.py`` worker in a daemon thread per
space; progress lines update ``sync_runs`` live so the dashboard polls one row.
# ponytail: overlapping manual+event triggers for the same space record
# 'skipped' instead of queueing (no lock table, running_run() is the guard).
# ponytail: pages listed by walking metadata.json per request — O(n) disk scan,
# fine to ~10k pages; add an FTS/cache table when search feels slow.
"""
from __future__ import annotations

import json
import os
import threading
import time

from .db import Database
from .models import PageState


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def iter_meta(output: str, space: str):
    """Yield parsed metadata.json dicts (collected first: no open walk during rmtree)."""
    found = []
    for dirpath, _, files in os.walk(os.path.join(output, space)):
        if 'metadata.json' in files:
            try:
                with open(os.path.join(dirpath, 'metadata.json'), encoding='utf-8') as f:
                    found.append(json.load(f))
            except (OSError, ValueError):
                continue
    yield from found


class SyncService:
    def __init__(self, settings, db: Database, runner) -> None:
        self.s = settings
        self.db = db
        self.runner = runner

    def start_sync(self, spaces: list[str], since: str = '') -> list[int]:
        """Queue one background run per space. Already-running space → 'skipped' row."""
        ids = []
        for space in spaces:
            if self.db.running_run(space):
                r = self.db.create_run(space, 'since' if since else 'full', since)
                self.db.update_run(r.id, status='skipped', error='another sync is running')
                ids.append(r.id)
                continue
            run = self.db.create_run(space, 'since' if since else 'full', since)
            t = threading.Thread(target=self._run_one, args=(run.id, space, since), daemon=True)
            t.start()
            ids.append(run.id)
        return ids

    def _run_one(self, run_id: int, space: str, since: str) -> None:
        try:
            stats = self.runner.run(
                space, since,
                on_progress=lambda done, total: self.db.update_run(run_id, written=done, fetched=total),
            )
        except RuntimeError as e:  # missing credentials etc. — fail fast, no thread leak
            self.db.update_run(run_id, status='failed', error=str(e)[:2000])
            return
        except Exception as e:  # worker crashed before producing stats
            self.db.update_run(run_id, status='failed', error=f'{type(e).__name__}: {e}'[:2000])
            return
        if stats.error:
            self.db.update_run(run_id, status='failed', fetched=stats.fetched,
                               written=stats.written, error=stats.error)
            return
        n = self.refresh_states(space)
        self.db.update_run(run_id, status='done', fetched=stats.fetched or n, written=n)
        self.db.set_state(f'lastSync/{space}', _now())

    def refresh_states(self, space: str) -> int:
        """Rebuild page_states for a space from metadata.json files on disk."""
        n = 0
        for meta in iter_meta(self.s.output, space):
            v = meta.get('version', {}) or {}
            exp = meta.get('_export', {}) or {}
            self.db.upsert_page(PageState(
                space=space, page_id=str(meta.get('id', '')),
                version=int(v.get('number') or 0), title=str(meta.get('title', '')),
                path=str(exp.get('path', '')), updated_at=str(v.get('when', '')),
            ))
            n += 1
        return n


class PagesService:
    """Read-only view over the export tree."""

    def __init__(self, settings, db: Database) -> None:
        self.s = settings
        self.db = db

    def list_spaces(self) -> list[dict]:
        keys: set[str] = set(self.s.spaces)
        if os.path.isdir(self.s.output):
            keys.update(d for d in os.listdir(self.s.output)
                        if os.path.isdir(os.path.join(self.s.output, d)))
        out = []
        for key in sorted(keys):
            last = self.db.last_run(key)
            out.append({'key': key, 'pages': self.db.count_pages(key),
                        'last_sync': self.db.get_state(f'lastSync/{key}'),
                        'last_status': last.status if last else ''})
        return out

    def list_pages(self, space: str, q: str = '') -> list[dict]:
        q = q.casefold()
        pages = []
        for meta in iter_meta(self.s.output, space):
            title = str(meta.get('title', ''))
            if q and q not in title.casefold():
                continue
            v = meta.get('version', {}) or {}
            pid = str(meta.get('id', ''))
            st = self.db.get_page_state(space, pid)
            iv = (st.ingested_version if st else 0) or 0
            vn = int(v.get('number') or 0)
            index = f'v{iv}/{st.chunk_count} chunks' if st and iv == vn and vn else (
                f'stale (page v{vn}, indexed v{iv})' if st and iv else 'not indexed')
            pages.append({'id': pid, 'title': title,
                          'version': v.get('number'), 'path': (meta.get('_export', {}) or {}).get('path', ''),
                          'updated': v.get('when', ''), 'index': index})
        pages.sort(key=lambda p: p['path'])
        return pages

    def get_page(self, space: str, page_id: str) -> dict | None:
        for meta in iter_meta(self.s.output, space):
            if str(meta.get('id', '')) == page_id:
                exp = meta.get('_export', {}) or {}
                d = os.path.join(self.s.output, space, *str(exp.get('path', '')).split('/'))
                try:
                    with open(os.path.join(d, 'content.md'), encoding='utf-8') as f:
                        body = f.read()
                except OSError:
                    body = ''
                return {'metadata': meta, 'content_md': body}
        return None

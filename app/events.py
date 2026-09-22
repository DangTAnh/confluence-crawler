"""Event intake + single-page worker. No polling: Confluence (Automation rule /
webhook) POSTs here per page change; a bounded thread pool fetches that page.

Flow per event: queue → ``GET /rest/api/content/{id}?expand=...`` → reuse the
worker's id-tree layout (same ``<id>/{content.md,metadata.json}``) → upsert
``page_states`` → ``vector_upsert`` hook (no-op until phase 3 wires a store).
# ponytail: stdlib queue + threads, no task lib. In-flight set dedups repeat
# events for the same page (Automation retries / double rules).
"""
from __future__ import annotations
import base64
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import shutil

# ponytail: single-page fetch imports the storage→md converter from the worker
# script instead of duplicating the parser here; port in-process later as one unit.
import crawl_confluence as worker

from .models import PageState
from .services import iter_meta

_EVENTS = ('created', 'updated', 'removed')


def _auth_header(user: str, secret: str) -> str:
    if user:
        return 'Basic ' + base64.b64encode(f'{user}:{secret}'.encode()).decode()
    return 'Bearer ' + secret


class EventService:
    def __init__(self, settings, db, runner) -> None:
        self.s = settings
        self.db = db
        self.runner = runner  # reserved for attachments phase; fetch uses REST directly
        self._q: queue.Queue = queue.Queue()
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.vector_upsert = lambda space, page_id: None  # phase 3 replaces this hook

    # -- intake (called by the HTTP layer) --------------------------------
    def check_key(self, key: str) -> bool:
        """Shared-secret gate (?key=). Empty EVENT_KEY = open (LAN trust)."""
        return not self.s.event_key or key == self.s.event_key

    def submit(self, space: str, page_id: str, event: str) -> dict:
        if event not in _EVENTS:
            raise ValueError(f'event must be one of {_EVENTS}')
        if not page_id:
            raise ValueError('page_id required')
        run = self.db.create_run(space, 'event', event)
        key = f'{space}/{page_id}'
        with self._lock:
            if key in self._inflight:
                self.db.update_run(run.id, status='skipped', error='duplicate in-flight event')
                return {'run_id': run.id, 'deduped': True}
            self._inflight.add(key)
        self._q.put((run.id, space, page_id, event))
        return {'run_id': run.id, 'deduped': False}

    def depth(self) -> int:
        return self._q.qsize()

    # -- worker pool -------------------------------------------------------
    def start(self) -> None:
        for _ in range(max(1, self.s.event_workers)):
            t = threading.Thread(target=self._loop, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle(*item)
            finally:
                with self._lock:
                    self._inflight.discard(f'{item[1]}/{item[2]}')
                self._q.task_done()

    # -- one page -----------------------------------------------------------
    def _fetch(self, page_id: str) -> dict | None:
        self.s.require_confluence()  # raises when creds missing — stubbed in tests
        url = (f'{self.s.base_url}/rest/api/content/{page_id}'
               f'?expand={worker.EXPAND}&status=current')
        req = urllib.request.Request(
            url, headers={'Authorization': _auth_header(self.s.user, self.s.token),
                          'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=self.s.timeout) as h:
                if h.status == 404:
                    return None
                return json.load(h)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def _handle(self, run_id: int, space: str, page_id: str, event: str) -> None:
        try:
            page = self._fetch(page_id)
        except Exception as e:
            self.db.update_run(run_id, status='failed', error=f'{type(e).__name__}: {e}'[:2000])
            return
        if page is None or event == 'removed' or page.get('status') in ('trashed', 'archived'):
            self._remove(space, page_id)
            self.db.update_run(run_id, status='done', fetched=0, written=0)
            return
        self._write(space, page)
        self.db.update_run(run_id, status='done', fetched=1, written=1)
        try:
            self.vector_upsert(space, page_id)
        except Exception as e:  # vector failure must not mask a good crawl
            self.db.update_run(run_id, status='done', fetched=1, written=1,
                               error=f'vector: {e}'[:2000])

    def _write(self, space: str, page: dict) -> None:
        page_id = str(page['id'])
        anc = sorted(page.get('ancestors', []), key=lambda x: int(x['id']))
        ids = [a['id'] for a in anc] + [page_id]  # id-only path, same as full crawl
        d = os.path.join(self.s.output, space, *ids)
        os.makedirs(worker.fs(d), exist_ok=True)
        meta = dict(page)
        meta['_export'] = {'space': space, 'url': f'{self.s.base_url}/pages/{page_id}',
                           'path': '/'.join(ids)}
        meta['_export']['crawled_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        with open(worker.fs(os.path.join(d, 'metadata.json')), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        body = worker.storage_to_md((page.get('body') or {}).get('storage', {}).get('value', ''))
        with open(worker.fs(os.path.join(d, 'content.md')), 'w', encoding='utf-8') as f:
            f.write(f"---\nid: {page_id}\ntitle: {json.dumps(page.get('title'), ensure_ascii=False)}\n"
                    f"url: {meta['_export']['url']}\n---\n\n# {page.get('title')}\n\n{body}")
        v = page.get('version', {}) or {}
        self.db.upsert_page(PageState(space=space, page_id=page_id,
                                      version=int(v.get('number') or 0),
                                      title=str(page.get('title', '')),
                                      path='/'.join(ids), updated_at=str(v.get('when', ''))))
        self.db.set_state(f'lastSync/{space}', meta['_export']['crawled_at'])
    def _remove(self, space: str, page_id: str) -> None:
        for meta in iter_meta(self.s.output, space):
            if str(meta.get('id', '')) == page_id:
                exp = meta.get('_export', {}) or {}
                d = os.path.join(self.s.output, space, *str(exp.get('path', '')).split('/'))
                shutil.rmtree(d, ignore_errors=True)  # no \\\\?\\ prefix: rmtree+prefix silently no-ops
                break
        # page files are gone → ingest_page() reports missing, so purge directly
        from .ingest import IngestService
        try:
            (getattr(self, '_ingest', None) or IngestService(self.s, self.db)).purge_page(space, page_id)
        except Exception:  # removal must succeed even if the vector file is locked
            pass

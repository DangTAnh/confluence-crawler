"""Entry point. Run: ``python -m app.main [--host H] [--port P]`` (cwd = repo root).

Wiring: Settings → Database → CrawlRunner → SyncService + PagesService →
IngestService → EventService --vector_upsert--> IngestService → http.server.
Event intake (Confluence Automation rule / webhook POSTs /v1/events) replaces
the old interval scheduler: syncs fire on change, plus manual POST /v1/sync.
"""
from __future__ import annotations

import argparse
import dataclasses

from .config import Settings
from .crawl_runner import CrawlRunner
from .db import Database
from .events import EventService
from .ingest import IngestService
from .server import _Ctx, serve
from .services import PagesService, SyncService


def build(host: str = '', port: int = 0):
    s = Settings.from_env()
    if host:
        s = dataclasses.replace(s, host=host)
    if port:
        s = dataclasses.replace(s, port=port)
    db = Database(s.db_path)
    runner = CrawlRunner(s)
    sync = SyncService(s, db, runner)
    pages = PagesService(s, db)
    ingest = IngestService(s, db)
    events = EventService(s, db, runner)
    # close the loop: single-page event writes auto-index into the vector store
    events.vector_upsert = lambda space, page_id: ingest.ingest_page(space, page_id)
    events.start()
    return s, serve(_Ctx(s, db, sync, pages, events, ingest)), events, db


def main() -> None:
    ap = argparse.ArgumentParser(description='Confluence sync service (dashboard + APIs)')
    ap.add_argument('--host', default='')
    ap.add_argument('--port', type=int, default=0)
    a = ap.parse_args()
    s, httpd, events, db = build(a.host, a.port)
    print(f'serving http://{s.host}:{s.port}  event_workers={s.event_workers}', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        events.stop()
        db.close()
        httpd.server_close()


if __name__ == '__main__':
    main()

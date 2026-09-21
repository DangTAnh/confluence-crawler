"""Entry point. Run: ``python -m app.main [--host H] [--port P]`` (cwd = repo root).

Wiring: Settings → Database → CrawlRunner → SyncService + PagesService →
SchedulerService → http.server. Ctrl+C stops scheduler, then the server.
"""
from __future__ import annotations

import argparse
import dataclasses

from .config import Settings
from .crawl_runner import CrawlRunner
from .db import Database
from .scheduler import SchedulerService
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
    scheduler = SchedulerService(s, db, sync, pages)
    return s, serve(_Ctx(s, db, sync, pages, scheduler)), scheduler, db


def main() -> None:
    ap = argparse.ArgumentParser(description='Confluence sync service (dashboard + APIs)')
    ap.add_argument('--host', default='')
    ap.add_argument('--port', type=int, default=0)
    a = ap.parse_args()
    s, httpd, scheduler, db = build(a.host, a.port)
    scheduler.start()
    print(f'serving http://{s.host}:{s.port}  scheduler={"on" if s.scheduler_enabled else "off"}',
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        db.close()
        httpd.server_close()


if __name__ == '__main__':
    main()

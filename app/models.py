"""Tracking records. Plain dataclasses; persistence lives in ``db.Database``."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SyncRun:
    id: int = 0
    space: str = ''
    mode: str = 'full'  # full | since | event
    status: str = 'queued'  # queued | running | done | failed | skipped
    fetched: int = 0
    written: int = 0
    skipped: int = 0
    error: str = ''
    since: str = ''
    started_at: str = ''
    finished_at: str = ''


@dataclass
class PageState:
    space: str = ''
    page_id: str = ''
    version: int = 0
    title: str = ''
    path: str = ''
    updated_at: str = ''
    last_seen: str = ''
    ingested_version: int = 0
    chunk_count: int = 0
    ingested_at: str = ''

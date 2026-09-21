"""Crawl worker wrapper. Reuses ``crawl_confluence.py`` as a subprocess (stdlib only).

Contract with callers (``services.SyncService``): :meth:`run` blocks until the
worker exits, streams ``(done, total)`` progress, and returns :class:`RunStats`.
Later: port the fetch loop in-process and swap these internals — callers stay put.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

_FETCHED_RE = re.compile(r'^\[(?P<space>.+?)\]\s+fetched\s+(?P<n>\d+)\s+pages')
_PROGRESS_RE = re.compile(r'^\[(?P<space>.+?)\]\s+(?P<done>\d+)/(?P<total>\d+)\s*$')


def parse_progress(line: str) -> tuple[str, int, int] | None:
    """``'fetched'`` (list phase) or ``'written'`` (write phase) + counts; else None."""
    m = _PROGRESS_RE.match(line.strip())
    if m:
        return ('written', int(m['done']), int(m['total']))
    m = _FETCHED_RE.match(line.strip())
    if m:
        return ('fetched', 0, int(m['n']))
    return None


@dataclass
class RunStats:
    fetched: int = 0
    written: int = 0
    error: str = ''


class CrawlRunner:
    def __init__(self, settings) -> None:
        self.s = settings

    def build_command(self, space: str, since: str = '', resume: bool = True) -> list[str]:
        cmd = [sys.executable, os.path.abspath(self.s.script),
               '--space', space, '-o', self.s.output, '--timeout', str(self.s.timeout)]
        if since:
            cmd += ['--since', since]
        if resume:
            cmd += ['--resume']
        if self.s.attachments:
            cmd += ['--attachments']
        return cmd

    def child_env(self) -> dict:
        env = dict(os.environ)
        env['CONFLUENCE_BASE_URL'] = self.s.base_url
        if self.s.user:
            env['CONFLUENCE_EMAIL'] = self.s.user
        env['CONFLUENCE_API_TOKEN'] = self.s.token
        return env

    def run(self, space: str, since: str = '', resume: bool = True,
            on_progress: Callable[[int, int], None] | None = None) -> RunStats:
        """Spawn worker, forward progress. Raises RuntimeError when credentials missing."""
        self.s.require_confluence()
        stats = RunStats()
        proc = subprocess.Popen(self.build_command(space, since, resume),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=self.child_env())
        tail: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            tail.append(line.rstrip('\n'))
            del tail[:-20]
            parsed = parse_progress(line)
            if parsed:
                kind, done, total = parsed
                if kind == 'fetched':
                    stats.fetched = total
                else:
                    stats.written = done
                    stats.fetched = max(stats.fetched, total)
                if on_progress:
                    on_progress(stats.written, stats.fetched)
        if proc.wait() != 0:
            stats.error = '\n'.join(tail)[-2000:] or f'worker exit {proc.returncode}'
        return stats

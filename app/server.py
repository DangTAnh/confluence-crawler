"""HTTP surface: JSON APIs (``/v1/*``) + SSR dashboard (``/``). Stdlib only.

APIs:
  GET  /healthz | GET /v1/status
  POST /v1/sync {spaces[], since?} -> {run_ids}              (manual/backfill)
  POST /v1/events?key= {space, page_id, event} -> {run_id}   (Confluence Automation/webhook)
  GET  /v1/sync/runs[/{id}] | GET /v1/spaces
  GET  /v1/pages?space=&q= | GET /v1/pages/{space}/{id}
  POST /v1/retrieve {query, top_k, space?} -> chunks+citations (RAG reads this)
Dashboard: / (spaces + trigger), /runs, /runs/{id} (auto-refresh when active),
  /spaces/{key}/pages (search + index status).
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_CSS = ('body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}'
        'table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.4rem .6rem;text-align:left}'
        '.ok{color:green}.bad{color:red}.mut{color:#666}nav a{margin-right:1rem}')


def _esc(v) -> str:
    return html.escape('' if v is None else str(v))


def _layout(title: str, body: str) -> bytes:
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{_esc(title)}</title>'
            f'<style>{_CSS}</style></head><body><nav><a href="/">spaces</a>'
            f'<a href="/runs">runs</a><a href="/v1/status">status json</a></nav>'
            f'<h1>{_esc(title)}</h1>{body}</body></html>').encode()


class _Ctx:
    """Shared handles injected into every request handler."""

    def __init__(self, settings, db, sync, pages, events, ingest) -> None:
        self.s = settings
        self.db = db
        self.sync = sync
        self.pages = pages
        self.events = events
        self.ingest = ingest


def _dashboard_index(ctx: _Ctx) -> bytes:
    rows = ''.join(
        f'<tr><td><a href="/spaces/{_esc(s["key"])}/pages">{_esc(s["key"])}</a></td>'
        f'<td>{s["pages"]}</td><td class="mut">{_esc(s["last_sync"])}</td>'
        f'<td>{_esc(s["last_status"])}</td>'
        f'<td><button onclick="syncSpace(\'{_esc(s["key"])}\')">sync</button></td></tr>'
        for s in ctx.pages.list_spaces())
    js = ('<script>async function syncSpace(k){await fetch("/v1/sync",{method:"POST",'
          'headers:{"Content-Type":"application/json"},body:JSON.stringify({spaces:[k]})});'
          'location.href="/runs"}</script>')
    return _layout('spaces', '<table><tr><th>space</th><th>pages</th><th>last sync</th>'
                   '<th>last status</th><th></th></tr>' + rows + '</table>' + js)


def _dashboard_runs(ctx: _Ctx, space: str = '') -> bytes:
    runs = ctx.db.list_runs(space)
    if not runs:
        return _layout('sync runs', '<p>no runs yet — POST /v1/sync to start one</p>')
    rows = ''.join(
        f'<tr><td><a href="/runs/{r.id}">{r.id}</a></td><td>{_esc(r.space)}</td>'
        f'<td>{_esc(r.mode)}</td><td>{_esc(r.status)}</td>'
        f'<td>{r.written}/{r.fetched}</td><td class="mut">{_esc(r.started_at)}</td></tr>'
        for r in runs)
    return _layout('sync runs', '<table><tr><th>id</th><th>space</th><th>mode</th><th>status</th>'
                   '<th>written/fetched</th><th>started</th></tr>' + rows + '</table>')


def _dashboard_run(ctx: _Ctx, run_id: int) -> bytes:
    r = ctx.db.get_run(run_id)
    if not r:
        return _layout('not found', '<p class="bad">run not found</p>')
    refresh = '<meta http-equiv="refresh" content="2">' if r.status in ('queued', 'running') else ''
    extra = f'<p>event {_esc(r.since)}</p>' if r.mode == 'event' else ''
    body = (f'<p>space <b>{_esc(r.space)}</b> mode {_esc(r.mode)} status <b>{_esc(r.status)}</b></p>'
            + extra +
            f'<p>fetched {r.fetched} written {r.written} skipped {r.skipped}</p>'
            + (f'<pre class="bad">{_esc(r.error)}</pre>' if r.error else ''))
    page = _layout(f'run {r.id}', body)
    return page.replace(b'</title>', f'</title>{refresh}'.encode()) if refresh else page


def _dashboard_pages(ctx: _Ctx, space: str, q: str) -> bytes:
    items = ctx.pages.list_pages(space, q)
    rows = ''.join(
        f'<tr><td>{_esc(p["id"])}</td><td>{_esc(p["title"])}</td>'
        f'<td>{p["version"]}</td><td>{_esc(p["index"])}</td>'
        f'<td class="mut">{_esc(p["updated"])}</td></tr>'
        for p in items[:500])
    form = (f'<form method="get"><input name="q" value="{_esc(q)}" placeholder="search title">'
            f'<button>search</button></form>')
    note = '' if len(items) <= 500 else f'<p class="mut">showing 500/{len(items)}</p>'
    return _layout(f'{space} pages ({len(items)})', f'{form}{note}<table><tr><th>id</th>'
                   f'<th>title</th><th>v</th><th>index</th><th>updated</th></tr>{rows}</table>')


class _Handler(BaseHTTPRequestHandler):
    ctx: _Ctx = None  # type: ignore  # injected by serve()
    server_version = 'ConfluenceSync/1'

    # -- helpers ------------------------------------------------------
    def _json(self, obj, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, data: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            return {}

    def log_message(self, *args) -> None:  # keep stdout for worker progress, not hits
        pass

    # -- routes -------------------------------------------------------
    def do_GET(self) -> None:
        ctx, q = self.ctx, parse_qs(urlparse(self.path).query)
        path = urlparse(self.path).path
        if path == '/healthz':
            return self._json({'ok': True})
        if path == '/v1/status':
            return self._json({
                'events': {'workers': ctx.s.event_workers, 'queued': ctx.events.depth()},
                'spaces': ctx.pages.list_spaces()})
        if path == '/v1/sync/runs':
            return self._json([asdict(r) for r in ctx.db.list_runs(q.get('space', [''])[0],
                                                                   int(q.get('limit', ['50'])[0]))])
        if m := re.fullmatch(r'/v1/sync/runs/(\d+)', path):
            r = ctx.db.get_run(int(m[1]))
            return self._json(asdict(r)) if r else self._json({'error': 'not found'}, 404)
        if path == '/v1/spaces':
            return self._json(ctx.pages.list_spaces())
        if path == '/v1/pages':
            if not q.get('space', [''])[0]:
                return self._json({'error': 'space required'}, 400)
            return self._json(ctx.pages.list_pages(q['space'][0], q.get('q', [''])[0]))
        if m := re.fullmatch(r'/v1/pages/([^/]+)/(\d+)', path):
            p = ctx.pages.get_page(m[1], m[2])
            return self._json(p) if p else self._json({'error': 'not found'}, 404)
        if path == '/':
            return self._html(_dashboard_index(ctx))
        if path == '/runs':
            return self._html(_dashboard_runs(ctx))
        if m := re.fullmatch(r'/runs/(\d+)', path):
            return self._html(_dashboard_run(ctx, int(m[1])))
        if m := re.fullmatch(r'/spaces/([^/]+)/pages', path):
            return self._html(_dashboard_pages(ctx, m[1], q.get('q', [''])[0]))
        return self._json({'error': 'not found'}, 404)

    def do_POST(self) -> None:
        ctx, q = self.ctx, parse_qs(urlparse(self.path).query)
        path = urlparse(self.path).path
        if path == '/v1/sync':
            body = self._body()
            spaces = body.get('spaces') or [s['key'] for s in ctx.pages.list_spaces()] \
                or list(ctx.s.spaces)
            since = str(body.get('since') or '')
            ids = ctx.sync.start_sync([str(s) for s in spaces], since)
            return self._json({'run_ids': ids}, 202)
        if path == '/v1/events':
            if not ctx.events.check_key(q.get('key', [''])[0]):
                return self._json({'error': 'bad key'}, 403)
            body = self._body()
            try:
                res = ctx.events.submit(str(body.get('space', '')), str(body.get('page_id', '')),
                                        str(body.get('event', 'updated')))
            except ValueError as e:
                return self._json({'error': str(e)}, 400)
            return self._json(res, 202)
        if path == '/v1/retrieve':
            body = self._body()
            if not str(body.get('query', '')).strip():
                return self._json({'error': 'query required'}, 400)
            try:
                top_k = min(int(body.get('top_k', 8)), 20)
            except (ValueError, TypeError):
                return self._json({'error': 'top_k must be int'}, 400)
            filt = body.get('filter') or {}
            try:
                hits = ctx.ingest.retrieve(str(body['query']), top_k,
                                           space=str(filt.get('space') or ''),
                                           page_ids=[str(p) for p in filt.get('page_ids') or []])
            except RuntimeError as e:  # misconfigured embedder/store
                return self._json({'error': str(e)}, 503)
            return self._json(hits)
        return self._json({'error': 'not found'}, 404)


def serve(ctx: _Ctx) -> ThreadingHTTPServer:
    """Build (not start) the server. Caller owns serve_forever/shutdown."""
    _Handler.ctx = ctx
    return ThreadingHTTPServer((ctx.s.host, ctx.s.port), _Handler)

"""Index pipeline: heading chunker + JSONL file vector store + ingest service.

Zero third-party deps (company machine constraint):
  - chunking: stdlib ``re`` on markdown headings (``#/##``), char budget with
    word overlap. No tiktoken — ``len//4`` token estimate is enough for sizing.
  - embeddings: OpenAI-compatible ``/embeddings`` over ``urllib`` when
    ``EMBED_*`` env is set; else ``HashEmbedder`` (char-trigram hashing,
    L2-normalized) so the whole loop works offline. Swap the embedder, not callers.
  - store: one JSONL file per space under ``data/vectors/<SPACE>.jsonl``
    (``{"id", "vector", "payload"}`` per line). Linear scan + heapq top-k:
    fine to ~100k chunks; migrate to Qdrant/Chroma when measured slow.
Point id ``{page_id}#v{version}#{md5(chunk)}`` — deterministic, re-runs are clean.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import re
import urllib.request
from dataclasses import dataclass

_CHUNK_CHARS = 2000  # ~500 tokens
_OVERLAP_WORDS = 50
_MIN_CHUNK = 50
_HEADING_RE = re.compile(r'^(#{1,3})\s+(.*)$')


@dataclass
class Chunk:
    text: str
    headings: tuple[str, ...]  # breadcrumb, e.g. ("Onboarding", "VPN")
    idx: int


def chunk_markdown(md: str, max_chars: int = _CHUNK_CHARS,
                   overlap: int = _OVERLAP_WORDS) -> list[Chunk]:
    """Split on h1-h3. Oversized sections cut on word boundary with overlap."""
    sections: list[tuple[tuple[str, ...], list[str]]] = [((), [])]
    stack: list[str] = []
    for line in (md or '').splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            level, title = len(m[1]), m[2].strip()
            stack = stack[:level - 1] + [title]
            sections.append((tuple(stack), []))
        else:
            sections[-1][1].append(line)
    out: list[Chunk] = []
    for headings, lines in sections:
        text = '\n'.join(lines).strip()
        if len(text) < _MIN_CHUNK:  # bare heading / stub — not retrievable
            continue
        words = text.split()
        start, idx = 0, len(out)
        while start < len(words):
            piece = ' '.join(words[start:start + max_chars])
            if len(piece) < _MIN_CHUNK and out:
                break  # trailing sliver merges into previous chunk implicitly
            out.append(Chunk(text=piece, headings=headings, idx=idx))
            idx += 1
            if start + max_chars >= len(words):
                break
            start += max_chars - overlap
    return out


class HashEmbedder:
    """Offline baseline: char-trigram hash → L2 unit vector. Deterministic, free."""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = []
        for t in texts:
            v = [0.0] * self.dim
            s = t.lower()
            for i in range(len(s) - 2):
                v[int(hashlib.md5(s[i:i + 3].encode()).hexdigest(), 16) % self.dim] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            vecs.append([x / n for x in v])
        return vecs


class OpenAIEmbedder:
    """OpenAI-compatible /embeddings (works with any compatible gateway)."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 60) -> None:
        if not api_key or not model:
            raise RuntimeError('EMBED_API_KEY / EMBED_MODEL required for OpenAIEmbedder')
        self.url = base_url.rstrip('/') + '/embeddings'
        self.key = api_key
        self.model = model
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 100):  # batch cap per request
            req = urllib.request.Request(
                self.url,
                data=json.dumps({'model': self.model, 'input': texts[i:i + 100]}).encode(),
                headers={'Authorization': f'Bearer {self.key}',
                         'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=self.timeout) as h:
                out.extend(e['embedding'] for e in json.load(h)['data'])
        return out


def make_embedder(settings):
    if os.environ.get('EMBED_API_KEY') and os.environ.get('EMBED_MODEL'):
        return OpenAIEmbedder(os.environ.get('EMBED_BASE_URL', 'https://api.openai.com/v1'),
                              os.environ['EMBED_API_KEY'], os.environ['EMBED_MODEL'])
    return HashEmbedder(dim=int(os.environ.get('EMBED_DIM', '512')))


class FileVectorStore:
    """JSONL-per-space store. Upsert = rewrite without page_id + append (crash-safe via rename)."""

    def __init__(self, root: str = 'data/vectors') -> None:
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, space: str) -> str:
        safe = re.sub(r'[^A-Za-z0-9_-]', '_', space)
        return os.path.join(self.root, f'{safe}.jsonl')

    def upsert_page(self, space: str, page_id: str, version: int,
                    chunks: list[Chunk], vectors: list[list[float]], meta: dict) -> int:
        prefix = f'{page_id}#'
        kept = []
        path = self._path(space)
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                kept = [ln for ln in f if json.loads(ln)['id'].split('#v')[0] + '#' != prefix]
        with open(path + '.tmp', 'w', encoding='utf-8') as f:
            f.writelines(kept)
            for ch, vec in zip(chunks, vectors):
                cid = f"{page_id}#v{version}#{hashlib.md5(ch.text.encode()).hexdigest()[:12]}"
                f.write(json.dumps({
                    'id': cid,
                    'vector': vec,
                    'payload': {**meta, 'page_id': page_id, 'version': version,
                                'chunk_idx': ch.idx, 'headings': list(ch.headings),
                                'text': ch.text}}, ensure_ascii=False) + '\n')
        os.replace(path + '.tmp', path)  # atomic: readers never see half a page
        return len(chunks)

    def delete_page(self, space: str, page_id: str) -> None:
        prefix = f'{page_id}#'
        path = self._path(space)
        if not os.path.exists(path):
            return
        with open(path, encoding='utf-8') as f:
            kept = [ln for ln in f if json.loads(ln)['id'].split('#v')[0] + '#' != prefix]
        with open(path + '.tmp', 'w', encoding='utf-8') as f:
            f.writelines(kept)
        os.replace(path + '.tmp', path)

    def search(self, space: str, query_vec: list[float], top_k: int = 8,
               page_ids: list[str] | None = None) -> list[dict]:
        path = self._path(space)
        if not os.path.exists(path):
            return []
        want = set(page_ids) if page_ids else None
        qn = math.sqrt(sum(x * x for x in query_vec)) or 1.0
        heap: list[tuple[float, dict]] = []
        with open(path, encoding='utf-8') as f:
            for ln in f:
                row = json.loads(ln)
                if want and row['payload']['page_id'] not in want:
                    continue
                v = row['vector']
                s = sum(a * b for a, b in zip(query_vec, v)) / (
                    qn * (math.sqrt(sum(x * x for x in v)) or 1.0))
                entry = {'score': s, **row['payload']}
                if len(heap) < top_k:
                    heapq.heappush(heap, (s, entry))
                elif s > heap[0][0]:
                    heapq.heapreplace(heap, (s, entry))
        return [e for _, e in sorted(heap, reverse=True)]


class IngestService:
    """Chunk → embed → store one page. Version-gated: same version = skip."""

    def __init__(self, settings, db, embedder=None, store=None) -> None:
        self.s = settings
        self.db = db
        self.embedder = embedder or make_embedder(settings)
        self.store = store or FileVectorStore()

    def ingest_page(self, space: str, page_id: str) -> dict:
        from .services import iter_meta
        metas = [m for m in iter_meta(self.s.output, space) if str(m.get('id')) == page_id]
        if not metas:
            return {'status': 'missing'}
        meta = metas[0]
        version = int((meta.get('version') or {}).get('number') or 0)
        st = self.db.get_page_state(space, page_id)
        if st and st.ingested_version == version and version:
            return {'status': 'skipped', 'version': version}
        exp = meta.get('_export', {}) or {}
        d = os.path.join(self.s.output, space, *str(exp.get('path', '')).split('/'))
        try:
            with open(os.path.join(d, 'content.md'), encoding='utf-8') as f:
                md = f.read()
        except OSError:
            return {'status': 'missing'}
        chunks = chunk_markdown(md)
        if not chunks:
            self.db.mark_ingested(space, page_id, version, 0)
            return {'status': 'empty', 'version': version}
        vecs = self.embedder.embed([c.text for c in chunks])
        n = self.store.upsert_page(
            space, page_id, version, chunks, vecs,
            {'title': meta.get('title', ''), 'url': exp.get('url', ''),
             'space': space, 'path': exp.get('path', '')})
        self.db.mark_ingested(space, page_id, version, n)
        return {'status': 'indexed', 'version': version, 'chunks': n}

    def purge_page(self, space: str, page_id: str) -> None:
        self.store.delete_page(space, page_id)

    def retrieve(self, query: str, top_k: int = 8, space: str = '',
                 page_ids: list[str] | None = None) -> list[dict]:
        if not query.strip():
            return []
        spaces = [space] if space else self._known_spaces()
        if space and page_ids:
            pass  # both filters combine (store ANDs them)
        qv = self.embedder.embed([query])[0]
        hits: list[dict] = []
        for sp in spaces:
            for h in self.store.search(sp, qv, top_k, page_ids):
                h['headings_path'] = ' > '.join(h.get('headings') or [])
                hits.append(h)
        hits.sort(key=lambda h: h['score'], reverse=True)
        return hits[:top_k]

    def _known_spaces(self) -> list[str]:
        if not os.path.isdir(self.store.root):
            return []
        return [fn[:-6] for fn in os.listdir(self.store.root) if fn.endswith('.jsonl')]

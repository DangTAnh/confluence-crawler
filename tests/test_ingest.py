"""Ingest contract: chunker splits on headings; version-gated file store; purge."""
import json
import os
import tempfile
import unittest

from app.db import Database
from app.ingest import FileVectorStore, HashEmbedder, IngestService, chunk_markdown
from app.services import iter_meta


class TestChunker(unittest.TestCase):
    def test_splits_on_headings(self):
        md = '# A\n\n' + ('word ' * 600) + '\n\n## B\n\nshort but long enough text ' * 3
        chunks = chunk_markdown(md, max_chars=500)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(chunks[0].headings, ('A',))
        self.assertTrue(all(len(c.text) >= 50 for c in chunks))

    def test_skips_stubs(self):
        self.assertEqual(chunk_markdown('# Just a title\n'), [])


class TestIngestService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        out = os.path.join(self.tmp.name, 'out')
        d = os.path.join(out, 'DKB', '1')
        os.makedirs(d)
        with open(os.path.join(d, 'metadata.json'), 'w', encoding='utf-8') as f:
            json.dump({'id': '1', 'title': 'Doc', 'version': {'number': 2, 'when': '2026-01-01'},
                       '_export': {'path': '1', 'url': 'http://x/pages/1'}}, f)
        with open(os.path.join(d, 'content.md'), 'w', encoding='utf-8') as f:
            f.write('# Doc\n\n' + 'retrieval content about vpn setup. ' * 40)
        from types import SimpleNamespace
        self.s = SimpleNamespace(output=out)
        self.db = Database(os.path.join(self.tmp.name, 't.db'))
        from app.models import PageState
        self.db.upsert_page(PageState(space='DKB', page_id='1', version=2, title='Doc', path='1'))
        self.svc = IngestService(self.s, self.db, embedder=HashEmbedder(dim=64),
                                 store=FileVectorStore(os.path.join(self.tmp.name, 'vec')))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_ingest_then_skip_same_version(self):
        r1 = self.svc.ingest_page('DKB', '1')
        self.assertEqual(r1['status'], 'indexed')
        self.assertGreater(r1['chunks'], 0)
        r2 = self.svc.ingest_page('DKB', '1')
        self.assertEqual(r2['status'], 'skipped')
        st = self.db.get_page_state('DKB', '1')
        self.assertEqual((st.ingested_version, st.chunk_count), (2, r1['chunks']))

    def test_retrieve_finds_page(self):
        self.svc.ingest_page('DKB', '1')
        hits = self.svc.retrieve('vpn setup', top_k=3, space='DKB')
        self.assertTrue(hits)
        self.assertEqual(hits[0]['page_id'], '1')
        self.assertIn('url', hits[0])

    def test_purge_removes_hits(self):
        self.svc.ingest_page('DKB', '1')
        self.svc.purge_page('DKB', '1')
        hits = self.svc.retrieve('vpn setup', top_k=3, space='DKB')
        self.assertEqual(hits, [])

    def test_missing_page(self):
        self.assertEqual(self.svc.ingest_page('DKB', '999')['status'], 'missing')


if __name__ == '__main__':
    unittest.main()

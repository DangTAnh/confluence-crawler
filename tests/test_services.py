"""Pages + events contracts (offline: stub sync/fetch, tmp export tree)."""
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from app.db import Database
from app.events import EventService
from app.services import PagesService, iter_meta


def _settings(output, spaces=()):
    return SimpleNamespace(output=output, spaces=spaces, base_url='http://x',
                           timeout=5, event_key='', event_workers=1,
                           user='', token='t')


def _meta(path, pid, title='T', n=1, exp_path=None):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, 'metadata.json'), 'w', encoding='utf-8') as f:
        json.dump({'id': pid, 'title': title, 'version': {'number': n, 'when': '2026-01-01'},
                   '_export': {'path': exp_path or pid}}, f)
    with open(os.path.join(path, 'content.md'), 'w', encoding='utf-8') as f:
        f.write('# ' + title)


class TestPages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _meta(os.path.join(self.tmp.name, 'DKB', '1', '2'), '2', 'Hello World', exp_path='1/2')
        _meta(os.path.join(self.tmp.name, 'DKB', '3'), '3', 'Other')
        self.db = Database(os.path.join(self.tmp.name, 't.db'))
        self.pages = PagesService(_settings(self.tmp.name), self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_iter_meta_skips_missing(self):
        self.assertEqual(len(list(iter_meta(self.tmp.name, 'NOSUCH'))), 0)

    def test_list_and_search(self):
        allp = self.pages.list_pages('DKB')
        self.assertEqual(len(allp), 2)
        self.assertEqual(self.pages.list_pages('DKB', 'hello')[0]['id'], '2')

    def test_get_page(self):
        p = self.pages.get_page('DKB', '2')
        self.assertIn('Hello World', p['content_md'])
        self.assertIsNone(self.pages.get_page('DKB', '9'))


class TestEvents(unittest.TestCase):
    def _svc(self, fetch='page', **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = _settings(os.path.join(tmp.name, 'out'))
        for k, v in kw.items():  # event_key etc. land on the settings namespace
            setattr(s, k, v)
        db = Database(':memory:')
        self.addCleanup(db.close)
        svc = EventService(s, db, runner=None)
        if fetch == 'page':  # stub skips require_confluence (no creds in tests)
            svc._fetch = lambda pid: {'id': pid, 'title': 'P', 'status': 'current',
                                      'ancestors': [],
                                      'version': {'number': 3, 'when': '2026-01-02'},
                                      'body': {'storage': {'value': '<p>hi</p>'}}}
        elif fetch == 'gone':
            svc._fetch = lambda pid: None
        return svc, db

    def test_submit_bad_event(self):
        svc, _ = self._svc()
        with self.assertRaises(ValueError):
            svc.submit('DKB', '1', 'nope')

    def test_submit_key_gate(self):
        svc, _ = self._svc(event_key='s3cr3t')
        self.assertFalse(svc.check_key('wrong'))
        self.assertTrue(svc.check_key('s3cr3t'))

    def test_dedup_inflight(self):
        svc, _ = self._svc()
        a = svc.submit('DKB', '7', 'updated')
        b = svc.submit('DKB', '7', 'updated')  # still queued → deduped
        self.assertFalse(a['deduped'])
        self.assertTrue(b['deduped'])

    def test_handle_writes_tree_and_state(self):
        svc, db = self._svc()
        run = db.create_run('DKB', 'event', 'updated')
        svc._handle(run.id, 'DKB', '7', 'updated')  # direct call, no thread timing
        d = os.path.join(svc.s.output, 'DKB', '7')
        with open(os.path.join(d, 'content.md'), encoding='utf-8') as f:
            self.assertIn('# P', f.read())
        with open(os.path.join(d, 'metadata.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['version']['number'], 3)
        self.assertEqual(db.count_pages('DKB'), 1)

    def test_handle_removed_purges(self):
        svc, db = self._svc(fetch='gone')  # Gone from Confluence → 404-style None
        _meta(os.path.join(svc.s.output, 'DKB', '9'), '9', 'Gone', exp_path='9')
        run = db.create_run('DKB', 'event', 'removed')
        svc._handle(run.id, 'DKB', '9', 'removed')
        self.assertFalse(os.path.exists(os.path.join(svc.s.output, 'DKB', '9')))
        self.assertEqual(db.get_run(run.id).status, 'done')


if __name__ == '__main__':
    unittest.main()

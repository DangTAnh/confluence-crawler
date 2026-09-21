"""Scheduler + pages contracts (offline: stub sync, tmp export tree)."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from app.db import Database
from app.scheduler import SchedulerService
from app.services import PagesService, iter_meta


def _settings(output, spaces=()):
    return SimpleNamespace(output=output, spaces=spaces)


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


class StubSync:
    def __init__(self):
        self.calls = []

    def start_sync(self, spaces, since=''):
        self.calls.append((spaces, since))
        return [1]


class TestScheduler(unittest.TestCase):
    def test_tick_triggers_due_space(self):
        db = Database(':memory:')
        sched = SchedulerService(SimpleNamespace(spaces=('DKB',), interval_min=15,
                                                 scheduler_enabled=True),
                                 db, StubSync(), None)
        ids = sched.tick_once()  # never synced → due
        self.assertEqual(ids, [1])
        db.close()

    def test_tick_skips_fresh_space(self):
        db = Database(':memory:')
        r = db.create_run('DKB', 'full')
        db.update_run(r.id, status='done', finished_at='2099-01-01T00:00:00Z')
        stub = StubSync()
        sched = SchedulerService(SimpleNamespace(spaces=('DKB',), interval_min=15,
                                                 scheduler_enabled=True),
                                 db, stub, None)
        self.assertEqual(sched.tick_once(), [])
        self.assertEqual(stub.calls, [])
        db.close()


if __name__ == '__main__':
    unittest.main()

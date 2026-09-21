"""DB contract: run lifecycle, kv state, deadlock regression (create_run→get_run)."""
import os
import tempfile
import unittest

from app.db import Database


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, 't.db'))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_run_lifecycle_no_deadlock(self):
        r = self.db.create_run('DKB', 'full')  # used to deadlock on Lock (now RLock)
        self.assertEqual(r.status, 'running')
        self.db.update_run(r.id, fetched=5, written=4)
        self.db.update_run(r.id, status='done')
        got = self.db.get_run(r.id)
        self.assertEqual((got.status, got.fetched, got.written), ('done', 5, 4))
        self.assertTrue(got.finished_at)

    def test_running_guard_and_last(self):
        r = self.db.create_run('DKB', 'since', '2026/09/21 00:00')
        self.assertEqual(self.db.running_run('DKB').id, r.id)
        self.db.update_run(r.id, status='failed', error='x')
        self.assertIsNone(self.db.running_run('DKB'))
        self.assertEqual(self.db.last_run('DKB').id, r.id)
        self.assertIsNone(self.db.last_run('DKB', ok_only=True))

    def test_state_kv(self):
        self.db.set_state('lastSync/DKB', 'v1')
        self.db.set_state('lastSync/DKB', 'v2')
        self.assertEqual(self.db.get_state('lastSync/DKB'), 'v2')
        self.assertEqual(self.db.get_state('missing', 'dflt'), 'dflt')


if __name__ == '__main__':
    unittest.main()

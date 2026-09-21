"""Runner contract: progress-line parsing + worker command shape (no network)."""
import unittest
from types import SimpleNamespace

from app.crawl_runner import CrawlRunner, parse_progress


def _settings(**kw):
    base = dict(script='crawl_confluence.py', output='out', timeout=30,
                attachments=False, user='u', token='t', base_url='http://x')
    base.update(kw)
    return SimpleNamespace(**base)


class TestProgress(unittest.TestCase):
    def test_written(self):
        self.assertEqual(parse_progress('[DKB] 12/300'), ('written', 12, 300))

    def test_fetched(self):
        self.assertEqual(parse_progress('[DKB] fetched 300 pages'), ('fetched', 0, 300))

    def test_noise(self):
        self.assertIsNone(parse_progress('5 spaces: A, B'))
        self.assertIsNone(parse_progress(''))

    def test_full(self):
        cmd = CrawlRunner(_settings()).build_command('DKB')
        self.assertEqual(cmd[cmd.index('--space') + 1], 'DKB')
        self.assertIn('--resume', cmd)
        self.assertNotIn('--since', cmd)
        self.assertNotIn('--attachments', cmd)

    def test_since_and_attachments(self):
        cmd = CrawlRunner(_settings(attachments=True)).build_command('DKB', '2026/09/21 00:00')
        self.assertIn('--since', cmd)
        self.assertIn('2026/09/21 00:00', cmd)
        self.assertIn('--attachments', cmd)


if __name__ == '__main__':
    unittest.main()

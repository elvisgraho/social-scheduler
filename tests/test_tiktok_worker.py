"""Exercise the real worker against an isolated SQLite database."""

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

import run_worker as worker
from src import database
from src.platforms import tiktok
from src.upload_outcomes import PUBLISH_PENDING


class SimulatedCrash(BaseException):
    pass


class TikTokWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(database, 'DB_FILE', str(Path(self.temp.name) / 'test.db'))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.init_db()
        video = Path(self.temp.name) / 'sample.mp4'
        video.write_bytes(b'local fixture')
        self.queue_id = database.add_to_queue(str(video), None, 'Title', 'Caption', '["tiktok"]')
        self.uploader = Mock()
        platforms = {'tiktok': {'label': 'TikTok', 'connected': lambda: True, 'uploader': self.uploader}}
        for patcher in (
            patch.object(worker, 'get_platforms', return_value=platforms),
            patch.object(worker, '_preflight_platform', return_value=(True, 'Valid')),
            patch.object(worker, '_notify'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def row(self):
        return database.get_queue_item(self.queue_id)

    def test_crash_after_checkpoint_survives_restart_without_repost(self):
        def crash_after_checkpoint(path, caption, *, before_publish):
            before_publish()
            self.assertEqual(json.loads(self.row()['platform_logs'])['tiktok'], PUBLISH_PENDING)
            raise SimulatedCrash()

        self.uploader.side_effect = crash_after_checkpoint
        with self.assertRaises(SimulatedCrash):
            worker.process_video(self.row(), is_forced=True)
        worker.reset_stale_tasks()
        worker.process_video(self.row(), is_forced=True)
        self.assertEqual(self.uploader.call_count, 1)
        self.assertEqual(self.row()['status'], 'failed')
        self.assertEqual(database.get_uploaded_count(), 0)
        self.assertEqual(database.get_config(worker.PAUSE_KEY), '1')

    def test_confirmed_publication_is_archived(self):
        def confirmed(path, caption, *, before_publish):
            before_publish()
            return True, 'Upload Successful (TikTok confirmed publication)'

        self.uploader.side_effect = confirmed
        worker.process_video(self.row(), is_forced=True)
        self.assertIsNone(self.row())
        self.assertEqual(database.get_uploaded_count(), 1)

    def test_exception_after_checkpoint_remains_reviewable(self):
        def broken_connection(path, caption, *, before_publish):
            before_publish()
            raise OSError('connection lost')

        self.uploader.side_effect = broken_connection
        worker.process_video(self.row(), is_forced=True)
        self.assertEqual(json.loads(self.row()['platform_logs'])['tiktok'], PUBLISH_PENDING)
        self.assertEqual(self.row()['status'], 'failed')
        self.assertEqual(database.get_uploaded_count(), 0)

    def test_manual_clear_allows_explicit_retry(self):
        database.update_queue_status(self.queue_id, 'failed', None, {'tiktok': PUBLISH_PENDING})
        self.assertTrue(database.clear_platform_status(self.queue_id, 'tiktok'))
        self.uploader.return_value = (False, 'Failed before publishing')
        worker.process_video(self.row(), is_forced=True)
        self.uploader.assert_called_once()

    def test_real_adapter_and_worker_with_local_browser_fixture(self):
        form = (Path(__file__).parent / 'fixtures/tiktok_upload.html').read_text(encoding='utf-8')
        video = Path(self.row()['file_path'])
        video.write_bytes(b'0' * 150_000)
        observed_cookies = []

        @contextmanager
        def routed_playwright():
            with sync_playwright() as playwright:
                original_launch = playwright.chromium.launch

                def launch(**options):
                    browser = original_launch(**options)
                    original_context = browser.new_context

                    def new_context(**options):
                        context = original_context(**options)

                        def local_page(route):
                            observed_cookies.extend(context.cookies())
                            route.fulfill(status=200, content_type='text/html', body=form)

                        context.route('**/*', local_page)
                        return context

                    browser.new_context = new_context
                    return browser

                playwright.chromium.launch = launch
                yield playwright

        self.uploader.side_effect = tiktok.upload
        with patch.object(tiktok, 'sync_playwright', routed_playwright), patch.object(
            tiktok, 'ensure_session_valid', return_value=(True, 'local-test-session', 'Valid')
        ):
            worker.process_video(self.row(), is_forced=True)
        self.assertIsNone(self.row())
        self.assertEqual(database.get_uploaded_count(), 1)
        self.assertTrue(any(cookie['name'] == 'sessionid' and cookie['value'] == 'local-test-session'
                            for cookie in observed_cookies))


if __name__ == '__main__':
    unittest.main()

"""Local Chromium checks. These never contact TikTok or publish a real post."""

import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import TimeoutError, sync_playwright

from src.tiktok_browser import TikTokUpload


FORM = (Path(__file__).parent / "fixtures/tiktok_upload.html").read_text(encoding="utf-8")


class TikTokBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()
        cls.temp = tempfile.TemporaryDirectory()
        cls.video = Path(cls.temp.name) / "sample.mp4"
        cls.video.write_bytes(b"local file upload fixture")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.set_content(FORM)
        self.flow = TikTokUpload(self.page, confirmation_timeout_ms=700)

    def tearDown(self):
        self.context.close()

    def test_upload_caption_hashtags_and_confirmation(self):
        caption = "A test #video #hello\nSecond line"
        self.flow.prepare(str(self.video), caption)
        checkpoint = []
        self.flow.publish(lambda: checkpoint.append(self.page.evaluate("window.posts")))
        self.assertEqual(checkpoint, [0])
        self.assertEqual(self.page.evaluate("window.posts"), 1)
        self.assertEqual(self.page.locator('[contenteditable]').inner_text(), caption)
        self.assertEqual(self.page.locator('input').evaluate("el => el.files[0].name"), "sample.mp4")

    def test_nested_frame_and_shadow_cookie_banner(self):
        self.page.set_content('<iframe id="outer"></iframe>')
        self.page.locator('#outer').evaluate("(el, html) => el.srcdoc = html", '<iframe id="inner"></iframe>')
        inner = self.page.frame_locator('#outer').locator('#inner')
        inner.evaluate("(el, html) => el.srcdoc = html", FORM)
        self.page.evaluate("""() => {
          const banner = document.createElement('tiktok-cookie-banner');
          banner.attachShadow({mode:'open'}).innerHTML =
            '<button style="position:fixed;top:0">Decline optional cookies</button>';
          banner.shadowRoot.querySelector('button').onclick = () => banner.remove();
          document.body.append(banner);
        }""")
        self.flow.prepare(str(self.video), "")
        self.flow.publish(lambda: None)
        self.assertEqual(self.page.locator('tiktok-cookie-banner').count(), 0)
        self.assertEqual(self.flow.frame.locator('[contenteditable]').inner_text().strip(), "")

    def test_redirect_without_confirmation_requires_review(self):
        self.flow.prepare(str(self.video), "Caption")
        self.page.locator('[data-e2e]').evaluate("el => el.onclick = () => location.href = 'about:blank'")
        with self.assertRaises(TimeoutError):
            self.flow.publish(lambda: None)
        self.assertTrue(self.flow.publish_started)

    def test_editor_and_post_outside_upload_iframe(self):
        self.page.set_content(FORM)
        self.page.locator('input').evaluate('el => el.remove()')
        self.page.evaluate("""() => {
          const frame = document.createElement('iframe');
          frame.srcdoc = '<input type="file">';
          document.body.append(frame);
          document.querySelector('[aria-label="Replace"]').hidden = false;
          document.querySelector('[data-e2e]').disabled = false;
        }""")
        self.flow.prepare(str(self.video), 'Caption outside iframe')
        self.flow.publish(lambda: None)
        self.assertEqual(self.page.evaluate('window.posts'), 1)

    def test_preexisting_message_cannot_confirm_a_new_post(self):
        self.flow.prepare(str(self.video), 'Caption')
        self.page.locator('#result').evaluate("el => el.textContent = 'Post published'")
        with self.assertRaises(RuntimeError):
            self.flow.publish(lambda: None)
        self.assertFalse(self.flow.publish_started)
        self.assertEqual(self.page.evaluate('window.posts'), 0)

    def test_failed_checkpoint_prevents_post(self):
        self.flow.prepare(str(self.video), "Caption")

        def failed_checkpoint():
            raise OSError("database unavailable")

        with self.assertRaises(OSError):
            self.flow.publish(failed_checkpoint)
        self.assertFalse(self.flow.publish_started)
        self.assertEqual(self.page.evaluate('window.posts'), 0)

    def test_caption_failure_prevents_publishing(self):
        self.page.locator('[contenteditable]').evaluate(
            "el => el.addEventListener('input', () => el.textContent = 'wrong caption')"
        )
        with self.assertRaises(AssertionError):
            self.flow.prepare(str(self.video), "Required caption")
        self.assertFalse(self.flow.publish_started)
        self.assertEqual(self.page.evaluate('window.posts'), 0)

    def test_unconfirmed_post_is_clicked_once(self):
        self.flow.prepare(str(self.video), "Caption")
        self.page.locator('[data-e2e]').evaluate("el => el.onclick = () => window.posts++")
        with self.assertRaises(TimeoutError):
            self.flow.publish(lambda: None)
        self.assertTrue(self.flow.publish_started)
        self.assertEqual(self.page.evaluate('window.posts'), 1)


if __name__ == "__main__":
    unittest.main()

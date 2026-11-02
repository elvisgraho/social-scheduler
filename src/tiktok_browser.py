"""TikTok's upload UI, driven by Playwright without a browser driver."""

import re
import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import Error, Frame, Locator, Page, TimeoutError, expect


PUBLISHED = re.compile(
    r"^(post published[.!]?|your video has been (uploaded|published)[.!]?)$", re.I
)


class TikTokUpload:
    def __init__(self, page: Page, *, upload_timeout_ms: int = 360_000,
                 confirmation_timeout_ms: int = 60_000):
        self.page = page
        self.frame: Frame = page.main_frame
        self.upload_timeout_ms = upload_timeout_ms
        self.confirmation_timeout_ms = confirmation_timeout_ms
        self.publish_started = False

    def dismiss_popups(self) -> None:
        """Only dismiss known consent/help controls, never generic confirmations."""
        consent = re.compile(
            r"^(decline optional cookies|decline optional|reject optional|"
            r"allow all cookies|allow all|accept all cookies|accept all|agree to all)$", re.I
        )
        for frame in self.page.frames:
            for name in (consent, re.compile(r"^(got it|okay)$", re.I)):
                buttons = frame.get_by_role("button", name=name)
                for button in buttons.all():
                    try:
                        if button.is_visible():
                            button.click(timeout=1_000)
                    except Error:
                        # Frames and interstitials can disappear while being inspected.
                        continue
            dialogs = frame.get_by_role("dialog").filter(
                has_text=re.compile(r"sure you want to exit|discard post", re.I)
            )
            for dialog in dialogs.all():
                cancel = dialog.get_by_role("button", name=re.compile(r"^(cancel|keep editing)$", re.I))
                if cancel.count() and cancel.first.is_visible():
                    cancel.first.click(timeout=1_000)

    def find_upload_frame(self, timeout_ms: int = 30_000) -> Frame:
        deadline = time.monotonic() + timeout_ms / 1_000
        while time.monotonic() < deadline:
            self.dismiss_popups()
            # page.frames includes nested and cross-origin frames.
            for frame in self.page.frames:
                try:
                    if frame.locator('input[type="file"]').count():
                        self.frame = frame
                        return frame
                except Error:
                    continue
            self.page.wait_for_timeout(250)
        raise TimeoutError("TikTok file input not found. Check the session and upload page.")

    def prepare(self, video_path: str, description: str) -> None:
        frame = self.find_upload_frame()
        frame.locator('input[type="file"]').first.set_input_files(
            str(Path(video_path).resolve()), timeout=30_000
        )
        self._wait_for_upload()
        self.dismiss_popups()
        editor = self._find_control(
            '.public-DraftEditor-content[contenteditable="true"], '
            '[contenteditable="true"][data-e2e="caption-editor"], '
            '[role="textbox"][contenteditable="true"]'
        )
        # Clear the filename/default caption even when the requested caption is empty.
        editor.fill(description, timeout=15_000)
        editor.press("Escape")
        expect(editor).to_have_text(description, use_inner_text=True, timeout=5_000)

    def _find_control(self, selector: str, timeout_ms: int = 15_000) -> Locator:
        """Upload input, editor and Post button may live in different frames."""
        deadline = time.monotonic() + timeout_ms / 1_000
        while time.monotonic() < deadline:
            self.dismiss_popups()
            for frame in self.page.frames:
                try:
                    for control in frame.locator(selector).all():
                        if control.is_visible():
                            return control
                except Error:
                    continue
            self.page.wait_for_timeout(250)
        raise TimeoutError(f"TikTok control did not appear: {selector}")

    def _wait_for_upload(self) -> None:
        deadline = time.monotonic() + self.upload_timeout_ms / 1_000
        while time.monotonic() < deadline:
            self.dismiss_popups()
            for frame in self.page.frames:
                try:
                    ready = frame.locator(
                        'button[aria-label="Replace"]:visible, '
                        'button:has-text("Replace"):visible, .info-status.success:visible'
                    )
                    cancel = frame.get_by_role("button", name="Cancel", exact=True)
                    if ready.count() and not any(b.is_visible() for b in cancel.all()):
                        return
                except Error:
                    continue
            self.page.wait_for_timeout(500)
        raise TimeoutError("TikTok video processing did not complete before the timeout.")

    def publish(self, before_publish: Callable[[], None]) -> None:
        post = self._find_control('button[data-e2e="post_video_button"], button:text-is("Post")')
        expect(post).to_be_enabled(timeout=60_000)
        self.dismiss_popups()
        for frame in self.page.frames:
            if any(message.is_visible() for message in frame.get_by_text(PUBLISHED).all()):
                raise RuntimeError("Publication message already present before Post; check TikTok.")
        # Persist intent BEFORE a click, including clicks that time out after dispatch.
        before_publish()
        self.publish_started = True
        post.click(timeout=15_000)
        deadline = time.monotonic() + self.confirmation_timeout_ms / 1_000
        confirmation_clicked = False
        confirmation = re.compile(r"^(post now|continue to post)$", re.I)
        while time.monotonic() < deadline:
            for frame in self.page.frames:
                try:
                    # Require explicit confirmation, never just a redirect or missing button.
                    messages = frame.get_by_text(PUBLISHED)
                    if any(message.is_visible() for message in messages.all()):
                        return
                    if not confirmation_clicked:
                        buttons = frame.get_by_role("button", name=confirmation)
                        if buttons.count() and buttons.first.is_visible():
                            confirmation_clicked = True
                            buttons.first.click(timeout=5_000)
                except Error:
                    if self.page.is_closed():
                        raise
            self.page.wait_for_timeout(250)
        raise TimeoutError("TikTok did not confirm publication. Review the account before retrying.")

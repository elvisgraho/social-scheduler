"""Instagram compatibility checks without login or network requests."""

import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec, patch

import imageio_ffmpeg
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, LoginRequired
from instagrapi.mixins.clip import analyze_video
from pydantic import ValidationError

from src.platforms import instagram
from src.upload_outcomes import needs_review


class InstagramTests(unittest.TestCase):
    def test_saved_device_identity_survives_app_profile_upgrade(self):
        old = Client().get_settings()
        old['device_settings'].update(app_version='410.1.0.63.71', bloks_versioning_id=None)
        old['user_agent'] = 'Instagram 410.1.0.63.71 old-profile'
        original_ids = copy.deepcopy(old['uuids'])
        hardware = old['device_settings']['model']
        with patch.object(instagram, 'get_config', return_value=json.dumps(old)):
            client = instagram._initialize_client()
            self.assertTrue(instagram._load_settings(client))
        self.assertEqual(client.get_settings()['uuids'], original_ids)
        self.assertEqual(client.device_settings['model'], hardware)
        self.assertTrue(client.bloks_versioning_id)
        self.assertNotIn('410.1.0.63.71', client.user_agent)
        self.assertEqual(client.locale, 'de_DE')

    def test_real_video_metadata_and_thumbnail_without_moviepy(self):
        ffmpeg = shutil.which('ffmpeg') or imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / 'sample.mp4'
            subprocess.run(
                [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                 '-f', 'lavfi', '-i', 'color=c=blue:s=96x160:r=10',
                 '-t', '0.5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)],
                check=True, capture_output=True, timeout=30,
            )
            client = create_autospec(Client, instance=True)
            thumbnail_paths = []

            def check_upload(path, caption, *, thumbnail, show_preview_in_feed):
                self.assertEqual(len(caption), 2200)
                self.assertFalse(show_preview_in_feed)
                self.assertTrue(thumbnail.is_file())
                # Run the actual installed instagrapi metadata path used by clip_upload.
                thumb, width, height, duration = analyze_video(path, thumbnail)
                self.assertEqual((width, height), (96, 160))
                self.assertGreater(duration, 0)
                thumbnail_paths.append(thumb)
                return SimpleNamespace(pk='123', product_type='clips')

            client.clip_upload.side_effect = check_upload
            media = instagram._upload_reel(client, str(video), 'x' * 2300)
            self.assertEqual(media.pk, '123')
            self.assertFalse(thumbnail_paths[0].exists())

    def run_upload(self, effect):
        self.client = Mock()
        with patch.object(instagram, '_initialize_client', return_value=self.client), patch.object(
            instagram, '_load_settings', return_value=True
        ), patch.object(instagram, '_login', return_value=(True, 'Valid')) as login, patch.object(
            instagram, '_upload_reel', side_effect=effect
        ) as upload, patch.object(instagram, '_store_settings'), patch.object(instagram, 'set_account_state'):
            result = instagram.upload('sample.mp4', 'Caption')
            return result, upload.call_count, login.call_count

    def test_reel_id_is_required_for_success(self):
        result, calls, _ = self.run_upload([SimpleNamespace(pk='123', product_type='clips')])
        self.assertTrue(result[0])
        self.assertEqual(calls, 1)
        result, _, _ = self.run_upload([SimpleNamespace(pk='123', product_type='feed')])
        self.assertFalse(result[0])
        self.assertTrue(needs_review(result[1]))

    def test_unreadable_response_requires_review_without_retry(self):
        error = ValidationError.from_exception_data('Media', [])
        result, calls, _ = self.run_upload([error])
        self.assertFalse(result[0])
        self.assertTrue(needs_review(result[1]))
        self.assertEqual(calls, 1)

    def test_generic_failure_does_not_trigger_relogin_or_repost(self):
        result, calls, logins = self.run_upload([OSError('connection lost')])
        self.assertFalse(result[0])
        self.assertEqual((calls, logins), (1, 1))

    def test_explicit_login_expiry_retries_once_and_checks_reel_type(self):
        result, calls, logins = self.run_upload([
            LoginRequired('expired'), SimpleNamespace(pk='123', product_type='feed')
        ])
        self.assertFalse(result[0])
        self.assertTrue(needs_review(result[1]))
        self.assertEqual((calls, logins), (2, 2))

    def test_challenge_stops_without_retry(self):
        result, calls, _ = self.run_upload([ChallengeRequired('challenge')])
        self.assertFalse(result[0])
        self.assertIn('2FA', result[1])
        self.assertEqual(calls, 1)


if __name__ == '__main__':
    unittest.main()

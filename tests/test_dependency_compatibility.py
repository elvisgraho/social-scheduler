"""Check the pinned dependencies against the application's actual UI."""

import importlib.metadata
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from src import database


ROOT = Path(__file__).resolve().parents[1]


class DependencyCompatibilityTests(unittest.TestCase):
    def test_direct_dependency_pins_match_installed_versions(self):
        for line in (ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            package, expected = line.split('==')
            with self.subTest(package=package):
                self.assertEqual(importlib.metadata.version(package), expected)

    def test_streamlit_app_starts_with_isolated_database(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            database, 'DB_FILE', str(Path(directory) / 'scheduler.db')
        ):
            app = AppTest.from_file(str(ROOT / 'main.py')).run(timeout=30)
            self.assertEqual(list(app.exception), [])
            self.assertEqual([tab.label for tab in app.tabs],
                             ['Dashboard', 'Queue', 'Accounts', 'Settings', 'Logs'])


if __name__ == '__main__':
    unittest.main()

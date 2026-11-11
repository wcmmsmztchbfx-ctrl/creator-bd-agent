import unittest
from pathlib import Path
from app import main


class UIAssetTests(unittest.TestCase):
    def test_ui_is_a_separate_utf8_asset(self):
        path = Path(main.__file__).with_name('ui.html')
        self.assertTrue(path.is_file())
        self.assertEqual(main.HTML_CONTENT, path.read_text(encoding='utf-8'))

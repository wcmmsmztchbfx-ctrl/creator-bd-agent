import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RuntimePathsTests(unittest.TestCase):
    def test_fresh_working_directory_uses_checkout_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory).resolve() / "fresh checkout"
            shutil.copytree(ROOT / "app", checkout / "app", ignore=shutil.ignore_patterns("__pycache__"))
            env = {**os.environ, "PYTHONPATH": str(checkout)}
            env.pop("CREATOR_BD_HOME", None)
            result = subprocess.check_output(
                [
                    sys.executable, "-c",
                    "import json; from app import db, exporter; "
                    "print(json.dumps([str(db.DB_PATH), str(exporter.EXPORTS_DIR)]))",
                ],
                cwd=directory,
                env=env,
                text=True,
            )
            self.assertEqual(json.loads(result), [
                str(checkout / "data" / "creator_kb.db"), str(checkout / "exports"),
            ])

    def test_custom_workspace_places_database_and_exports_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "workspace with spaces"
            env = {**os.environ, "PYTHONPATH": str(ROOT), "CREATOR_BD_HOME": str(home)}
            result = subprocess.check_output(
                [
                    sys.executable, "-c",
                    "import json; from app import db, exporter; "
                    "print(json.dumps([str(db.DB_PATH), str(exporter.EXPORTS_DIR)]))",
                ],
                cwd=directory,
                env=env,
                text=True,
            )
            self.assertEqual(json.loads(result), [
                str(home / "data" / "creator_kb.db"), str(home / "exports"),
            ])

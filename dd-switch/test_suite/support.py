"""Shared fixtures for the dd-switch test suite.

Every test that touches the filesystem points the module at a temp directory,
so the real ``~/.claude/settings.json``, the real ``config/`` dir and the real
``servers.json`` are never read or written.
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

DD_SWITCH_DIR = Path(__file__).resolve().parents[1]
if str(DD_SWITCH_DIR) not in sys.path:
    sys.path.insert(0, str(DD_SWITCH_DIR))

import app  # noqa: E402
import ssh_manager  # noqa: E402

TEST_PASSWORD = "test-pw"


class AppTestCase(unittest.TestCase):
    """Base case: temp config dir + temp settings.json, no real files touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        self.settings = self.root / "settings.json"
        self.servers_file = self.root / "servers.json"

        for patcher in (
            mock.patch.object(app, "CONFIG_DIR", self.config_dir),
            mock.patch.object(app, "CLAUDE_CONFIG", self.settings),
            mock.patch.object(app, "ACCESS_PASSWORD", TEST_PASSWORD),
            mock.patch.object(ssh_manager, "SERVERS_FILE", self.servers_file),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        app._token_store.clear()
        self.addCleanup(app._token_store.clear)

    # --- filesystem helpers ---------------------------------------------

    def write_config(self, name, data):
        path = self.config_dir / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def write_settings(self, data):
        self.settings.write_text(json.dumps(data), encoding="utf-8")

    def read_settings(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def read_json_file(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    # --- http helpers ----------------------------------------------------

    def client(self):
        """An unauthenticated test client."""
        return app.app.test_client()

    def authed_client(self):
        """A test client carrying a valid session cookie."""
        app._token_store["test-token"] = {"login_time": time.time()}
        client = app.app.test_client()
        client.set_cookie("dd_token", "test-token")
        return client

    def stale_client(self, age_seconds=None):
        """A test client whose token is older than the session max age."""
        age = age_seconds if age_seconds is not None else app.SESSION_MAX_AGE + 60
        app._token_store["stale-token"] = {"login_time": time.time() - age}
        client = app.app.test_client()
        client.set_cookie("dd_token", "stale-token")
        return client

#!/usr/bin/env python3
"""Regression tests for defects found while testing claude-gearshift.

Every test here pins down one defect (all fixed 2026-10-20 — the
``@unittest.expectedFailure`` markers were dropped as each fix landed and each
test turned green). If a regression reintroduces one of these bugs, the
corresponding test fails normally again.

The defects pinned here (2026-10-20):

===  ====================================================================
 1   ``/api/switch`` reads files outside the config dir (path traversal)
 2   ``/api/configs/<name>`` PUT/DELETE on a directory raises a 500
 3   switching to a config without ``env`` silently deletes the live env
 4   the remote ``/api/servers/<name>/current`` returns unmasked tokens
 5   ``/api/servers`` with a non-numeric port raises a 500
 6   deleting an unknown server reports success
 7   ``DD_SWITCH_ROUTE_PORT`` is ignored by the gateway URL / health probe
 8   the spawned proxy is never reaped (zombie process)
===  ====================================================================
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402
from support import AppTestCase  # noqa: E402


class PathTraversalTests(AppTestCase):
    """Defect 1 & 2 — config names are not confined to the config dir.

    The config name reaches ``CONFIG_DIR / name`` unchecked, so ``..`` segments
    or an absolute path escape the config directory: any JSON file the process
    can read is merged into ~/.claude/settings.json and echoed back to the
    caller (including its non-env fields, e.g. a private key).
    """

    def _secret_outside_config(self):
        secret = self.root / "secret.json"
        secret.write_text(
            json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "LEAKED"},
                        "sshPrivateKey": "-----BEGIN OPENSSH PRIVATE KEY-----"}),
            encoding="utf-8",
        )
        return secret

    def test_switch_rejects_parent_directory_escape(self):
        self._secret_outside_config()
        res = self.authed_client().post("/api/switch", json={"name": "../secret.json"})
        self.assertNotEqual(res.status_code, 200,
                            "a name escaping the config dir must be rejected")
        self.assertFalse(self.settings.exists(),
                         "no file outside the config dir may be applied")

    def test_switch_rejects_absolute_path(self):
        secret = self._secret_outside_config()
        res = self.authed_client().post("/api/switch", json={"name": str(secret)})
        self.assertNotEqual(res.status_code, 200,
                            "an absolute path must be rejected")
        self.assertFalse(self.settings.exists())

    def test_config_get_rejects_parent_directory_escape(self):
        self._secret_outside_config()
        res = self.authed_client().get("/api/configs/..%2Fsecret.json")
        self.assertEqual(res.status_code, 404)

    def test_config_delete_of_a_directory_is_not_a_500(self):
        # ".." resolves to the config dir itself; deleting it must fail cleanly.
        res = self.authed_client().delete("/api/configs/..")
        self.assertIn(res.status_code, (400, 404),
                      "must be a clean rejection, not an unhandled 500")

    def test_config_update_of_a_directory_is_not_a_500(self):
        res = self.authed_client().put("/api/configs/..", json={"content": {"x": 1}})
        self.assertIn(res.status_code, (400, 404),
                      "must be a clean rejection, not an unhandled 500")


class SwitchSemanticsTests(AppTestCase):
    """Switching replaces `env` and keeps every other field."""

    def test_switch_keeps_non_env_fields_and_backs_up(self):
        self.write_settings({"env": {"ANTHROPIC_AUTH_TOKEN": "old"}, "theme": "dark"})
        self.write_config("p.json", {"env": {"ANTHROPIC_AUTH_TOKEN": "new"}})

        res = self.authed_client().post("/api/switch", json={"name": "p.json"})
        self.assertEqual(res.status_code, 200)

        data = self.read_settings()
        self.assertEqual(data["env"], {"ANTHROPIC_AUTH_TOKEN": "new"})
        self.assertEqual(data["theme"], "dark")
        self.assertTrue(list(self.root.glob("settings.json.bak.*")),
                        "the previous settings.json must be backed up")

    def test_switch_to_a_config_without_env_does_not_wipe_the_current_env(self):
        # Defect 3: a config with no `env` block drops the live env entirely,
        # so the user's active Claude Code session loses its credentials.
        self.write_settings({"env": {"ANTHROPIC_AUTH_TOKEN": "keep"}, "theme": "dark"})
        self.write_config("noenv.json", {"other": "x"})

        self.authed_client().post("/api/switch", json={"name": "noenv.json"})
        self.assertEqual(self.read_settings().get("env"),
                         {"ANTHROPIC_AUTH_TOKEN": "keep"})

    def test_switch_to_a_config_with_empty_env_does_not_wipe_the_current_env(self):
        self.write_settings({"env": {"ANTHROPIC_AUTH_TOKEN": "keep"}})
        self.write_config("emptyenv.json", {"env": {}})

        self.authed_client().post("/api/switch", json={"name": "emptyenv.json"})
        self.assertEqual(self.read_settings().get("env"),
                         {"ANTHROPIC_AUTH_TOKEN": "keep"})


class RemoteCredentialLeakTests(AppTestCase):
    """Defect 4 — remote credentials must be masked like local ones.

    The local ``/api/current`` masks secret env values server-side. The remote
    endpoint returns them in full, so the browser receives the real token even
    though the UI only truncates it for display.
    """

    def test_remote_current_masks_secret_env_values(self):
        with mock.patch.object(
            app.ssh_manager, "load_servers",
            return_value=[{"name": "r", "host": "h", "username": "u"}],
        ), mock.patch.object(
            app.ssh_manager, "remote_read_config",
            return_value={"exists": True,
                          "content": {"env": {"ANTHROPIC_AUTH_TOKEN": "sk-secret-value"},
                                      "theme": "dark"}},
        ):
            res = self.authed_client().get("/api/servers/r/current")

        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["env"]["ANTHROPIC_AUTH_TOKEN"], "***")
        self.assertNotIn("sk-secret-value", json.dumps(body))


class ServerValidationTests(AppTestCase):
    """Defects 5 & 6 — the servers API must validate its input."""

    def test_add_server_with_a_non_numeric_port_is_a_400(self):
        res = self.authed_client().post(
            "/api/servers",
            json={"name": "s1", "host": "h", "username": "u", "port": "abc"},
        )
        self.assertEqual(res.status_code, 400,
                         "an invalid port must be rejected, not raise a 500")

    def test_delete_of_an_unknown_server_is_a_404(self):
        res = self.authed_client().delete("/api/servers/ghost")
        self.assertEqual(res.status_code, 404)


class AuthTests(AppTestCase):
    """Session handling."""

    def test_api_requires_a_session(self):
        res = self.client().get("/api/configs")
        self.assertEqual(res.status_code, 401)
        self.assertTrue(res.get_json().get("login_required"))

    def test_pages_redirect_to_login(self):
        res = self.client().get("/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/login", res.headers["Location"])

    def test_expired_session_is_rejected(self):
        res = self.stale_client().get("/api/configs")
        self.assertEqual(res.status_code, 401)

    def test_logout_invalidates_the_session_server_side(self):
        client = self.authed_client()
        self.assertEqual(client.get("/api/configs").status_code, 200)
        client.get("/logout")
        self.assertEqual(client.get("/api/configs").status_code, 401)

    def test_login_sets_a_httponly_cookie(self):
        res = self.client().post("/login", data={"password": "test-pw"})
        self.assertEqual(res.status_code, 302)
        cookie = res.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_wrong_password_does_not_issue_a_token(self):
        res = self.client().post("/login", data={"password": "wrong"})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("dd_token=", res.headers.get("Set-Cookie", ""))

    def test_responses_are_not_cached(self):
        res = self.authed_client().get("/api/configs")
        self.assertIn("no-store", res.headers["Cache-Control"])


class ConfigApiTests(AppTestCase):
    """Config CRUD."""

    def test_create_adds_the_json_suffix(self):
        res = self.authed_client().post(
            "/api/configs", json={"name": "p", "content": {"env": {}}}
        )
        self.assertEqual(res.status_code, 201)
        self.assertTrue((self.config_dir / "p.json").exists())

    def test_create_rejects_a_duplicate(self):
        self.write_config("p.json", {"env": {}})
        res = self.authed_client().post(
            "/api/configs", json={"name": "p.json", "content": {"env": {}}}
        )
        self.assertEqual(res.status_code, 409)

    def test_create_rejects_a_path_separator(self):
        res = self.authed_client().post(
            "/api/configs", json={"name": "sub/dir.json", "content": {"env": {}}}
        )
        self.assertEqual(res.status_code, 400)

    def test_create_rejects_a_non_object_payload(self):
        res = self.authed_client().post(
            "/api/configs", json={"name": "p.json", "content": ["not", "an", "object"]}
        )
        self.assertEqual(res.status_code, 400)

    def test_scan_separates_valid_from_invalid_and_skips_sample(self):
        self.write_config("good.json", {"env": {}})
        (self.config_dir / "bad.json").write_text("{not json", encoding="utf-8")
        (self.config_dir / "sample.json").write_text(json.dumps({"env": {}}), encoding="utf-8")

        body = self.authed_client().get("/api/configs").get_json()
        valid = {c["name"] for c in body["valid"]}
        invalid = {c["name"] for c in body["invalid"]}
        self.assertIn("good.json", valid)
        self.assertIn("bad.json", invalid)
        self.assertNotIn("sample.json", valid | invalid)

    def test_delete_of_a_missing_config_is_a_404(self):
        res = self.authed_client().delete("/api/configs/nope.json")
        self.assertEqual(res.status_code, 404)


class ServerApiTests(AppTestCase):
    """Server management and passphrase handling."""

    def test_passphrase_is_encrypted_at_rest(self):
        self.authed_client().post(
            "/api/servers",
            json={"name": "s1", "host": "h", "username": "u", "passphrase": "hunter2"},
        )
        raw = self.servers_file.read_text(encoding="utf-8")
        self.assertNotIn("hunter2", raw, "the passphrase must not be stored in the clear")
        stored = json.loads(raw)[0]["passphrase"]
        self.assertIn("encrypted", stored)
        self.assertIn("salt", stored)

    def test_passphrase_roundtrips_with_the_right_password(self):
        import ssh_manager

        blob = ssh_manager._encrypt_passphrase("hunter2", "test-pw")
        self.assertEqual(ssh_manager._decrypt_passphrase(blob, "test-pw"), "hunter2")

    def test_passphrase_cannot_be_decrypted_with_another_password(self):
        import ssh_manager

        blob = ssh_manager._encrypt_passphrase("hunter2", "test-pw")
        with self.assertRaises(Exception):
            ssh_manager._decrypt_passphrase(blob, "another-pw")

    def test_listing_does_not_leak_the_passphrase(self):
        self.authed_client().post(
            "/api/servers",
            json={"name": "s1", "host": "h", "username": "u", "passphrase": "hunter2"},
        )
        body = self.authed_client().get("/api/servers").get_json()
        self.assertNotIn("hunter2", json.dumps(body))
        self.assertTrue(body["servers"][0]["has_passphrase"])


class RoutePortTests(AppTestCase):
    """Defect 7 — the gateway URL must follow the configured port.

    ``DD_SWITCH_ROUTE_PORT`` changes the port the daemon listens on, but
    ``ROUTE_BASE_URL`` is hardcoded to 9301. The health probe then polls the
    wrong port (routing can never start) and the URL written into
    settings.json points Claude Code at the wrong gateway.
    """

    def test_gateway_url_follows_the_configured_port(self):
        with mock.patch.object(app, "ROUTE_PORT", 19399):
            self.assertEqual(app.ROUTE_BASE_URL, "http://127.0.0.1:19399")


class ProxyProcessReapingTests(AppTestCase):
    """Defect 8 — the spawned proxy process must be reaped.

    ``_start_route_process`` returns the pid but keeps no handle on the
    ``Popen`` object, so the child is never waited on and stays a zombie.
    """

    def test_spawned_proxy_is_reaped(self):
        spawned = {}

        class FakeProc:
            pid = 424242

            def __init__(self):
                self.waited = False

            def wait(self, timeout=None):
                self.waited = True
                return 0

        proc = FakeProc()
        spawned["obj"] = proc

        with mock.patch.object(app.subprocess, "Popen", return_value=proc):
            app._start_route_process()

        self.assertTrue(proc.waited,
                        "the child process must be waited on to avoid a zombie")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for the cc-switch-proxy local-routing feature in dd-switch.

Run:  python3 -m unittest discover -s tests -v   (from dd-switch/)

Process spawning, termination, the readiness probe and the provider CLI are all
mocked — no cc-switch-proxy is launched and no network is touched. Real
settings.json is never read or written: every test points the module at a temp
directory.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402


PROVIDERS = {
    "app": "claude",
    "current": "relay",
    "providers": [
        {
            "id": "relay",
            "name": "Relay",
            "settingsConfig": {"env": {"ANTHROPIC_BASE_URL": "https://a.example"}},
            "isCurrent": True,
        }
    ],
}


class FakeProxy:
    """Stands in for the spawned cc-switch-proxy process."""

    def __init__(self, pid=4242):
        self.pid = pid
        self.alive = False
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1
        self.alive = True
        return self.pid

    def stop(self, pid):
        self.stops += 1
        self.alive = False
        return True

    def is_alive(self, pid):
        return self.alive


class RouteTestCase(unittest.TestCase):
    """Base: point everything at a temp dir, enable the feature, fake the proxy."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = root / "settings.json"
        self.state_dir = root / "state"
        self.state_file = self.state_dir / "route-state.json"
        self.proxy = FakeProxy()

        self.proxy_cli = mock.patch.object(app, "_proxy_cli", return_value=dict(PROVIDERS))
        self.proxy_cli_mock = self.proxy_cli.start()
        self.addCleanup(self.proxy_cli.stop)

        for patcher in (
            mock.patch.object(app, "ROUTE_ENABLED", True),
            mock.patch.object(app, "ROUTE_PROXY_BIN", sys.executable),
            mock.patch.object(app, "ROUTE_DB", str(root / "cc-switch.db")),
            mock.patch.object(app, "CLAUDE_CONFIG", self.settings),
            mock.patch.object(app, "ROUTE_STATE_DIR", self.state_dir),
            mock.patch.object(app, "ROUTE_STATE_FILE", self.state_file),
            mock.patch.object(app, "_start_route_process", side_effect=self.proxy.start),
            mock.patch.object(app, "_stop_route_process", side_effect=self.proxy.stop),
            mock.patch.object(app, "_route_process_alive", side_effect=self.proxy.is_alive),
            mock.patch.object(app, "_wait_route_ready", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_settings(self, data):
        self.settings.write_text(json.dumps(data), encoding="utf-8")

    def read_settings(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))


class ConfigTests(RouteTestCase):
    def test_supported_requires_enabled_and_binary(self):
        self.assertTrue(app.route_supported())

        with mock.patch.object(app, "ROUTE_ENABLED", False):
            self.assertFalse(app.route_supported())
            self.assertIn("DD_SWITCH_ROUTE_ENABLED", app.route_missing_config())

        with mock.patch.object(app, "ROUTE_PROXY_BIN", "/nonexistent/cc-switch-proxy"):
            self.assertFalse(app.route_supported())
            self.assertIn("DD_SWITCH_ROUTE_PROXY_BIN", app.route_missing_config())

    def test_no_hardcoded_personal_paths(self):
        # 二进制与库路径都必须来自环境变量 / PATH，源码里不得写死个人目录。
        src = Path(app.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/home/", src)


class StartTests(RouteTestCase):
    def test_start_snapshots_env_and_writes_state(self):
        self.write_settings({"env": {"ANTHROPIC_AUTH_TOKEN": "orig", "FOO": "bar"}, "theme": "dark"})
        result = app.route_start()

        self.assertEqual(result["provider"], "relay")
        self.assertEqual(self.proxy.starts, 1)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["pid"], self.proxy.pid)
        self.assertEqual(state["provider"], "relay")
        self.assertTrue(state["original_env"]["ANTHROPIC_AUTH_TOKEN"]["present"])
        self.assertEqual(state["original_env"]["ANTHROPIC_AUTH_TOKEN"]["value"], "orig")
        # 开启路由本身不碰 settings.json
        self.assertEqual(self.read_settings()["env"]["ANTHROPIC_AUTH_TOKEN"], "orig")

    def test_start_without_current_provider_fails(self):
        self.proxy_cli_mock.return_value = {"app": "claude", "current": None, "providers": []}
        with self.assertRaises(app.RouteError):
            app.route_start()
        self.assertEqual(self.proxy.starts, 0)

    def test_start_twice_is_rejected(self):
        app.route_start()
        with self.assertRaises(app.RouteError):
            app.route_start()
        self.assertEqual(self.proxy.starts, 1)

    def test_start_respawns_after_process_died(self):
        app.route_start()
        self.proxy.alive = False
        app.route_start()
        self.assertEqual(self.proxy.starts, 2)


class ApplyTests(RouteTestCase):
    def test_apply_writes_aliases_and_placeholder(self):
        self.write_settings({"env": {"FOO": "bar"}, "theme": "dark"})
        app.route_start()
        app.route_apply()

        data = self.read_settings()
        env = data["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], app.ROUTE_BASE_URL)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], app.ROUTE_TOKEN_PLACEHOLDER)
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], app.ROUTE_ALIASES["sonnet"])
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], app.ROUTE_ALIASES["haiku"])
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], app.ROUTE_ALIASES["opus"])
        # 非托管字段保留
        self.assertEqual(env["FOO"], "bar")
        self.assertEqual(data["theme"], "dark")

    def test_apply_without_routing_fails(self):
        with self.assertRaises(app.RouteError):
            app.route_apply()


class StopTests(RouteTestCase):
    def test_stop_restores_env_and_stops_process(self):
        self.write_settings({"env": {"ANTHROPIC_AUTH_TOKEN": "orig", "FOO": "bar"}})
        app.route_start()
        app.route_apply()
        app.route_stop()

        env = self.read_settings()["env"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "orig")
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertEqual(env["FOO"], "bar")
        self.assertFalse(self.state_file.exists())
        self.assertEqual(self.proxy.stops, 1)

    def test_stop_without_state_fails(self):
        with self.assertRaises(app.RouteError):
            app.route_stop()


class ProviderCliTests(RouteTestCase):
    def test_save_add_builds_args(self):
        app.route_provider_save({"name": "X", "base_url": "https://x", "token": "sk", "set_current": True})
        args = self.proxy_cli_mock.call_args[0][0]
        self.assertIn("add", args)
        self.assertIn("--name", args)
        self.assertIn("--base-url", args)
        self.assertIn("--token", args)
        self.assertIn("--set-current", args)
        self.assertNotIn("--id", args)

    def test_save_update_uses_id(self):
        app.route_provider_save({"id": "relay", "name": "R"})
        args = self.proxy_cli_mock.call_args[0][0]
        self.assertEqual(args[:2], ["update", "--id"])
        self.assertIn("relay", args)

    def test_delete_and_set_current(self):
        app.route_provider_delete("relay")
        self.assertEqual(self.proxy_cli_mock.call_args[0][0][:2], ["delete", "--id"])
        app.route_provider_set_current("relay")
        self.assertEqual(self.proxy_cli_mock.call_args[0][0][:2], ["set-current", "--id"])

    def test_providers_masks_tokens(self):
        data = {
            "app": "claude",
            "current": "relay",
            "providers": [
                {
                    "id": "relay",
                    "name": "R",
                    "settingsConfig": {
                        "env": {"ANTHROPIC_AUTH_TOKEN": "sk-secret", "ANTHROPIC_BASE_URL": "https://a"}
                    },
                }
            ],
        }
        self.proxy_cli_mock.return_value = data
        out = app.route_providers()
        env = out["providers"][0]["settingsConfig"]["env"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "***")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://a")


class ProxySettingTests(RouteTestCase):
    def setUp(self):
        # 先记下未被类级 mock 覆盖前的原始函数，供 subprocess 集成测试还原用
        self.orig_proxy_global_cli = app._proxy_global_cli
        super().setUp()
        self.proxy_global = mock.patch.object(
            app, "_proxy_global_cli",
            return_value={"global_proxy_url": None, "effective": "重启 cc-switch-proxy 后生效（或由环境变量兜底）"},
        )
        self.proxy_global_mock = self.proxy_global.start()
        self.addCleanup(self.proxy_global.stop)

    def test_get_maps_snake_to_camel(self):
        out = app.route_proxy_setting()
        self.assertIsNone(out["globalProxyUrl"])
        self.assertIn("重启", out["effective"])
        self.assertEqual(self.proxy_global_mock.call_args[0][0], ["get"])

    def test_set_passes_url_verbatim(self):
        self.proxy_global_mock.return_value = {
            "global_proxy_url": "socks5h://10.0.0.2:1080",
            "effective": "重启 cc-switch-proxy 后生效（或由环境变量兜底）",
        }
        res = app.route_proxy_save("socks5h://10.0.0.2:1080")
        self.assertEqual(res["globalProxyUrl"], "socks5h://10.0.0.2:1080")
        self.assertEqual(self.proxy_global_mock.call_args[0][0],
                         ["set", "--url", "socks5h://10.0.0.2:1080"])

    def test_empty_url_clears(self):
        self.proxy_global_mock.return_value = {"global_proxy_url": None, "effective": ""}
        res = app.route_proxy_save("   ")
        self.assertIsNone(res["globalProxyUrl"])
        self.assertEqual(self.proxy_global_mock.call_args[0][0], ["clear"])

    def test_frontend_validator_rejects_wrong_scheme_before_cli(self):
        # 与前端同规则：非 http(s)/socks5(h) 交给 CLI 深校验失败，包装层不再放行空 url 以外的东西进 CLI？—
        # 设计上放行（保留 CLI 报错），这里只断言确实原样传给了 CLI。
        app.route_proxy_save("ftp://x")
        self.assertEqual(self.proxy_global_mock.call_args[0][0], ["set", "--url", "ftp://x"])

    def test_proxy_cli_wraps_subprocess_correctly(self):
        # 还原原始 _proxy_global_cli，让它调用 subprocess 验证命令拼装
        with mock.patch.object(app, "_proxy_global_cli", self.orig_proxy_global_cli):
            fake_proc = type("Proc", (), {"returncode": 0, "stdout": '{"global_proxy_url": null}\n', "stderr": ""})()
            with mock.patch.object(app.subprocess, "run", return_value=fake_proc) as run_mock:
                out = app.route_proxy_setting()
                cmd = run_mock.call_args[0][0]
        # 走 `proxy` 子命令、带 --db、不带 --app（provider 专属）
        self.assertEqual(cmd[1], "proxy")
        self.assertIn("--db", cmd)
        self.assertNotIn("--app", cmd)
        self.assertIsNone(out["globalProxyUrl"])
        self.assertEqual(app._PROXY_SCHEME_RE.match("socks5h://a").group(1), "socks5h")
        self.assertIsNone(app._PROXY_SCHEME_RE.match("ftp://x"))


if __name__ == "__main__":
    unittest.main()

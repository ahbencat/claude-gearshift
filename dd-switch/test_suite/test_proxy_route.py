#!/usr/bin/env python3
"""Tests for the cc-switch-proxy local-routing feature in dd-switch.

Run:  python3 -m unittest discover -s test_suite -v   (from dd-switch/)

Process spawning, termination, the readiness probe and the provider CLI are all
mocked — no cc-switch-proxy is launched and no network is touched. Real
settings.json is never read or written: every test points the module at a temp
directory.
"""

import json
import sys
import tempfile
import time
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
        # 开启即应用：settings.json 已指向网关，真 key 只留在快照里
        env = self.read_settings()["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], app.ROUTE_BASE_URL)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], app.ROUTE_TOKEN_PLACEHOLDER)

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


class StartWritesPointingTests(RouteTestCase):
    def test_start_writes_gateway_pointing_only(self):
        # 模型名归用户管：开启即写两个网络分派键，用户自己写的模型键原样保留
        self.write_settings({
            "env": {
                "FOO": "bar",
                "ANTHROPIC_MODEL": "mimo-v2.6",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "hy4-preview[1M]",
            },
            "theme": "dark",
        })
        app.route_start()

        data = self.read_settings()
        env = data["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], app.ROUTE_BASE_URL)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], app.ROUTE_TOKEN_PLACEHOLDER)
        self.assertEqual(env["ANTHROPIC_MODEL"], "mimo-v2.6")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "hy4-preview[1M]")
        self.assertNotIn("ANTHROPIC_DEFAULT_SONNET_MODEL", env)
        self.assertNotIn("ANTHROPIC_DEFAULT_HAIKU_MODEL", env)
        # 非托管字段保留
        self.assertEqual(env["FOO"], "bar")
        self.assertEqual(data["theme"], "dark")


class StopTests(RouteTestCase):
    def test_stop_restores_env_and_stops_process(self):
        self.write_settings({"env": {"ANTHROPIC_AUTH_TOKEN": "orig", "FOO": "bar"}})
        app.route_start()
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

    def test_stop_after_respawn_still_restores(self):
        # 进程死后重开：复用快照并重写指向；关闭仍能按快照恢复（旧 applied
        # 门控下这条路径会漏恢复）
        self.write_settings({"env": {"ANTHROPIC_AUTH_TOKEN": "orig"}})
        app.route_start()
        self.proxy.alive = False
        app.route_start()
        env = self.read_settings()["env"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], app.ROUTE_TOKEN_PLACEHOLDER)
        app.route_stop()
        self.assertEqual(self.read_settings()["env"]["ANTHROPIC_AUTH_TOKEN"], "orig")


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

    def test_unsupported_scheme_passes_through_to_cli(self):
        # 后端不做 scheme 预检（前端 toast 拦截 + Rust 深校验是唯一真相来源），原样透传。
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


class ProviderApiFormatTests(RouteTestCase):
    def test_save_passes_api_format_flag(self):
        app.route_provider_save({"name": "X", "api_format": "openai_chat"})
        args = self.proxy_cli_mock.call_args[0][0]
        self.assertIn("--api-format", args)
        self.assertIn("openai_chat", args)

    def test_save_omits_flag_when_empty(self):
        app.route_provider_save({"name": "X"})
        args = self.proxy_cli_mock.call_args[0][0]
        self.assertNotIn("--api-format", args)
        self.assertNotIn("api_format", args)

    def _authed_client(self):
        app._token_store["t"] = {"login_time": time.time()}
        client = app.app.test_client()
        client.set_cookie("dd_token", "t")
        return client

    def test_api_rejects_invalid_format(self):
        res = self._authed_client().post(
            "/api/route/providers", json={"name": "X", "api_format": "ftp"}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("未知的上游协议格式", res.get_json()["error"])
        self.proxy_cli_mock.assert_not_called()

    def test_api_accepts_valid_format(self):
        res = self._authed_client().post(
            "/api/route/providers", json={"name": "X", "api_format": "gemini_native"}
        )
        self.assertEqual(res.status_code, 200)
        args = self.proxy_cli_mock.call_args[0][0]
        self.assertIn("--api-format", args)
        self.assertIn("gemini_native", args)


class ImportTests(RouteTestCase):
    """Device-side import: uploaded files are staged and handed to the CLI.

    The source is the browsing device (e.g. a Windows box), not the server's
    config/ — payloads arrive in the request body, get staged into a temp dir
    and are passed to `provider import --dir` from there.
    """

    def _capture(self):
        captured = {}

        def fake(args, *a, **k):
            captured["args"] = args
            stage = Path(args[args.index("--dir") + 1])
            captured["files"] = {
                p.name: json.loads(p.read_text(encoding="utf-8"))
                for p in stage.glob("*.json")
            }
            return {"ok": True, "imported": [n[:-len(".json")] for n in captured["files"]],
                    "errors": []}

        self.proxy_cli_mock.side_effect = fake
        return captured

    def test_uploaded_files_are_staged_for_the_cli(self):
        captured = self._capture()
        content = {"env": {"ANTHROPIC_BASE_URL": "https://u.example"}}
        result = app.route_provider_import_files(
            [{"name": "my_provider.json", "content": content}]
        )

        self.assertEqual(result["imported"], ["my_provider"])
        self.assertEqual(captured["args"][0], "import")
        self.assertEqual(captured["files"], {"my_provider.json": content})

    def test_import_strips_the_chat_completions_suffix(self):
        captured = self._capture()
        app.route_provider_import_files([
            {"name": "x", "content": {
                "env": {"ANTHROPIC_BASE_URL": "https://u.example/v1/chat/completions"}}},
        ])
        self.assertEqual(captured["files"]["x.json"]["env"]["ANTHROPIC_BASE_URL"],
                         "https://u.example")

    def test_import_rejects_an_empty_batch(self):
        with self.assertRaises(app.RouteError):
            app.route_provider_import_files([])

    def test_import_rejects_a_path_in_the_name(self):
        with self.assertRaises(app.RouteError):
            app.route_provider_import_files([{"name": "../evil.json", "content": {}}])
        self.proxy_cli_mock.assert_not_called()

    def test_api_import_requires_files(self):
        app._token_store["t"] = {"login_time": time.time()}
        client = app.app.test_client()
        client.set_cookie("dd_token", "t")
        res = client.post("/api/route/providers/import", json={"set_current": False})
        self.assertEqual(res.status_code, 400)
        self.proxy_cli_mock.assert_not_called()


class ModelTableTests(RouteTestCase):
    """settings_config.modelNodes / modelRoutes 的编排、校验与掩码。"""

    def test_save_single_type_omits_model_flags(self):
        app.route_provider_save({"name": "X", "base_url": "https://x", "token": "sk"})
        args = self.proxy_cli_mock.call_args[0][0]
        self.assertNotIn("--model-nodes", args)
        self.assertNotIn("--model-routes", args)

    def test_save_mixed_sends_json_flags(self):
        app.route_provider_save({
            "name": "X", "id": "x",
            "model_nodes": [{"name": "a", "url": "https://a/v1/chat/completions", "key": ""}],
            "model_routes": {"mimo-v2.6": {"node": "a", "format": "openai_chat"}},
        })
        args = self.proxy_cli_mock.call_args[0][0]
        self.assertEqual(json.loads(args[args.index("--model-nodes") + 1]),
                         [{"name": "a", "url": "https://a"}])  # chat 后缀被剥；"" key 显式清空
        self.assertEqual(json.loads(args[args.index("--model-routes") + 1]),
                         {"mimo-v2.6": {"node": "a", "format": "openai_chat"}})

    def test_save_update_inherits_stored_node_keys(self):
        # update 时留空 key 从存量同名节点继承——编辑节点不必重输 key
        self.proxy_cli_mock.return_value = {
            "providers": [{"id": "x", "name": "X",
                           "settingsConfig": {"modelNodes": [{"name": "a", "key": "sk-old"}]}}],
        }
        app.route_provider_save({"id": "x", "name": "X", "model_nodes": [{"name": "a"}]})
        args = self.proxy_cli_mock.call_args[0][0]
        self.assertEqual(json.loads(args[args.index("--model-nodes") + 1]),
                         [{"name": "a", "key": "sk-old"}])

    def test_save_partial_update_omits_base_url_flag(self):
        # 部分更新只传 name/base_url 缺席时，不得出现 --base-url ""
        # （CLI 会把空串当成清除 ANTHROPIC_BASE_URL，env 会凭空丢掉它——
        # 就地归一化曾把 None 变 ""，E2E 发现过这个回归）。
        app.route_provider_save({"id": "x", "name": "X"})
        args = self.proxy_cli_mock.call_args[0][0]
        self.assertNotIn("--base-url", args)

    def test_save_rejects_duplicate_node_names(self):
        with self.assertRaises(app.RouteError):
            app.route_provider_save({
                "name": "X", "model_nodes": [{"name": "a"}, {"name": "a"}]})
        self.proxy_cli_mock.assert_not_called()

    def test_save_rejects_unknown_route_format(self):
        with self.assertRaises(app.RouteError):
            app.route_provider_save({
                "name": "X", "model_routes": {"m": {"format": "ftp"}}})
        self.proxy_cli_mock.assert_not_called()

    def test_save_rejects_one_m_suffixed_model_key(self):
        with self.assertRaises(app.RouteError):
            app.route_provider_save({
                "name": "X", "model_routes": {"m[1M]": {"format": "anthropic"}}})
        self.proxy_cli_mock.assert_not_called()

    def test_providers_masks_node_keys(self):
        self.proxy_cli_mock.return_value = {
            "providers": [{
                "id": "x", "name": "X",
                "settingsConfig": {
                    "env": {"ANTHROPIC_AUTH_TOKEN": "sk-secret"},
                    "modelNodes": [{"name": "a", "url": "https://a", "key": "sk-node"}],
                    "modelRoutes": {"m": {"node": "a", "format": "openai_chat"}},
                },
            }],
        }
        out = app.route_providers()
        node = out["providers"][0]["settingsConfig"]["modelNodes"][0]
        self.assertEqual(node["key"], "***")
        self.assertEqual(node["url"], "https://a")
        self.assertEqual(out["providers"][0]["settingsConfig"]["modelRoutes"],
                         {"m": {"node": "a", "format": "openai_chat"}})

    def _authed_client(self):
        app._token_store["t"] = {"login_time": time.time()}
        client = app.app.test_client()
        client.set_cookie("dd_token", "t")
        return client

    def test_api_rejects_non_list_nodes(self):
        res = self._authed_client().post(
            "/api/route/providers", json={"name": "X", "model_nodes": {"name": "a"}})
        self.assertEqual(res.status_code, 400)
        self.assertIn("节点表必须是数组", res.get_json()["error"])
        self.proxy_cli_mock.assert_not_called()

    def test_api_rejects_bad_row_format(self):
        res = self._authed_client().post(
            "/api/route/providers",
            json={"name": "X", "model_routes": {"m": {"format": "ftp"}}})
        self.assertEqual(res.status_code, 400)
        self.assertIn("未知的接口形态", res.get_json()["error"])
        self.proxy_cli_mock.assert_not_called()

    def test_api_accepts_mixed_payload(self):
        res = self._authed_client().post(
            "/api/route/providers",
            json={"name": "X",
                  "model_nodes": [{"name": "a", "url": "https://a", "key": "sk"}],
                  "model_routes": {"m": {"node": "a", "format": "openai_responses"}}})
        self.assertEqual(res.status_code, 200)
        args = self.proxy_cli_mock.call_args[0][0]
        self.assertIn("--model-nodes", args)
        self.assertIn("--model-routes", args)


if __name__ == "__main__":
    unittest.main()

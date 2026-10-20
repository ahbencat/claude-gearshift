#!/usr/bin/env python3
"""End-to-end tests for local routing (cc-switch-proxy) against a real upstream.

These tests drive the **real** ``cc-switch-proxy`` binary and the **real**
upstream endpoint from ``test_data/.testdata``. Nothing is mocked: a daemon is
spawned on a throwaway SQLite DB, Claude-Code-shaped requests are sent through
it, and the replies are checked.

What is covered:

* provider CRUD through the real ``provider`` CLI subcommands
* the global outbound-proxy setting through the real ``proxy`` subcommands
* the Anthropic ⇄ OpenAI protocol relay (the "转接口" path)
* the stable tier aliases (``claude-sonnet-5`` …) mapping to the upstream's
  real model names
* the full dd-switch lifecycle: ``route_start`` → ``route_apply`` → traffic →
  ``route_stop`` restoring the original ``~/.claude/settings.json``

The endpoint is read from ``test_data/.testdata``::

    model_name=mimo-v2.6-flash
    end_point=https://opencode.ai/inference/go/openai/v1/chat/completions
    key=oc_sk_...

The tests skip (rather than fail) when the endpoint, the proxy binary or the
WebKitGTK runtime libraries are unavailable, so the suite stays green offline.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402

# --- locating the pieces under test ---------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]          # linux_switch/
WORKSPACE_ROOT = REPO_ROOT.parent                          # claude_gear/
TESTDATA_FILE = WORKSPACE_ROOT / "test_data" / ".testdata"
PROXY_BIN = REPO_ROOT / "patches" / "bin" / "cc-switch-proxy-linux-x86_64"

# The prebuilt binary links Tauri, so it needs the WebKitGTK/GTK runtime libs.
# On the deploy host they live unpacked outside any git repo; honour the
# caller's LD_LIBRARY_PATH and additionally probe the documented location.
RUNTIME_LIB_CANDIDATES = [
    Path.home() / "deploy" / "runtime-libs" / "usr" / "lib" / "x86_64-linux-gnu",
    Path.home() / "deploy" / "runtime-libs" / "usr" / "lib",
]


def _load_testdata():
    """Parse test_data/.testdata into a dict of key=value pairs."""
    if not TESTDATA_FILE.exists():
        return None
    data = {}
    for line in TESTDATA_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    if not {"model_name", "end_point", "key"} <= data.keys():
        return None
    return data


TESTDATA = _load_testdata()


def _proxy_env():
    """Environment for the proxy binary, with the GTK runtime libs on the path."""
    env = os.environ.copy()
    parts = [p for p in env.get("LD_LIBRARY_PATH", "").split(":") if p]
    for candidate in RUNTIME_LIB_CANDIDATES:
        if candidate.is_dir() and str(candidate) not in parts:
            parts.append(str(candidate))
    if parts:
        env["LD_LIBRARY_PATH"] = ":".join(parts)
    return env


def _binary_usable():
    """True when the proxy binary can actually start on this host."""
    if not PROXY_BIN.exists():
        return False
    try:
        proc = subprocess.run(
            [str(PROXY_BIN), "--help"], capture_output=True, text=True, timeout=30,
            env=_proxy_env(),
        )
    except Exception:
        return False
    return proc.returncode == 0


def _endpoint_reachable(timeout=10):
    """True when the upstream host resolves and accepts TCP connections."""
    if not TESTDATA:
        return False
    url = TESTDATA["end_point"]
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except OSError:
        return False


PROXY_AVAILABLE = _binary_usable()
ENDPOINT_AVAILABLE = _endpoint_reachable()

# The daemon is spawned with start_new_session=True and is terminated with a
# signal, so the Popen object is never waited on — Python emits a
# ResourceWarning for it at GC time. That is a property of the app (see the
# reaping regression test), not of this suite; keep the test output readable.
warnings.filterwarnings("ignore", category=ResourceWarning)

# The upstream is an OpenAI-Chat endpoint: the base URL the proxy should be
# given is everything before the "/v1/chat/completions" suffix. Passing the
# full endpoint as base_url makes the proxy append the path a second time
# ("…/v1/chat/completions/v1/chat/completions"), which the upstream rejects.
_CHAT_SUFFIX = "/v1/chat/completions"


def upstream_base_url():
    end_point = TESTDATA["end_point"]
    if end_point.endswith(_CHAT_SUFFIX):
        return end_point[: -len(_CHAT_SUFFIX)]
    return end_point


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sse_events(raw):
    """Extract the `event:` names from an SSE stream."""
    return [
        line.split(":", 1)[1].strip()
        for line in raw.splitlines()
        if line.startswith("event:")
    ]


class ProxyTestCase(unittest.TestCase):
    """Base: throwaway DB + settings, real binary, real upstream."""

    def setUp(self):
        if not PROXY_AVAILABLE:
            self.skipTest(f"cc-switch-proxy binary unusable: {PROXY_BIN}")
        if not ENDPOINT_AVAILABLE:
            self.skipTest("upstream endpoint unreachable")

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.db = self.root / "cc-switch.db"
        self.settings = self.root / "settings.json"
        self.state_dir = self.root / "state"

        for patcher in (
            _patch(app, "ROUTE_ENABLED", True),
            _patch(app, "ROUTE_PROXY_BIN", str(PROXY_BIN)),
            _patch(app, "ROUTE_DB", str(self.db)),
            _patch(app, "CLAUDE_CONFIG", self.settings),
            _patch(app, "ROUTE_STATE_DIR", self.state_dir),
            _patch(app, "ROUTE_STATE_FILE", self.state_dir / "route-state.json"),
            _patch(app, "ROUTE_LOG_FILE", self.state_dir / "route.log"),
            _patch(app, "ROUTE_PORT", self.port),
            _patch(app, "ROUTE_BASE_URL", self.base_url),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        # The spawned daemon inherits the environment, so the GTK runtime libs
        # have to be visible to it as well.
        self.env_patch = _patch(os, "environ", os.environ.copy())
        os.environ.update(_proxy_env())

        self.addCleanup(self._stop_route)

    # --- helpers ----------------------------------------------------------

    def _stop_route(self):
        try:
            if app._route_state() is not None:
                app.route_stop()
        except Exception:
            pass

    def add_provider(self, name="OpenCodeGo", set_current=True, api_format="openai_chat"):
        payload = {
            "name": name,
            "base_url": upstream_base_url(),
            "token": TESTDATA["key"],
            "sonnet": TESTDATA["model_name"],
            "haiku": TESTDATA["model_name"],
            "opus": TESTDATA["model_name"],
            "api_format": api_format,
        }
        if set_current:
            payload["set_current"] = True
        return app.route_provider_save(payload)

    def start_route(self, attempts=3):
        """Start the daemon, retrying a *transient* failure to start it.

        Only a startup timeout / immediate exit is retried: an "already
        active" error is a legitimate outcome and must reach the caller.
        """
        last_error = None
        for attempt in range(attempts):
            try:
                return app.route_start()
            except app.RouteError as exc:
                last_error = exc
                message = str(exc)
                transient = ("启动超时" in message or "启动后即退出" in message
                             or "启动 cc-switch-proxy 失败" in message)
                if not transient or attempt + 1 >= attempts:
                    raise
                # A failed start leaves a half-open state behind; clear it so
                # the next attempt starts from scratch.
                try:
                    if app._route_state() is not None:
                        app.route_stop()
                except Exception:
                    pass
                time.sleep(2)
        raise last_error

    def apply_route(self):
        return app.route_apply()

    def stop_route(self):
        result = app.route_stop()
        self._stop_route = lambda: None
        return result

    def read_settings(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def anthropic_request(self, model="claude-sonnet-5", text="Say pong", stream=False,
                          timeout=90, attempts=3):
        """Send a Claude-Code-shaped request through the proxy.

        The upstream is a live service, so a transient failure (a 5xx, a
        dropped connection, or an error payload relayed by the proxy) must not
        turn into a flaky test: retry a few times before giving up. Only the
        last failure is reported.
        """
        last_error = None
        for attempt in range(attempts):
            try:
                raw, content_type = self._anthropic_request_once(
                    model, text, stream, timeout
                )
                if not stream and '"error"' in raw[:200]:
                    raise AssertionError(f"proxy relayed an error payload: {raw[:200]}")
                return raw, content_type
            except Exception as exc:  # noqa: BLE001 - re-raised below
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(2)
        raise last_error

    def _anthropic_request_once(self, model="claude-sonnet-5", text="Say pong",
                                 stream=False, timeout=90):
        body = {
            "model": model,
            "max_tokens": 100,
            "messages": [{"role": "user", "content": text}],
        }
        if stream:
            body["stream"] = True
        req = urllib.request.Request(
            self.base_url + "/v1/messages",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": app.ROUTE_TOKEN_PLACEHOLDER,
                "user-agent": "claude-cli/2.0.0",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            content_type = resp.headers.get("Content-Type", "")
        return raw, content_type


def _patch(obj, name, value):
    """Patch an attribute, or an environment variable when obj is os.environ."""
    if obj is os.environ:
        original = os.environ.get(name)
        os.environ[name] = str(value)

        class _Env:
            def stop(self):
                if original is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = original

        return _Env()
    from unittest import mock

    return mock.patch.object(obj, name, value)


class ProviderCliTests(ProxyTestCase):
    """The `provider` subcommands against the real binary and a real DB."""

    def test_add_list_roundtrip_preserves_model_mapping(self):
        self.add_provider()
        providers = app.route_providers()
        self.assertEqual(providers["current"], "opencodego")

        env = providers["providers"][0]["settingsConfig"]["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], upstream_base_url())
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], TESTDATA["model_name"])
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], TESTDATA["model_name"])
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], TESTDATA["model_name"])

    def test_token_is_masked_in_the_api_response(self):
        self.add_provider()
        env = app.route_providers()["providers"][0]["settingsConfig"]["env"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "***")
        self.assertNotIn(TESTDATA["key"], json.dumps(app.route_providers()))

    def test_api_format_is_stored_and_reported(self):
        self.add_provider()
        provider = app.route_providers()["providers"][0]
        self.assertEqual(provider.get("apiFormat"), "openai_chat")

    def test_update_changes_the_current_provider(self):
        self.add_provider()
        app.route_provider_save({"id": "opencodego", "name": "Renamed"})
        providers = app.route_providers()
        self.assertEqual(providers["providers"][0]["name"], "Renamed")

    def test_set_current_and_delete(self):
        self.add_provider(name="First")
        self.add_provider(name="Second", set_current=False)
        app.route_provider_set_current("second")
        self.assertEqual(app.route_providers()["current"], "second")

        app.route_provider_delete("second")
        ids = [p["id"] for p in app.route_providers()["providers"]]
        self.assertNotIn("second", ids)

    def test_deleting_the_current_provider_falls_back(self):
        self.add_provider(name="First")
        self.add_provider(name="Second", set_current=False)
        app.route_provider_set_current("second")
        app.route_provider_delete("second")
        # The proxy must still have a usable current provider afterwards.
        self.assertTrue(app.route_providers()["providers"])


class OutboundProxyTests(ProxyTestCase):
    """The global outbound-proxy setting through the real `proxy` subcommand."""

    def test_get_set_clear_roundtrip(self):
        self.assertIsNone(app.route_proxy_setting()["globalProxyUrl"])

        saved = app.route_proxy_save("http://127.0.0.1:7890")
        self.assertEqual(saved["globalProxyUrl"], "http://127.0.0.1:7890")

        self.assertEqual(app.route_proxy_setting()["globalProxyUrl"], "http://127.0.0.1:7890")

        cleared = app.route_proxy_clear()
        self.assertIsNone(cleared["globalProxyUrl"])

    def test_empty_url_clears_the_setting(self):
        app.route_proxy_save("http://127.0.0.1:7890")
        self.assertIsNone(app.route_proxy_save("   ")["globalProxyUrl"])


class DaemonTests(ProxyTestCase):
    """The daemon itself: health, status and a clean shutdown."""

    def test_health_endpoint_reports_ready(self):
        self.add_provider()
        self.start_route()
        with urllib.request.urlopen(self.base_url + "/health", timeout=10) as resp:
            self.assertEqual(resp.status, 200)

    def test_status_endpoint_reports_running(self):
        self.add_provider()
        self.start_route()
        with urllib.request.urlopen(self.base_url + "/status", timeout=10) as resp:
            status = json.loads(resp.read())
        self.assertTrue(status["running"])

    def test_starting_twice_is_rejected(self):
        self.add_provider()
        self.start_route()
        with self.assertRaises(app.RouteError):
            self.start_route()

    def test_stop_terminates_the_daemon(self):
        self.add_provider()
        self.start_route()
        app.route_stop()
        self._stop_route = lambda: None
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(self.base_url + "/health", timeout=5)


class ProtocolRelayTests(ProxyTestCase):
    """The Anthropic ⇄ OpenAI protocol relay against the real upstream."""

    def test_anthropic_request_is_answered_with_an_anthropic_response(self):
        self.add_provider()
        self.start_route()

        raw, content_type = self.anthropic_request(text="Say pong")
        body = json.loads(raw)

        self.assertEqual(body["type"], "message")
        self.assertEqual(body["role"], "assistant")
        self.assertIn("content", body)
        self.assertTrue(body["content"])
        for block in body["content"]:
            self.assertIn(block["type"], ("text", "thinking"))
        self.assertIsNotNone(body.get("stop_reason"))
        self.assertIn("input_tokens", body.get("usage", {}))

    def test_tier_alias_is_mapped_to_the_upstream_model(self):
        self.add_provider()
        self.start_route()

        raw, _ = self.anthropic_request(model="claude-sonnet-5")
        body = json.loads(raw)
        # The client asked for the stable alias; the reply reports the real
        # upstream model the proxy mapped it to.
        self.assertEqual(body["model"], TESTDATA["model_name"])

    def test_response_content_reflects_the_prompt(self):
        self.add_provider()
        self.start_route()

        raw, _ = self.anthropic_request(text="Reply with exactly the word BANANA")
        body = json.loads(raw)
        text = "".join(
            block.get("text", "") for block in body["content"] if block["type"] == "text"
        )
        self.assertIn("BANANA", text.upper())

    def test_streaming_returns_anthropic_sse_events(self):
        self.add_provider()
        self.start_route()

        raw, content_type = self.anthropic_request(stream=True)
        self.assertIn("text/event-stream", content_type)

        events = _sse_events(raw)
        self.assertEqual(events[0], "message_start")
        self.assertEqual(events[-1], "message_stop")
        self.assertIn("content_block_start", events)
        self.assertIn("content_block_delta", events)

        # Every data line must be valid JSON carrying an Anthropic event type.
        for line in raw.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[len("data:"):].strip())
                self.assertIn("type", payload)

    def test_switching_the_current_provider_takes_effect_without_restart(self):
        self.add_provider(name="First")
        self.add_provider(name="Second", set_current=False)
        self.start_route()

        app.route_provider_set_current("second")
        raw, _ = self.anthropic_request()
        body = json.loads(raw)
        self.assertEqual(body["model"], TESTDATA["model_name"])


class DdSwitchLifecycleTests(ProxyTestCase):
    """The full dd-switch flow: start → apply → traffic → stop."""

    def test_apply_writes_the_gateway_and_aliases(self):
        self.add_provider()
        self.settings.write_text(
            json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "orig", "FOO": "bar"},
                        "theme": "dark"}),
            encoding="utf-8",
        )
        self.start_route()
        self.apply_route()

        data = self.read_settings()
        env = data["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], self.base_url)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], app.ROUTE_TOKEN_PLACEHOLDER)
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], app.ROUTE_ALIASES["sonnet"])
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], app.ROUTE_ALIASES["haiku"])
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], app.ROUTE_ALIASES["opus"])
        # Untouched by routing.
        self.assertEqual(env["FOO"], "bar")
        self.assertEqual(data["theme"], "dark")

    def test_traffic_flows_through_the_applied_gateway(self):
        self.add_provider()
        self.settings.write_text(json.dumps({"env": {}}), encoding="utf-8")
        self.start_route()
        self.apply_route()

        # The settings now point Claude Code at the local gateway.
        self.assertEqual(self.read_settings()["env"]["ANTHROPIC_BASE_URL"], self.base_url)

        raw, _ = self.anthropic_request(text="Say pong")
        body = json.loads(raw)
        self.assertEqual(body["type"], "message")
        self.assertTrue(body["content"])

    def test_stop_restores_the_original_settings(self):
        self.add_provider()
        original = {"env": {"ANTHROPIC_AUTH_TOKEN": "orig", "FOO": "bar"}, "theme": "dark"}
        self.settings.write_text(json.dumps(original), encoding="utf-8")

        self.start_route()
        self.apply_route()
        self.stop_route()

        self.assertEqual(self.read_settings(), original)

    def test_stop_removes_managed_keys_that_were_absent_before(self):
        self.add_provider()
        self.settings.write_text(json.dumps({"env": {"FOO": "bar"}}), encoding="utf-8")

        self.start_route()
        self.apply_route()
        self.assertIn("ANTHROPIC_BASE_URL", self.read_settings()["env"])

        self.stop_route()
        env = self.read_settings()["env"]
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertEqual(env["FOO"], "bar")

    def test_state_file_is_private_and_removed_on_stop(self):
        self.add_provider()
        self.start_route()

        state_file = self.state_dir / "route-state.json"
        self.assertTrue(state_file.exists())
        self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)

        self.stop_route()
        self.assertFalse(state_file.exists())

    def test_a_backup_of_the_settings_is_written_on_apply(self):
        self.add_provider()
        self.settings.write_text(json.dumps({"env": {"FOO": "bar"}}), encoding="utf-8")
        self.start_route()
        self.apply_route()

        backups = list(self.root.glob("settings.json.bak.*"))
        self.assertTrue(backups, "apply should back up settings.json first")


if __name__ == "__main__":
    unittest.main()

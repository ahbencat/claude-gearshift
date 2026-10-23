#!/usr/bin/env python3
"""Local-routing E2E against the REAL opencode gateway, all four API shapes.

``test_local_routing_e2e.py`` covers the routing machinery with one OpenAI-Chat
endpoint (plus a localhost sink for the per-model dispatch mechanics). This
suite asks the question only a genuinely multi-protocol upstream can answer:
does each provider shape survive live traffic **on the real opencode
gateway** — a node that natively serves all four wire protocols:

======  ============================================================
shape   upstream path
======  ============================================================
openai_chat      ``{base}/v1/chat/completions``
openai_responses ``{base}/v1/responses``
anthropic        ``{base}/v1/messages``
gemini_native    ``{base}/models/{model}:generateContent``
======  ============================================================

Two coverage aims (the dd-switch provider model calls them 单类型/多类型):

* **Single-type providers** — one provider is one upstream API shape. Each of
  the four shapes, held in its own provider, must answer Claude-Code traffic
  through the real proxy (Anthropic inbound → the provider's shape →
  Anthropic reply).
* **Mixed-type provider** — one provider whose ``model_nodes`` table holds one
  node per shape and whose ``model_routes`` map four models onto four
  different wire protocols. The provider's own access point is a deliberately
  dead URL and its token is junk, so the *only* working path to the upstream
  is the node table: per-node URL + per-node key override, and the per-model
  format picking the protocol.

Node config is read from ``test_data/.opencode-node.json`` (gitignored, holds
the real key; ``DD_OPENCODE_NODE`` overrides the path)::

    {
      "key": "oc_sk_…",
      "shapes": {
        "openai_chat":      {"base_url": "https://opencode.ai/inference/go/openai/v1",
                             "model": "mimo-v2.6-flash"},
        "openai_responses": {"base_url": "https://opencode.ai/inference/go/openai/v1",
                             "model": "grok-4.7"},
        "anthropic":        {"base_url": "https://opencode.ai/inference/go/anthropic",
                             "model": "minimax-m3"},
        "gemini_native":    {"base_url": "https://opencode.ai/inference/go/google/v1beta",
                             "model": "gemini-4-argon-contributor"}
      }
    }

How these bases were obtained: the opencode documentation's Endpoints table
(the same source ``zen``/``go`` model-sync scripts parse) — models funnel into
``/go/openai/v1`` (chat + responses), ``/go/anthropic`` and
``/go/google/v1beta``. Region-locked models (gpt-6-luna, muse-spark-…) are
excluded on purpose: they 403 without an outbound egress, which is a property
of those models, not of routing. Everything skips (never fails offline) when
the config, the network or the proxy binary is unavailable.
"""

import functools
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402
from test_local_routing_e2e import (  # noqa: E402
    WORKSPACE_ROOT,
    ProxyTestCase,
    _tcp_reachable,
)

# --- the real opencode node ------------------------------------------------

DEFAULT_NODE_FILE = WORKSPACE_ROOT / "test_data" / ".opencode-node.json"
# 四种接口形态的值域直接取自 app（与后端白名单同源），不在此复述。
SHAPES = tuple(app.MODEL_ROUTE_FORMATS)


def _load_node():
    path = Path(os.environ.get("DD_OPENCODE_NODE", DEFAULT_NODE_FILE))
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not cfg.get("key"):
        return None
    shapes = cfg.get("shapes") or {}
    for fmt in SHAPES:
        shape = shapes.get(fmt) or {}
        if not shape.get("base_url") or not shape.get("model"):
            return None
    return cfg


NODE = _load_node()


@functools.lru_cache(maxsize=1)
def _upstream_reachable():
    """True when opencode.ai accepts TCP connections (probed at most once)."""
    if not NODE:
        return False
    return _tcp_reachable(NODE["shapes"]["openai_chat"]["base_url"], timeout=8)


def _reply_text(body):
    """Concatenate every text/thinking block of an Anthropic reply."""
    parts = []
    for block in body.get("content", []):
        if isinstance(block, dict):
            parts.append(block.get("text") or "")
            parts.append(block.get("thinking") or "")
    return "".join(parts).strip()


class OpenCodeNodeTestCase(ProxyTestCase):
    """Base: throwaway DB + settings, real binary, real opencode gateway.

    The daemon fixture (temp DB/settings, patched route knobs, lifecycle
    retries) is ``test_local_routing_e2e.ProxyTestCase``; this class only adds
    the opencode-specific dependency gates (node config + gateway reachability)
    and the reply assertions.
    """

    # 外部依赖门：这套测试不读 test_data/.testdata，改看节点配置与网关连通。
    REQUIRE_ENDPOINT = False
    REQUIRE_NODE = True
    REQUIRE_UPSTREAM = True

    def setUp(self):
        if self.REQUIRE_NODE and NODE is None:
            self.skipTest("node config missing (test_data/.opencode-node.json)")
        if self.REQUIRE_UPSTREAM and not _upstream_reachable():
            self.skipTest("opencode gateway unreachable")
        super().setUp()

    def anthropic_request(self, model, text="Say pong", timeout=120, attempts=3):
        """A Claude-Code-shaped request through the proxy; returns raw text.

        200 输出预算：thinking 型模型会先花 token 再吐文本。
        """
        raw, _ = super().anthropic_request(
            model, text=text, timeout=timeout, attempts=attempts, max_tokens=200,
        )
        return raw

    def assert_anthropic_reply(self, raw, label):
        """The relay must return a usable Anthropic message, not an error."""
        try:
            body = json.loads(raw)
        except ValueError:
            self.fail(f"{label}: non-JSON reply: {raw[:200]}")
        self.assertEqual(body.get("type"), "message",
                         f"{label}: {json.dumps(body, ensure_ascii=False)[:220]}")
        self.assertTrue(_reply_text(body),
                        f"{label}: empty reply: {json.dumps(body, ensure_ascii=False)[:220]}")
        return body


class SingleTypeNodeTests(OpenCodeNodeTestCase):
    """单类型节点：一个供应商就是一个上游 API 形态，四种形态各立一户。

    四条腿共用一个 daemon（切换 current 供应商即换上游，无需重启）——每个
    形态仍是独立供应商、独立真实流量，只是省掉三次进程拉起。
    """

    def _provider_for(self, api_format):
        shape = NODE["shapes"][api_format]
        res = app.route_provider_save({
            "name": f"OpenCode-{api_format}",
            "base_url": shape["base_url"],
            "token": NODE["key"],
            "api_format": api_format,
            "set_current": True,
        })
        return res["id"], shape["model"]

    def test_single_type_serves_claude_traffic_in_every_shape(self):
        providers = {fmt: self._provider_for(fmt) for fmt in SHAPES}
        self.start_route()  # 四户先建好（route_start 要求已有 current）
        for api_format in SHAPES:
            with self.subTest(api_format=api_format):
                provider_id, model = providers[api_format]
                app.route_provider_set_current(provider_id)
                raw = self.anthropic_request(model)
                self.assert_anthropic_reply(
                    raw, f"single/{api_format} (model={model})")


class MixedTypeNodeTests(OpenCodeNodeTestCase):
    """多类型节点：一个供应商 + 节点表 + 每模型路由，四形态分道扬镳。

    The provider's own access point points into a wall (port 1, nothing
    listens) and its token is junk — the only working route to the upstream
    goes through the node table (one node per shape), proving per-node URL +
    key override and the per-model format picking the wire protocol, all on
    live traffic.
    """

    def _mixed_provider(self):
        nodes = [{"name": fmt, "url": NODE["shapes"][fmt]["base_url"],
                  "key": NODE["key"]} for fmt in SHAPES]
        routes = {NODE["shapes"][fmt]["model"]: {"node": fmt, "format": fmt}
                  for fmt in SHAPES}
        app.route_provider_save({
            "name": "OpenCode-mixed",
            "base_url": "http://127.0.0.1:1",           # 必死的默认接入点
            "token": "unused-node-key-must-override",   # 假 token → 节点 key 必须接管
            "model_nodes": nodes,
            "model_routes": routes,
            "set_current": True,
        })
        return routes

    def test_one_mixed_provider_answers_all_four_shapes(self):
        routes = self._mixed_provider()
        self.start_route()
        for model in routes:
            with self.subTest(model=model):
                raw = self.anthropic_request(model)
                self.assert_anthropic_reply(raw, f"mixed (model={model})")


class _SinkProviderMixin(OpenCodeNodeTestCase):
    """离线基座：真实 daemon + 本地 sink 上游，不触外网（回归 #15/#16）。

    供应商默认接入点指死端口、token 是假值，只有节点表指向本测试进程内的
    HTTP sink——路由命中与否一目了然，出站头（含凭据）在请求到达时被抓走。
    """

    REQUIRE_NODE = False
    REQUIRE_UPSTREAM = False

    PROVIDER_TOKEN = "unused-node-key-must-override"
    NODE_KEY = "oc_sk_node_override_key"

    def setUp(self):
        super().setUp()
        self.captured = []

        mixin = self

        class _Sink(BaseHTTPRequestHandler):
            def do_POST(self):
                mixin._sink_do(self)

            def log_message(self, *args):
                pass

        sink = ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
        self.sink_port = sink.server_address[1]
        threading.Thread(target=sink.serve_forever, daemon=True).start()
        self.addCleanup(sink.shutdown)

    def _sink_do(self, request):
        """读请求体、抓路径与出站头，按 `_sink_payload` 回应。"""
        length = int(request.headers.get("Content-Length") or 0)
        request.rfile.read(length)
        self.captured.append({
            "path": request.path,
            "headers": {k.lower(): v for k, v in request.headers.items()},
        })
        status, body = self._sink_payload(request)
        self._respond(request, status, body)

    def _sink_payload(self, request):
        """默认回 500，让 daemon 走错误映射路径。"""
        return 500, b'{"error":"captured"}'

    @staticmethod
    def _respond(request, status, body):
        request.send_response(status)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    def _save_mixed_provider(self, routes):
        """一个供应商 + 单节点形状映射；节点 = 本地 sink。"""
        app.route_provider_save({
            "name": "Sink-mixed",
            "base_url": "http://127.0.0.1:1",
            "token": self.PROVIDER_TOKEN,
            "model_nodes": [{"name": "sink", "url": f"http://127.0.0.1:{self.sink_port}",
                             "key": self.NODE_KEY}],
            "model_routes": routes,
            "set_current": True,
        })

    def _ping(self, model):
        """一发不打转的 Claude 请求：返回 (错误或 None, 原始响应或 None)。"""
        raw, tag = self._single_request(model)
        return (tag, None) if tag.startswith("error") else (None, raw)


class NodeKeyOverrideTests(_SinkProviderMixin):
    """#15 回归：路由命中节点时出站凭据必须是节点 key，替身 token 不得外泄。

    anthropic 腿的 header 形状（x-api-key vs Bearer）由供应商级策略推断决定，
    这里不与形状强绑，只断言凭据**值**：节点 key 到场、假 token 消失。
    """

    ROUTES = {
        "mimo-x": {"node": "sink", "format": "openai_chat"},
        "mini-x": {"node": "sink", "format": "anthropic"},
        "gmi-x": {"node": "sink", "format": "gemini_native"},
    }
    CRED_HEADERS = ("authorization", "x-api-key", "x-goog-api-key")

    def test_every_routed_leg_carries_the_node_key(self):
        self._save_mixed_provider(self.ROUTES)
        self.start_route()
        for model in self.ROUTES:
            self._ping(model)
        self.assertEqual(
            [item["path"] for item in self.captured],
            ["/v1/chat/completions", "/v1/messages", "/v1beta/models/gmi-x:generateContent"],
            "三条腿都应命中各自格式对应的路径",
        )
        for item in self.captured:
            headers = item["headers"]
            values = {headers.get(h) for h in self.CRED_HEADERS} - {None}
            self.assertEqual(
                values,
                {f"Bearer {self.NODE_KEY}"},
                f"{item['path']} 的出站凭据应为节点 key: {headers}",
            )


class ResponseTransformTests(_SinkProviderMixin):
    """#16 回归：多类型供应商的响应必须按 per-model 格式转回 Anthropic。

    供应商级格式是 vendor 默认（anthropic）曾把整条流降级成透传——客户端会
    直接收到上游 openai 回复。此处用 sink 回放标准 openai chat 回复，断言
    守护进程把消息转回 Anthropic 形状；anthropic 腿则直通保形。
    """

    ROUTES = {
        "mimo-x": {"node": "sink", "format": "openai_chat"},
        "mini-x": {"node": "sink", "format": "anthropic"},
    }
    OPENAI_REPLY = {
        "id": "chatcmpl-sink", "object": "chat.completion", "created": 0,
        "model": "mimo-x",
        "choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "pong-sink"},
        }],
    }
    ANTHROPIC_REPLY = {
        "id": "msg-sink", "type": "message", "role": "assistant",
        "model": "mini-x",
        "content": [{"type": "text", "text": "pass-through-sink"}],
        "stop_reason": "end_turn",
    }

    def _sink_payload(self, request):
        reply = self.OPENAI_REPLY if "chat" in request.path else self.ANTHROPIC_REPLY
        return 200, json.dumps(reply).encode("utf-8")

    def test_openai_chat_reply_is_converted_back_to_anthropic(self):
        self._save_mixed_provider(self.ROUTES)
        self.start_route()

        error, raw = self._ping("mimo-x")
        self.assertIsNone(error, "openai_chat 腿不应报错")
        body = self.assert_anthropic_reply(raw, "sink 转换")
        self.assertIn("pong-sink", _reply_text(body), "转换后的文本应命中 sink 回放的内容")

    def test_anthropic_route_passes_through_unchanged(self):
        self._save_mixed_provider(self.ROUTES)
        self.start_route()

        error, raw = self._ping("mini-x")
        self.assertIsNone(error, "anthropic 腿不应报错")
        body = self.assert_anthropic_reply(raw, "sink 直通")
        self.assertIn("pass-through-sink", _reply_text(body), "anthropic 腿应保形直通")


if __name__ == "__main__":
    unittest.main()

# claude-gearshift — test report

> **Status:** every defect below is fixed — #1–#8 (`91c28a5`), #9–#12
> (`a03bb31`), #14–#16 (routing patch + rebuilt binary, 2026-10-22) — and
> the suite passes with zero expected failures (see "Recommendations").
> History: "Second audit" (2026-10-21) found #9–#12; "Third audit"
> (2026-10-22, node-type suite against the real opencode gateway, 单类型 /
> 多类型) found #14–#16. #13 is documented behaviour, not a defect.

Date: 2026-10-20
Scope: `linux_switch/` (the Flask Web UI, `dd-switch/`) + the local-routing
path through the real `cc-switch-proxy` binary against a real upstream.

## What was run

```bash
cd linux_switch/dd-switch
.venv/bin/python -m unittest discover -s test_suite
```

| Suite | Tests | Result |
|---|---|---|
| `test_proxy_route.py` (mocked routing engine) | 39 | pass |
| `test_regressions.py` (defect guards) | 35 | pass (zero expected failures) |
| `test_i18n.py` | 5 | pass |
| `test_local_routing_e2e.py` (real binary + real upstream) | 25 | pass |
| `test_local_routing_opencode_e2e.py` (real opencode gateway; 2 live + 3 offline sink) | 5 | pass |
| **total** | **109** | **OK** |

The 2026-10-23 counting run: **109/109 OK** in one sweep (~2 min; the live E2E
legs dominate the wall time). Network-dependent suites skip cleanly (never
fail) when the opencode node config, the endpoint or the proxy binary is
unavailable; the new offline sink tests (`NodeKeyOverrideTests`,
`ResponseTransformTests`) run with **no** network access.

## Local routing — end-to-end verified

The README marked the local-routing feature "⏳ pending end-to-end verification".
That gap is now closed: `test_local_routing_e2e.py` drives the **real**
`cc-switch-proxy` binary and the **real** upstream from
`../../test_data/.testdata`.

Verified against the live upstream:

- **Protocol relay works.** An Anthropic `POST /v1/messages` is translated into
  an OpenAI-chat request upstream and the reply is translated back. The reply
  is a well-formed Anthropic message (`type: "message"`, `content` blocks of
  type `text`/`thinking`, `stop_reason`, `usage.input_tokens`/`output_tokens`).
- **Streaming works.** `stream: true` returns `text/event-stream` with the
  Anthropic event sequence: `message_start` → `content_block_start` →
  `content_block_delta` → `content_block_stop` → `message_delta` →
  `message_stop`. Every `data:` line is valid JSON with an Anthropic `type`.
- **Tier aliases map correctly.** The client asks for the stable alias
  `claude-sonnet-5`; the reply reports the upstream's real model
  (`mimo-v2.6-flash`), i.e. the alias→provider mapping is applied.
- **Provider CRUD works** against the real binary: `add` / `list` / `update` /
  `set-current` / `delete`, with `meta.apiFormat` persisted and reported.
- **The global outbound-proxy setting works**: `proxy get` / `set` / `clear`,
  and an empty URL clears it.
- **Switching the current provider takes effect without a restart.**
- **The full dd-switch lifecycle works**: `route_start` → `route_apply` →
  traffic through the gateway → `route_stop` restores the original
  `~/.claude/settings.json` byte for byte, including removing managed keys
  that were absent before, and the state file is written with mode 0600 and
  removed on stop.

### Two configuration traps worth documenting

1. **`base_url` must be the endpoint *minus* its `/v1/chat/completions`
   suffix.** The proxy appends that path itself. Passing the full endpoint
   produces `…/v1/chat/completions/v1/chat/completions`, which the upstream
   rejects with an empty `upstream_error`. The E2E suite strips the suffix
   automatically — and since `a03bb31` the Web UI normalises on save too
   (see #12), so pasting the full endpoint URL is harmless.
2. **The upstream needs an `x-opencode-session` header.** The proxy handles
   this itself (it detects `opencode.ai` and synthesises a stable session id),
   so no manual step is needed — good, but it means the upstream is only
   reachable through the proxy, not through a plain `curl`.

## Defects found

Each is pinned by a test in `test_regressions.py` marked
`@unittest.expectedFailure`. When a defect is fixed, the test reports an
*unexpected success* — that is the signal to drop the marker.

### 1. Path traversal in `/api/switch` — **high**

`api_switch` builds `CONFIG_DIR / name` from an unchecked user-supplied name
(`app.py:800`). A name containing `..` or an absolute path escapes the config
directory, so **any JSON file the process can read** is merged into
`~/.claude/settings.json` and echoed back to the caller.

Verified: `POST /api/switch {"name": "../secret.json"}` and
`{"name": "/tmp/…/secret.json"}` both return 200. The response's
`other_fields` are returned **unmasked** — a file containing
`{"sshPrivateKey": "-----BEGIN OPENSSH PRIVATE KEY-----"}` leaks verbatim into
the HTTP response, and its `env` block is written into the live settings.

The sibling endpoint `POST /api/configs` *does* sanitise the name (rejects `/`
and `\`), so the gap is inconsistent within the same file.

### 2. `/api/configs/<name>` PUT/DELETE on a directory → 500 — **medium**

`PUT` and `DELETE` on `/api/configs/..` raise unhandled exceptions
(`IsADirectoryError` / `OSError: Device or resource busy`) and return a 500
with a stack trace. `GET` on the same path correctly returns 404 (it checks
`is_file()`), so only the mutating verbs are affected.

### 3. Switching to a config without `env` silently deletes the live env — **medium**

`do_switch` only writes `merged["env"]` when the selected config has a
non-empty `env` (`app.py:190-191`). A config with no `env` (or `"env": {}`)
therefore leaves the merged settings **without an `env` key at all** — the
user's active `ANTHROPIC_AUTH_TOKEN` and every other env var disappear from
`~/.claude/settings.json`, breaking the running Claude Code session.

This contradicts the documented semantics ("replaces only `env`" — replacing
with *nothing* is not the same as replacing with the config's env).

### 4. The remote settings endpoint returns unmasked credentials — **medium**

`GET /api/servers/<name>/current` (`app.py:1110`) returns `env` verbatim,
including `ANTHROPIC_AUTH_TOKEN`. The local `GET /api/current` masks the same
values server-side via `_mask_secret_env`, and the README advertises that
"API tokens shown in the UI are truncated … never rendered in full".

The frontend only truncates `ANTHROPIC_AUTH_TOKEN` *for display*, so the real
token still travels to the browser in the JSON response — and any other secret
key (`ANTHROPIC_API_KEY`, `*_SECRET`, `*_PASSWORD`) is displayed in full.

### 5. `POST /api/servers` with a non-numeric port → 500 — **medium**

`int(port)` (`app.py:1014`) is not guarded, so `{"port": "abc"}` raises
`ValueError` and returns an unhandled 500 instead of a 400.

### 6. Deleting an unknown server reports success — **low**

`DELETE /api/servers/ghost` returns `200 {"message": "已删除"}` — the name is
never checked for existence, so the UI reports a deletion that did not happen.

### 7. `DD_SWITCH_ROUTE_PORT` is ignored by the gateway URL and the health probe — **high**

`ROUTE_PORT` is configurable (`DD_SWITCH_ROUTE_PORT`), but `ROUTE_BASE_URL` is
hardcoded to `http://127.0.0.1:9301` (`app.py:211`). Two consequences:

- The readiness probe (`_route_ready`, `app.py:349`) polls port 9301 while the
  daemon listens on the configured port, so **routing can never start** with a
  non-default port — `route_start` always fails with a startup timeout.
  Reproduced against the real binary: `ROUTE_PORT=19601` → `route_start
  FAILED: cc-switch-proxy 启动超时: <urlopen error [Errno 111] Connection
  refused>`.
- `route_apply` writes `ANTHROPIC_BASE_URL=http://127.0.0.1:9301` into
  `~/.claude/settings.json`, pointing Claude Code at the wrong gateway.

The deployment happens to use the default port, so this is latent there — but
the port is documented as configurable, so any other value breaks routing.

### 8. The spawned proxy process is never reaped — **low**

`_start_route_process` returns `proc.pid` and drops the `Popen` object
(`app.py:314-320`), so the child is never `wait()`ed and stays a zombie.
Measured: one zombie remains after each start/stop cycle. The count does not
grow without bound (Python reaps older children when a new `Popen` is
created), but the process is left in an unreaped state for the lifetime of the
daemon.

## Second audit — four more defects (2026-10-21)

A follow-up audit on the fixed code. All pinned with `@unittest.expectedFailure`
in `test_regressions.py`; a fix makes the corresponding test report an
*unexpected success*. (All four were fixed the same day — commit `a03bb31`;
the markers are dropped and the tests are plain regression guards now.)

### 9. Same-second backups overwrite each other — **medium**

The backup name is `settings.json.bak.<%Y%m%d_%H%M%S>` (second granularity).
Two switches within the same second copy to the same name: the second copy
overwrites the first and the **original** settings are lost — the exact
situation a backup exists for. Same pattern in three places: the switch
backup (`app.py:577-579`), the routing backup (`app.py:204-205`) and the
remote backup (`ssh_manager.py:157-159`).

Repro: mock `time.strftime` to a constant, switch to config A then config B
— a single backup file remains and no backup holds the pre-A snapshot.

### 10. The login password is compared with plain `==` — **low (hardening)**

`app.py:728` uses `password == ACCESS_PASSWORD`, leaking password
prefix/length through timing. `hmac.compare_digest` is the standard fix.

### 11. Expired tokens are purged only when their own request arrives — **low**

`app.py:727-730` adds a token per login; `_check_token` (`app.py:48-56`)
deletes an expired token only when that token is *presented*. Tokens whose
browser tab was closed or re-issued are never swept, so the in-memory store
grows without bound in the long-running waitress process.

### 12. The provider base_url is stored verbatim — **medium (local routing)**

`route_provider_save` (`app.py:473-482`) passes `base_url` through to
`provider add` unchanged. The proxy appends `/v1/chat/completions` itself,
so a user pasting the full endpoint — the natural copy-paste from a provider
page — produces `…/v1/chat/completions/v1/chat/completions`, which the
upstream rejects with an empty `upstream_error` (reconfirmed live against
the real binary). Fixed in `a03bb31`: the save path strips the known suffix
— now the shared `_strip_chat_suffix` helper, also applied to node URLs and
provider import.

## Third audit — the multi-protocol node suite (2026-10-22)

New suite `dd-switch/test_suite/test_local_routing_opencode_e2e.py` drives the
**real opencode gateway** (bases obtained from the opencode docs' Endpoints
table, the same source the `deploy/lite_llm` model-sync scripts parse — no
LiteLLM in the loop):

| shape | upstream path |
|---|---|
| `openai_chat` | `{base}/v1/chat/completions` |
| `openai_responses` | `{base}/v1/responses` |
| `anthropic` | `{base}/v1/messages` |
| `gemini_native` | `{base}/models/{model}:generateContent` |

Live results on the fixed binary: all four **single-type**
providers answer Claude-Code traffic, and one **mixed-type** provider (its own
access point deliberately dead, junk token, node table carrying the real
endpoints) dispatches four models onto four different wire protocols. Region
note: some opencode Responses-group models (gpt-6-luna, muse-spark-…) are
geo-locked and need an egress proxy — the suite uses `grok-4.7`, which speaks
`/responses` direct. That is upstream model policy, not a routing defect.

### 14. Anthropic passthrough panics the headless proxy — **high (routing)**

Symptom: a provider with `api_format=anthropic` makes the daemon close the
client connection without any response (`RemoteDisconnected`). The route log
shows a rustls panic:

    Could not automatically determine the process-level CryptoProvider ...

Root cause: the GUI app installs the process-level rustls CryptoProvider once
at startup (`cc-switch/src-tauri/src/lib.rs:466`). The headless
`cc-switch-proxy` bin added by the patch never runs that setup, and the
anthropic-format relay is the one production path that builds a rustls client
— first use panics the tokio worker and the connection dies bare. (The other
three `install_default` calls in `forwarder.rs` are `#[cfg(test)]` helpers.)

Fix (2026-10-22): the patch installs the provider at daemon start (bin
`main`, mirroring `lib.rs:466`, defensive `let _`); `patches/bin/…` rebuilt.
Also repaired while landing this: the patch Makefile's checkout probe broke
with the cc-switch submodule move (now `git rev-parse --git-dir`, which
handles plain checkouts and submodule gitfiles alike).

### 15. Per-node keys never reached the wire — **high (routing)**

Symptom: the mixed-type live leg answered **401** `Upstream request failed:
Invalid credential` — and an offline HTTP sink proved the daemon sent the
**junk provider token** (`Bearer sk-unused…`) upstream, not the route's node
key. Route hit itself was fine: the request did land on the node URL with the
per-model wire format; only the credential was wrong.

Root cause ×2 in `forwarder.rs` (all introduced by the node-routing patch):
the key-override block was gated on
`auth.strategy == AuthStrategy::Anthropic`, while `extract_auth` derives the
strategy from the **provider-level** api format (`openai_chat`/`openai_responses`
map to `Bearer`, gemini-like to the Google strategies) — so the override
almost never fired; and the gemini leg looked the model up from
`filtered_body`, whose outbound gemini body carries the model in the **URL**,
not the body — the lookup got `None`.

Fix (2026-10-22): the override now applies to every *static-key* strategy
(`Anthropic | ClaudeAuth | Bearer | Google | GoogleOAuth`; dynamic OAuth
managers keep their placeholders), and the model is captured into
`routed_model` from `mapped_body` *before* the format conversion moves
`mapped_body` (an E0382 tried otherwise). Offline-sink regression
`NodeKeyOverrideTests` pins: all three wire shapes carry the node key, the
junk token never leaks.

### 16. Multi-type responses fell back to raw pass-through — **high (routing)**

Symptom: after #15, the live mixed leg returned the upstream **chat.completion
unmodified** to the client — conversion simply never ran; Cloudflare's
upstream headers rode along.

Root cause in `handlers.rs`: the transform decision is
`adapter.needs_transform(provider)` which reads the **provider-level** api
format only; a multi-type provider falls back to `"anthropic"`, so
`needs_transform == false` and the handler took the pass-through arm while the
request side was happily converted by the per-model route.

Fix (2026-10-22) ×2: `ClaudeAdapter::needs_transform` (`claude.rs`) now enters
the transform handler for any provider declaring `settings_config.modelRoutes`;
and the transform arm itself understands a per-model anthropic route —
`(None, Some(response)) if api_format == "anthropic"` returns the response
unchanged (before, an anthropic reply would have been fed to
`openai_to_anthropic`). Sink regression `ResponseTransformTests` pins both:
the openai_chat reply is converted back to an Anthropic message and the
anthropic route passes through unchanged.

### Cloudflare 403s are a header fingerprint, not routing — **note**

While hunting #15/#16 the daemon's direct calls all received 403 challenge
pages from opencode.ai's edge, while curl succeeded immediately. The daemon
forwards the client's `User-Agent`; a browser-less UA (`Python-urllib/3.13`)
gets challenged, `curl/8.5.0` passes 4/4, and the normal `claude-cli/…` UA the
suite uses passed every live leg in the final 5/5 run. Environment behaviour
of the upstream edge, not a routing defect — only worth re-checking if future
clients send unusual UAs.

## Notes / non-defects

- **The `X-Forwarded-Prefix` open redirect.** `Location: //evil.example.com/login`
  is derivable from that header. This is a documented nginx-deployment feature
  (the header is meant to come from the trusted reverse proxy), so it is only
  exploitable if the Web UI is reachable directly. Recorded here, not pinned as
  a defect.
- **`i18n` parity is clean.** All 138 keys exist in both `zh` and `en`; every
  key referenced by the templates and by `ERROR_MAP` resolves in both locales.
  `test_i18n.py` keeps it that way.
- **Remote scan reads `~/config`.** Deriving the remote provider dir from
  `~/.claude/settings.json` yields `~/config` (`ssh_manager.py:195`), which no
  doc mentions. `RemoteScanBehaviorTests` pins the behaviour as-is; treat any
  future change as a deliberate convention change.

## Recommendations

All defects listed above are fixed — #1–#8 in `91c28a5`, #9–#12 in `a03bb31`,
#14–#16 in the node-routing patch (2026-10-22, `patches/bin/…` rebuilt and
sink-verified) — and every `expectedFailure` marker has been dropped (the
tests are plain regression guards). Configuration trap 1 is also stripped
automatically now, both on provider save (#12) and on import. Nothing
outstanding.

## Reproducing

```bash
cd linux_switch/dd-switch
.venv/bin/python -m unittest discover -s test_suite                                             # whole suite
.venv/bin/python -m unittest test_suite.test_local_routing_opencode_e2e -v                      # multi-protocol node suite
.venv/bin/python -m unittest test_suite.test_local_routing_opencode_e2e.NodeKeyOverrideTests     # offline #15 regression (tin sink)
.venv/bin/python -m unittest test_suite.test_local_routing_opencode_e2e.ResponseTransformTests   # offline #16 regression (local sink)
.venv/bin/python -m unittest test_suite.test_local_routing_e2e -v      # E2E only
.venv/bin/python -m unittest test_suite.test_regressions -v            # defects only
```

The E2E suite needs `test_data/.testdata` (endpoint + key), the prebuilt proxy
binary at `patches/bin/cc-switch-proxy-linux-x86_64`, and the WebKitGTK
runtime libs on `LD_LIBRARY_PATH`. Missing any of them ⇒ skip, not fail.

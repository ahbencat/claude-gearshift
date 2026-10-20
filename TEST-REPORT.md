# claude-gearshift — test report

> **Follow-up (2026-10-20):** all 8 defects below are fixed (commit
> `91c28a5`); the 11 `expectedFailure` markers in `test_regressions.py`
> are dropped and the whole suite (81 tests) passes with zero expected
> failures.

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
| `test_proxy_route.py` (pre-existing, fully mocked) | 23 | pass |
| `test_regressions.py` (new) | 30 | pass (11 `expectedFailure` pinning known defects) |
| `test_i18n.py` (new) | 5 | pass |
| `test_local_routing_e2e.py` (new, real binary + real upstream) | 23 | pass |
| **total** | **81** | **OK (expected failures=11)** |

Run three times in a row: green every time (48–92 s per run, mostly upstream
latency). The E2E suite skips cleanly (23 skips, 0 failures) when the endpoint
or the proxy binary is unavailable.

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
   automatically — but the Web UI has no such normalisation, so a user pasting
   the endpoint URL gets an opaque failure.
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

## Notes / non-defects

- **The `X-Forwarded-Prefix` open redirect.** `Location: //evil.example.com/login`
  is derivable from that header. This is a documented nginx-deployment feature
  (the header is meant to come from the trusted reverse proxy), so it is only
  exploitable if the Web UI is reachable directly. Recorded here, not pinned as
  a defect.
- **`i18n` parity is clean.** All 138 keys exist in both `zh` and `en`; every
  key referenced by the templates and by `ERROR_MAP` resolves in both locales.
  `test_i18n.py` keeps it that way.

## Recommendations

1. Fix #1 first — sanitise the config name in `api_switch` the same way
   `api_config_create` already does (reject `/`, `\`, and any name that does
   not resolve inside `CONFIG_DIR`).
2. Fix #7 by deriving `ROUTE_BASE_URL` from `ROUTE_PORT`
   (`f"http://127.0.0.1:{ROUTE_PORT}"`), which also removes the hardcoded port.
3. Fix #4 by applying `_mask_secret_env` to the remote `env` as well.
4. Fix #3 by always writing an `env` key (the selected config's env, even when
   empty) instead of dropping it.
5. Turn the remaining `expectedFailure` markers into plain assertions as each
   defect is fixed.

## Reproducing

```bash
cd linux_switch/dd-switch
.venv/bin/python -m unittest discover -s test_suite                    # whole suite
.venv/bin/python -m unittest test_suite.test_local_routing_e2e -v      # E2E only
.venv/bin/python -m unittest test_suite.test_regressions -v            # defects only
```

The E2E suite needs `test_data/.testdata` (endpoint + key), the prebuilt proxy
binary at `patches/bin/cc-switch-proxy-linux-x86_64`, and the WebKitGTK
runtime libs on `LD_LIBRARY_PATH`. Missing any of them ⇒ skip, not fail.

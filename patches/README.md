# patches/

Changes we maintain **on top of an unmodified `cc-switch` checkout** (a git
submodule of upstream, kept pristine). Never edit the submodule contents in
place and never commit our changes into it — carry them in the patch here.

## `cc-switch-headless-proxy.patch`

Adds a headless Linux daemon, `cc-switch-proxy`, that runs cc-switch's existing
Rust local-routing engine (`src-tauri/src/proxy/`) **without the Tauri window or
tray**, reading providers/models from cc-switch's SQLite DB. This is the piece
that lets `linux_switch` spawn the Rust router instead of LiteLLM.

- Applies to cc-switch `479964f2` (v4.0.10). Touches:
  - `src-tauri/Cargo.toml` — enables tokio's `signal` feature
  - `src-tauri/Cargo.lock` — records that feature (adds `signal-hook-registry` to tokio's deps)
  - `src-tauri/src/database/mod.rs` — splits `Database::init()` into `init()` + `init_at(path)`
  - `src-tauri/src/lib.rs` — declares `pub mod headless;`, `pub mod provider_cli;`, `pub mod proxy_cli;`
  - `src-tauri/src/headless.rs` — new module: arg parsing, DB open, proxy start/stop
  - `src-tauri/src/provider_cli.rs` — new module: `provider` subcommands (DAO-backed)
  - `src-tauri/src/proxy_cli.rs` — new module: `proxy` subcommand (global outbound proxy, DAO-backed)
  - `src-tauri/src/bin/cc-switch-proxy.rs` — new binary entrypoint
  - Native engine (per-model transport, see below): `src-tauri/src/proxy/model_route.rs`
    (new), `src-tauri/src/proxy/mod.rs` (module decl), `src-tauri/src/proxy/forwarder.rs`
    (url/key/format overrides), `src-tauri/src/proxy/handlers.rs` (anthropic
    passthrough arm), `src-tauri/src/proxy/providers/claude.rs` (`needs_transform` gate)

### Apply

`cc-switch` is a **git submodule** of this repository, pinned to the commit
the patch targets (`479964f2`, v4.0.10). Check it out and apply there:

```bash
git submodule update --init          # or: git submodule update
cd cc-switch
git apply /path/to/patches/cc-switch-headless-proxy.patch
```

`patches/Makefile` does this automatically for a build: its `CC_SWITCH_DIR`
defaults to `../cc-switch` (the submodule); override with
`make binary CC_SWITCH_DIR=/path/to/cc-switch` when you want to build from a
different checkout.

Verify it applies first with `git apply --check <patch>`.

### Build & run

From this directory, **`make binary`** does the whole rebuild in one shot —
applies the patch in a throwaway git worktree, release-builds, strips, and
installs the result to `bin/` (the cc-switch tree is left untouched). Point it at
another checkout with `make binary CC_SWITCH_DIR=/path/to/cc-switch`.

To build by hand instead (needs the Tauri Linux prerequisites — see
`../cc-switch/CONTRIBUTING.md`). The frontend `dist/` placeholder must exist even
for this backend-only binary:

```bash
mkdir -p dist
cargo build --release --bin cc-switch-proxy --manifest-path src-tauri/Cargo.toml
./src-tauri/target/release/cc-switch-proxy --help
# ./src-tauri/target/release/cc-switch-proxy --port 9301
```

Providers must be configured in cc-switch's DB (`~/.cc-switch/cc-switch.db`);
override with `--db <path>`. `RUST_LOG` sets the log level (default `info`).

### Prebuilt binary

`bin/cc-switch-proxy-linux-x86_64` is a stripped **release** build of the patched
binary (≈13 MB), so you can skip the Rust toolchain entirely:

- **Platform**: Linux x86_64, glibc.
- **Runtime**: still needs the WebKitGTK 4.1 / GTK 3 runtime libraries, because it
  links cc-switch's crate (`cc_switch_lib` → Tauri). On a minimal host the missing
  pieces are `libwebkit2gtk-4.1.so.0` and `libjavascriptcoregtk-4.1.so.0` plus their
  deps (`libgstgl-1.0`, `libwebpdemux`, `libharfbuzz-icu`, `libmanette-0.2`,
  `libenchant-2`, `libhyphen`, `libsecret-1`, and the mesa/egl/gbm + dictionaries
  packages). Install them with:
  `sudo apt-get install libwebkit2gtk-4.1-0` (pulls GTK 3, libsoup 3, and the rest).
  Without root, extract the runtime `.deb`s into a directory and point the daemon at
  them with `LD_LIBRARY_PATH` (the deployment under `~/deploy` keeps them in
  `runtime-libs/`):
  ```bash
  mkdir -p ~/deploy/runtime-libs && cd ~/deploy/runtime-libs
  apt-get download $(apt-get --print-uris -y install --no-install-recommends \
    libwebkit2gtk-4.1-0 | grep "^'" | awk -F"'" '{print $2}' | xargs -n1 basename | sed 's/_.*//')
  for f in *.deb; do dpkg-deb -x "$f" .; done && rm -f *.deb
  export LD_LIBRARY_PATH=$HOME/deploy/runtime-libs/usr/lib/x86_64-linux-gnu
  ```
- **Compile-time**: needs the full Tauri Linux prerequisites (WebKitGTK/GTK **dev**
  packages), because the proxy links the whole `cc_switch_lib` crate. This is a
  Tauri requirement, **not** OAuth. Root-free build recipe (the dev closure into
  a sysroot; **already-installed dev packages do not appear in the download
  closure**, so also let pkg-config/gcc fall back to the host after the sysroot):
  ```bash
  # 1. sysroot = dev-package closure, extracted without root
  mkdir -p ~/.cache/cc-switch-proxy-sysroot && cd $_
  apt-get download $(apt-get --print-uris -y install --no-install-recommends \
    libwebkit2gtk-4.1-dev | grep "^'" | awk -F"'" '{print $2}' | xargs -n1 basename | sed 's/_.*//')
  for f in *.deb; do dpkg-deb -x "$f" .; done && rm -f *.deb

  # 2. fix the download-closure gap: dev packages' `.so` symlinks point to
  #    runtime `.so.NN` files whose packages are absent (already-installed dev
  #    packages do not appear in the closure); resolve them by downloading the
  #    packages that own the missing targets
  find $S/usr/lib -xtype l -print0 | while IFS= read -r -d '' l; do
    tgt=$(basename "$(readlink -f "$l" 2>/dev/null || echo $l/__)")
    path=/usr/lib/x86_64-linux-gnu/$tgt
    if [ ! -e "$S/usr/lib/x86_64-linux-gnu/$tgt" ]; then
      dpkg-query -S "$path" 2>/dev/null | head -1 | awk -F: '{print $1}'
    fi
  done | sort -u > /tmp/rt-pkgs.txt
  cd $S && apt-get download $(cat /tmp/rt-pkgs.txt) && for f in *.deb; do dpkg-deb -x "$f" .; done && rm -f *.deb

  # 3. build env: sysroot first, host dirs as fallback gaps are filled
  export S=~/.cache/cc-switch-proxy-sysroot
  export PKG_CONFIG_SYSROOT_DIR=$S
  export PKG_CONFIG_LIBDIR=$S/usr/lib/x86_64-linux-gnu/pkgconfig:$S/usr/lib/pkgconfig:$S/usr/share/pkgconfig:/usr/lib/x86_64-linux-gnu/pkgconfig:/usr/lib/pkgconfig:/usr/share/pkgconfig
  export CPATH=$S/usr/include:$S/usr/include/x86_64-linux-gnu:/usr/include:/usr/include/x86_64-linux-gnu
  export LIBRARY_PATH=$S/usr/lib/x86_64-linux-gnu:$S/usr/lib:/usr/lib/x86_64-linux-gnu:/usr/lib
  export LD_LIBRARY_PATH=$S/usr/lib/x86_64-linux-gnu:$S/usr/lib:/usr/lib/x86_64-linux-gnu:/usr/lib
  # Rust ≥1.95 defaults to rust-lld through a gcc-ld shim that drops -L args;
  # switch back to the GNU linker, which honors LIBRARY_PATH
  export RUSTFLAGS="-C linker-features=-lld"
  ```
- **OAuth**: login-based providers (GitHub Copilot, ChatGPT/Codex, xAI) need the
  cc-switch **desktop** app — the headless daemon has no `AppHandle`, so only
  API-key providers (Anthropic, OpenAI-compatible, Gemini, OpenRouter, …) work.
- **License**: it is a build of cc-switch (MIT © 2025 Jason Young); see
  [`LICENSE.cc-switch`](LICENSE.cc-switch). Redistribution must keep that notice.
- **Keep it in sync**: the binary is a build of the patch at the pinned commit.
  When the patch or the cc-switch version changes, rebuild and replace it with
  `make binary`; do not ship a stale binary.

```bash
install -m755 bin/cc-switch-proxy-linux-x86_64 ~/.local/bin/cc-switch-proxy
cc-switch-proxy --help
# point dd-switch at it:
#   DD_SWITCH_ROUTE_PROXY_BIN=~/.local/bin/cc-switch-proxy
```

### Provider subcommands

`cc-switch-proxy provider …` reads/writes the DB's `providers` table through
cc-switch's own DAO (no desktop app, no hand-written SQL), so the schema and
migrations always match. Every command prints one line of JSON to stdout.

```bash
cc-switch-proxy provider list --db ~/.cc-switch/cc-switch.db
cc-switch-proxy provider add --name "Relay" --base-url https://api.example.com \
    --token sk-... --sonnet claude-sonnet-5 --haiku claude-haiku-5-5 \
    --opus claude-opus-5 --api-format openai_chat --set-current
cc-switch-proxy provider update --id relay --token sk-new
cc-switch-proxy provider set-current --id relay
cc-switch-proxy provider delete --id relay
cc-switch-proxy provider import --dir ./config --set-current   # skips sample.json
```

`import` treats each `config/*.json` as a Claude provider: the file's
`{"env": {…}}` becomes the provider's `settingsConfig`. `--set-current` picks a
provider so the router has something to forward to.

`--api-format` declares the upstream protocol and is stored as the provider's
`meta.apiFormat` — the key the proxy reads to decide protocol conversion
(`anthropic` = passthrough, the default when omitted; `openai_chat`,
`openai_responses`, `gemini_native` trigger conversion). Unknown values are
rejected. `update` without the flag keeps the stored value. A top-level
`"api_format"` in an imported `config/*.json` is honored the same way (legacy
path). `list` reports it as `apiFormat` (absent on rows written by older
binaries).

**Per-model transport (`--model-nodes` / `--model-routes`)** — for upstreams
that expose more than one API shape (opencode-style: `/v1/messages` and
`/v1/responses` both live). Both flags take JSON and are stored inside the
provider's `settings_config` alongside `env`; an empty string clears the table;
`update` without the flags keeps the stored ones:

```bash
cc-switch-proxy provider update --id relay \
  --model-nodes '[{"name":"a","url":"https://a.example","key":"sk-a"},{"name":"b","key":"sk-b"}]' \
  --model-routes '{"mimo-v2.6":{"node":"a","format":"openai_chat"},"deepseek-v4":{"format":"openai_responses"}}'
```

- `modelNodes` — `{name, url?, key?}`; `url`+`key` = an independent access
  point, key-only = a multi-key supplement; omitted fields fall back to the
  provider default (`env.ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`).
- `modelRoutes` — `{model: {node?, format?}}`; at least one of the two per row;
  `format` shares the `--api-format` value domain; a model key must NOT carry
  the `[1m]` suffix (the proxy strips it before matching).
- The forwarder consults the tables per request: a matching model routes to its
  node's URL/key and API shape; misses use the provider default. `list` returns
  both tables inside `settingsConfig` (dd-switch masks node keys as `***`).
- `update` preserves unknown top-level keys of the existing
  `settings_config` (only `env` is rebuilt), so tables and legacy fields
  survive partial updates.

### Native engine changes (原生路由引擎改动)

> ⭐ **Core selling point of this project** — upstream cc-switch routes at
> whole-provider granularity (one protocol per provider); this patch
> dispatches url/key/protocol **per model, per request** inside a single
> provider. If you take one thing away from this file, take this section.

The CLI surface above is only half the patch — the other half teaches
cc-switch's existing Rust router to honor the per-model tables at request
time. All native changes live under `src-tauri/src/proxy/`; the headless
daemon reuses them untouched (no forked forwarding logic):

- **`proxy/model_route.rs` (new, ~170 lines + 5 unit tests)** — the single
  lookup for `settings_config.modelRoutes` / `modelNodes`. `lookup(provider,
  model)` strips the `[1m]` context marker before matching (same helper the
  forwarder already uses, so table keys are always the stripped names),
  resolves `{node, format}`, and returns `{api_format, base_url, api_key}`
  with per-field fallback to the provider default. `has_routes(provider)`
  reports whether a provider declares a route table at all. Unknown formats,
  blank url/key strings, and missing tables all degrade to `None` (= legacy
  single-protocol path). `MODEL_ROUTE_FORMATS` mirrors the `--api-format`
  value domain (`anthropic` / `openai_chat` / `openai_responses` /
  `gemini_native`).
- **`proxy/forwarder.rs` (3 hooks, one shared `lookup`)** — per request, after
  the model name is captured once into `routed_model` (shared by the url and
  key hooks so both see the same name): ① base-url override for the Claude
  adapter (node url replaces `base_url`, `isFullUrl` providers exempt); ②
  api-key override for static-key strategies only (dynamic OAuth managers —
  Copilot/Codex/xAI/GeminiCli — keep their tokens); ③
  `resolve_claude_api_format` checks the route table first, so a routed
  model's `format` wins over the provider-level protocol.
- **`proxy/handlers.rs` (1 arm)** — a per-model route that resolves back to
  `anthropic` already carries an Anthropic response; the new match arm returns
  it directly instead of falling into the `openai_to_anthropic` fallback.
- **`proxy/providers/claude.rs` (`needs_transform` gate)** — a provider that
  declares `modelRoutes` is per-model by definition, so its provider-level
  `anthropic` default must not downgrade the whole stream to passthrough: the
  gate is now `has_routes(provider) || <legacy format check>`, sending mixed
  providers through the unified response path where the per-model `format`
  decides per request.

Net effect: three usage shapes collapse onto one table — full url+key node =
independent access point, key-only node = multi-key supplement, format-only
route = protocol switch — with misses falling back to the provider default.
`cargo test --lib model_route` covers the lookup matrix; the live matrix
(dead-url no-fallback, format-only recovery, path selection) is exercised by
`dd-switch/test_suite/test_local_routing_opencode_e2e.py`.

### Global outbound proxy subcommand

Local routing is only worth running for two reasons: **① protocol relay**
(upstream speaks OpenAI/Gemini style) and **② outbound proxy** (reaching the
upstream requires an HTTP/HTTPS/SOCKS5 network proxy). The `proxy` subcommand
serves case ② on a headless host, by writing the same `global_proxy_url`
setting the desktop app's "全局代理" uses:

```bash
cc-switch-proxy proxy get
cc-switch-proxy proxy set --url http://proxy-host:7890   # http/https/socks5/socks5h
cc-switch-proxy proxy clear
```

Without an explicit setting the daemon's HTTP client follows
`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` (via reqwest's system-proxy logic),
with an anti-recursion guard that bypasses loopback proxies. Configured in the
DB, the setting wins over env vars. **The daemon reads it once at startup** —
restart `cc-switch-proxy` after `set`/`clear` for it to take effect
(dd-switch restarts the child whenever the feature is toggled off/on).

### Caveats

- **Verified** on Ubuntu 24.04 with Rust 1.95: `cargo build --bin cc-switch-proxy`
  links, `cargo test --lib headless provider_cli proxy_cli` passes (14 tests
  across the three subcommand modules), `cargo fmt`/`clippy -D warnings` are
  clean, the full `cargo test --lib` suite is green (3637 passed — the upstream
  codex timing test occasionally flaps on a heavily loaded box but passes
  standalone), the daemon boots
  (`/health` → 200, `/status` → running, clean SIGTERM shutdown), and both
  `provider add/list/update/set-current/delete/import` and `proxy
  get/set/clear` subcommands were exercised end-to-end against throwaway DBs.
- The binary still links Tauri/WebKitGTK runtime libraries (it shares the crate
  with the desktop app), so a headless host needs the WebKitGTK runtime installed
  to *run* it — and the full Tauri Linux build prerequisites to compile it.
- **OAuth providers are unusable headless (confirmed in code).** The daemon
  passes `None` as `AppHandle` (`ProxyServer::new(config, db.clone(), None)`),
  so the Copilot / CodexOAuth / XaiOAuth strategies hit their `else` arm and
  fail every request with `AuthError` (`forwarder.rs`: Copilot, Codex, XAI
  branches). GeminiCliOAuth degrades further: the key field must hold OAuth
  JSON with a *live* `access_token`, and no refresh path exists in either
  mode. Trap: such providers can still be added/imported and set current
  with no warning — the failure only surfaces as request-time 401/AuthError.
  Only static API-key providers (Anthropic, OpenAI-compatible, Gemini API,
  OpenRouter, …) work headless; plain Anthropic/OpenAI/Gemini routing is
  otherwise unaffected. Failover UI events are likewise unavailable.

### Regenerating

From a cc-switch checkout with the changes applied:

```bash
git add -A
git diff --cached > /path/to/linux_switch/patches/cc-switch-headless-proxy.patch
git reset
```

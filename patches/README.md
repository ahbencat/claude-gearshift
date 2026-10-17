# patches/

Changes we maintain **on top of an unmodified `cc-switch` checkout**. The
`cc-switch/` directory is a fork of upstream and is kept pristine — never edit
it in place, never commit our changes into it. Instead, apply the patch here.

## `cc-switch-headless-proxy.patch`

Adds a headless Linux daemon, `cc-switch-proxy`, that runs cc-switch's existing
Rust local-routing engine (`src-tauri/src/proxy/`) **without the Tauri window or
tray**, reading providers/models from cc-switch's SQLite DB. This is the piece
that lets `linux_switch` spawn the Rust router instead of LiteLLM.

- Applies to cc-switch `479964f2` (v4.0.10). Touches:
  - `src-tauri/Cargo.toml` — enables tokio's `signal` feature
  - `src-tauri/Cargo.lock` — records that feature (adds `signal-hook-registry` to tokio's deps)
  - `src-tauri/src/database/mod.rs` — splits `Database::init()` into `init()` + `init_at(path)`
  - `src-tauri/src/lib.rs` — declares `pub mod headless;` and `pub mod provider_cli;`
  - `src-tauri/src/headless.rs` — new module: arg parsing, DB open, proxy start/stop
  - `src-tauri/src/provider_cli.rs` — new module: `provider` subcommands (DAO-backed)
  - `src-tauri/src/bin/cc-switch-proxy.rs` — new binary entrypoint

### Apply

```bash
git clone https://github.com/farion1231/cc-switch.git
cd cc-switch
git checkout 479964f2            # or a compatible main
git apply /path/to/cc-switch-headless-proxy.patch
```

Verify it applies first with `git apply --check <patch>`.

### Build & run

From this directory, **`make binary`** does the whole rebuild in one shot —
applies the patch in a throwaway git worktree, release-builds, strips, and
installs the result to `bin/` (the cc-switch tree is left untouched). Point it at
another checkout with `make binary CC_SWITCH_DIR=/path/to/cc-switch`.

To build by hand instead (needs the Tauri Linux prerequisites — see
`cc-switch/CONTRIBUTING.md`). The frontend `dist/` placeholder must exist even
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
  links cc-switch's crate. Install them first, e.g. on Debian/Ubuntu:
  `sudo apt-get install libwebkit2gtk-4.1-0 libgtk-3-0 libsoup-3.0-0`.
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
    --opus claude-opus-5 --set-current
cc-switch-proxy provider update --id relay --token sk-new
cc-switch-proxy provider set-current --id relay
cc-switch-proxy provider delete --id relay
cc-switch-proxy provider import --dir ./config --set-current   # skips sample.json
```

`import` treats each `config/*.json` as a Claude provider: the file's
`{"env": {…}}` becomes the provider's `settingsConfig`. `--set-current` picks a
provider so the router has something to forward to.

### Caveats

- **Verified** on Ubuntu 24.04 with Rust 1.95: `cargo build --bin cc-switch-proxy`
  links, `cargo test --lib headless` and `cargo test --lib provider_cli` pass
  (8 tests total), `cargo fmt`/`clippy -D warnings` are clean, the daemon boots
  (`/health` → 200, `/status` → running, clean SIGTERM shutdown), and the
  `provider add/list/update/set-current/delete/import` subcommands were
  exercised end-to-end against a throwaway DB.
- The binary still links Tauri/WebKitGTK runtime libraries (it shares the crate
  with the desktop app), so a headless host needs the WebKitGTK runtime installed
  to *run* it — and the full Tauri Linux build prerequisites to compile it.
- No Tauri `AppHandle` is available, so failover UI events and Copilot/Codex/xAI
  OAuth state are unavailable; plain Anthropic/OpenAI/Gemini routing is unaffected.

### Regenerating

From a cc-switch checkout with the changes applied:

```bash
git add -A
git diff --cached > /path/to/linux_switch/patches/cc-switch-headless-proxy.patch
git reset
```

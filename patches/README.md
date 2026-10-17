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

Needs the Tauri Linux prerequisites (see `cc-switch/CONTRIBUTING.md`). The
frontend `dist/` placeholder must exist even for this backend-only binary:

```bash
mkdir -p dist
cargo build --release --bin cc-switch-proxy --manifest-path src-tauri/Cargo.toml
./src-tauri/target/release/cc-switch-proxy --help
# ./src-tauri/target/release/cc-switch-proxy --port 9301
```

Providers must be configured in cc-switch's DB (`~/.cc-switch/cc-switch.db`);
override with `--db <path>`. `RUST_LOG` sets the log level (default `info`).

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

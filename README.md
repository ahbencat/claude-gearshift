# claude-gearshift

**English | [简体中文](README.zh-CN.md)**

Shift gears for Claude Code — quickly switch model/provider configs on Linux while leaving every other setting untouched.

Two ways to use it, sharing the same `config/` directory and the same switch semantics (back up → replace `env` only → atomic write):

| Mode | Path | Description |
|---|---|---|
| CLI script | `switch_claude_config.sh` | Interactive menu, `bash` + `jq` |
| Web UI | `dd-switch/` | Flask + waitress, browser-based, password-protected |

## CLI Script Usage

```bash
# Reads configs from ./config/ by default
./switch_claude_config.sh

# Or point it at another config directory
./switch_claude_config.sh ~/.claude/configs/
```

## Web UI Usage

```bash
cd dd-switch
python3 app.py
# open http://localhost:10086
```

- Set the access password via the `DD_SWITCH_PASSWORD` env var (defaults to `admin`)
- Production mode serves via waitress; `DD_SWITCH_DEBUG=1` falls back to Flask's dev server
- Browse / create / edit / delete / switch configs under `config/` right from the page
- Same switch semantics as the CLI script — both can be used interchangeably

## Config Directory Structure

Drop one JSON file per provider into `config/`:

```
config/
├── cf_ark_177.json
├── cf_anthropic.json
├── cf_openrouter.json
└── cf_aws_bedrock.json
```

Each JSON only needs an `env` block, for example:

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "sk-ant-...",
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    "ANTHROPIC_MODEL": "claude-sonnet-4-20250514"
  }
}
```

More optional fields: [Claude Code environment variables](https://docs.anthropic.com/en/docs/claude-code/settings).

## Features

| Step | What it does |
|---|---|
| **Scan** | Walks the config directory for `.json` files |
| **Validate** | Syntax-checks each file with `jq`; invalid files are skipped with the reason |
| **Summarize** | Shows the current config's Base URL and Model |
| **Select** | Interactive menu to pick a config |
| **Preview** | Prints the full selected file before switching |
| **Confirm** | Switches only after explicit confirmation |
| **Back up** | Backs up the current config to `settings.json.bak.<timestamp>` |
| **Merge** | Replaces only `env`, preserving `theme` / `permissions` / `actions` / `skills` |
| **Atomic write** | Temp file + `mv rename` — no half-written JSON |

## Dependencies

- `bash` 4.0+ (`mapfile` support)
- `jq` (JSON validation & merge)

Install `jq`:

```bash
# Ubuntu/Debian
sudo apt install jq

# macOS
brew install jq

# Arch Linux
sudo pacman -S jq
```

## Workflow Example

```bash
# 1. Prepare configs
mkdir -p config
cp ~/.claude/settings.json config/cf_default.json
# edit or fetch additional configs

# 2. Switch
./switch_claude_config.sh
```

```
════════════════════════════════════════════
 JSON validation results
════════════════════════════════════════════

  ✅ Valid files: 3

════════════════════════════════════════════
 Current config
════════════════════════════════════════════
   Path: /home/user/settings.json
   Base URL: https://ark.cn-beijing.volces.com/api/coding
   Model:    deepseek-v4-flash

════════════════════════════════════════════
 Select a config file:
════════════════════════════════════════════

1) config/cf_anthropic.json
2) config/cf_ark_177.json
3) config/cf_openrouter.json

Enter a number (or 0 to quit):
```

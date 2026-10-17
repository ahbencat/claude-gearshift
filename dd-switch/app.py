#!/usr/bin/env python3
"""claude-gearshift Web UI: switch Claude Code configurations from the browser."""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from flask import Flask, jsonify, make_response, render_template, request, redirect, url_for
from functools import wraps

import ssh_manager

app = Flask(__name__)

# --- 密码验证 ---
ACCESS_PASSWORD = os.environ.get("DD_SWITCH_PASSWORD", "admin")

# 服务端 token 存储（不用 Flask session）
# { token -> {"login_time": timestamp} }
_token_store = {}
SESSION_MAX_AGE = 8 * 3600  # 8 小时


def _get_token():
    """从 cookie 读取 token"""
    return request.cookies.get("dd_token", "")


def _set_token_cookie(resp, token, max_age=None):
    resp.set_cookie("dd_token", token, httponly=True, samesite="Lax",
                    max_age=max_age or SESSION_MAX_AGE)


def _clear_token_cookie(resp):
    resp.set_cookie("dd_token", "", max_age=0, expires=0, httponly=True, samesite="Lax")


def _check_token():
    """返回 True 表示已登录，False 表示需要登录"""
    token = _get_token()
    if not token or token not in _token_store:
        return False
    login_time = _token_store[token].get("login_time", 0)
    if time.time() - login_time > SESSION_MAX_AGE:
        del _token_store[token]
        return False
    return True


def _url_prefix():
    """从 nginx 的 X-Forwarded-Prefix 获取路径前缀"""
    return request.headers.get("X-Forwarded-Prefix", "")


def _login_url():
    """生成登录页 URL（带前缀）"""
    return _url_prefix() + "/login"


def _index_url():
    """生成首页 URL（带前缀）"""
    return _url_prefix() + "/"


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _check_token():
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录", "login_required": True}), 401
            return redirect(_login_url())
        return f(*args, **kwargs)
    return decorated


@app.after_request
def add_no_cache_headers(response):
    """禁止浏览器缓存所有页面和 API 响应"""
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# --- Configuration ---
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CLAUDE_CONFIG = Path.home() / ".claude" / "settings.json"
CLAUDE_DIR = CLAUDE_CONFIG.parent
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CLAUDE_DIR.mkdir(parents=True, exist_ok=True)


# --- Helpers ---

def validate_json(data, name="unknown"):
    """Validate that data is a dict with at least an env field or is a valid JSON object."""
    if not isinstance(data, dict):
        raise ValueError(f"{name}: 根元素必须是 JSON 对象")
    return data


def read_json(path):
    """Read and parse a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path, data):
    """Atomic write: temp file + rename."""
    tmp = tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.tmp.",
        suffix="",
        delete=False,
    )
    try:
        with open(tmp.name, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        if path.exists():
            shutil.copymode(path, tmp.name)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise
    tmp.close()
    os.replace(tmp.name, path)


def scan_configs():
    """Scan config directory, validate, return (valid, invalid) lists."""
    valid = []
    invalid = []
    for f in sorted(CONFIG_DIR.glob("*.json")):
        if f.name == "sample.json":
            continue
        name = f.name
        try:
            data = read_json(f)
            validate_json(data, name)
            valid.append({"name": name, "path": str(f)})
        except Exception as e:
            invalid.append({"name": name, "error": str(e)})
    return valid, invalid


def get_current_summary():
    """Get current settings.json summary."""
    if not CLAUDE_CONFIG.exists():
        return None
    try:
        data = read_json(CLAUDE_CONFIG)
        env = data.get("env", {})
        other_fields = {k: v for k, v in data.items() if k != "env"}
        return {
            "path": str(CLAUDE_CONFIG),
            "env": env,
            "other_fields": other_fields,
        }
    except Exception:
        return None


def do_switch(config_path):
    """Switch to the given config file."""
    selected = read_json(config_path)
    selected_env = selected.get("env", {})

    if not CLAUDE_CONFIG.exists():
        # No existing config, write selected directly
        write_json_atomic(CLAUDE_CONFIG, selected)
        return True

    # Backup
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = CLAUDE_CONFIG.with_name(f"settings.json.bak.{ts}")
    shutil.copy2(CLAUDE_CONFIG, bak)

    # Merge: keep non-env fields from current, replace env
    current = read_json(CLAUDE_CONFIG)
    merged = {k: v for k, v in current.items() if k != "env"}
    if selected_env:
        merged["env"] = selected_env

    write_json_atomic(CLAUDE_CONFIG, merged)
    return True


# --- Local routing (cc-switch-proxy) ---
# 开启后：以子进程按需拉起 cc-switch-proxy（cc-switch 的无界面本地路由），把 Claude Code
# 指向本机 loopback。关闭时：恢复路由前的 env 键并结束该进程。不是 systemd 服务，随时可关。
#
# 供应商不在这里配置，而是写进 cc-switch 的 SQLite 库（providers 表），由
# `cc-switch-proxy provider …` 子命令读写。档位模型映射（haiku/sonnet/opus）随供应商走。
#
# 全部可变项都走 dd-switch 的 EnvironmentFile，不写死任何个人路径：
#   DD_SWITCH_ROUTE_ENABLED      开关
#   DD_SWITCH_ROUTE_PROXY_BIN    cc-switch-proxy 可执行文件（默认从 PATH 找）
#   DD_SWITCH_ROUTE_DB           cc-switch 的 SQLite 库（默认 ~/.cc-switch/cc-switch.db）
#   DD_SWITCH_ROUTE_APP          应用类型（默认 claude）
#   DD_SWITCH_ROUTE_PORT         监听端口（默认 9301）

ROUTE_BASE_URL = "http://127.0.0.1:9301"
ROUTE_PORT = int(os.environ.get("DD_SWITCH_ROUTE_PORT", "9301") or "9301")
ROUTE_STATE_DIR = Path.home() / ".config" / "dd-switch"
ROUTE_STATE_FILE = ROUTE_STATE_DIR / "route-state.json"
ROUTE_LOG_FILE = ROUTE_STATE_DIR / "route.log"
ROUTE_ENABLED = os.environ.get("DD_SWITCH_ROUTE_ENABLED", "").lower() in ("1", "true", "yes")
ROUTE_PROXY_BIN = os.environ.get("DD_SWITCH_ROUTE_PROXY_BIN", "cc-switch-proxy")
ROUTE_DB = os.environ.get("DD_SWITCH_ROUTE_DB", str(Path.home() / ".cc-switch" / "cc-switch.db"))
ROUTE_APP = os.environ.get("DD_SWITCH_ROUTE_APP", "claude")
ROUTE_START_TIMEOUT = 30  # 等待 /health 就绪的秒数
ROUTE_STOP_TIMEOUT = 10   # SIGTERM 后等待进程退出的秒数

# 代理契约里的稳定模型别名：客户端只看到这几个名字，真实模型由代理按供应商映射。
ROUTE_ALIASES = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
}
# 代理不校验进来的凭据；Claude Code 需要一个非空 token，写占位符即可。
ROUTE_TOKEN_PLACEHOLDER = "PROXY_MANAGED"

# 本功能写入/恢复的 env 键；其余 env 与非 env 字段一概不动。
MANAGED_ENV_KEYS = [
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
]

_SECRET_ENV_RE = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.I)

_route_lock = threading.Lock()


class RouteError(Exception):
    """Local-routing operation failed; message is safe to show in the UI."""


def _proxy_path():
    """Resolve the cc-switch-proxy executable (PATH first, then literal)."""
    return shutil.which(ROUTE_PROXY_BIN) or ROUTE_PROXY_BIN


def route_missing_config():
    """Names of unset options — labels only, never values."""
    missing = []
    if not ROUTE_ENABLED:
        missing.append("DD_SWITCH_ROUTE_ENABLED")
    if not (shutil.which(ROUTE_PROXY_BIN) or Path(ROUTE_PROXY_BIN).exists()):
        missing.append("DD_SWITCH_ROUTE_PROXY_BIN")
    return missing


def route_supported():
    return not route_missing_config()


def _route_state():
    """Return the route state dict, or None when routing is not started."""
    if not ROUTE_STATE_FILE.exists():
        return None
    try:
        data = read_json(ROUTE_STATE_FILE)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_route_state(state):
    ROUTE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(ROUTE_STATE_FILE, state)
    os.chmod(ROUTE_STATE_FILE, 0o600)


def _pid_is_ours(pid):
    """True when pid is a live cc-switch-proxy we started (not a reused PID)."""
    if not pid:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return False
    return Path(ROUTE_PROXY_BIN).name in cmdline


def _route_process_alive(pid):
    return _pid_is_ours(pid)


def _start_route_process():
    """Spawn cc-switch-proxy detached. Returns its PID."""
    ROUTE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        _proxy_path(), "--db", ROUTE_DB,
        "--host", "127.0.0.1", "--port", str(ROUTE_PORT),
    ]
    log = open(ROUTE_LOG_FILE, "ab")
    try:
        proc = subprocess.Popen(
            cmd, env=os.environ.copy(),
            stdout=log, stderr=log, start_new_session=True,
        )
    finally:
        log.close()
    return proc.pid


def _stop_route_process(pid):
    """Terminate the process group we started. True when it is gone."""
    if not _pid_is_ours(pid):
        return True
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return True
    deadline = time.time() + ROUTE_STOP_TIMEOUT
    while time.time() < deadline:
        if not _pid_is_ours(pid):
            return True
        time.sleep(0.2)
    for killer in (os.killpg, os.kill):
        try:
            killer(pid, signal.SIGKILL)
        except OSError:
            pass
    return not _pid_is_ours(pid)


def _route_ready():
    """True when the proxy answers /health."""
    req = urllib.request.Request(ROUTE_BASE_URL + "/health")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status == 200


def _wait_route_ready(pid):
    deadline = time.time() + ROUTE_START_TIMEOUT
    last = None
    while time.time() < deadline:
        if not _route_process_alive(pid):
            raise RouteError("cc-switch-proxy 进程启动后即退出，详见 route 日志")
        try:
            if _route_ready():
                return
        except Exception as e:
            last = e
        time.sleep(1)
    raise RouteError(f"cc-switch-proxy 启动超时: {last or '/health 不可达'}")


def _proxy_cli(subargs, timeout=20):
    """Run `cc-switch-proxy provider …` and return the parsed JSON output."""
    cmd = [_proxy_path(), "provider"] + subargs + ["--db", ROUTE_DB, "--app", ROUTE_APP]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        raise RouteError(f"调用 cc-switch-proxy 失败: {e}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RouteError(f"供应商操作失败: {detail or f'exit {proc.returncode}'}")
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise RouteError("cc-switch-proxy 未返回结果")
    try:
        return json.loads(lines[-1])
    except Exception:
        raise RouteError("cc-switch-proxy 返回了无法解析的输出")


def route_providers():
    """List providers (tokens masked) plus the current one."""
    data = _proxy_cli(["list"])
    for provider in data.get("providers", []):
        env = (provider.get("settingsConfig") or {}).get("env") or {}
        for key in list(env):
            if _SECRET_ENV_RE.search(key) and env[key]:
                env[key] = "***"
    return data


def route_provider_save(payload):
    """Add or update a provider. `id` present → update, else add."""
    args = ["update", "--id", payload["id"]] if payload.get("id") else ["add"]
    for flag, key in (
        ("name", "name"), ("base-url", "base_url"), ("token", "token"),
        ("model", "model"), ("haiku", "haiku"), ("sonnet", "sonnet"), ("opus", "opus"),
    ):
        value = payload.get(key)
        if value is not None:
            args += [f"--{flag}", str(value)]
    if payload.get("set_current"):
        args += ["--set-current"]
    return _proxy_cli(args)


def route_provider_delete(provider_id):
    return _proxy_cli(["delete", "--id", provider_id])


def route_provider_set_current(provider_id):
    return _proxy_cli(["set-current", "--id", provider_id])


def route_provider_import(directory, set_current=False):
    args = ["import", "--dir", directory]
    if set_current:
        args += ["--set-current"]
    return _proxy_cli(args, timeout=60)


def _snapshot_managed_env(settings_env):
    original = {}
    for key in MANAGED_ENV_KEYS:
        if key in settings_env:
            original[key] = {"present": True, "value": settings_env[key]}
        else:
            original[key] = {"present": False}
    return original


def _apply_route_env(settings):
    env = dict(settings.get("env") or {})
    env["ANTHROPIC_BASE_URL"] = ROUTE_BASE_URL
    env["ANTHROPIC_AUTH_TOKEN"] = ROUTE_TOKEN_PLACEHOLDER
    env["ANTHROPIC_MODEL"] = ROUTE_ALIASES["sonnet"]
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = ROUTE_ALIASES["haiku"]
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = ROUTE_ALIASES["opus"]
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = ROUTE_ALIASES["sonnet"]
    # *_MODEL_NAME 只是显示用别名：原本有才更新，避免引入非本项目的约定。
    for key, tier in (
        ("ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME", "haiku"),
        ("ANTHROPIC_DEFAULT_OPUS_MODEL_NAME", "opus"),
        ("ANTHROPIC_DEFAULT_SONNET_MODEL_NAME", "sonnet"),
    ):
        if key in env:
            env[key] = ROUTE_ALIASES[tier]
    settings["env"] = env


def _restore_managed_env(settings, original):
    env = dict(settings.get("env") or {})
    for key, entry in (original or {}).items():
        if entry.get("present"):
            env[key] = entry["value"]
        else:
            env.pop(key, None)
    settings["env"] = env


def _backup_settings():
    if CLAUDE_CONFIG.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        shutil.copy2(CLAUDE_CONFIG, CLAUDE_CONFIG.with_name(f"settings.json.bak.{ts}"))


def _mask_secret_env(env):
    masked = {}
    for key, val in env.items():
        if _SECRET_ENV_RE.search(key) and isinstance(val, str) and val:
            masked[key] = "***"
        else:
            masked[key] = val
    return masked


def _find_active_profile(env):
    for f in sorted(CONFIG_DIR.glob("*.json")):
        if f.name == "sample.json":
            continue
        try:
            data = read_json(f)
        except Exception:
            continue
        if data.get("env") == env:
            return f.name
    return None


def route_status():
    state = _route_state()
    active = state is not None
    running = _route_process_alive(state.get("pid")) if active else False
    return {
        "supported": route_supported(),
        "missing": route_missing_config(),
        "active": active,
        "applied": bool(state.get("applied")) if active else False,
        "running": running,
        "provider": state.get("provider") if active else None,
    }


def route_start():
    if not route_supported():
        raise RouteError("本地路由功能未启用或缺少配置")

    providers = _proxy_cli(["list"])
    if not providers.get("current"):
        raise RouteError("请先在下方添加并选择一个供应商，再开启路由")

    state = _route_state()
    if state is not None:
        if _route_process_alive(state.get("pid")):
            raise RouteError("本地路由已处于启用状态")
        # 进程已退出（例如重启过主机）：只重新拉起进程，不重做快照
        pid = _spawn_route()
        try:
            _wait_route_ready(pid)
        except Exception:
            _stop_route_process(pid)
            raise
        state["pid"] = pid
        _write_route_state(state)
        return {"provider": state.get("provider")}

    pid = _spawn_route()
    try:
        _wait_route_ready(pid)
    except Exception:
        # 就绪失败：结束本次启动的进程，不留半开状态
        _stop_route_process(pid)
        raise

    current = read_json(CLAUDE_CONFIG) if CLAUDE_CONFIG.exists() else {}
    env = current.get("env") or {}
    state = {
        "version": 2,
        "started_at": int(time.time()),
        "pid": pid,
        "applied": False,
        "provider": providers.get("current"),
        "env_existed": "env" in current,
        "original_env": _snapshot_managed_env(env),
    }
    _write_route_state(state)
    return {"provider": state.get("provider")}


def _spawn_route():
    try:
        return _start_route_process()
    except Exception as e:
        raise RouteError(f"启动 cc-switch-proxy 失败: {e}")


def route_apply():
    state = _route_state()
    if state is None:
        raise RouteError("本地路由未启用")
    if not _route_process_alive(state.get("pid")):
        raise RouteError("cc-switch-proxy 进程未运行，请重新开启路由")

    providers = _proxy_cli(["list"])
    current = providers.get("current")
    if not current:
        raise RouteError("当前没有选中的供应商，无法应用")

    current = read_json(CLAUDE_CONFIG) if CLAUDE_CONFIG.exists() else {}
    _backup_settings()
    settings = dict(current)
    _apply_route_env(settings)
    write_json_atomic(CLAUDE_CONFIG, settings)

    state["applied"] = True
    state["provider"] = providers.get("current")
    _write_route_state(state)
    return {"provider": state["provider"]}


def route_stop():
    state = _route_state()
    if state is None:
        raise RouteError("本地路由未启用")

    # 先恢复 settings，成功后才结束进程/删状态，失败可原样重试
    if state.get("applied") and CLAUDE_CONFIG.exists():
        current = read_json(CLAUDE_CONFIG)
        _backup_settings()
        restored = dict(current)
        if state.get("env_existed", True):
            _restore_managed_env(restored, state.get("original_env") or {})
        else:
            restored.pop("env", None)
        write_json_atomic(CLAUDE_CONFIG, restored)

    if not _stop_route_process(state.get("pid")):
        raise RouteError("无法结束 cc-switch-proxy 进程，请检查进程状态后重试")

    try:
        os.remove(ROUTE_STATE_FILE)
    except FileNotFoundError:
        pass
    return {"stopped": True}


# --- Routes ---

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ACCESS_PASSWORD:
            token = str(uuid.uuid4())
            _token_store[token] = {"login_time": time.time()}
            resp = make_response(redirect(_index_url()))
            _set_token_cookie(resp, token)
            return resp
        return render_template("login.html", error="密码错误")
    # GET: 已登录则直接跳首页
    if _check_token():
        return redirect(_index_url())
    return render_template("login.html")


@app.route("/logout")
def logout():
    token = _get_token()
    if token and token in _token_store:
        del _token_store[token]
    login_href = _login_url()
    resp = make_response(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>已退出</title>
<style>body{{font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#1a1b2e;color:#e2e4f0}}div{{text-align:center}}a{{color:#6366f1}}</style>
</head><body><div><h2>已退出登录</h2><p><a href="{login_href}">重新登录</a></p></div></body></html>""")
    _clear_token_cookie(resp)
    return resp


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/configs")
@login_required
def api_configs():
    valid, invalid = scan_configs()
    return jsonify({"valid": valid, "invalid": invalid})


@app.route("/api/configs/<name>")
@login_required
def api_config_get(name):
    path = CONFIG_DIR / name
    if not path.exists() or not path.is_file():
        return jsonify({"error": f"配置文件 {name} 不存在"}), 404
    try:
        data = read_json(path)
        return jsonify({"name": name, "content": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/configs", methods=["POST"])
@login_required
def api_config_create():
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    content = body.get("content")

    if not name or not content:
        return jsonify({"error": "name 和 content 不能为空"}), 400
    if not name.endswith(".json"):
        name += ".json"
    # basic sanitize: no path traversal
    if "/" in name or "\\" in name:
        return jsonify({"error": "文件名不合法"}), 400

    try:
        validate_json(content, name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    path = CONFIG_DIR / name
    if path.exists():
        return jsonify({"error": f"文件 {name} 已存在"}), 409

    write_json_atomic(path, content)
    return jsonify({"name": name, "message": "创建成功"}), 201


@app.route("/api/configs/<name>", methods=["PUT"])
@login_required
def api_config_update(name):
    path = CONFIG_DIR / name
    if not path.exists():
        return jsonify({"error": f"配置文件 {name} 不存在"}), 404

    body = request.get_json(force=True)
    content = body.get("content")
    if content is None:
        return jsonify({"error": "content 不能为空"}), 400

    try:
        validate_json(content, name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    write_json_atomic(path, content)
    return jsonify({"name": name, "message": "保存成功"})


@app.route("/api/configs/<name>", methods=["DELETE"])
@login_required
def api_config_delete(name):
    path = CONFIG_DIR / name
    if not path.exists():
        return jsonify({"error": f"配置文件 {name} 不存在"}), 404
    os.remove(path)
    return jsonify({"name": name, "message": "已删除"})


@app.route("/api/current")
@login_required
def api_current():
    summary = get_current_summary()
    if summary is None:
        return jsonify({"exists": False, "message": "~/.claude/settings.json 不存在"})
    env = summary["env"]
    # 在服务端做 profile 匹配，然后脱敏凭据，浏览器不需要拿到真实密钥
    return jsonify({
        "exists": True,
        "path": summary["path"],
        "env": _mask_secret_env(env),
        "other_fields": summary["other_fields"],
        "active_profile": _find_active_profile(env),
        "route": route_status(),
    })


@app.route("/api/switch", methods=["POST"])
@login_required
def api_switch():
    body = request.get_json(force=True)
    name = body.get("name", "").strip()

    if not name:
        return jsonify({"error": "name 不能为空"}), 400

    path = CONFIG_DIR / name
    if not path.exists():
        return jsonify({"error": f"配置文件 {name} 不存在"}), 404

    with _route_lock:
        if _route_state() is not None:
            return jsonify({"error": "本地路由启用中，请先关闭路由再切换配置"}), 409
        try:
            do_switch(path)
            summary = get_current_summary()
            return jsonify({"message": f"已切换为 {name}", "current": {"path": summary["path"], "env": _mask_secret_env(summary["env"]), "other_fields": summary["other_fields"]}})
        except Exception as e:
            return jsonify({"error": f"切换失败: {e}"}), 500


# --- Local routing API ---

@app.route("/api/route/status")
@login_required
def api_route_status():
    return jsonify(route_status())


@app.route("/api/route/providers")
@login_required
def api_route_providers():
    try:
        return jsonify(route_providers())
    except RouteError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"读取供应商失败: {e}"}), 500


@app.route("/api/route/providers", methods=["POST"])
@login_required
def api_route_provider_save():
    body = request.get_json(force=True) or {}
    payload = {
        "id": (body.get("id") or "").strip() or None,
        "name": (body.get("name") or "").strip(),
        "base_url": (body.get("base_url") or "").strip(),
        "model": (body.get("model") or "").strip(),
        "haiku": (body.get("haiku") or "").strip(),
        "sonnet": (body.get("sonnet") or "").strip(),
        "opus": (body.get("opus") or "").strip(),
        "set_current": bool(body.get("set_current")),
    }
    # token 只在用户填写时传递（列表里是掩码，不能回写）。
    token = body.get("token")
    if isinstance(token, str) and token.strip():
        payload["token"] = token.strip()
    if not payload["id"] and not payload["name"]:
        return jsonify({"error": "供应商名称必填"}), 400
    with _route_lock:
        try:
            return jsonify(route_provider_save(payload))
        except RouteError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"保存供应商失败: {e}"}), 500


@app.route("/api/route/providers/delete", methods=["POST"])
@login_required
def api_route_provider_delete():
    body = request.get_json(force=True) or {}
    provider_id = (body.get("id") or "").strip()
    if not provider_id:
        return jsonify({"error": "缺少供应商 id"}), 400
    with _route_lock:
        try:
            return jsonify(route_provider_delete(provider_id))
        except RouteError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"删除供应商失败: {e}"}), 500


@app.route("/api/route/providers/current", methods=["POST"])
@login_required
def api_route_provider_current():
    body = request.get_json(force=True) or {}
    provider_id = (body.get("id") or "").strip()
    if not provider_id:
        return jsonify({"error": "缺少供应商 id"}), 400
    with _route_lock:
        try:
            return jsonify(route_provider_set_current(provider_id))
        except RouteError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"切换供应商失败: {e}"}), 500


@app.route("/api/route/providers/import", methods=["POST"])
@login_required
def api_route_provider_import():
    body = request.get_json(force=True) or {}
    with _route_lock:
        try:
            return jsonify(route_provider_import(str(CONFIG_DIR), bool(body.get("set_current"))))
        except RouteError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"导入供应商失败: {e}"}), 500


@app.route("/api/route/start", methods=["POST"])
@login_required
def api_route_start():
    with _route_lock:
        try:
            return jsonify(route_start())
        except RouteError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"开启路由失败: {e}"}), 500


@app.route("/api/route/apply", methods=["POST"])
@login_required
def api_route_apply():
    with _route_lock:
        try:
            return jsonify(route_apply())
        except RouteError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"应用路由失败: {e}"}), 500


@app.route("/api/route/stop", methods=["POST"])
@login_required
def api_route_stop():
    with _route_lock:
        try:
            return jsonify(route_stop())
        except RouteError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"关闭路由失败: {e}"}), 500


# --- Server Management ---

@app.route("/api/servers")
@login_required
def api_servers_list():
    """List all configured servers (without passphrase)."""
    servers = ssh_manager.load_servers()
    # Remove passphrase from response
    safe = []
    for s in servers:
        safe.append({
            "name": s["name"],
            "host": s["host"],
            "port": s.get("port", 22),
            "username": s["username"],
            "key_path": s.get("key_path", ""),
            "has_passphrase": s.get("passphrase") is not None,
        })
    return jsonify({"servers": safe})


@app.route("/api/servers", methods=["POST"])
@login_required
def api_servers_add():
    """Add a new server."""
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    host = body.get("host", "").strip()
    port = body.get("port", 22)
    username = body.get("username", "").strip()
    key_path = body.get("key_path", "").strip()
    passphrase = body.get("passphrase", "")

    if not name or not host or not username:
        return jsonify({"error": "name、host、username 不能为空"}), 400
    if "/" in name or "\\" in name:
        return jsonify({"error": "名称不合法"}), 400

    server = {
        "name": name,
        "host": host,
        "port": int(port) if port else 22,
        "username": username,
        "key_path": key_path,
        "passphrase": passphrase,
    }

    try:
        ssh_manager.add_server(server, ACCESS_PASSWORD)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        return jsonify({"error": f"添加失败: {e}"}), 500

    return jsonify({"name": name, "message": "添加成功"}), 201


@app.route("/api/servers/<name>", methods=["PUT"])
@login_required
def api_servers_update(name):
    """Update an existing server."""
    body = request.get_json(force=True)
    updates = {}
    for field in ["host", "port", "username", "key_path", "passphrase"]:
        if field in body:
            updates[field] = body[field]

    if not updates:
        return jsonify({"error": "没有要更新的字段"}), 400

    try:
        ssh_manager.update_server(name, updates, ACCESS_PASSWORD)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"更新失败: {e}"}), 500

    return jsonify({"name": name, "message": "更新成功"})


@app.route("/api/servers/<name>", methods=["DELETE"])
@login_required
def api_servers_delete(name):
    """Delete a server."""
    try:
        ssh_manager.delete_server(name)
    except Exception as e:
        return jsonify({"error": f"删除失败: {e}"}), 500
    return jsonify({"name": name, "message": "已删除"})


@app.route("/api/servers/<name>/test")
@login_required
def api_servers_test(name):
    """Test SSH connection to a server."""
    servers = ssh_manager.load_servers()
    server = next((s for s in servers if s["name"] == name), None)
    if not server:
        return jsonify({"error": f"服务器 {name} 不存在"}), 404
    try:
        client = ssh_manager.get_ssh_client(server, ACCESS_PASSWORD)
        client.close()
        return jsonify({"message": "连接成功"})
    except Exception as e:
        return jsonify({"error": f"连接失败: {e}"}), 400


@app.route("/api/servers/<name>/configs")
@login_required
def api_servers_configs(name):
    """Get configs from a remote server."""
    servers = ssh_manager.load_servers()
    server = next((s for s in servers if s["name"] == name), None)
    if not server:
        return jsonify({"error": f"服务器 {name} 不存在"}), 404
    try:
        configs = ssh_manager.remote_scan_configs(server, ACCESS_PASSWORD)
        return jsonify({"configs": configs})
    except Exception as e:
        return jsonify({"error": f"获取配置失败: {e}"}), 500


@app.route("/api/servers/<name>/current")
@login_required
def api_servers_current(name):
    """Get current config from a remote server."""
    servers = ssh_manager.load_servers()
    server = next((s for s in servers if s["name"] == name), None)
    if not server:
        return jsonify({"error": f"服务器 {name} 不存在"}), 404
    try:
        result = ssh_manager.remote_read_config(server, ACCESS_PASSWORD)
        if not result["exists"]:
            return jsonify({"exists": False, "message": "远程 settings.json 不存在"})
        content = result["content"]
        env = content.get("env", {})
        other_fields = {k: v for k, v in content.items() if k != "env"}
        return jsonify({"exists": True, "env": env, "other_fields": other_fields, "path": ssh_manager.REMOTE_CONFIG_PATH})
    except Exception as e:
        return jsonify({"error": f"读取失败: {e}"}), 500


@app.route("/api/servers/<name>/switch", methods=["POST"])
@login_required
def api_servers_switch(name):
    """Switch config on a remote server."""
    servers = ssh_manager.load_servers()
    server = next((s for s in servers if s["name"] == name), None)
    if not server:
        return jsonify({"error": f"服务器 {name} 不存在"}), 404

    body = request.get_json(force=True)
    config_content = body.get("content")
    if not config_content:
        return jsonify({"error": "content 不能为空"}), 400

    try:
        result = ssh_manager.remote_switch(server, ACCESS_PASSWORD, config_content)
        return jsonify({"message": f"已切换", "result": result})
    except Exception as e:
        return jsonify({"error": f"切换失败: {e}"}), 500


if __name__ == "__main__":
    print(f"claude-gearshift Web UI running at http://0.0.0.0:10086")
    print(f"配置目录: {CONFIG_DIR}")
    print(f"Claude 配置: {CLAUDE_CONFIG}")
    # 生产用 waitress；debug 模式下回退到 Flask 开发服务器
    if os.environ.get("DD_SWITCH_DEBUG"):
        app.run(host="0.0.0.0", port=10086, debug=True)
    else:
        from waitress import serve
        serve(app, host="0.0.0.0", port=10086, threads=8)
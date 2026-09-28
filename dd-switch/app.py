#!/usr/bin/env python3
"""dd-switch: Web UI for switching Claude Code configurations."""

import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, make_response, render_template, request, redirect, url_for
from functools import wraps

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
    return jsonify({"exists": True, **summary})


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

    try:
        do_switch(path)
        summary = get_current_summary()
        return jsonify({"message": f"已切换为 {name}", "current": {"path": summary["path"], "env": summary["env"], "other_fields": summary["other_fields"]}})
    except Exception as e:
        return jsonify({"error": f"切换失败: {e}"}), 500


if __name__ == "__main__":
    print(f"dd-switch 启动在 http://0.0.0.0:10086")
    print(f"配置目录: {CONFIG_DIR}")
    print(f"Claude 配置: {CLAUDE_CONFIG}")
    # 生产用 waitress；debug 模式下回退到 Flask 开发服务器
    if os.environ.get("DD_SWITCH_DEBUG"):
        app.run(host="0.0.0.0", port=10086, debug=True)
    else:
        from waitress import serve
        serve(app, host="0.0.0.0", port=10086, threads=8)
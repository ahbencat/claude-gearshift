#!/usr/bin/env python3
"""SSH connection manager for remote server config switching."""

import json
import os
import tempfile
import time
from pathlib import Path

import paramiko
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64


SERVERS_FILE = Path(__file__).resolve().parent / "servers.json"
REMOTE_CONFIG_PATH = "~/.claude/settings.json"


def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a Fernet key from password using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def _encrypt_passphrase(passphrase: str, password: str) -> dict:
    """Encrypt SSH passphrase with Web UI password."""
    salt = os.urandom(16)
    key = _derive_key(password, salt)
    f = Fernet(key)
    encrypted = f.encrypt(passphrase.encode())
    return {
        "salt": base64.b64encode(salt).decode(),
        "encrypted": base64.b64encode(encrypted).decode(),
    }


def _decrypt_passphrase(data: dict, password: str) -> str:
    """Decrypt SSH passphrase with Web UI password."""
    salt = base64.b64decode(data["salt"])
    encrypted = base64.b64decode(data["encrypted"])
    key = _derive_key(password, salt)
    f = Fernet(key)
    return f.decrypt(encrypted).decode()


def load_servers() -> list:
    """Load servers list from servers.json."""
    if not SERVERS_FILE.exists():
        return []
    try:
        with open(SERVERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_servers(servers: list):
    """Save servers list to servers.json."""
    with open(SERVERS_FILE, "w", encoding="utf-8") as f:
        json.dump(servers, f, indent=2, ensure_ascii=False)
        f.write("\n")


def add_server(server: dict, password: str) -> dict:
    """Add a new server. Encrypt passphrase before storing."""
    servers = load_servers()
    # Check duplicate
    for s in servers:
        if s["name"] == server["name"]:
            raise ValueError(f"服务器 {server['name']} 已存在")
    # Encrypt passphrase
    if server.get("passphrase"):
        server["passphrase"] = _encrypt_passphrase(server["passphrase"], password)
    else:
        server["passphrase"] = None
    servers.append(server)
    save_servers(servers)
    return server


def update_server(name: str, updates: dict, password: str) -> dict:
    """Update an existing server."""
    servers = load_servers()
    for i, s in enumerate(servers):
        if s["name"] == name:
            if "passphrase" in updates and updates["passphrase"]:
                updates["passphrase"] = _encrypt_passphrase(updates["passphrase"], password)
            elif "passphrase" in updates and not updates["passphrase"]:
                updates["passphrase"] = None
            servers[i].update(updates)
            save_servers(servers)
            return servers[i]
    raise ValueError(f"服务器 {name} 不存在")


def delete_server(name: str):
    """Delete a server."""
    servers = load_servers()
    servers = [s for s in servers if s["name"] != name]
    save_servers(servers)


def get_ssh_client(server: dict, password: str) -> paramiko.SSHClient:
    """Create and return a connected SSH client."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    passphrase = None
    if server.get("passphrase"):
        passphrase = _decrypt_passphrase(server["passphrase"], password)

    connect_kwargs = {
        "hostname": server["host"],
        "port": server.get("port", 22),
        "username": server["username"],
        "key_filename": server.get("key_path"),
        "passphrase": passphrase,
        "timeout": 10,
    }
    client.connect(**connect_kwargs)
    return client


def remote_read_config(server: dict, password: str) -> dict:
    """Read remote ~/.claude/settings.json."""
    client = get_ssh_client(server, password)
    try:
        sftp = client.open_sftp()
        remote_path = REMOTE_CONFIG_PATH
        try:
            with sftp.open(remote_path, "r") as f:
                content = json.load(f)
            return {"exists": True, "content": content}
        except FileNotFoundError:
            return {"exists": False, "content": None}
    finally:
        client.close()


def remote_write_config(server: dict, password: str, content: dict):
    """Write content to remote ~/.claude/settings.json."""
    client = get_ssh_client(server, password)
    try:
        sftp = client.open_sftp()
        remote_path = REMOTE_CONFIG_PATH
        # Backup existing
        try:
            sftp.stat(remote_path)
            ts = time.strftime("%Y%m%d_%H%M%S")
            backup_path = f"{remote_path}.bak.{ts}"
            sftp.rename(remote_path, backup_path)
        except FileNotFoundError:
            pass
        # Write new
        with sftp.open(remote_path, "w") as f:
            json.dump(content, f, indent=2, ensure_ascii=False)
            f.write("\n")
    finally:
        client.close()


def remote_switch(server: dict, password: str, config_content: dict) -> dict:
    """Switch remote server to the given config content."""
    # Read current remote config
    current = remote_read_config(server, password)
    if current["exists"]:
        current_data = current["content"]
        # Merge: keep non-env fields, replace env
        merged = {k: v for k, v in current_data.items() if k != "env"}
        selected_env = config_content.get("env", {})
        if selected_env:
            merged["env"] = selected_env
        remote_write_config(server, password, merged)
    else:
        remote_write_config(server, password, config_content)
    # Return new state
    result = remote_read_config(server, password)
    return result


def remote_scan_configs(server: dict, password: str) -> list:
    """Scan remote config directory for JSON files."""
    client = get_ssh_client(server, password)
    try:
        sftp = client.open_sftp()
        config_dir = str(Path(REMOTE_CONFIG_PATH).parent.parent / "config")
        try:
            entries = sftp.listdir(config_dir)
        except FileNotFoundError:
            return []
        configs = []
        for name in sorted(entries):
            if not name.endswith(".json") or name == "sample.json":
                continue
            try:
                with sftp.open(f"{config_dir}/{name}", "r") as f:
                    content = json.load(f)
                configs.append({"name": name, "content": content})
            except Exception:
                pass
        return configs
    finally:
        client.close()

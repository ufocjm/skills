#!/usr/bin/env python3
"""
OpenWrt OpenClash Management CLI.
Uses Python 3 standard library only and controls OpenClash over SSH.
Keeps SSH targets and optional credentials outside the skill directory.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import pty
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any


CONFIG_DIR = "/etc/openclash/config"
OPENCLASH_SH = "/usr/share/openclash/openclash.sh"
OPENCLASH_INIT = "/etc/init.d/openclash"
CLASH_BIN = "/etc/openclash/clash"
CLASH_WORKDIR = "/etc/openclash"
LOG_FILE = "/tmp/openclash.log"


class OpenClashError(Exception):
    def __init__(self, message: str, code: str = "ERROR", payload: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.payload = payload


def get_config_path() -> Path:
    explicit = os.environ.get("OPENCLASH_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "openclash" / "config.json"


def write_private_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_config_raw() -> dict[str, Any]:
    cfg_path = get_config_path()
    if not cfg_path.exists():
        return {}
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        raise OpenClashError(f"Failed to read config file {cfg_path}: {exc}", code="CONFIG_ERROR")


def load_runtime_config(overrides: argparse.Namespace | None = None) -> dict[str, Any]:
    cfg = read_config_raw()
    result: dict[str, Any] = {
        "target": str(cfg.get("target") or "").strip(),
        "port": int(cfg["port"]) if cfg.get("port") else None,
        "identity_file": str(cfg.get("identity_file") or "").strip(),
        "password": str(cfg.get("password") or ""),
    }

    env_target = os.environ.get("OPENCLASH_SSH_TARGET", "").strip()
    env_port = os.environ.get("OPENCLASH_SSH_PORT", "").strip()
    env_identity = os.environ.get("OPENCLASH_SSH_IDENTITY", "").strip()
    env_password = os.environ.get("OPENCLASH_SSH_PASSWORD", "")

    if env_target:
        result["target"] = env_target
    if env_port:
        try:
            result["port"] = int(env_port)
        except ValueError:
            raise OpenClashError(f"Invalid OPENCLASH_SSH_PORT: {env_port}", code="CONFIG_ERROR")
    if env_identity:
        result["identity_file"] = env_identity
    if env_password:
        result["password"] = env_password

    if overrides is not None:
        if getattr(overrides, "target", None):
            result["target"] = str(overrides.target).strip()
        if getattr(overrides, "port", None):
            result["port"] = int(overrides.port)
        if getattr(overrides, "identity", None):
            result["identity_file"] = str(overrides.identity).strip()
        if getattr(overrides, "password", None) is not None:
            result["password"] = str(overrides.password)

    if not result["target"]:
        cfg_path = get_config_path()
        raise OpenClashError(
            "OpenClash SSH target is not configured. "
            f"Run `python3 {Path(__file__).name} init --target <ssh_alias_or_user@ip>` "
            f"(add `--password ...` if SSH keys are not configured), or set OPENCLASH_SSH_TARGET, "
            f"or create {cfg_path}.",
            code="CONFIG_MISSING",
        )
    return result


def build_ssh_base_cmd(cfg: dict[str, Any], batch_mode: bool) -> list[str]:
    cmd = ["ssh", "-o", "ConnectTimeout=10"]
    if batch_mode:
        cmd += ["-o", "BatchMode=yes"]
    else:
        cmd += ["-o", "NumberOfPasswordPrompts=1", "-o", "StrictHostKeyChecking=accept-new"]
    if cfg.get("port"):
        cmd += ["-p", str(cfg["port"])]
    if cfg.get("identity_file"):
        cmd += ["-i", str(Path(cfg["identity_file"]).expanduser())]
    cmd.append(cfg["target"])
    return cmd


def run_remote_sh(cfg: dict[str, Any], script: str, timeout: int = 30) -> tuple[int, str, str]:
    """
    Execute a POSIX sh script on the OpenWrt router using base64 transport
    so spaces, quotes, and UTF-8 names (e.g. Chinese config filenames) never break.
    Uses BatchMode by default, or PTY-driven password login when password is set.
    """
    if not shutil.which("ssh"):
        raise OpenClashError("`ssh` binary not found in PATH.", code="SSH_MISSING")

    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    remote_cmd = f"printf '%s' '{b64}' | base64 -d | sh"

    password = cfg.get("password") or ""
    if not password:
        cmd = build_ssh_base_cmd(cfg, batch_mode=True) + [remote_cmd]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise OpenClashError(f"SSH command timed out after {timeout}s.", code="SSH_TIMEOUT")

        stderr = proc.stderr or ""
        if proc.returncode == 255:
            lower = stderr.lower()
            if "permission denied" in lower or "publickey" in lower or "password" in lower:
                raise OpenClashError(
                    f"SSH authentication failed for '{cfg['target']}'. "
                    "Configure SSH key auth (`setup-key`), use an SSH alias in ~/.ssh/config, "
                    "or initialize with `--password`.",
                    code="AUTH_FAILED",
                    payload={"stderr": stderr.strip()},
                )
            if "host key verification failed" in lower or "identification has changed" in lower:
                raise OpenClashError(
                    f"SSH host key verification failed for '{cfg['target']}'. Check ~/.ssh/known_hosts.",
                    code="HOST_KEY_ERROR",
                    payload={"stderr": stderr.strip()},
                )
            raise OpenClashError(
                f"SSH connection failed for '{cfg['target']}': {stderr.strip() or 'exit 255'}",
                code="SSH_ERROR",
                payload={"stderr": stderr.strip()},
            )
        return proc.returncode, proc.stdout or "", stderr

    return _run_remote_sh_pty(cfg, remote_cmd, password, timeout=timeout)


def _run_remote_sh_pty(cfg: dict[str, Any], remote_cmd: str, password: str, timeout: int = 30) -> tuple[int, str, str]:
    nonce = uuid.uuid4().hex[:12]
    begin_tag = f"__OC_BEGIN_{nonce}__"
    end_tag = f"__OC_END_{nonce}__"

    wrapped = (
        f"printf '\\n{begin_tag}\\n'; "
        f"{remote_cmd}; "
        f"__oc_rc=$?; "
        f"printf '\\n{end_tag}:%d\\n' \"$__oc_rc\""
    )
    cmd = build_ssh_base_cmd(cfg, batch_mode=False) + [wrapped]

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)

    buf = bytearray()
    sent_password = False
    password_prompts = 0
    deadline = time.monotonic() + timeout

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                raise OpenClashError(f"SSH command timed out after {timeout}s.", code="SSH_TIMEOUT")

            rlist, _, _ = select.select([master_fd], [], [], min(0.5, remaining))
            if master_fd in rlist:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                buf.extend(chunk)
                text_so_far = buf.decode("utf-8", errors="replace")

                if begin_tag not in text_so_far:
                    lower = text_so_far.lower()
                    if "host key verification failed" in lower or "identification has changed" in lower:
                        proc.kill()
                        raise OpenClashError(
                            f"SSH host key verification failed for '{cfg['target']}'.",
                            code="HOST_KEY_ERROR",
                        )
                    if "permission denied" in lower:
                        proc.kill()
                        raise OpenClashError(
                            f"SSH password authentication rejected for '{cfg['target']}'.",
                            code="AUTH_FAILED",
                        )
                    if re.search(r"password\s*:\s*$", text_so_far, re.I):
                        password_prompts += 1
                        if sent_password or password_prompts > 1:
                            proc.kill()
                            raise OpenClashError(
                                f"SSH password authentication failed for '{cfg['target']}'.",
                                code="AUTH_FAILED",
                            )
                        os.write(master_fd, (password + "\n").encode("utf-8"))
                        sent_password = True
            else:
                if proc.poll() is not None:
                    # Drain any remaining bytes
                    while True:
                        r2, _, _ = select.select([master_fd], [], [], 0.05)
                        if master_fd not in r2:
                            break
                        try:
                            chunk = os.read(master_fd, 4096)
                        except OSError:
                            break
                        if not chunk:
                            break
                        buf.extend(chunk)
                    break

        proc.wait(timeout=5)
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    raw_text = buf.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    m = re.search(
        rf"{re.escape(begin_tag)}\n(.*?)\n{re.escape(end_tag)}:(\d+)",
        raw_text,
        re.S,
    )
    if not m:
        lower = raw_text.lower()
        if "permission denied" in lower or "authentication" in lower:
            raise OpenClashError(
                f"SSH authentication failed for '{cfg['target']}'.",
                code="AUTH_FAILED",
                payload={"output": raw_text.strip()},
            )
        raise OpenClashError(
            f"SSH command failed on '{cfg['target']}' (rc={proc.returncode}): {raw_text.strip()}",
            code="SSH_ERROR",
            payload={"output": raw_text.strip()},
        )

    return int(m.group(2)), m.group(1), ""


def sh_quote(s: str) -> str:
    return shlex.quote(s)


def fetch_remote_state(cfg: dict[str, Any], include_secrets: bool = False) -> dict[str, Any]:
    inc_flag = "1" if include_secrets else "0"
    script = f"""
. /lib/functions.sh
INCLUDE_SECRETS={inc_flag}
echo "===STATUS==="
/etc/init.d/openclash status 2>/dev/null || echo "unknown"
echo "===UCI==="
for k in enable core_type en_mode proxy_mode cn_port config_path restart; do
  v=$(uci -q get openclash.config.$k || true)
  printf "%s\t%s\n" "$k" "$v"
done
echo "===PS==="
ps w | grep "/etc/openclash/clash" | grep -v grep | head -n 1 || true
echo "===WATCHDOG==="
ps w | grep "openclash_watchdog.sh" | grep -v grep | head -n 1 || true
echo "===SUBS==="
config_load openclash
dump_sub() {{
  local s="$1"
  local enabled name sub_ua sub_convert address
  config_get_bool enabled "$s" enabled 1
  config_get name "$s" name ""
  config_get sub_ua "$s" sub_ua ""
  config_get sub_convert "$s" sub_convert 0
  if [ "$INCLUDE_SECRETS" = "1" ]; then
    config_get address "$s" address ""
  else
    address=""
  fi
  printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$s" "$enabled" "$name" "$sub_ua" "$sub_convert" "$address"
}}
config_foreach dump_sub config_subscribe
echo "===FILES==="
if [ -d "{CONFIG_DIR}" ]; then
  for f in "{CONFIG_DIR}"/*.yaml "{CONFIG_DIR}"/*.yml; do
    [ -e "$f" ] || continue
    sz=$(wc -c < "$f" 2>/dev/null || echo 0)
    mt=$(date -r "$f" "+%Y-%m-%d %H:%M:%S" 2>/dev/null || ls -l "$f" 2>/dev/null | awk '{{print $6" "$7" "$8}}')
    printf "%s\t%s\t%s\n" "$f" "$sz" "$mt"
  done
fi
"""
    rc, out, err = run_remote_sh(cfg, script, timeout=25)
    if rc != 0:
        raise OpenClashError(f"Failed to inspect OpenClash state: {err or out}", code="REMOTE_ERROR")

    sections: dict[str, list[str]] = {}
    current = None
    for line in out.splitlines():
        if line.startswith("===") and line.endswith("==="):
            current = line.strip("=")
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    service_status = (sections.get("STATUS") or ["unknown"])[0].strip() or "unknown"
    uci_map: dict[str, str] = {}
    for line in sections.get("UCI", []):
        if "\t" in line:
            k, v = line.split("\t", 1)
            uci_map[k] = v

    ps_lines = [l for l in sections.get("PS", []) if l.strip()]
    clash_pid = None
    clash_running_cfg = None
    if ps_lines:
        parts = ps_lines[0].strip().split()
        if parts and parts[0].isdigit():
            clash_pid = int(parts[0])
        m_cfg = re.search(r"\s-f\s+(.+)$", ps_lines[0])
        if m_cfg:
            clash_running_cfg = m_cfg.group(1).strip()

    watchdog_running = bool([l for l in sections.get("WATCHDOG", []) if l.strip()])
    active_path = uci_map.get("config_path", "")

    files_map: dict[str, dict[str, Any]] = {}
    configs: list[dict[str, Any]] = []
    for line in sections.get("FILES", []):
        if not line.strip():
            continue
        parts = line.split("\t")
        fpath = parts[0]
        size_bytes = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 0
        mtime = parts[2].strip() if len(parts) > 2 else ""
        fname = os.path.basename(fpath)
        stem = re.sub(r"\.(ya?ml)$", "", fname, flags=re.I)
        item = {
            "name": stem,
            "filename": fname,
            "path": fpath,
            "size_bytes": size_bytes,
            "mtime": mtime,
            "active": bool(active_path and fpath == active_path),
        }
        files_map[fpath] = item
        configs.append(item)

    subscriptions: list[dict[str, Any]] = []
    sub_by_path: dict[str, str] = {}
    for idx, line in enumerate(sections.get("SUBS", [])):
        if not line.strip():
            continue
        parts = line.split("\t")
        sec_id = parts[0] if len(parts) > 0 else ""
        enabled = (parts[1] == "1") if len(parts) > 1 else True
        name = parts[2] if len(parts) > 2 else ""
        sub_ua = parts[3] if len(parts) > 3 else ""
        sub_convert = (parts[4] == "1") if len(parts) > 4 else False
        address = parts[5] if len(parts) > 5 else ""

        expected_name = name or "config"
        cfg_file = f"{CONFIG_DIR}/{expected_name}.yaml"
        finfo = files_map.get(cfg_file)
        sub_obj: dict[str, Any] = {
            "index": idx,
            "section": sec_id,
            "name": expected_name,
            "enabled": enabled,
            "sub_ua": sub_ua,
            "sub_convert": sub_convert,
            "config_file": cfg_file,
            "exists": finfo is not None,
            "size_bytes": finfo["size_bytes"] if finfo else 0,
            "mtime": finfo["mtime"] if finfo else "",
            "active": bool(active_path and cfg_file == active_path),
        }
        if include_secrets:
            sub_obj["address"] = address
        subscriptions.append(sub_obj)
        sub_by_path[cfg_file] = expected_name

    for c in configs:
        c["subscription_name"] = sub_by_path.get(c["path"])

    active_name = os.path.basename(active_path) if active_path else ""
    running = (service_status == "running") or (clash_pid is not None)
    mismatch = False
    if active_path and clash_running_cfg:
        mismatch = os.path.basename(active_path) != os.path.basename(clash_running_cfg)

    return {
        "target": cfg["target"],
        "status": {
            "service_status": service_status,
            "running": running,
            "uci_enable": uci_map.get("enable") == "1",
            "core_type": uci_map.get("core_type", ""),
            "en_mode": uci_map.get("en_mode", ""),
            "proxy_mode": uci_map.get("proxy_mode", ""),
            "cn_port": int(uci_map["cn_port"]) if uci_map.get("cn_port", "").isdigit() else None,
            "config_path": active_path,
            "config_name": active_name,
            "clash_pid": clash_pid,
            "clash_running_config": clash_running_cfg,
            "config_mismatch": mismatch,
            "watchdog_running": watchdog_running,
        },
        "subscriptions": subscriptions,
        "configs": configs,
    }


def resolve_match(query: str, items: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, Any]:
    q = query.strip()
    if not q:
        raise OpenClashError("Empty target name.", code="INVALID_ARGUMENT")

    q_stem = re.sub(r"\.(ya?ml)$", "", os.path.basename(q), flags=re.I)

    for item in items:
        for k in keys:
            val = str(item.get(k) or "")
            if val and val == q:
                return item

    for item in items:
        for k in keys:
            val = str(item.get(k) or "")
            val_stem = re.sub(r"\.(ya?ml)$", "", os.path.basename(val), flags=re.I)
            if val_stem and (val_stem == q or val_stem == q_stem):
                return item

    ci_matches = []
    q_low = q.lower()
    for item in items:
        if any(q_low == str(item.get(k) or "").lower() for k in keys):
            ci_matches.append(item)
    if len(ci_matches) == 1:
        return ci_matches[0]

    sub_matches = []
    for item in items:
        if any(q_low in str(item.get(k) or "").lower() for k in keys):
            sub_matches.append(item)
    if len(sub_matches) == 1:
        return sub_matches[0]
    if len(sub_matches) > 1:
        names = [str(m.get("name") or m.get("filename") or m.get("path")) for m in sub_matches]
        raise OpenClashError(
            f"Ambiguous target '{query}'; matched multiple items: {', '.join(names)}",
            code="AMBIGUOUS_TARGET",
        )

    avail = [str(it.get("name") or it.get("filename") or it.get("path")) for it in items]
    raise OpenClashError(
        f"Target '{query}' not found. Available: {', '.join(avail) if avail else '(none)'}",
        code="NOT_FOUND",
    )


def format_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    return f"{num / (1024 * 1024):.1f} MB"


def cmd_init(args: argparse.Namespace) -> Any:
    cfg = read_config_raw()
    target = (getattr(args, "target", None) or "").strip()
    if not target and args.host:
        user = (args.user or "root").strip()
        target = f"{user}@{args.host.strip()}" if "@" not in args.host else args.host.strip()
    if not target:
        target = str(cfg.get("target") or "").strip()
    if not target:
        raise OpenClashError("Provide --target (e.g. `op` or `root@192.168.1.1`) or --host.", code="INVALID_ARGUMENT")

    new_cfg: dict[str, Any] = {"target": target}
    arg_port = getattr(args, "port", None)
    port = arg_port if arg_port is not None else cfg.get("port")
    if port:
        new_cfg["port"] = int(port)
    arg_identity = getattr(args, "identity", None)
    identity = arg_identity if arg_identity is not None else cfg.get("identity_file")
    if identity:
        new_cfg["identity_file"] = str(identity).strip()

    arg_password = getattr(args, "password", None)
    if args.clear_password:
        pass
    elif args.prompt_password:
        new_cfg["password"] = getpass.getpass(f"SSH password for {target}: ")
    elif arg_password is not None:
        if arg_password:
            new_cfg["password"] = arg_password
    elif cfg.get("password"):
        new_cfg["password"] = cfg["password"]

    cfg_path = get_config_path()
    write_private_json(cfg_path, new_cfg)

    result: dict[str, Any] = {
        "saved_to": str(cfg_path),
        "target": new_cfg["target"],
        "port": new_cfg.get("port"),
        "identity_file": new_cfg.get("identity_file", ""),
        "has_password": bool(new_cfg.get("password")),
    }
    if args.verify:
        loaded = load_runtime_config(args)
        result["doctor"] = _run_doctor(loaded)
    return result


def _run_doctor(cfg: dict[str, Any]) -> dict[str, Any]:
    script = f"""
. /etc/openwrt_release 2>/dev/null || true
echo "DIST=$DISTRIB_DESCRIPTION"
echo "HOST=$(uci -q get system.@system[0].hostname || hostname)"
echo "PKG=$(opkg list-installed luci-app-openclash 2>/dev/null | head -n 1)"
echo "INIT=$([ -x '{OPENCLASH_INIT}' ] && echo yes || echo no)"
echo "SUBSH=$([ -x '{OPENCLASH_SH}' ] && echo yes || echo no)"
echo "CLASH=$([ -x '{CLASH_BIN}' ] && echo yes || echo no)"
echo "STATUS=$('{OPENCLASH_INIT}' status 2>/dev/null || echo unknown)"
"""
    rc, out, err = run_remote_sh(cfg, script, timeout=20)
    if rc != 0:
        raise OpenClashError(f"Doctor check failed: {err or out}", code="REMOTE_ERROR")

    kv: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()

    return {
        "ssh_ok": True,
        "target": cfg["target"],
        "auth_mode": "password" if cfg.get("password") else "key/ssh-config",
        "hostname": kv.get("HOST", ""),
        "openwrt": kv.get("DIST", ""),
        "package": kv.get("PKG", ""),
        "init_script": kv.get("INIT") == "yes",
        "subscribe_script": kv.get("SUBSH") == "yes",
        "clash_binary": kv.get("CLASH") == "yes",
        "service_status": kv.get("STATUS", "unknown"),
    }


def cmd_doctor(args: argparse.Namespace) -> Any:
    cfg = load_runtime_config(args)
    return _run_doctor(cfg)


def cmd_setup_key(args: argparse.Namespace) -> Any:
    cfg = load_runtime_config(args)
    ssh_dir = Path.home() / ".ssh"
    pub_path: Path | None = Path(args.pubkey).expanduser() if args.pubkey else None
    if pub_path is None:
        for cand in (ssh_dir / "id_ed25519.pub", ssh_dir / "id_rsa.pub", ssh_dir / "id_ecdsa.pub"):
            if cand.exists():
                pub_path = cand
                break

    if pub_path is None or not pub_path.exists():
        if args.generate:
            priv = ssh_dir / "id_ed25519"
            if priv.exists():
                raise OpenClashError(f"{priv} exists without .pub; pass --pubkey explicitly.", code="KEY_ERROR")
            ssh_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(ssh_dir, 0o700)
            subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(priv)], check=True)
            pub_path = ssh_dir / "id_ed25519.pub"
        else:
            raise OpenClashError(
                "No local SSH public key found (~/.ssh/id_ed25519.pub or id_rsa.pub). "
                "Re-run with `--generate` or pass `--pubkey <path>`.",
                code="KEY_MISSING",
            )

    pubkey = pub_path.read_text(encoding="utf-8").strip()
    if not pubkey or " " not in pubkey:
        raise OpenClashError(f"Invalid public key file: {pub_path}", code="KEY_ERROR")

    if not cfg.get("password") and args.prompt_password:
        cfg["password"] = getpass.getpass(f"SSH password for {cfg['target']}: ")

    script = f"""
KEY={sh_quote(pubkey)}
if [ -d /etc/dropbear ]; then
  AUTH="/etc/dropbear/authorized_keys"
  mkdir -p /etc/dropbear
  chmod 700 /etc/dropbear
else
  AUTH="$HOME/.ssh/authorized_keys"
  mkdir -p "$HOME/.ssh"
  chmod 700 "$HOME/.ssh"
fi
touch "$AUTH"
chmod 600 "$AUTH"
if grep -Fqx "$KEY" "$AUTH"; then
  echo "ALREADY_PRESENT $AUTH"
else
  printf '%s\n' "$KEY" >> "$AUTH"
  echo "INSTALLED $AUTH"
fi
"""
    rc, out, err = run_remote_sh(cfg, script, timeout=25)
    if rc != 0:
        raise OpenClashError(f"Failed to install SSH key: {err or out}", code="REMOTE_ERROR")

    verify_cfg = dict(cfg)
    verify_cfg["password"] = ""
    rc_v, _, err_v = run_remote_sh(verify_cfg, "echo OK", timeout=15)
    if rc_v != 0:
        raise OpenClashError(f"Key installed, but BatchMode verification failed: {err_v}", code="AUTH_FAILED")

    dropped_password = False
    if args.drop_password:
        saved = read_config_raw()
        if "password" in saved:
            del saved["password"]
            write_private_json(get_config_path(), saved)
            dropped_password = True

    return {
        "pubkey": str(pub_path),
        "remote_result": out.strip(),
        "batch_verified": True,
        "dropped_saved_password": dropped_password,
    }


def clash_api_request(
    cfg: dict[str, Any],
    method: str,
    api_path: str,
    body: Any = None,
    timeout: int = 20,
) -> Any:
    payload_str = json.dumps(body, ensure_ascii=False) if body is not None else ""
    body_arg = f"-H 'Content-Type: application/json' --data-binary {sh_quote(payload_str)}" if body is not None else ""
    script = f"""
if ! command -v curl >/dev/null 2>&1; then
  echo "__CURL_MISSING__"
  exit 0
fi
PORT=$(uci -q get openclash.config.cn_port || echo 9090)
SECRET=$(uci -q get openclash.config.dashboard_password || true)
URL="http://127.0.0.1:${{PORT}}{api_path}"
if [ -n "$SECRET" ]; then
  RESP=$(curl -sS --max-time {int(timeout)} -w "\\n__HTTP_CODE__:%{{http_code}}" -X {sh_quote(method)} -H "Authorization: Bearer $SECRET" {body_arg} "$URL" 2>&1)
else
  RESP=$(curl -sS --max-time {int(timeout)} -w "\\n__HTTP_CODE__:%{{http_code}}" -X {sh_quote(method)} {body_arg} "$URL" 2>&1)
fi
CURL_RC=$?
echo "CURL_RC=$CURL_RC"
printf '%s\n' "$RESP"
"""
    rc, out, err = run_remote_sh(cfg, script, timeout=timeout + 10)
    if rc != 0:
        raise OpenClashError(f"SSH error calling Clash API: {err or out}", code="REMOTE_ERROR")

    if "__CURL_MISSING__" in out:
        raise OpenClashError("`curl` is not installed on the OpenWrt router.", code="CURL_MISSING")

    lines = out.splitlines()
    curl_rc = 0
    if lines and lines[0].startswith("CURL_RC="):
        try:
            curl_rc = int(lines[0].split("=", 1)[1].strip())
        except ValueError:
            curl_rc = 0
        body_lines = lines[1:]
    else:
        body_lines = lines

    raw_resp = "\n".join(body_lines)
    if curl_rc != 0:
        raise OpenClashError(
            f"Clash controller unreachable (curl rc={curl_rc}): {raw_resp.strip()}",
            code="CONTROLLER_UNREACHABLE",
        )

    m = re.search(r"\n?__HTTP_CODE__:(\d+)\s*$", raw_resp)
    if not m:
        raise OpenClashError(f"Unexpected Clash API response: {raw_resp.strip()}", code="CONTROLLER_ERROR")

    http_code = int(m.group(1))
    resp_body = raw_resp[: m.start()].strip()
    parsed: Any = None
    if resp_body:
        try:
            parsed = json.loads(resp_body)
        except json.JSONDecodeError:
            parsed = resp_body

    if http_code in (401, 403):
        raise OpenClashError("Clash controller authentication failed (check openclash.config.dashboard_password).", code="CONTROLLER_AUTH_FAILED")
    if http_code >= 400:
        msg = parsed.get("message") if isinstance(parsed, dict) and parsed.get("message") else resp_body
        raise OpenClashError(f"Clash API HTTP {http_code}: {msg}", code="CONTROLLER_ERROR", payload={"http_code": http_code, "body": parsed})

    return parsed


def resolve_name_list(query: str, candidates: list[str], kind: str = "item") -> str:
    q = query.strip()
    if not q:
        raise OpenClashError(f"Empty {kind} name.", code="INVALID_ARGUMENT")

    if q in candidates:
        return q

    q_low = q.lower()
    ci = [c for c in candidates if c.lower() == q_low]
    if len(ci) == 1:
        return ci[0]

    sub = [c for c in candidates if q_low in c.lower()]
    if len(sub) == 1:
        return sub[0]
    if len(sub) > 1:
        raise OpenClashError(
            f"Ambiguous {kind} '{query}'; matched: {', '.join(sub[:10])}" + (" ..." if len(sub) > 10 else ""),
            code="AMBIGUOUS_TARGET",
        )

    raise OpenClashError(
        f"{kind.capitalize()} '{query}' not found.",
        code="NOT_FOUND",
        payload={"available_count": len(candidates)},
    )


def latest_delay(proxy_obj: dict[str, Any] | None) -> int | None:
    if not isinstance(proxy_obj, dict):
        return None
    hist = proxy_obj.get("history")
    if isinstance(hist, list) and hist:
        last = hist[-1]
        if isinstance(last, dict) and isinstance(last.get("delay"), int) and last["delay"] > 0:
            return last["delay"]
    return None


def cmd_status(args: argparse.Namespace) -> Any:
    cfg = load_runtime_config(args)
    state = fetch_remote_state(cfg, include_secrets=False)
    st = state["status"]
    if st["running"]:
        try:
            rt = clash_api_request(cfg, "GET", "/configs", timeout=8)
            if isinstance(rt, dict):
                st["runtime_mode"] = rt.get("mode", "")
                st["allow_lan"] = rt.get("allow-lan")
                st["tun_enable"] = (rt.get("tun") or {}).get("enable") if isinstance(rt.get("tun"), dict) else None
        except OpenClashError:
            pass
    return st


def cmd_list(args: argparse.Namespace) -> Any:
    cfg = load_runtime_config(args)
    state = fetch_remote_state(cfg, include_secrets=args.show_secrets)
    return {
        "active_config_path": state["status"]["config_path"],
        "subscriptions": state["subscriptions"],
        "configs": state["configs"],
    }


def wait_for_service_stable(cfg: dict[str, Any], wait_seconds: int = 15) -> dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_state = fetch_remote_state(cfg, include_secrets=False)
        st = last_state["status"]
        if st["running"] and st["clash_pid"] is not None:
            return st
        time.sleep(1.5)
    return last_state.get("status", {})


def cmd_refresh(args: argparse.Namespace) -> Any:
    cfg = load_runtime_config(args)
    state = fetch_remote_state(cfg, include_secrets=False)
    subs = state["subscriptions"]
    if not subs:
        raise OpenClashError("No subscription entries found in OpenClash UCI config.", code="NO_SUBSCRIPTION")

    if args.name:
        sub = resolve_match(args.name, subs, ("name", "config_file"))
    else:
        active_path = state["status"]["config_path"]
        if not active_path:
            raise OpenClashError("No active config_path in OpenClash UCI config.", code="NO_ACTIVE_CONFIG")
        matched = [s for s in subs if s["config_file"] == active_path]
        if not matched:
            raise OpenClashError(
                f"Active config '{active_path}' is not managed by a UCI config_subscribe entry. "
                "Specify a subscription name explicitly.",
                code="NO_SUBSCRIPTION",
            )
        sub = matched[0]

    sub_name = sub["name"]
    cfg_file = sub["config_file"]
    is_active = sub["active"]

    script = f"""
TARGET_NAME={sh_quote(sub_name)}
CFG_FILE={sh_quote(cfg_file)}
PRE_LOG_LINES=$(wc -l < "{LOG_FILE}" 2>/dev/null || echo 0)
PRE_MD5=$(md5sum "$CFG_FILE" 2>/dev/null | awk '{{print $1}}' || true)
PRE_SIZE=$(wc -c < "$CFG_FILE" 2>/dev/null || echo 0)

"{OPENCLASH_SH}" "$TARGET_NAME"
SH_RC=$?

sleep 1
POST_MD5=$(md5sum "$CFG_FILE" 2>/dev/null | awk '{{print $1}}' || true)
POST_SIZE=$(wc -c < "$CFG_FILE" 2>/dev/null || echo 0)
RESTART_FLAG=$(uci -q get openclash.config.restart || echo 0)

echo "SH_RC=$SH_RC"
echo "PRE_MD5=$PRE_MD5"
echo "POST_MD5=$POST_MD5"
echo "PRE_SIZE=$PRE_SIZE"
echo "POST_SIZE=$POST_SIZE"
echo "RESTART_FLAG=$RESTART_FLAG"
echo "===NEW_LOGS==="
tail -n +"$((PRE_LOG_LINES + 1))" "{LOG_FILE}" 2>/dev/null | tail -n 60 || true
"""
    rc, out, err = run_remote_sh(cfg, script, timeout=args.timeout)
    if rc != 0:
        raise OpenClashError(f"Failed to run subscription refresh: {err or out}", code="REMOTE_ERROR")

    kv: dict[str, str] = {}
    new_logs: list[str] = []
    in_logs = False
    for line in out.splitlines():
        if line == "===NEW_LOGS===":
            in_logs = True
            continue
        if in_logs:
            new_logs.append(line)
        elif "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()

    pre_md5 = kv.get("PRE_MD5", "")
    post_md5 = kv.get("POST_MD5", "")
    changed = bool(post_md5 and post_md5 != pre_md5)
    no_change_logged = any("No Change, Do Nothing" in l for l in new_logs)
    error_lines = [l for l in new_logs if re.search(r"(Error:|Subscribed Failed|Validation Failed|Test.*Fail|level=fatal|level=error)", l, re.I)]

    restart_triggered = is_active and changed and not error_lines
    final_status = None
    if restart_triggered:
        time.sleep(2)
        final_status = wait_for_service_stable(cfg, wait_seconds=18)
    else:
        final_status = fetch_remote_state(cfg, include_secrets=False)["status"]

    return {
        "subscription": sub_name,
        "config_file": cfg_file,
        "was_active": is_active,
        "changed": changed,
        "no_change": no_change_logged and not changed,
        "restart_triggered": restart_triggered,
        "pre_size": int(kv.get("PRE_SIZE") or 0),
        "post_size": int(kv.get("POST_SIZE") or 0),
        "errors": error_lines,
        "logs": new_logs[-20:],
        "status": final_status,
    }


def cmd_switch(args: argparse.Namespace) -> Any:
    cfg = load_runtime_config(args)
    state = fetch_remote_state(cfg, include_secrets=False)
    configs = state["configs"]
    if not configs:
        raise OpenClashError(f"No config files found in {CONFIG_DIR}.", code="NO_CONFIGS")

    target_cfg = resolve_match(args.name, configs, ("path", "filename", "name", "subscription_name"))
    target_path = target_cfg["path"]

    if not args.skip_test:
        test_script = f'"{CLASH_BIN}" -t -d "{CLASH_WORKDIR}" -f {sh_quote(target_path)}'
        rc_t, out_t, err_t = run_remote_sh(cfg, test_script, timeout=25)
        if rc_t != 0 or "test failed" in (out_t + err_t).lower():
            raise OpenClashError(
                f"Config test failed for '{target_path}'; aborted switch without modifying UCI.",
                code="CONFIG_TEST_FAILED",
                payload={"output": (out_t + "\n" + err_t).strip()},
            )

    already_active = state["status"]["config_path"] == target_path
    do_restart = not args.no_restart

    switch_script = f"""
PRE_LOG_LINES=$(wc -l < "{LOG_FILE}" 2>/dev/null || echo 0)
uci set openclash.config.config_path={sh_quote(target_path)}
uci commit openclash
echo "UCI_COMMITTED=1"
"""
    if do_restart:
        switch_script += f'"{OPENCLASH_INIT}" restart >/dev/null 2>&1\n'

    switch_script += f"""
echo "===NEW_LOGS==="
tail -n +"$((PRE_LOG_LINES + 1))" "{LOG_FILE}" 2>/dev/null | tail -n 50 || true
"""
    rc, out, err = run_remote_sh(cfg, switch_script, timeout=60)
    if rc != 0:
        raise OpenClashError(f"Failed to switch config: {err or out}", code="REMOTE_ERROR")

    new_logs: list[str] = []
    in_logs = False
    for line in out.splitlines():
        if line == "===NEW_LOGS===":
            in_logs = True
        elif in_logs:
            new_logs.append(line)

    final_status = wait_for_service_stable(cfg, wait_seconds=18) if do_restart else fetch_remote_state(cfg)["status"]
    error_lines = [l for l in new_logs if re.search(r"(level=fatal|level=error|FTL \[|Error:|Failed)", l, re.I)]

    return {
        "switched_to": target_path,
        "filename": target_cfg["filename"],
        "was_already_active": already_active,
        "restarted": do_restart,
        "errors": error_lines,
        "logs": new_logs[-20:],
        "status": final_status,
    }


def cmd_service(action: str, args: argparse.Namespace) -> Any:
    cfg = load_runtime_config(args)
    script = f"""
PRE_LOG_LINES=$(wc -l < "{LOG_FILE}" 2>/dev/null || echo 0)
"{OPENCLASH_INIT}" {sh_quote(action)} >/dev/null 2>&1
RC=$?
echo "RC=$RC"
echo "===NEW_LOGS==="
tail -n +"$((PRE_LOG_LINES + 1))" "{LOG_FILE}" 2>/dev/null | tail -n 50 || true
"""
    rc, out, err = run_remote_sh(cfg, script, timeout=60)
    if rc != 0:
        raise OpenClashError(f"Service {action} failed: {err or out}", code="REMOTE_ERROR")

    new_logs: list[str] = []
    in_logs = False
    for line in out.splitlines():
        if line == "===NEW_LOGS===":
            in_logs = True
        elif in_logs:
            new_logs.append(line)

    if action in ("start", "restart"):
        final_status = wait_for_service_stable(cfg, wait_seconds=18)
    else:
        final_status = fetch_remote_state(cfg, include_secrets=False)["status"]

    error_lines = [l for l in new_logs if re.search(r"(level=fatal|level=error|FTL \[|Error:|Failed)", l, re.I)]
    return {
        "action": action,
        "errors": error_lines,
        "logs": new_logs[-20:],
        "status": final_status,
    }


def cmd_logs(args: argparse.Namespace) -> Any:
    cfg = load_runtime_config(args)
    n = max(1, min(int(args.lines), 500))
    if args.errors:
        script = f'grep -iE "(level=fatal|level=error|level=warning|FTL \\[|Error:|Failed)" "{LOG_FILE}" 2>/dev/null | tail -n {n} || true'
    else:
        script = f'tail -n {n} "{LOG_FILE}" 2>/dev/null || true'
    rc, out, err = run_remote_sh(cfg, script, timeout=20)
    if rc != 0:
        raise OpenClashError(f"Failed to read logs: {err or out}", code="REMOTE_ERROR")
    lines = out.splitlines()
    return {"lines": lines, "count": len(lines)}


def cmd_groups(args: argparse.Namespace) -> Any:
    cfg = load_runtime_config(args)
    data = clash_api_request(cfg, "GET", "/proxies", timeout=15)
    proxies = data.get("proxies", {}) if isinstance(data, dict) else {}
    group_names = [name for name, info in proxies.items() if isinstance(info, dict) and "all" in info]

    if args.group:
        g_name = resolve_name_list(args.group, group_names, kind="proxy group")
        g_info = proxies[g_name]
        now = g_info.get("now", "")
        members = []
        for pname in g_info.get("all", []):
            pinfo = proxies.get(pname, {})
            members.append({
                "name": pname,
                "type": pinfo.get("type", ""),
                "delay_ms": latest_delay(pinfo),
                "active": pname == now,
            })
        return {
            "group": g_name,
            "type": g_info.get("type", ""),
            "now": now,
            "members": members,
        }

    search = (args.search or "").strip().lower()
    groups = []
    for name in group_names:
        if search and search not in name.lower():
            continue
        info = proxies[name]
        now = info.get("now", "")
        now_info = proxies.get(now, {}) if now else {}
        groups.append({
            "name": name,
            "type": info.get("type", ""),
            "now": now,
            "now_delay_ms": latest_delay(now_info),
            "count": len(info.get("all", [])),
        })
    return {"groups": groups, "count": len(groups)}


def cmd_node(args: argparse.Namespace) -> Any:
    cfg = load_runtime_config(args)
    data = clash_api_request(cfg, "GET", "/proxies", timeout=15)
    proxies = data.get("proxies", {}) if isinstance(data, dict) else {}
    group_names = [name for name, info in proxies.items() if isinstance(info, dict) and "all" in info]

    g_name = resolve_name_list(args.group, group_names, kind="proxy group")
    g_info = proxies[g_name]
    g_type = g_info.get("type", "")
    if g_type != "Selector":
        raise OpenClashError(
            f"Proxy group '{g_name}' is type '{g_type}' (not manually selectable Selector).",
            code="GROUP_NOT_SELECTABLE",
        )

    previous = g_info.get("now", "")
    candidates = list(g_info.get("all", []))
    target_proxy = resolve_name_list(args.proxy, candidates, kind=f"proxy in '{g_name}'")

    encoded_group = urllib.parse.quote(g_name, safe="")
    clash_api_request(cfg, "PUT", f"/proxies/{encoded_group}", body={"name": target_proxy}, timeout=15)

    verify = clash_api_request(cfg, "GET", f"/proxies/{encoded_group}", timeout=10)
    current = verify.get("now", "") if isinstance(verify, dict) else target_proxy
    if current != target_proxy:
        raise OpenClashError(
            f"Switch requested '{target_proxy}' in '{g_name}', but active node is '{current}'.",
            code="SWITCH_VERIFY_FAILED",
        )

    return {
        "group": g_name,
        "previous": previous,
        "now": current,
        "changed": previous != current,
    }


def cmd_delay(args: argparse.Namespace) -> Any:
    cfg = load_runtime_config(args)
    data = clash_api_request(cfg, "GET", "/proxies", timeout=15)
    proxies = data.get("proxies", {}) if isinstance(data, dict) else {}
    all_names = list(proxies.keys())
    target_name = resolve_name_list(args.target_name, all_names, kind="proxy or group")
    info = proxies[target_name]

    test_url = urllib.parse.quote(args.url, safe="")
    timeout_ms = max(500, min(int(args.timeout_ms), 30000))
    encoded_name = urllib.parse.quote(target_name, safe="")
    req_timeout = max(15, int(timeout_ms / 1000) + 10)

    if isinstance(info, dict) and "all" in info:
        raw = clash_api_request(
            cfg,
            "GET",
            f"/group/{encoded_name}/delay?timeout={timeout_ms}&url={test_url}",
            timeout=req_timeout,
        )
        delays = raw if isinstance(raw, dict) else {}
        members = info.get("all", [])
        results = []
        for pname in members:
            d = delays.get(pname)
            results.append({
                "name": pname,
                "delay_ms": d if isinstance(d, int) and d > 0 else None,
                "active": pname == info.get("now"),
            })
        results.sort(key=lambda x: (x["delay_ms"] is None, x["delay_ms"] or 0, x["name"]))
        return {
            "kind": "group",
            "target": target_name,
            "url": args.url,
            "timeout_ms": timeout_ms,
            "results": results,
            "reachable_count": sum(1 for r in results if r["delay_ms"] is not None),
            "total_count": len(results),
        }

    raw = clash_api_request(
        cfg,
        "GET",
        f"/proxies/{encoded_name}/delay?timeout={timeout_ms}&url={test_url}",
        timeout=req_timeout,
    )
    d_val = raw.get("delay") if isinstance(raw, dict) else None
    return {
        "kind": "proxy",
        "target": target_name,
        "url": args.url,
        "timeout_ms": timeout_ms,
        "delay_ms": d_val if isinstance(d_val, int) and d_val > 0 else None,
    }


def cmd_mode(args: argparse.Namespace) -> Any:
    cfg = load_runtime_config(args)
    if not args.mode_name:
        rt = clash_api_request(cfg, "GET", "/configs", timeout=10)
        rc, uci_mode, _ = run_remote_sh(cfg, "uci -q get openclash.config.proxy_mode || true", timeout=15)
        uci_val = uci_mode.strip() if rc == 0 else ""
        return {
            "runtime_mode": rt.get("mode", "") if isinstance(rt, dict) else "",
            "uci_proxy_mode": uci_val,
            "tun_enable": (rt.get("tun") or {}).get("enable") if isinstance(rt, dict) and isinstance(rt.get("tun"), dict) else None,
            "allow_lan": rt.get("allow-lan") if isinstance(rt, dict) else None,
            "persisted": False,
        }

    target_mode = args.mode_name.strip().lower()
    if target_mode not in ("rule", "global", "direct"):
        raise OpenClashError("Mode must be one of: rule, global, direct.", code="INVALID_ARGUMENT")

    before = clash_api_request(cfg, "GET", "/configs", timeout=10)
    prev_mode = before.get("mode", "") if isinstance(before, dict) else ""

    clash_api_request(cfg, "PATCH", "/configs", body={"mode": target_mode}, timeout=10)
    after = clash_api_request(cfg, "GET", "/configs", timeout=10)
    cur_mode = after.get("mode", "") if isinstance(after, dict) else ""
    if str(cur_mode).lower() != target_mode:
        raise OpenClashError(
            f"Requested mode '{target_mode}', but controller reports '{cur_mode}'.",
            code="MODE_VERIFY_FAILED",
        )

    persisted = False
    if args.persist:
        rc, out, err = run_remote_sh(
            cfg,
            f"uci set openclash.config.proxy_mode={sh_quote(target_mode)} && uci commit openclash",
            timeout=15,
        )
        if rc != 0:
            raise OpenClashError(f"Runtime mode changed, but failed to persist UCI: {err or out}", code="REMOTE_ERROR")
        persisted = True

    return {
        "previous": prev_mode,
        "runtime_mode": cur_mode,
        "persisted": persisted,
    }


def print_human(command: str, data: Any) -> None:
    if command == "init":
        print(f"Saved config to {data['saved_to']} (target={data['target']}, auth={'password' if data['has_password'] else 'key/ssh-config'})")
        if "doctor" in data:
            print_human("doctor", data["doctor"])
        return

    if command == "doctor":
        print(f"SSH OK: {data['target']} ({data['auth_mode']})")
        print(f"Host: {data['hostname']} | {data['openwrt']}")
        print(f"Package: {data['package'] or 'not found'}")
        print(
            f"Service: {data['service_status']} | init={data['init_script']} "
            f"sub={data['subscribe_script']} clash={data['clash_binary']}"
        )
        return

    if command == "setup-key":
        print(f"Public key: {data['pubkey']}")
        print(f"Remote: {data['remote_result']}")
        print(f"BatchMode SSH verified: {data['batch_verified']}")
        if data.get("dropped_saved_password"):
            print("Removed saved password from local config.json.")
        return

    if command == "status":
        print(
            f"Status: {data['service_status']} (running={data['running']}, "
            f"pid={data['clash_pid']}, uci_enable={data['uci_enable']})"
        )
        rt_mode = data.get("runtime_mode") or data["proxy_mode"]
        mode_note = f"{rt_mode} (uci={data['proxy_mode']})" if data.get("runtime_mode") and data.get("runtime_mode") != data["proxy_mode"] else rt_mode
        print(
            f"Core: {data['core_type']} | Mode: {data['en_mode']} / {mode_note} | "
            f"Controller port: {data['cn_port']}"
        )
        print(f"Active config: {data['config_path'] or '(none)'}")
        if data.get("config_mismatch"):
            print(f"WARNING: Running clash uses '{data['clash_running_config']}', differs from UCI config_path!")
        return

    if command == "list":
        print(f"Active config: {data['active_config_path'] or '(none)'}\n")
        print("Subscriptions (UCI):")
        if not data["subscriptions"]:
            print("  (none)")
        for s in data["subscriptions"]:
            mark = "*" if s["active"] else " "
            en = "enabled" if s["enabled"] else "disabled"
            file_state = format_size(s["size_bytes"]) if s["exists"] else "missing file"
            print(f"  {mark} [{s['index']}] {s['name']} ({en}, {file_state}) -> {s['config_file']}")
            if s.get("address"):
                print(f"      address: {s['address']}")
        print("\nConfig files (/etc/openclash/config):")
        if not data["configs"]:
            print("  (none)")
        for c in data["configs"]:
            mark = "*" if c["active"] else " "
            sub = f" [sub: {c['subscription_name']}]" if c.get("subscription_name") else " [local]"
            print(f"  {mark} {c['filename']} ({format_size(c['size_bytes'])}, {c['mtime']}){sub}")
        return

    if command == "refresh":
        if data["no_change"]:
            state_str = "no change"
        elif data["changed"]:
            state_str = f"updated ({format_size(data['pre_size'])} -> {format_size(data['post_size'])})"
        else:
            state_str = f"finished ({format_size(data['post_size'])})"
        print(f"Refresh [{data['subscription']}]: {state_str}")
        print(f"Restart triggered: {data['restart_triggered']}")
        if data["errors"]:
            print("Errors:")
            for l in data["errors"]:
                print(f"  {l}")
        if data["status"]:
            st = data["status"]
            print(f"Service: {st['service_status']} (pid={st['clash_pid']}, config={st['config_name']})")
        return

    if command == "switch":
        print(f"Switched active config to: {data['switched_to']} (restarted={data['restarted']})")
        if data["errors"]:
            print("Errors:")
            for l in data["errors"]:
                print(f"  {l}")
        if data["status"]:
            st = data["status"]
            print(f"Service: {st['service_status']} (pid={st['clash_pid']}, config={st['config_name']})")
        return

    if command in ("restart", "start", "stop"):
        st = data["status"]
        print(f"Action `{data['action']}` complete -> status: {st['service_status']} (pid={st['clash_pid']}, config={st['config_name']})")
        if data["errors"]:
            print("Errors:")
            for l in data["errors"]:
                print(f"  {l}")
        return

    if command == "logs":
        for l in data["lines"]:
            print(l)
        return

    if command == "groups":
        if "members" in data:
            print(f"Group: {data['group']} ({data['type']}) | active: {data['now']}")
            for m in data["members"]:
                mark = "*" if m["active"] else " "
                delay_s = f"{m['delay_ms']} ms" if m["delay_ms"] is not None else "-"
                print(f"  {mark} {m['name']} ({m['type']}, {delay_s})")
        else:
            for g in data["groups"]:
                delay_s = f" [{g['now_delay_ms']} ms]" if g["now_delay_ms"] is not None else ""
                print(f"  {g['name']} ({g['type']}, {g['count']}) -> {g['now']}{delay_s}")
        return

    if command == "node":
        print(f"Group [{data['group']}]: {data['previous']} -> {data['now']}")
        return

    if command == "delay":
        if data["kind"] == "group":
            print(
                f"Delay test [{data['target']}] ({data['reachable_count']}/{data['total_count']} reachable, "
                f"url={data['url']}):"
            )
            for r in data["results"]:
                mark = "*" if r["active"] else " "
                delay_s = f"{r['delay_ms']} ms" if r["delay_ms"] is not None else "timeout"
                print(f"  {mark} {r['name']}: {delay_s}")
        else:
            delay_s = f"{data['delay_ms']} ms" if data["delay_ms"] is not None else "timeout"
            print(f"Delay [{data['target']}]: {delay_s} (url={data['url']})")
        return

    if command == "mode":
        if "previous" in data:
            note = " (persisted to UCI)" if data["persisted"] else " (runtime only)"
            print(f"Proxy mode: {data['previous']} -> {data['runtime_mode']}{note}")
        else:
            print(
                f"Runtime mode: {data['runtime_mode']} | UCI proxy_mode: {data['uci_proxy_mode']} | "
                f"TUN: {data['tun_enable']} | allow-lan: {data['allow_lan']}"
            )
        return

    print(json.dumps(data, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--target", help="Override SSH target (e.g. `op` or `root@192.168.1.1`)")
    common.add_argument("--port", type=int, help="Override SSH port")
    common.add_argument("--identity", help="Override SSH private key path")
    common.add_argument("--password", help="Override SSH password (for hosts without SSH key auth)")
    common.add_argument("--json", action="store_true", help="Output structured JSON")

    parser = argparse.ArgumentParser(
        prog="openclash.py",
        description="Manage OpenWrt OpenClash over SSH.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", parents=[common], help="Save router SSH target/auth to ~/.config/openclash/config.json")
    p_init.add_argument("--host", help="Router IP or hostname (combined with --user if --target omitted)")
    p_init.add_argument("--user", default="root", help="SSH username when using --host (default: root)")
    p_init.add_argument("--prompt-password", action="store_true", help="Prompt interactively for SSH password")
    p_init.add_argument("--clear-password", action="store_true", help="Remove saved password from config")
    p_init.add_argument("--verify", action="store_true", help="Run doctor check immediately after saving")

    p_key = sub.add_parser("setup-key", parents=[common], help="Install local SSH public key onto OpenWrt router")
    p_key.add_argument("--pubkey", help="Path to public key (.pub)")
    p_key.add_argument("--generate", action="store_true", help="Generate ~/.ssh/id_ed25519 if no public key exists")
    p_key.add_argument("--prompt-password", action="store_true", help="Prompt interactively for SSH password during setup")
    p_key.add_argument("--drop-password", action="store_true", help="Remove saved password from config after verification")

    sub.add_parser("doctor", parents=[common], help="Verify SSH connectivity and OpenClash installation")
    sub.add_parser("status", parents=[common], help="Show OpenClash service status and active config")

    p_list = sub.add_parser("list", parents=[common], help="List OpenClash subscriptions and config files")
    p_list.add_argument("--show-secrets", action="store_true", help="Include subscription URLs in output")

    p_ref = sub.add_parser("refresh", parents=[common], help="Refresh the active (or named) OpenClash subscription")
    p_ref.add_argument("name", nargs="?", help="Subscription name or config file (defaults to active config)")
    p_ref.add_argument("--timeout", type=int, default=90, help="Command timeout in seconds (default: 90)")

    p_sw = sub.add_parser("switch", parents=[common], help="Switch active OpenClash config/subscription and restart")
    p_sw.add_argument("name", help="Target config name, filename, or path")
    p_sw.add_argument("--no-restart", action="store_true", help="Update UCI config_path without restarting OpenClash")
    p_sw.add_argument("--skip-test", action="store_true", help="Skip `clash -t` pre-flight validation")

    sub.add_parser("restart", parents=[common], help="Restart OpenClash service")
    sub.add_parser("start", parents=[common], help="Start OpenClash service")
    sub.add_parser("stop", parents=[common], help="Stop OpenClash service")

    p_logs = sub.add_parser("logs", parents=[common], help="Tail /tmp/openclash.log")
    p_logs.add_argument("--lines", type=int, default=40, help="Number of lines (default: 40)")
    p_logs.add_argument("--errors", action="store_true", help="Filter for error/warning/fatal lines only")

    p_grp = sub.add_parser("groups", parents=[common], help="List Clash proxy groups or nodes in a group")
    p_grp.add_argument("group", nargs="?", help="Optional group name (supports unique substring match)")
    p_grp.add_argument("--search", help="Filter group names by keyword")

    p_node = sub.add_parser("node", parents=[common], help="Switch active proxy node in a Selector group")
    p_node.add_argument("group", help="Proxy group name (supports unique substring match)")
    p_node.add_argument("proxy", help="Target node/proxy name (supports unique substring match)")

    p_delay = sub.add_parser("delay", parents=[common], help="Test latency for a proxy group or single proxy node")
    p_delay.add_argument("target_name", help="Proxy group or node name (supports unique substring match)")
    p_delay.add_argument("--url", default="https://www.gstatic.com/generate_204", help="Latency test URL")
    p_delay.add_argument("--timeout-ms", type=int, default=5000, help="Timeout in milliseconds (default: 5000)")

    p_mode = sub.add_parser("mode", parents=[common], help="Inspect or switch Clash runtime proxy mode (rule/global/direct)")
    p_mode.add_argument("mode_name", nargs="?", help="Target mode: rule, global, or direct")
    p_mode.add_argument("--persist", action="store_true", help="Also save proxy_mode to OpenClash UCI config")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.command

    try:
        if cmd == "init":
            data = cmd_init(args)
        elif cmd == "setup-key":
            data = cmd_setup_key(args)
        elif cmd == "doctor":
            data = cmd_doctor(args)
        elif cmd == "status":
            data = cmd_status(args)
        elif cmd == "list":
            data = cmd_list(args)
        elif cmd == "refresh":
            data = cmd_refresh(args)
        elif cmd == "switch":
            data = cmd_switch(args)
        elif cmd in ("restart", "start", "stop"):
            data = cmd_service(cmd, args)
        elif cmd == "logs":
            data = cmd_logs(args)
        elif cmd == "groups":
            data = cmd_groups(args)
        elif cmd == "node":
            data = cmd_node(args)
        elif cmd == "delay":
            data = cmd_delay(args)
        elif cmd == "mode":
            data = cmd_mode(args)
        else:
            raise OpenClashError(f"Unknown command: {cmd}", code="INVALID_COMMAND")

        if getattr(args, "json", False):
            print(json.dumps({"ok": True, "command": cmd, "data": data}, ensure_ascii=False, indent=2))
        else:
            print_human(cmd, data)
        return 0
    except OpenClashError as exc:
        if getattr(args, "json", False):
            err_obj: dict[str, Any] = {"ok": False, "code": exc.code, "message": exc.message}
            if exc.payload is not None:
                err_obj["payload"] = exc.payload
            print(json.dumps(err_obj, ensure_ascii=False, indent=2), file=sys.stderr)
        else:
            print(f"[{exc.code}] {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

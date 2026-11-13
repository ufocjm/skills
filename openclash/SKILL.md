---
name: openclash
description: Manage OpenClash on an OpenWrt router over SSH and the Clash/Mihomo controller API. Use whenever the user asks to check OpenClash status, list subscriptions or YAML configs, list proxy groups or nodes, switch a proxy node in a group, test node/group latency (`delay`), switch proxy mode (`rule`/`global`/`direct`), refresh the active or a specific subscription, switch the active OpenClash subscription/config, start/stop/restart OpenClash, inspect logs, or configure SSH access (`init` / `setup-key`).
---

# OpenWrt OpenClash Manager

Control OpenClash on an OpenWrt router via `scripts/openclash.py` (relative to this skill directory) using Python 3 stdlib + SSH. Never store router hosts, IPs, SSH keys, or passwords inside the skill directory; runtime config lives in `~/.config/openclash/config.json` (with `0600` permissions) or `OPENCLASH_SSH_TARGET` / `OPENCLASH_SSH_PORT` / `OPENCLASH_SSH_IDENTITY` / `OPENCLASH_SSH_PASSWORD`.

## 1. Connect

Run `doctor` first if the router connection has not been verified in this session:

```sh
python3 <skill_dir>/scripts/openclash.py doctor
```

On `CONFIG_MISSING`, configure the SSH target once:

```sh
# Case A: Local ~/.ssh/config alias or existing SSH key auth (e.g. `ssh op`)
python3 <skill_dir>/scripts/openclash.py init --target op --verify

# Case B: Another machine without ~/.ssh/config alias (user@ip + password)
python3 <skill_dir>/scripts/openclash.py init --host 192.168.1.1 --user root --prompt-password --verify
```

On `AUTH_FAILED` when migrating a machine from password auth to SSH key auth, install the local public key onto OpenWrt (supports Dropbear `/etc/dropbear/authorized_keys` and OpenSSH `~/.ssh/authorized_keys`) and drop the saved password:

```sh
python3 <skill_dir>/scripts/openclash.py setup-key --generate --prompt-password --drop-password
```

## 2. Inspect

```sh
python3 <skill_dir>/scripts/openclash.py status
python3 <skill_dir>/scripts/openclash.py list
python3 <skill_dir>/scripts/openclash.py groups
python3 <skill_dir>/scripts/openclash.py groups "<group_name>"
python3 <skill_dir>/scripts/openclash.py mode
python3 <skill_dir>/scripts/openclash.py logs --lines 40
python3 <skill_dir>/scripts/openclash.py logs --errors --lines 30
```

Add `--json` when structured fields are needed. Pass `--target <host>` (or `--port` / `--password`) to target a router ad hoc without overwriting saved config.

## 3. Operate

### Switch Proxy Node, Test Latency & Switch Mode (`node` / `delay` / `mode`)

These run live against the Clash/Mihomo External Controller (`cn_port`, reading `dashboard_password` directly from UCI over SSH) without restarting OpenClash:

```sh
# Switch active node in a Selector group (unique substring match supported for both group and node)
python3 <skill_dir>/scripts/openclash.py node "<group>" "<proxy>"

# Test latency (ms) for an entire group or a single proxy node
python3 <skill_dir>/scripts/openclash.py delay "<group_or_proxy>" --timeout-ms 5000

# Switch runtime proxy mode (rule | global | direct); add --persist to also commit UCI proxy_mode
python3 <skill_dir>/scripts/openclash.py mode rule
python3 <skill_dir>/scripts/openclash.py mode global --persist
```

### Refresh Subscription (`refresh`)

Without arguments, refreshes the subscription corresponding to the currently active `openclash.config.config_path`. Pass a subscription name or config filename to refresh a specific one:

```sh
python3 <skill_dir>/scripts/openclash.py refresh
python3 <skill_dir>/scripts/openclash.py refresh "<subscription_name>"
```

When the active subscription changes on disk, OpenClash automatically restarts its core in the background; the script waits for the service to come back up and reports final status and any errors from `/tmp/openclash.log`.

### Switch Active Config / Subscription (`switch`)

Resolves by subscription name, `.yaml` filename, full path, or unique case-insensitive substring, validates the target YAML with `/etc/openclash/clash -t` before touching UCI, commits `openclash.config.config_path`, and restarts OpenClash:

```sh
python3 <skill_dir>/scripts/openclash.py switch "<name_or_yaml>"
python3 <skill_dir>/scripts/openclash.py switch "<name_or_yaml>" --no-restart
```

### Restart / Start / Stop

```sh
python3 <skill_dir>/scripts/openclash.py restart
python3 <skill_dir>/scripts/openclash.py start
python3 <skill_dir>/scripts/openclash.py stop
```

## Safeguards

- Restarting, switching, or stopping OpenClash briefly interrupts proxy traffic (and stops proxied connections on `stop`). Run `stop` only when explicitly asked.
- `switch` runs `clash -t` pre-flight validation by default and aborts before changing UCI if validation fails; do not pass `--skip-test` unless the user explicitly asks to bypass validation.
- Pass `--show-secrets` to `list` only when the user explicitly asks to inspect subscription URLs.
- Always check `status` and `errors` in the command output after `refresh`, `switch`, or `restart` to confirm the core recovered cleanly.

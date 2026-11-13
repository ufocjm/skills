---
name: subboost
description: Manage Clash / Mihomo subscriptions, proxy nodes, routing rules, rule-sets, rule matching priority, and proxy groups on a self-hosted SubBoost instance. Use whenever the user asks to add, edit, delete, reorder, or inspect Clash rules, subscription URLs, proxy nodes (vless/vmess/ss/trojan/hy2/yaml), node filters, proxy groups, relay chains, or export/verify Clash YAML via SubBoost — even if they don't name "SubBoost" explicitly.
---

# SubBoost Clash Subscription Manager

Operate SubBoost via `scripts/subboost.py` (relative to this skill directory). Never store instance URLs or credentials inside the skill directory; runtime config lives in `~/.config/subboost/config.json` (or `SUBBOOST_URL` / `SUBBOOST_USERNAME` / `SUBBOOST_PASSWORD`) and cookies/metadata cache in `~/.cache/subboost/`.

## 1. Connect

Run `doctor` first if the instance hasn't been verified in this session:

```sh
python3 <skill_dir>/scripts/subboost.py doctor
```

On `CONFIG_MISSING`, obtain the instance URL, username, and password from the user and initialize once (never retry failed logins blindly; SubBoost rate-limits at 8 failures / 15 min):

```sh
python3 <skill_dir>/scripts/subboost.py init --url "<URL>" --username "<USER>" --password "<PASS>"
```

## 2. Inspect

Use `@primary` for the primary subscription or pass a subscription ID/name:

```sh
python3 <skill_dir>/scripts/subboost.py list
python3 <skill_dir>/scripts/subboost.py show <sub_id_or_name>
python3 <skill_dir>/scripts/subboost.py rule list <sub_id_or_name>
python3 <skill_dir>/scripts/subboost.py group list <sub_id_or_name>
python3 <skill_dir>/scripts/subboost.py meta
```

Add `--json` when exact IDs or full config objects are needed.

## 3. Modify

### Rules & Matching Priority (`rule` / `rule-set`)

Position syntax for `--position` and `rule move --to`: `top`, `bottom`, `custom-top`, `custom-bottom`, `before:<key>`, `after:<key>`, where `<key>` is `custom-rule:<id>`, `custom-rule-set:<id>`, or `module:<mod>:<rule>`.

```sh
# Add a custom rule (target: module id/name, custom group name, DIRECT, or REJECT)
python3 <skill_dir>/scripts/subboost.py rule add <sub_id> --type DOMAIN-SUFFIX --value claude.ai --target ai --position top

# Add --no-resolve for IP-CIDR / IP-CIDR6 / IP-SUFFIX / IP-ASN / GEOIP
python3 <skill_dir>/scripts/subboost.py rule add <sub_id> --type IP-CIDR --value 10.0.0.0/8 --target DIRECT --no-resolve

# Search built-in rule catalog and attach a rule-set
python3 <skill_dir>/scripts/subboost.py rule search openai
python3 <skill_dir>/scripts/subboost.py rule-set add <sub_id> --name "OpenAI" --behavior domain --format mrs --path "geosite/openai.mrs" --target ai

# Reorder any custom or built-in rule
python3 <skill_dir>/scripts/subboost.py rule move <sub_id> --key custom-rule:<id> --to before:module:cn:geolocation-cn

# Remove rules
python3 <skill_dir>/scripts/subboost.py rule remove <sub_id> --id <rule_id>
python3 <skill_dir>/scripts/subboost.py rule remove <sub_id> --match claude.ai
```

### Sources, Nodes & Filters (`source` / `node` / `filter` / `refresh`)

Use `source add` for subscription URLs, raw node links (`vless://`, `vmess://`, `ss://`, `trojan://`, `hy2://`), or Clash `proxies:` YAML — it saves to `config.sources` and refreshes automatically so the server parses them into `nodes`:

```sh
python3 <skill_dir>/scripts/subboost.py source add <sub_id> --type nodes --content "vless://..." --tag "自建"
python3 <skill_dir>/scripts/subboost.py source add <sub_id> --type url --content "https://..."
python3 <skill_dir>/scripts/subboost.py nodes <sub_id> --search "香港"
python3 <skill_dir>/scripts/subboost.py node remove <sub_id> --pattern "过期|官网"
python3 <skill_dir>/scripts/subboost.py filter set <sub_id> --exclude "过期|剩余流量"
python3 <skill_dir>/scripts/subboost.py refresh <sub_id>
```

If an internal/LAN URL is rejected by SSRF protection, run `settings --allow-unsafe true`.

### Proxy Groups & Arbitrary Config (`group` / `patch`)

```sh
python3 <skill_dir>/scripts/subboost.py group toggle <sub_id> --enable ai gemini --disable adult
python3 <skill_dir>/scripts/subboost.py patch <sub_id> --json '{"config": {"mixedPort": 7897}}'
```

For `customProxyGroups`, `dialerProxyGroups`, `builtinRuleEdits`, `groupListeners`, or `dnsYaml`, read `references/schema.md` before calling `patch`.

## 4. Verify Output

After every change, inspect the generated Clash YAML to confirm the final proxies/groups/rules match intent:

```sh
python3 <skill_dir>/scripts/subboost.py yaml <sub_id> --grep "claude.ai"
python3 <skill_dir>/scripts/subboost.py yaml <sub_id> --section rules --limit 30
```

## Safeguards

- Always modify via `subboost.py` (never send raw partial `PUT` payloads; the server overwrites `config` wholesale).
- Pass `--show-secrets` only when the user explicitly asks for their subscription link or upstream URLs.
- Run `delete` or `reset-link` only when explicitly requested.

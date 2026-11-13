# SubBoost JSON Schema Reference

Reference for `patch <sub_id> --json ...` and rule/group payloads. Live built-in module IDs and rule IDs come from `meta` / `group list`.

## 1. Custom Rules (`config.customRules[]`)

```json
{
  "id": "custom-rule-domain-suffix-example",
  "type": "DOMAIN-SUFFIX",
  "value": "example.com",
  "target": { "kind": "module", "id": "ai" },
  "noResolve": false
}
```

- **Allowed `type`**: `DOMAIN`, `DOMAIN-SUFFIX`, `DOMAIN-KEYWORD`, `DOMAIN-WILDCARD`, `DOMAIN-REGEX`, `GEOSITE`, `IP-CIDR`, `IP-CIDR6`, `IP-SUFFIX`, `IP-ASN`, `GEOIP`, `SRC-GEOIP`, `SRC-IP-ASN`, `SRC-IP-CIDR`, `SRC-IP-SUFFIX`, `DST-PORT`, `SRC-PORT`, `IN-PORT`, `IN-TYPE`, `IN-USER`, `IN-NAME`, `REMATCH-NAME`, `PROCESS-NAME`, `PROCESS-PATH`, `PROCESS-NAME-WILDCARD`, `PROCESS-PATH-WILDCARD`, `PROCESS-NAME-REGEX`, `PROCESS-PATH-REGEX`, `UID`, `NETWORK`, `DSCP`, `AND`, `OR`, `NOT`.
- **`noResolve`**: valid only on `IP-CIDR`, `IP-CIDR6`, `IP-SUFFIX`, `IP-ASN`, `GEOIP` (and `RULE-SET`).
- **`target`**: `{"kind": "module", "id": "<module_id>"}` | `{"kind": "custom", "id": "<custom_group_id>"}` | `"DIRECT"` | `"REJECT"` | `"PASS"` | `"<dialer_group_name>"`.

## 2. Custom Rule Sets (`config.customRuleSets[]`)

```json
{
  "id": "openai-extra",
  "name": "OpenAI Extra",
  "behavior": "domain",
  "format": "mrs",
  "path": "geosite/openai.mrs",
  "target": { "kind": "module", "id": "ai" },
  "noResolve": false
}
```

- `behavior`: `"domain"` | `"ipcidr"` | `"classical"` (`"classical"` cannot use `format: "mrs"`).
- `format`: `"mrs"` | `"yaml"` | `"text"`.
- `path`: built-in path (`geosite/...mrs`, `geoip/...mrs` from `rule search`) or `http(s)://...`.

## 3. Rule Order (`config.ruleOrder[]`)

Key formats: `custom-rule:<id>`, `custom-rule-set:<id>`, `module:<moduleId>:<ruleId>`, `special:experimental-cn`. (`MATCH` is always last.)

- Only `custom-rule:*` / `custom-rule-set:*` keys -> sorts custom entries only (inserted before `gemini`).
- Includes any `module:*` or `special:*` key -> full-order mode across all pre-match rules.

## 4. Custom & Dialer Proxy Groups

`config.customProxyGroups[]`:
```json
{
  "id": "custom-jp-lowlatency",
  "name": "🇯🇵 日本低延迟",
  "emoji": "🇯🇵",
  "enabled": true,
  "groupType": "url-test",
  "strategy": "consistent-hashing",
  "memberSource": "filtered-nodes",
  "includeInGroupMembers": false,
  "advanced": {
    "regions": ["jp"],
    "includeRegex": "IPLC|专线",
    "excludeRegex": "倍率|实验",
    "sourceIds": [],
    "testUrl": "https://www.gstatic.com/generate_204",
    "extraMembers": [{ "kind": "direct" }],
    "excludedMembers": [{ "kind": "node", "name": "exact-node-name" }]
  }
}
```
- `groupType`: `"select"` | `"url-test"` | `"fallback"` | `"load-balance"` | `"direct-first"` | `"reject-first"`.
- `strategy` (when `load-balance`): `"consistent-hashing"` | `"round-robin"` | `"sticky-sessions"`.
- `regions`: `"us"`, `"hk"`, `"jp"`, `"sg"`, `"tw"`, `"kr"`, `"uk"`, `"de"`, `"fr"`, `"ca"`, `"au"`, `"other"`.
- `ProxyGroupMemberRef`: `{"kind": "node", "name": "..."}` | `{"kind": "module", "id": "..."}` | `{"kind": "custom", "id": "..."}` | `{"kind": "direct"}` | `{"kind": "reject"}`.

`config.dialerProxyGroups[]` (relay chains):
```json
{
  "id": "relay-us",
  "enabled": true,
  "name": "美国中转",
  "relayNodes": ["香港中转节点A", "香港中转节点B"],
  "type": "select",
  "targetNodes": ["美国落地节点1"]
}
```

## 5. Sources, Node Filter, Listeners & Other Config Fields

- `config.sources[]`: `{ "id": "<uuid>", "type": "url"|"nodes"|"yaml", "content": "...", "tag": "...", "nameTemplate": "...", "useProxyProviders": false }`
- `config.nodeNameFilter`:
  ```json
  {
    "enabled": true,
    "includeEnabled": false,
    "excludeEnabled": true,
    "includeRegexes": [],
    "excludeRegexes": ["过期", "剩余流量"]
  }
  ```
- `config.groupListeners[]`:
  ```json
  {
    "id": "listener-ai",
    "target": { "kind": "module", "id": "ai" },
    "port": 7901,
    "enabled": true,
    "allowLan": false
  }
  ```
- `config.builtinRuleEdits`: `{ "module:cn:cn-ip": { "target": "DIRECT" }, "module:ad:category-ads-all": { "enabled": false } }`
- `config.proxyGroupNameOverrides`: `{ "ai": "🤖 AI" }`
- `config.proxyGroupAdvanced`: `{ "<module_id>": { ...ProxyGroupAdvancedConfig } }` (when `proxyGroupAdvancedModeEnabled: true`)
- `config.dnsYaml`: raw YAML string for Clash `dns:` section
- Top-level / misc: `name`, `autoUpdateInterval` (seconds, integer >= 360 or `null`), `smartNodeMatchingEnabled` (bool), `config.mixedPort`, `config.allowLan`, `config.testUrl`, `config.testInterval`, `config.cnIpNoResolve`, `config.experimentalCnUseCnRuleSet`.

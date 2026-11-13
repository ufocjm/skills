#!/usr/bin/env python3
"""
SubBoost Clash/Mihomo Subscription Management CLI.
Uses Python 3 standard library only.
Keeps credentials, cookies, and dynamically fetched instance metadata outside the skill directory.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any


# Standard Clash/Mihomo rule types accepted by SubBoost customRules
CUSTOM_RULE_TYPES: set[str] = {
    "DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD", "DOMAIN-WILDCARD", "DOMAIN-REGEX", "GEOSITE",
    "IP-CIDR", "IP-CIDR6", "IP-SUFFIX", "IP-ASN", "GEOIP",
    "SRC-GEOIP", "SRC-IP-ASN", "SRC-IP-CIDR", "SRC-IP-SUFFIX",
    "DST-PORT", "SRC-PORT", "IN-PORT", "IN-TYPE", "IN-USER", "IN-NAME", "REMATCH-NAME",
    "PROCESS-NAME", "PROCESS-PATH", "PROCESS-NAME-WILDCARD", "PROCESS-PATH-WILDCARD",
    "PROCESS-NAME-REGEX", "PROCESS-PATH-REGEX",
    "UID", "NETWORK", "DSCP", "AND", "OR", "NOT",
}

NO_RESOLVE_TYPES: set[str] = {"RULE-SET", "IP-CIDR", "IP-CIDR6", "IP-SUFFIX", "IP-ASN", "GEOIP"}
METADATA_TTL_SECONDS = 24 * 3600


class SubBoostError(Exception):
    def __init__(self, message: str, code: str = "ERROR", status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.payload = payload


def get_config_path() -> Path:
    explicit = os.environ.get("SUBBOOST_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "subboost" / "config.json"


def get_cache_dir() -> Path:
    explicit = os.environ.get("SUBBOOST_CACHE_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "subboost"


def get_cookie_file() -> Path:
    return get_cache_dir() / "cookies.json"


def get_metadata_file() -> Path:
    return get_cache_dir() / "metadata.json"


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


def normalize_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    return url


def load_runtime_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    cfg_path = get_config_path()
    if cfg_path.exists():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in ("url", "username", "password"):
                    v = raw.get(k)
                    if isinstance(v, str) and v.strip():
                        cfg[k] = v.strip()
        except Exception as exc:
            raise SubBoostError(f"Failed to read config file {cfg_path}: {exc}", code="CONFIG_ERROR")

    env_map = {
        "url": os.environ.get("SUBBOOST_URL", "").strip(),
        "username": os.environ.get("SUBBOOST_USERNAME", "").strip(),
        "password": os.environ.get("SUBBOOST_PASSWORD", "").strip(),
    }
    for k, v in env_map.items():
        if v:
            cfg[k] = v

    if "url" in cfg:
        cfg["url"] = normalize_base_url(cfg["url"])

    missing = [k for k in ("url", "username", "password") if not cfg.get(k)]
    if missing:
        raise SubBoostError(
            "SubBoost configuration is missing: "
            + ", ".join(missing)
            + f". Run `python3 {Path(__file__).name} init` or set SUBBOOST_URL / SUBBOOST_USERNAME / SUBBOOST_PASSWORD "
            + f"or create {cfg_path}.",
            code="CONFIG_MISSING",
        )
    return cfg


def load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cookie_for_url(base_url: str, cookies: dict[str, str]) -> None:
    store = load_json_dict(get_cookie_file())
    store[base_url] = cookies
    write_private_json(get_cookie_file(), store)


def clear_cookie_for_url(base_url: str) -> None:
    store = load_json_dict(get_cookie_file())
    if base_url in store:
        del store[base_url]
        write_private_json(get_cookie_file(), store)


class SubBoostClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30):
        self.base_url = normalize_base_url(base_url)
        self.username = username
        self.password = password
        self.timeout = timeout
        cookie_store = load_json_dict(get_cookie_file())
        self.cookies: dict[str, str] = dict(cookie_store.get(self.base_url) or {})

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def _update_cookies_from_headers(self, headers: Any) -> None:
        raw_list = []
        if hasattr(headers, "get_all"):
            raw_list = headers.get_all("Set-Cookie") or []
        elif "Set-Cookie" in headers:
            raw_list = [headers["Set-Cookie"]]
        changed = False
        for header_val in raw_list:
            sc = SimpleCookie()
            sc.load(header_val)
            for name, morsel in sc.items():
                if morsel.value:
                    self.cookies[name] = morsel.value
                    changed = True
                elif name in self.cookies and morsel.get("max-age") == "0":
                    del self.cookies[name]
                    changed = True
        if changed:
            save_cookie_for_url(self.base_url, self.cookies)

    def raw_request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> tuple[int, Any, str]:
        url = self.base_url + path
        req_headers = {"Accept": "application/json, text/plain, */*"}
        if self.cookies:
            req_headers["Cookie"] = self._cookie_header()
        data_bytes: bytes | None = None
        if body is not None:
            data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(url, data=data_bytes, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                self._update_cookies_from_headers(resp.headers)
                raw_text = resp.read().decode("utf-8", errors="replace")
                return resp.status, resp.headers, raw_text
        except urllib.error.HTTPError as err:
            self._update_cookies_from_headers(err.headers)
            raw_text = err.read().decode("utf-8", errors="replace")
            return err.code, err.headers, raw_text
        except urllib.error.URLError as err:
            raise SubBoostError(f"Network error contacting {url}: {err.reason}", code="NETWORK_ERROR")

    def login(self) -> dict[str, Any]:
        status, resp_headers, raw_text = self.raw_request(
            "POST",
            "/api/auth/login",
            body={"username": self.username, "password": self.password},
        )
        data = _try_parse_json(raw_text)
        if status == 429:
            retry_after = resp_headers.get("Retry-After", "later") if resp_headers else "later"
            raise SubBoostError(
                f"Login rate-limited (429). Retry after {retry_after}s. Do NOT retry repeatedly.",
                code="RATE_LIMITED",
                status=429,
                payload=data,
            )
        if status >= 400:
            msg = data.get("error") if isinstance(data, dict) else raw_text
            raise SubBoostError(f"Login failed ({status}): {msg}", code="AUTH_FAILED", status=status, payload=data)
        return data if isinstance(data, dict) else {"success": True}

    def request_json(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        auth_required: bool = True,
        timeout: int | None = None,
    ) -> Any:
        if auth_required and not self.cookies:
            self.login()

        status, resp_headers, raw_text = self.raw_request(method, path, body=body, headers=headers, timeout=timeout)
        if status == 401 and auth_required:
            clear_cookie_for_url(self.base_url)
            self.cookies = {}
            self.login()
            status, resp_headers, raw_text = self.raw_request(method, path, body=body, headers=headers, timeout=timeout)

        data = _try_parse_json(raw_text)
        if status == 429:
            retry_after = resp_headers.get("Retry-After", "later") if resp_headers else "later"
            raise SubBoostError(f"Rate limited (429). Retry after {retry_after}s.", code="RATE_LIMITED", status=429, payload=data)
        if status >= 400:
            msg = data.get("error") if isinstance(data, dict) and data.get("error") else raw_text[:500]
            code = data.get("code") if isinstance(data, dict) and data.get("code") else f"HTTP_{status}"
            raise SubBoostError(f"{method} {path} failed ({status}): {msg}", code=str(code), status=status, payload=data)
        return data if data is not None else raw_text

    def request_text(self, method: str, path: str, timeout: int | None = None) -> tuple[int, Any, str]:
        status, resp_headers, raw_text = self.raw_request(method, path, timeout=timeout)
        if status >= 400:
            data = _try_parse_json(raw_text)
            msg = data.get("error") if isinstance(data, dict) and data.get("error") else raw_text[:500]
            raise SubBoostError(f"{method} {path} failed ({status}): {msg}", code=f"HTTP_{status}", status=status, payload=data)
        return status, resp_headers, raw_text


def _try_parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


# ==================== Dynamic Instance Metadata Extraction ====================


def _extract_balanced(text: str, start_idx: int, open_ch: str, close_ch: str) -> str | None:
    depth = 0
    in_str: str | None = None
    esc = False
    for idx in range(start_idx, len(text)):
        ch = text[idx]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in ('"', "'"):
            in_str = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start_idx : idx + 1]
    return None


def _js_literal_to_json(js_literal: str) -> Any:
    out: list[str] = []
    i = 0
    n = len(js_literal)
    in_str: str | None = None
    esc = False
    while i < n:
        ch = js_literal[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
            i += 1
            continue
        if ch == '"':
            in_str = '"'
            out.append(ch)
            i += 1
        elif js_literal.startswith("!0", i) and (i + 2 == n or not js_literal[i + 2].isalnum()):
            out.append("true")
            i += 2
        elif js_literal.startswith("!1", i) and (i + 2 == n or not js_literal[i + 2].isalnum()):
            out.append("false")
            i += 2
        elif ch in "{,":
            out.append(ch)
            i += 1
            while i < n and js_literal[i].isspace():
                out.append(js_literal[i])
                i += 1
            m = re.match(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*:", js_literal[i:])
            if m:
                out.append(f'"{m.group(1)}":')
                i += len(m.group(0))
        else:
            out.append(ch)
            i += 1
    return json.loads("".join(out))


def _fetch_metadata_from_frontend(client: SubBoostClient) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Fetch built-in proxy group modules and canonical RULE_ORDER directly from the
    running SubBoost frontend bundle (cached locally so subsequent calls are instant).
    """
    modules: list[dict[str, Any]] = []
    rule_order: list[str] = []

    candidate_pages = ["/", "/dashboard"]
    chunk_paths: list[str] = []
    for page in candidate_pages:
        try:
            _, _, html = client.request_text("GET", page, timeout=15)
            found = re.findall(r"/_next/static/chunks/[^\"'\s]+\.js", html)
            for p in found:
                if p not in chunk_paths:
                    chunk_paths.append(p)
        except Exception:
            continue

    for chunk in chunk_paths:
        try:
            _, _, js = client.request_text("GET", chunk, timeout=15)
        except Exception:
            continue
        if "groupType:" not in js or "rules:[" not in js:
            continue

        # Locate the modules array containing id:"select" ... groupType: ... rules:
        for m in re.finditer(r"(\[\{id:\"select\",[^\[]*?groupType:)", js):
            arr_js = _extract_balanced(js, m.start(1), "[", "]")
            if arr_js:
                try:
                    parsed = _js_literal_to_json(arr_js)
                    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                        modules = parsed
                        break
                except Exception:
                    pass

        # Locate the canonical module rule order array (starts with "ad","private",...)
        m2 = re.search(r"(\[\"ad\",\"private\",[^\]]+\])", js)
        if m2:
            try:
                parsed_order = _js_literal_to_json(m2.group(1))
                if isinstance(parsed_order, list) and all(isinstance(x, str) for x in parsed_order):
                    rule_order = parsed_order
            except Exception:
                pass

        if modules:
            break

    # Fallback: query /api/templates/builtin-full if frontend chunk layout changed
    if not modules:
        try:
            tpl = client.request_json("GET", "/api/templates/builtin-full", auth_required=False)
            enabled = ((tpl.get("template") or {}).get("config") or {}).get("enabledProxyGroups") or []
            modules = [{"id": gid, "name": gid, "rules": []} for gid in enabled if isinstance(gid, str)]
        except Exception:
            pass

    if not rule_order and modules:
        rule_order = [m["id"] for m in modules if m.get("id") not in ("select", "auto", "final")]

    return modules, rule_order


def get_instance_metadata(client: SubBoostClient, force_refresh: bool = False) -> dict[str, Any]:
    store = load_json_dict(get_metadata_file())
    cached = store.get(client.base_url)
    now = time.time()
    if (
        not force_refresh
        and isinstance(cached, dict)
        and isinstance(cached.get("modules"), list)
        and len(cached["modules"]) > 0
        and (now - float(cached.get("fetchedAt", 0))) < METADATA_TTL_SECONDS
    ):
        return cached

    modules, rule_order = _fetch_metadata_from_frontend(client)
    meta = {
        "baseUrl": client.base_url,
        "fetchedAt": int(now),
        "modules": modules,
        "ruleOrder": rule_order,
    }
    if modules:
        store[client.base_url] = meta
        write_private_json(get_metadata_file(), store)
    return meta


# ==================== Masking & Formatting ====================


def mask_secret(val: str) -> str:
    val = val.strip()
    if not val:
        return ""
    if len(val) <= 12:
        return val[:3] + "***"
    if val.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(val)
        tail = parsed.path[-6:] if len(parsed.path) > 6 else ""
        return f"{parsed.scheme}://{parsed.netloc}/***{tail}"
    return f"{val[:6]}...{val[-4:]}"


def mask_subscription_obj(sub: dict[str, Any], show_secrets: bool = False) -> dict[str, Any]:
    if show_secrets:
        return sub
    out = dict(sub)
    for k in ("token", "subscriptionUrl", "yamlUrl"):
        if isinstance(out.get(k), str) and out[k]:
            out[k] = mask_secret(out[k])
    if isinstance(out.get("urls"), list):
        out["urls"] = [mask_secret(u) if isinstance(u, str) else u for u in out["urls"]]
    cfg = out.get("config")
    if isinstance(cfg, dict) and isinstance(cfg.get("sources"), list):
        cfg_copy = dict(cfg)
        masked_sources = []
        for s in cfg["sources"]:
            if isinstance(s, dict):
                sc = dict(s)
                if sc.get("type") == "url" and isinstance(sc.get("content"), str):
                    sc["content"] = mask_secret(sc["content"])
                if isinstance(sc.get("userinfoUrl"), str):
                    sc["userinfoUrl"] = mask_secret(sc["userinfoUrl"])
                if isinstance(sc.get("lastParsedContent"), str) and sc.get("type") == "url":
                    sc["lastParsedContent"] = mask_secret(sc["lastParsedContent"])
                masked_sources.append(sc)
            else:
                masked_sources.append(s)
        cfg_copy["sources"] = masked_sources
        out["config"] = cfg_copy
    return out


def format_bytes(num: Any) -> str:
    if not isinstance(num, (int, float)) or num < 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    val = float(num)
    for u in units:
        if val < 1024.0 or u == units[-1]:
            return f"{val:.2f} {u}"
        val /= 1024.0
    return f"{val:.2f} TB"


# ==================== Subscription, Target, and RuleOrder Helpers ====================


def resolve_subscription(client: SubBoostClient, identifier: str) -> dict[str, Any]:
    subs = client.request_json("GET", "/api/subscriptions").get("subscriptions", [])
    if not subs:
        raise SubBoostError("No subscriptions found on this SubBoost instance.", code="NOT_FOUND")
    if identifier in ("@primary", "primary"):
        for s in subs:
            if s.get("isPrimary"):
                return s
        return subs[0]
    for s in subs:
        if s.get("id") == identifier or s.get("name") == identifier:
            return s
    lowered = identifier.lower()
    matches = [s for s in subs if lowered in str(s.get("name", "")).lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(f"{m['name']} ({m['id']})" for m in matches)
        raise SubBoostError(f"Ambiguous subscription '{identifier}': matches {names}", code="AMBIGUOUS")
    available = ", ".join(f"{s['name']} ({s['id']})" for s in subs)
    raise SubBoostError(f"Subscription '{identifier}' not found. Available: {available}", code="NOT_FOUND")


def get_subscription_detail(client: SubBoostClient, identifier: str) -> dict[str, Any]:
    summary = resolve_subscription(client, identifier)
    return client.request_json("GET", f"/api/subscriptions/{summary['id']}")["subscription"]


def strip_emoji_and_space(text: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff/+-]+", "", text, flags=re.UNICODE)
    return cleaned.lower()


def resolve_rule_target(
    detail: dict[str, Any],
    meta: dict[str, Any],
    target_input: str,
) -> tuple[Any, str | None]:
    """
    Resolve a target (e.g. 'ai', '🤖 AI 服务', 'DIRECT', custom group name, dialer group)
    using live subscription config + dynamically fetched instance metadata.
    """
    raw = target_input.strip()
    upper = raw.upper()
    if upper in ("DIRECT", "REJECT", "PASS"):
        return upper, None

    cfg = detail.get("config") or {}
    modules = meta.get("modules") or []
    module_map = {m["id"]: m for m in modules if isinstance(m, dict) and m.get("id")}
    enabled_set = set(get_effective_enabled_modules(cfg, meta))

    if raw.startswith("module:"):
        mod_id = raw.split(":", 1)[1].strip()
        warn = None
        if module_map and mod_id not in module_map:
            warn = f"Module '{mod_id}' was not listed in instance metadata; sending anyway."
        elif enabled_set and mod_id not in enabled_set:
            warn = f"Module '{mod_id}' is not in enabledProxyGroups; rules targeting it may fall back."
        return {"kind": "module", "id": mod_id}, warn

    if raw.startswith("custom:"):
        cid = raw.split(":", 1)[1].strip()
        return {"kind": "custom", "id": cid}, None

    custom_groups = cfg.get("customProxyGroups") or []
    dialer_groups = cfg.get("dialerProxyGroups") or []
    overrides = cfg.get("proxyGroupNameOverrides") or {}
    norm_in = strip_emoji_and_space(raw)

    # 1. Custom proxy groups from live subscription
    for g in custom_groups:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id", ""))
        gname = str(g.get("name", ""))
        if raw == gid or raw == gname or (norm_in and norm_in == strip_emoji_and_space(gname)):
            warn = f"Custom group '{gname}' is disabled." if g.get("enabled") is False else None
            return {"kind": "custom", "id": gid}, warn

    # 2. Dialer relay groups from live subscription (referenced by name string)
    for dg in dialer_groups:
        if not isinstance(dg, dict):
            continue
        dname = str(dg.get("name", ""))
        did = str(dg.get("id", ""))
        if raw == dname or raw == did or (norm_in and norm_in == strip_emoji_and_space(dname)):
            return dname, None

    # 3. Built-in modules from dynamically fetched instance metadata
    for m in modules:
        mid = str(m.get("id", ""))
        mname = str(m.get("name", mid))
        effective_name = str(overrides.get(mid) or mname)
        if (
            raw.lower() == mid.lower()
            or raw == mname
            or raw == effective_name
            or (norm_in and norm_in in (strip_emoji_and_space(mname), strip_emoji_and_space(effective_name)))
        ):
            warn = None
            if enabled_set and mid not in enabled_set:
                warn = f"Built-in group '{effective_name}' ({mid}) is currently not enabled in enabledProxyGroups."
            return {"kind": "module", "id": mid}, warn

    # 4. Also check enabledProxyGroups directly (in case metadata fallback had fewer entries)
    for mid in enabled_set:
        eff = str(overrides.get(mid) or mid)
        if raw.lower() == mid.lower() or raw == eff or (norm_in and norm_in == strip_emoji_and_space(eff)):
            return {"kind": "module", "id": mid}, None

    # 5. Substring fallback
    candidates: list[tuple[str, Any, str | None]] = []
    for g in custom_groups:
        if isinstance(g, dict) and norm_in and norm_in in strip_emoji_and_space(str(g.get("name", ""))):
            candidates.append((str(g.get("name")), {"kind": "custom", "id": g["id"]}, None))
    for m in modules:
        mid = str(m.get("id", ""))
        effective_name = str(overrides.get(mid) or m.get("name", mid))
        if norm_in and norm_in in strip_emoji_and_space(effective_name):
            warn = f"Built-in group '{effective_name}' ({mid}) is not enabled." if enabled_set and mid not in enabled_set else None
            candidates.append((effective_name, {"kind": "module", "id": mid}, warn))

    if len(candidates) == 1:
        return candidates[0][1], candidates[0][2]
    if len(candidates) > 1:
        names = ", ".join(c[0] for c in candidates)
        raise SubBoostError(f"Ambiguous rule target '{target_input}': matches {names}", code="AMBIGUOUS_TARGET")

    raise SubBoostError(
        f"Unknown rule target '{target_input}'. Run `group list <sub_id>` to see available group IDs/names, or use DIRECT / REJECT / module:<id>.",
        code="INVALID_TARGET",
    )


def get_effective_enabled_modules(cfg: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    for key in ("enabledGroups", "enabledProxyGroups", "enabledRules"):
        raw = cfg.get(key)
        if isinstance(raw, list) and len(raw) > 0:
            return [str(x) for x in raw if isinstance(x, str)]
    tpl = cfg.get("template")
    if tpl == "minimal":
        return ["select", "auto", "ad", "private", "cn", "global", "final"]
    modules = meta.get("modules") or []
    return [m["id"] for m in modules if isinstance(m, dict) and m.get("id") and m.get("id") != "adult"]


def build_canonical_pre_match_keys(cfg: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    modules = meta.get("modules") or []
    module_map = {m["id"]: m for m in modules if isinstance(m, dict) and m.get("id")}
    rule_order_seq = meta.get("ruleOrder") or [m["id"] for m in modules if isinstance(m, dict) and m.get("id")]
    enabled = set(get_effective_enabled_modules(cfg, meta))
    keys: list[str] = []

    def emit_module_rules(mod_id: str) -> None:
        mod = module_map.get(mod_id)
        if not mod:
            return
        for rule in mod.get("rules") or []:
            rule_id = rule.get("id") if isinstance(rule, dict) else rule
            if not isinstance(rule_id, str) or not rule_id:
                continue
            if mod_id == "streaming-west" and rule_id == "apple-tvplus":
                continue
            keys.append(f"module:{mod_id}:{rule_id}")

    editable_keys: list[str] = []
    for r in cfg.get("customRules") or []:
        if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"].strip():
            editable_keys.append(f"custom-rule:{r['id'].strip()}")
    for rs in cfg.get("customRuleSets") or []:
        if isinstance(rs, dict) and isinstance(rs.get("id"), str) and rs["id"].strip():
            editable_keys.append(f"custom-rule-set:{rs['id'].strip()}")

    inserted_editable = False
    for mod_id in rule_order_seq:
        if mod_id not in enabled:
            continue
        if mod_id == "gemini" and not inserted_editable:
            keys.extend(editable_keys)
            inserted_editable = True
        if mod_id == "apple" and "streaming-west" in enabled:
            keys.append("module:streaming-west:apple-tvplus")
        emit_module_rules(mod_id)

    for mod_id in enabled:
        if mod_id not in rule_order_seq and mod_id in module_map:
            emit_module_rules(mod_id)

    if not inserted_editable:
        keys.extend(editable_keys)

    if cfg.get("experimentalCnUseCnRuleSet"):
        keys.append("special:experimental-cn")

    return keys


def is_full_rule_order(order: list[str]) -> bool:
    return any(k.startswith(("module:", "special:")) for k in order)


def resolve_current_order_keys(cfg: dict[str, Any], meta: dict[str, Any]) -> tuple[bool, list[str]]:
    raw_order = [k.strip() for k in (cfg.get("ruleOrder") or []) if isinstance(k, str) and k.strip()]
    canonical = build_canonical_pre_match_keys(cfg, meta)
    editable = [k for k in canonical if k.startswith(("custom-rule:", "custom-rule-set:"))]

    if is_full_rule_order(raw_order):
        seen: set[str] = set()
        merged: list[str] = []
        valid_set = set(canonical)
        # Preserve any user-specified keys even if metadata fell back without module rules
        for k in raw_order:
            if (k in valid_set or k.startswith(("module:", "special:"))) and k not in seen:
                merged.append(k)
                seen.add(k)
        for k in canonical:
            if k not in seen:
                merged.append(k)
                seen.add(k)
        return True, merged

    seen = set()
    merged_editable: list[str] = []
    editable_set = set(editable)
    for k in raw_order:
        if k in editable_set and k not in seen:
            merged_editable.append(k)
            seen.add(k)
    for k in editable:
        if k not in seen:
            merged_editable.append(k)
            seen.add(k)

    it = iter(merged_editable)
    visual = [next(it) if k.startswith(("custom-rule:", "custom-rule-set:")) else k for k in canonical]
    return False, visual


def apply_rule_position(cfg: dict[str, Any], meta: dict[str, Any], entry_key: str, position: str) -> list[str]:
    pos = position.strip()
    raw_order = [k.strip() for k in (cfg.get("ruleOrder") or []) if isinstance(k, str) and k.strip()]
    currently_full = is_full_rule_order(raw_order)

    need_full = (
        currently_full
        or pos in ("top", "bottom")
        or (pos.startswith(("before:", "after:")) and pos.split(":", 1)[1].startswith(("module:", "special:")))
    )

    if need_full:
        _, order = resolve_current_order_keys(cfg, meta)
    else:
        canonical = build_canonical_pre_match_keys(cfg, meta)
        editable = [k for k in canonical if k.startswith(("custom-rule:", "custom-rule-set:"))]
        seen: set[str] = set()
        order = []
        for k in raw_order:
            if k in editable and k not in seen:
                order.append(k)
                seen.add(k)
        for k in editable:
            if k not in seen:
                order.append(k)
                seen.add(k)

    if entry_key in order:
        order.remove(entry_key)

    if pos in ("top", "custom-top"):
        order.insert(0, entry_key)
    elif pos in ("bottom", "custom-bottom"):
        order.append(entry_key)
    elif pos.startswith("before:"):
        anchor = pos.split(":", 1)[1].strip()
        if anchor not in order:
            raise SubBoostError(f"Anchor rule key '{anchor}' not found in current active rules.", code="NOT_FOUND")
        order.insert(order.index(anchor), entry_key)
    elif pos.startswith("after:"):
        anchor = pos.split(":", 1)[1].strip()
        if anchor not in order:
            raise SubBoostError(f"Anchor rule key '{anchor}' not found in current active rules.", code="NOT_FOUND")
        order.insert(order.index(anchor) + 1, entry_key)
    else:
        raise SubBoostError(
            f"Invalid position '{position}'. Use top, bottom, custom-top, custom-bottom, before:<key>, or after:<key>.",
            code="INVALID_POSITION",
        )
    return order


def make_rule_id(rule_type: str, value: str, existing_ids: set[str]) -> str:
    slug_type = re.sub(r"[^a-z0-9]+", "-", rule_type.lower()).strip("-") or "rule"
    slug_val = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:24] or "item"
    base = f"custom-rule-{slug_type}-{slug_val}"
    candidate = base
    i = 2
    while candidate in existing_ids:
        candidate = f"{base}-{i}"
        i += 1
    return candidate


def make_ruleset_id(name: str, existing_ids: set[str]) -> str:
    slug = re.sub(r"[^\w.@ -]+", "-", name.strip(), flags=re.UNICODE)
    slug = re.sub(r"\s+", "-", slug).lstrip(".") or "ruleset"
    candidate = slug
    i = 2
    while candidate in existing_ids:
        candidate = f"{slug}-{i}"
        i += 1
    return candidate


def sync_urls_from_sources(config: dict[str, Any], fallback_urls: list[str] | None = None) -> list[str]:
    urls: list[str] = []
    for s in config.get("sources") or []:
        if isinstance(s, dict) and s.get("type") == "url":
            c = str(s.get("content", "")).strip()
            if c:
                for line in c.splitlines():
                    line = line.strip()
                    if line:
                        urls.append(line)
    if urls:
        return urls
    return list(fallback_urls or [])


def put_subscription(client: SubBoostClient, sub_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return client.request_json("PUT", f"/api/subscriptions/{sub_id}", body=payload)["subscription"]


def deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    out = dict(dst)
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ==================== Command Handlers ====================


def cmd_init(args: argparse.Namespace) -> None:
    cfg_path = get_config_path()
    existing = load_json_dict(cfg_path)

    url = (args.url or "").strip()
    username = (args.username or "").strip()
    password = args.password or ""

    if not url:
        default_url = str(existing.get("url") or "")
        prompt = f"SubBoost URL [{default_url}]: " if default_url else "SubBoost URL (e.g. http://localhost:3001): "
        url = input(prompt).strip() or default_url
    if not username:
        default_user = str(existing.get("username") or "")
        prompt = f"Admin username [{default_user}]: " if default_user else "Admin username: "
        username = input(prompt).strip() or default_user
    if not password:
        password = getpass.getpass("Admin password: ")

    url = normalize_base_url(url)
    if not url or not username or not password:
        raise SubBoostError("url, username, and password are all required.", code="VALIDATION_ERROR")

    write_private_json(cfg_path, {"url": url, "username": username, "password": password})
    result: dict[str, Any] = {"configPath": str(cfg_path), "url": url, "username": username, "verified": False}

    if not args.no_verify:
        client = SubBoostClient(url, username, password)
        health = client.request_json("GET", "/api/health/ready", auth_required=False)
        client.login()
        me = client.request_json("GET", "/api/auth/me")
        meta = get_instance_metadata(client, force_refresh=True)
        result["verified"] = True
        result["health"] = health
        result["subscriptionCount"] = (me.get("user") or {}).get("subscriptionCount")
        result["moduleCount"] = len(meta.get("modules") or [])

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Saved SubBoost config to {cfg_path} (permissions 0600).")
        if result["verified"]:
            print(
                f"Verified login to {url} as '{username}' "
                f"(subscriptions: {result.get('subscriptionCount', '?')}, modules synced: {result.get('moduleCount', '?')})."
            )


def cmd_doctor(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    health = client.request_json("GET", "/api/health/ready", auth_required=False)
    me = client.request_json("GET", "/api/auth/me", auth_required=False)
    logged_in_via = "cached-cookie"
    if not me.get("authenticated"):
        client.login()
        me = client.request_json("GET", "/api/auth/me")
        logged_in_via = "fresh-login"
    subs = client.request_json("GET", "/api/subscriptions").get("subscriptions", [])
    meta = get_instance_metadata(client, force_refresh=args.refresh_meta)
    report = {
        "ok": True,
        "url": cfg["url"],
        "username": cfg["username"],
        "configPath": str(get_config_path()),
        "cookieFile": str(get_cookie_file()),
        "metadataFile": str(get_metadata_file()),
        "health": health,
        "auth": logged_in_via,
        "builtinModulesCount": len(meta.get("modules") or []),
        "subscriptionCount": len(subs),
        "subscriptions": [{"id": s["id"], "name": s["name"], "nodeCount": s.get("nodeCount")} for s in subs],
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"URL:      {cfg['url']}")
        print(f"Health:   {health}")
        print(f"Auth:     OK ({logged_in_via}, user={cfg['username']})")
        print(f"Metadata: {len(meta.get('modules') or [])} built-in modules synced from instance")
        print(f"Subs:     {len(subs)}")
        for s in subs:
            primary = " [primary]" if s.get("isPrimary") else ""
            print(f"  - {s['name']} ({s['id']}){primary}: {s.get('nodeCount', 0)} nodes, {s.get('sourceCount', 0)} sources")


def cmd_meta(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    meta = get_instance_metadata(client, force_refresh=args.refresh)
    if args.json:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return
    modules = meta.get("modules") or []
    print(f"Instance metadata from {client.base_url} ({len(modules)} built-in modules):")
    for m in modules:
        rules = [r.get("id") if isinstance(r, dict) else r for r in (m.get("rules") or [])]
        print(f"  - {m.get('id'):16s} {m.get('name')} | rules={rules}")
    print(f"\nCanonical module ruleOrder ({len(meta.get('ruleOrder') or [])}): {', '.join(meta.get('ruleOrder') or [])}")


def cmd_list(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    subs = client.request_json("GET", "/api/subscriptions").get("subscriptions", [])
    if args.json:
        print(json.dumps([mask_subscription_obj(s, args.show_secrets) for s in subs], ensure_ascii=False, indent=2))
        return
    if not subs:
        print("No subscriptions found.")
        return
    for s in subs:
        primary = " [primary]" if s.get("isPrimary") else ""
        interval = f"{s['autoUpdateInterval']}s" if s.get("autoUpdateInterval") else "manual"
        state = s.get("autoUpdateState") or {}
        warn = ""
        if state.get("disabledAt"):
            warn = f" | AUTO-UPDATE DISABLED: {state.get('disabledReason')}"
        elif state.get("externalFailureCount", 0) > 0:
            warn = f" | failures={state.get('externalFailureCount')}"
        print(
            f"- {s['name']}{primary} (id={s['id']})\n"
            f"  nodes={s.get('nodeCount', 0)}, sources={s.get('sourceCount', 0)}, "
            f"interval={interval}, updated={s.get('lastUpdatedAt') or '-'}{warn}"
        )
        if args.show_secrets:
            print(f"  yamlUrl={s.get('yamlUrl')}")


def cmd_show(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    if args.json:
        print(json.dumps(mask_subscription_obj(detail, args.show_secrets), ensure_ascii=False, indent=2))
        return

    meta = get_instance_metadata(client)
    masked = mask_subscription_obj(detail, args.show_secrets)
    sub_cfg = masked.get("config") or {}
    info = masked.get("subscriptionInfo") or {}
    print(f"Subscription: {masked['name']} (id={masked['id']}, primary={masked.get('isPrimary')})")
    print(f"YAML URL:     {masked.get('yamlUrl')}")
    print(f"AutoUpdate:   {masked.get('autoUpdateInterval') or 'off'} | SmartMatch={masked.get('smartNodeMatchingEnabled')}")
    print(f"Updated At:   {masked.get('lastUpdatedAt') or '-'}")
    if info:
        used = (info.get("upload") or 0) + (info.get("download") or 0)
        total = info.get("total")
        print(f"Traffic:      used={format_bytes(used)} / total={format_bytes(total)}, expire={info.get('expire', '-')}")

    sources = sub_cfg.get("sources") or []
    print(f"\nSources ({len(sources)}):")
    for s in sources:
        tag = f" tag={s.get('tag')}" if s.get("tag") else ""
        pp = " [proxy-providers]" if s.get("useProxyProviders") else ""
        preview = str(s.get("content", "")).replace("\n", " | ")
        if len(preview) > 90:
            preview = preview[:87] + "..."
        print(f"  - [{s.get('id')}] type={s.get('type')}{tag}{pp}: {preview}")

    nodes = masked.get("nodes") or []
    by_type: dict[str, int] = {}
    for n in nodes:
        t = str(n.get("type", "unknown"))
        by_type[t] = by_type.get(t, 0) + 1
    print(f"\nNodes ({len(nodes)}): {by_type}")
    for n in nodes[:15]:
        print(f"  - {n.get('name')} ({n.get('type')}, {n.get('server')}:{n.get('port')})")
    if len(nodes) > 15:
        print(f"  ... ({len(nodes) - 15} more nodes; use `nodes` subcommand to inspect)")

    enabled_groups = get_effective_enabled_modules(detail.get("config") or {}, meta)
    print(f"\nEnabled Built-in Groups ({len(enabled_groups)}): {', '.join(enabled_groups)}")
    custom_groups = sub_cfg.get("customProxyGroups") or []
    if custom_groups:
        print(f"Custom Groups ({len(custom_groups)}): " + ", ".join(f"{g.get('name')}({g.get('id')})" for g in custom_groups))
    dialer_groups = sub_cfg.get("dialerProxyGroups") or []
    if dialer_groups:
        print(f"Dialer Relay Groups ({len(dialer_groups)}): " + ", ".join(f"{g.get('name')}({g.get('id')})" for g in dialer_groups))

    custom_rules = sub_cfg.get("customRules") or []
    print(f"\nCustom Rules ({len(custom_rules)}):")
    for r in custom_rules:
        nr = ",no-resolve" if r.get("noResolve") else ""
        print(f"  - [{r.get('id')}] {r.get('type')},{r.get('value')} -> {r.get('target')}{nr}")

    custom_sets = sub_cfg.get("customRuleSets") or []
    if custom_sets:
        print(f"\nCustom RuleSets ({len(custom_sets)}):")
        for rs in custom_sets:
            print(f"  - [{rs.get('id')}] {rs.get('name')} ({rs.get('behavior')}/{rs.get('format', 'mrs')}) {rs.get('path')} -> {rs.get('target')}")

    is_full, order = resolve_current_order_keys(detail.get("config") or {}, meta)
    print(f"\nRule Order Mode: {'full' if is_full else 'editable-only'} ({len(order)} pre-match entries)")
    if sub_cfg.get("nodeNameFilter"):
        print(f"NodeNameFilter: {json.dumps(sub_cfg['nodeNameFilter'], ensure_ascii=False)}")


def cmd_nodes(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    nodes = detail.get("nodes") or []
    if args.search:
        pat = args.search.lower()
        nodes = [n for n in nodes if pat in str(n.get("name", "")).lower() or pat in str(n.get("server", "")).lower()]
    if args.type:
        t = args.type.lower()
        nodes = [n for n in nodes if str(n.get("type", "")).lower() == t]
    if args.json:
        print(json.dumps(nodes, ensure_ascii=False, indent=2))
        return
    print(f"Matched {len(nodes)} nodes:")
    for i, n in enumerate(nodes, 1):
        sid = f" src={n.get('sourceId')}" if n.get("sourceId") else ""
        print(f"{i:3d}. {n.get('name')} [{n.get('type')}] {n.get('server')}:{n.get('port')}{sid}")


def cmd_yaml(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    summary = resolve_subscription(client, args.subscription)
    _, _, yaml_text = client.request_text("GET", f"/api/subscriptions/{summary['token']}/config.yaml", timeout=60)

    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(yaml_text, encoding="utf-8")
        print(f"Wrote {len(yaml_text)} chars ({len(yaml_text.splitlines())} lines) to {out_path}")
        if not args.grep and not args.raw and not args.section:
            return

    if args.raw:
        print(yaml_text)
        return

    lines = yaml_text.splitlines()
    if args.grep:
        regex = re.compile(args.grep, re.I)
        matched = [(idx + 1, ln) for idx, ln in enumerate(lines) if regex.search(ln)]
        print(f"YAML lines={len(lines)}, grep '{args.grep}' matched {len(matched)} lines:")
        for lineno, ln in matched[: args.limit]:
            print(f"{lineno:5d}: {ln}")
        return

    if args.section:
        sec_key = f"{args.section}:"
        capturing = False
        sec_lines: list[str] = []
        for ln in lines:
            if re.match(r"^[a-zA-Z0-9_-]+:\s*$", ln):
                if ln.strip() == sec_key:
                    capturing = True
                    sec_lines.append(ln)
                    continue
                elif capturing:
                    break
            elif capturing:
                sec_lines.append(ln)
        print(f"Section '{args.section}' ({len(sec_lines)} lines):")
        for ln in sec_lines[: args.limit]:
            print(ln)
        if len(sec_lines) > args.limit:
            print(f"... ({len(sec_lines) - args.limit} more lines; pass --limit or --out)")
        return

    top_keys = [ln.rstrip(":") for ln in lines if re.match(r"^[a-zA-Z0-9_-]+:\s*$", ln)]
    rules_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "rules:"), None)
    rule_preview = lines[rules_idx + 1 : rules_idx + 1 + args.limit] if rules_idx is not None else []
    print(f"YAML generated OK: {len(lines)} lines, top-level keys={top_keys}")
    if rule_preview:
        print(f"First {len(rule_preview)} rules:")
        for ln in rule_preview:
            print(f"  {ln}")


def cmd_refresh(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    summary = resolve_subscription(client, args.subscription)
    res = client.request_json("POST", f"/api/subscriptions/{summary['id']}/refresh", timeout=120)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(
            f"Refreshed '{summary['name']}' ({summary['id']}): "
            f"nodes {summary.get('nodeCount', '?')} -> {res.get('nodeCount', '?')}, "
            f"refreshedSources={res.get('refreshedSourceCount', '?')}"
        )
        if res.get("warnings"):
            print("Warnings:", res["warnings"])


def cmd_rule_list(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    meta = get_instance_metadata(client)
    sub_cfg = detail.get("config") or {}
    is_full, order = resolve_current_order_keys(sub_cfg, meta)

    custom_by_key = {f"custom-rule:{r['id']}": r for r in (sub_cfg.get("customRules") or []) if isinstance(r, dict) and r.get("id")}
    ruleset_by_key = {f"custom-rule-set:{rs['id']}": rs for rs in (sub_cfg.get("customRuleSets") or []) if isinstance(rs, dict) and rs.get("id")}
    edits = sub_cfg.get("builtinRuleEdits") or {}

    if args.json:
        print(json.dumps({
            "mode": "full" if is_full else "editable-only",
            "order": order,
            "customRules": sub_cfg.get("customRules") or [],
            "customRuleSets": sub_cfg.get("customRuleSets") or [],
            "builtinRuleEdits": edits,
        }, ensure_ascii=False, indent=2))
        return

    print(f"Rule Order Mode: {'full' if is_full else 'editable-only'}")
    print("Effective Pre-Match Order (top -> bottom, MATCH is always last):")
    for idx, key in enumerate(order, 1):
        if key in custom_by_key:
            r = custom_by_key[key]
            print(f"{idx:3d}. [{key}] {r.get('type')},{r.get('value')} -> {r.get('target')}")
        elif key in ruleset_by_key:
            rs = ruleset_by_key[key]
            print(f"{idx:3d}. [{key}] RULE-SET:{rs.get('name')} ({rs.get('path')}) -> {rs.get('target')}")
        else:
            edit_note = f" (edited: {edits[key]})" if key in edits else ""
            print(f"{idx:3d}. [{key}] <builtin>{edit_note}")


def cmd_rule_add(args: argparse.Namespace) -> None:
    rule_type = args.type.strip().upper()
    if rule_type not in CUSTOM_RULE_TYPES:
        raise SubBoostError(f"Unsupported rule type '{rule_type}'. Supported: {sorted(CUSTOM_RULE_TYPES)}", code="INVALID_RULE_TYPE")
    if args.no_resolve and rule_type not in NO_RESOLVE_TYPES:
        raise SubBoostError(f"Rule type '{rule_type}' does not support --no-resolve. Supported: {sorted(NO_RESOLVE_TYPES)}", code="INVALID_OPTION")

    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    meta = get_instance_metadata(client)
    sub_cfg = dict(detail.get("config") or {})

    target_obj, warn = resolve_rule_target(detail, meta, args.target)
    custom_rules = list(sub_cfg.get("customRules") or [])
    existing_ids = {str(r.get("id")) for r in custom_rules if isinstance(r, dict) and r.get("id")}
    rule_id = args.id.strip() if args.id else make_rule_id(rule_type, args.value, existing_ids)
    if rule_id in existing_ids:
        raise SubBoostError(f"Rule id '{rule_id}' already exists.", code="DUPLICATE_ID")

    new_rule: dict[str, Any] = {
        "id": rule_id,
        "type": rule_type,
        "value": args.value.strip(),
        "target": target_obj,
    }
    if args.no_resolve:
        new_rule["noResolve"] = True

    custom_rules.append(new_rule)
    sub_cfg["customRules"] = custom_rules

    if args.position:
        sub_cfg["ruleOrder"] = apply_rule_position(sub_cfg, meta, f"custom-rule:{rule_id}", args.position)

    updated = put_subscription(client, detail["id"], {"config": sub_cfg})
    out = {"ok": True, "added": new_rule, "orderKey": f"custom-rule:{rule_id}", "subscriptionId": updated["id"]}
    if warn:
        out["warning"] = warn
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"Added rule [{rule_id}] {rule_type},{new_rule['value']} -> {target_obj}")
        if args.position:
            print(f"Placed at position '{args.position}' (key: custom-rule:{rule_id}).")
        if warn:
            print(f"WARNING: {warn}")


def cmd_rule_remove(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    sub_cfg = dict(detail.get("config") or {})
    custom_rules = list(sub_cfg.get("customRules") or [])

    removed: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for r in custom_rules:
        rid = str(r.get("id", ""))
        val = str(r.get("value", ""))
        hit = False
        if args.id and rid == args.id.strip():
            hit = True
        elif args.match and args.match.lower() in f"{r.get('type','')},{val}".lower():
            hit = True
        if hit:
            removed.append(r)
        else:
            kept.append(r)

    if not removed:
        raise SubBoostError("No matching custom rule found to remove.", code="NOT_FOUND")

    sub_cfg["customRules"] = kept
    removed_keys = {f"custom-rule:{r['id']}" for r in removed if r.get("id")}
    if isinstance(sub_cfg.get("ruleOrder"), list):
        sub_cfg["ruleOrder"] = [k for k in sub_cfg["ruleOrder"] if k not in removed_keys]

    put_subscription(client, detail["id"], {"config": sub_cfg})
    if args.json:
        print(json.dumps({"ok": True, "removed": removed}, ensure_ascii=False, indent=2))
    else:
        print(f"Removed {len(removed)} custom rule(s):")
        for r in removed:
            print(f"  - [{r.get('id')}] {r.get('type')},{r.get('value')}")


def cmd_rule_move(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    meta = get_instance_metadata(client)
    sub_cfg = dict(detail.get("config") or {})

    key = args.key.strip()
    canonical = build_canonical_pre_match_keys(sub_cfg, meta)
    if key not in canonical:
        if f"custom-rule:{key}" in canonical:
            key = f"custom-rule:{key}"
        elif f"custom-rule-set:{key}" in canonical:
            key = f"custom-rule-set:{key}"
        elif not key.startswith(("module:", "special:")):
            raise SubBoostError(f"Rule key '{args.key}' not found in active rules. Run `rule list` to see exact keys.", code="NOT_FOUND")

    to_pos = args.to.strip()
    if to_pos.startswith(("before:", "after:")):
        prefix, anchor = to_pos.split(":", 1)
        if anchor not in canonical:
            if f"custom-rule:{anchor}" in canonical:
                to_pos = f"{prefix}:custom-rule:{anchor}"
            elif f"custom-rule-set:{anchor}" in canonical:
                to_pos = f"{prefix}:custom-rule-set:{anchor}"

    new_order = apply_rule_position(sub_cfg, meta, key, to_pos)
    sub_cfg["ruleOrder"] = new_order
    put_subscription(client, detail["id"], {"config": sub_cfg})
    if args.json:
        print(json.dumps({"ok": True, "key": key, "to": to_pos, "ruleOrder": new_order}, ensure_ascii=False, indent=2))
    else:
        idx = new_order.index(key) + 1
        mode = "full" if is_full_rule_order(new_order) else "editable-only"
        print(f"Moved '{key}' -> '{to_pos}' (now at position #{idx} in {mode} ruleOrder).")


def cmd_ruleset_add(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    meta = get_instance_metadata(client)
    sub_cfg = dict(detail.get("config") or {})

    target_obj, warn = resolve_rule_target(detail, meta, args.target)
    rule_sets = list(sub_cfg.get("customRuleSets") or [])
    existing_ids = {str(rs.get("id")) for rs in rule_sets if isinstance(rs, dict) and rs.get("id")}
    rs_id = args.id.strip() if args.id else make_ruleset_id(args.name, existing_ids)
    if rs_id in existing_ids:
        raise SubBoostError(f"RuleSet id '{rs_id}' already exists.", code="DUPLICATE_ID")

    rs_obj: dict[str, Any] = {
        "id": rs_id,
        "name": args.name.strip(),
        "behavior": args.behavior,
        "format": args.format,
        "path": args.path.strip(),
        "target": target_obj,
    }
    if args.no_resolve:
        rs_obj["noResolve"] = True

    rule_sets.append(rs_obj)
    sub_cfg["customRuleSets"] = rule_sets
    if args.position:
        sub_cfg["ruleOrder"] = apply_rule_position(sub_cfg, meta, f"custom-rule-set:{rs_id}", args.position)

    put_subscription(client, detail["id"], {"config": sub_cfg})
    out = {"ok": True, "added": rs_obj, "orderKey": f"custom-rule-set:{rs_id}"}
    if warn:
        out["warning"] = warn
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"Added rule-set [{rs_id}] {rs_obj['name']} ({rs_obj['path']}) -> {target_obj}")
        if warn:
            print(f"WARNING: {warn}")


def cmd_ruleset_remove(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    sub_cfg = dict(detail.get("config") or {})
    rule_sets = list(sub_cfg.get("customRuleSets") or [])
    kept = [rs for rs in rule_sets if rs.get("id") != args.id.strip()]
    if len(kept) == len(rule_sets):
        raise SubBoostError(f"RuleSet '{args.id}' not found.", code="NOT_FOUND")
    sub_cfg["customRuleSets"] = kept
    key = f"custom-rule-set:{args.id.strip()}"
    if isinstance(sub_cfg.get("ruleOrder"), list):
        sub_cfg["ruleOrder"] = [k for k in sub_cfg["ruleOrder"] if k != key]
    put_subscription(client, detail["id"], {"config": sub_cfg})
    print(f"Removed rule-set '{args.id}'.")


def cmd_rule_search(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    params = {"keyword": args.keyword, "size": str(args.size), "page": str(args.page)}
    if args.type:
        params["type"] = args.type
    qs = urllib.parse.urlencode(params)
    res = client.request_json("GET", f"/api/rules/search?{qs}", auth_required=False)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    items = res.get("items") or []
    print(f"Found {res.get('totalMatched', len(items))} matching built-in rules (showing {len(items)}):")
    for it in items:
        label = it.get("nameZh") or it.get("name") or it.get("id")
        loc = it.get("path") or it.get("url") or "-"
        fmt = f"/{it.get('format')}" if it.get("format") else ""
        print(f"  - {it.get('id')} ({label}) | {it.get('behavior')}{fmt} | {loc}")


def cmd_rule_detect(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    res = client.request_json("POST", "/api/rules/detect", body={"url": args.url})
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_source_add(args: argparse.Namespace) -> None:
    if bool(args.content) == bool(args.file):
        raise SubBoostError("Specify exactly one of --content or --file.", code="VALIDATION_ERROR")
    content = Path(args.file).expanduser().read_text(encoding="utf-8") if args.file else args.content
    content = content.strip()
    if not content:
        raise SubBoostError("Source content is empty.", code="VALIDATION_ERROR")

    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    sub_cfg = dict(detail.get("config") or {})
    sources = list(sub_cfg.get("sources") or [])

    new_source: dict[str, Any] = {
        "id": args.id.strip() if args.id else str(uuid.uuid4()),
        "type": args.type,
        "content": content,
    }
    if args.tag:
        new_source["tag"] = args.tag.strip()
    if args.name_template:
        new_source["nameTemplate"] = args.name_template
    if args.use_proxy_providers and args.type == "url":
        new_source["useProxyProviders"] = True

    sources.append(new_source)
    sub_cfg["sources"] = sources
    urls = sync_urls_from_sources(sub_cfg, detail.get("urls"))

    put_subscription(client, detail["id"], {"urls": urls, "config": sub_cfg})
    out: dict[str, Any] = {"ok": True, "sourceId": new_source["id"], "type": args.type, "refreshed": False}

    if not args.no_refresh:
        refresh_res = client.request_json("POST", f"/api/subscriptions/{detail['id']}/refresh", timeout=120)
        out["refreshed"] = True
        out["nodeCount"] = refresh_res.get("nodeCount")
        out["refresh"] = refresh_res

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        msg = f"Added {args.type} source [{new_source['id']}] to '{detail['name']}'."
        if out["refreshed"]:
            msg += f" Refreshed -> total nodes: {out.get('nodeCount', '?')}."
        print(msg)


def cmd_source_remove(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    sub_cfg = dict(detail.get("config") or {})
    sources = list(sub_cfg.get("sources") or [])
    sid = args.id.strip()
    kept = [s for s in sources if s.get("id") != sid]
    if len(kept) == len(sources):
        raise SubBoostError(f"Source '{sid}' not found.", code="NOT_FOUND")

    sub_cfg["sources"] = kept
    urls = sync_urls_from_sources(sub_cfg, [])
    nodes = list(detail.get("nodes") or [])
    if args.prune_nodes:
        nodes = [n for n in nodes if n.get("sourceId") != sid]

    if not urls and not nodes and not kept:
        raise SubBoostError("Cannot remove the last source when no nodes remain (SubBoost requires at least 1 URL or node).", code="VALIDATION_ERROR")

    put_subscription(client, detail["id"], {"urls": urls, "nodes": nodes, "config": sub_cfg})
    if args.refresh and kept:
        res = client.request_json("POST", f"/api/subscriptions/{detail['id']}/refresh", timeout=120)
        print(f"Removed source '{sid}' and refreshed -> nodes: {res.get('nodeCount', '?')}.")
    else:
        print(f"Removed source '{sid}'.")


def cmd_source_probe(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    res = client.request_json("POST", "/api/source-import", body={"url": args.url}, timeout=60)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        pr = res.get("parseResult") or {}
        print(f"Probe OK: nodes={pr.get('nodeCount', len(pr.get('nodes', [])))}, errors={len(pr.get('errors', []))}")


def cmd_node_remove(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    nodes = list(detail.get("nodes") or [])
    regex = re.compile(args.pattern, re.I) if args.pattern else None

    kept = []
    removed = []
    for n in nodes:
        name = str(n.get("name", ""))
        hit = False
        if args.name and name == args.name:
            hit = True
        elif regex and regex.search(name):
            hit = True
        elif args.source_id and n.get("sourceId") == args.source_id:
            hit = True
        if hit:
            removed.append(name)
        else:
            kept.append(n)

    if not removed:
        raise SubBoostError("No nodes matched removal criteria.", code="NOT_FOUND")
    if not kept and not (detail.get("urls") or []):
        raise SubBoostError("Removing all nodes leaves 0 nodes and 0 URLs, which SubBoost rejects.", code="VALIDATION_ERROR")

    put_subscription(client, detail["id"], {"nodes": kept})
    if args.json:
        print(json.dumps({"ok": True, "removedCount": len(removed), "remainingCount": len(kept), "removed": removed}, ensure_ascii=False, indent=2))
    else:
        print(f"Removed {len(removed)} node(s); remaining: {len(kept)}.")


def cmd_node_rename(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    nodes = list(detail.get("nodes") or [])
    changed = 0
    for n in nodes:
        if n.get("name") == args.old_name:
            n["name"] = args.new_name
            changed += 1
    if changed == 0:
        raise SubBoostError(f"Node '{args.old_name}' not found.", code="NOT_FOUND")
    put_subscription(client, detail["id"], {"nodes": nodes})
    print(f"Renamed {changed} node(s): '{args.old_name}' -> '{args.new_name}'.")


def cmd_filter_set(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    sub_cfg = dict(detail.get("config") or {})

    if args.clear:
        sub_cfg["nodeNameFilter"] = {"enabled": False, "excludeRegexes": []}
    else:
        flt = dict(sub_cfg.get("nodeNameFilter") or {"enabled": True, "excludeRegexes": []})
        if args.include is not None:
            flt["includeRegexes"] = args.include
            flt["includeEnabled"] = len(args.include) > 0
        if args.exclude is not None:
            flt["excludeRegexes"] = args.exclude
            flt["excludeEnabled"] = len(args.exclude) > 0
        flt["enabled"] = bool(
            (flt.get("includeEnabled") and flt.get("includeRegexes"))
            or (flt.get("excludeEnabled", True) and flt.get("excludeRegexes"))
        )
        flt.setdefault("excludeRegexes", [])
        sub_cfg["nodeNameFilter"] = flt

    put_subscription(client, detail["id"], {"config": sub_cfg})
    print(f"Updated nodeNameFilter: {json.dumps(sub_cfg.get('nodeNameFilter'), ensure_ascii=False)}")


def cmd_group_list(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    meta = get_instance_metadata(client)
    sub_cfg = detail.get("config") or {}
    enabled = set(get_effective_enabled_modules(sub_cfg, meta))
    hidden = set(sub_cfg.get("hiddenProxyGroups") or [])
    overrides = sub_cfg.get("proxyGroupNameOverrides") or {}
    modules = meta.get("modules") or []

    # Include any enabled IDs not found in metadata so newer server versions never hide groups
    seen_ids = {m.get("id") for m in modules if isinstance(m, dict)}
    all_modules = list(modules) + [{"id": gid, "name": gid, "rules": []} for gid in enabled if gid not in seen_ids]

    if args.json:
        print(json.dumps({
            "builtin": [
                {
                    "id": m["id"],
                    "name": overrides.get(m["id"], m.get("name", m["id"])),
                    "enabled": m["id"] in enabled,
                    "hidden": m["id"] in hidden,
                    "rules": [r.get("id") if isinstance(r, dict) else r for r in (m.get("rules") or [])],
                }
                for m in all_modules
            ],
            "customProxyGroups": sub_cfg.get("customProxyGroups") or [],
            "dialerProxyGroups": sub_cfg.get("dialerProxyGroups") or [],
        }, ensure_ascii=False, indent=2))
        return

    print("Built-in Proxy Group Modules (from live instance):")
    for m in all_modules:
        mid = m["id"]
        is_on = mid in enabled
        mark = "ON " if is_on else "OFF"
        hid = " [hidden]" if mid in hidden else ""
        disp = overrides.get(mid, m.get("name", mid))
        rids = [r.get("id") if isinstance(r, dict) else r for r in (m.get("rules") or [])]
        rules_str = f" rules={rids}" if rids else ""
        print(f"  [{mark}] {mid:15s} {disp}{hid}{rules_str}")
    if sub_cfg.get("customProxyGroups"):
        print("\nCustom Proxy Groups:")
        for g in sub_cfg["customProxyGroups"]:
            gtype = g.get("groupType") or g.get("type")
            print(f"  - [{g.get('id')}] {g.get('name')} type={gtype} enabled={g.get('enabled', True)}")
    if sub_cfg.get("dialerProxyGroups"):
        print("\nDialer Relay Groups:")
        for g in sub_cfg["dialerProxyGroups"]:
            print(f"  - [{g.get('id')}] {g.get('name')} relay={g.get('relayNodes')} -> target={g.get('targetNodes')}")


def cmd_group_toggle(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)
    meta = get_instance_metadata(client)
    sub_cfg = dict(detail.get("config") or {})
    enabled = get_effective_enabled_modules(sub_cfg, meta)

    for gid in args.enable or []:
        if gid not in enabled:
            enabled.append(gid)
    for gid in args.disable or []:
        if gid in enabled:
            enabled.remove(gid)

    sub_cfg["enabledGroups"] = enabled
    sub_cfg["enabledRules"] = enabled
    if "enabledProxyGroups" in sub_cfg:
        sub_cfg["enabledProxyGroups"] = enabled
    put_subscription(client, detail["id"], {"config": sub_cfg})
    print(f"Updated enabledGroups ({len(enabled)}): {', '.join(enabled)}")


def cmd_patch(args: argparse.Namespace) -> None:
    if bool(args.json_str) == bool(args.file):
        raise SubBoostError("Specify exactly one of --json or --file.", code="VALIDATION_ERROR")
    raw_patch = Path(args.file).expanduser().read_text(encoding="utf-8") if args.file else args.json_str
    patch_obj = json.loads(raw_patch)
    if not isinstance(patch_obj, dict):
        raise SubBoostError("Patch payload must be a JSON object.", code="VALIDATION_ERROR")

    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    detail = get_subscription_detail(client, args.subscription)

    put_body: dict[str, Any] = {}
    for k in ("name", "urls", "nodes", "autoUpdateInterval", "smartNodeMatchingEnabled", "subscriptionInfo"):
        if k in patch_obj:
            put_body[k] = patch_obj[k]

    if "config" in patch_obj:
        if not isinstance(patch_obj["config"], dict):
            raise SubBoostError("patch.config must be a JSON object.", code="VALIDATION_ERROR")
        existing_cfg = dict(detail.get("config") or {})
        put_body["config"] = patch_obj["config"] if args.replace_config else deep_merge(existing_cfg, patch_obj["config"])

    if not put_body:
        raise SubBoostError("Patch object has no recognized subscription fields.", code="VALIDATION_ERROR")

    updated = put_subscription(client, detail["id"], put_body)
    print(json.dumps(mask_subscription_obj(updated, args.show_secrets), ensure_ascii=False, indent=2))


def cmd_create(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    sources: list[dict[str, Any]] = []
    urls: list[str] = []

    for u in args.url or []:
        u = u.strip()
        if u:
            urls.append(u)
            sources.append({"id": str(uuid.uuid4()), "type": "url", "content": u})

    nodes: list[dict[str, Any]] = []
    if args.nodes_json:
        nodes = json.loads(Path(args.nodes_json).expanduser().read_text(encoding="utf-8"))

    if not urls and not nodes:
        raise SubBoostError(
            "POST /api/subscriptions requires at least one --url (or pre-parsed --nodes-json). "
            "To add raw vless:// or YAML nodes without a URL, create with a URL first or pass --nodes-json.",
            code="VALIDATION_ERROR",
        )

    body: dict[str, Any] = {
        "name": args.name.strip(),
        "urls": urls,
        "nodes": nodes,
        "config": {
            "template": args.template,
            "sources": sources,
        },
    }
    created = client.request_json("POST", "/api/subscriptions", body=body)["subscription"]
    if not args.no_refresh:
        client.request_json("POST", f"/api/subscriptions/{created['id']}/refresh", timeout=120)
        created = client.request_json("GET", f"/api/subscriptions/{created['id']}")["subscription"]
    print(json.dumps(mask_subscription_obj(created, args.show_secrets), ensure_ascii=False, indent=2))


def cmd_delete(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    summary = resolve_subscription(client, args.subscription)
    client.request_json("DELETE", f"/api/subscriptions/{summary['id']}")
    print(f"Deleted subscription '{summary['name']}' ({summary['id']}).")


def cmd_reset_link(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    summary = resolve_subscription(client, args.subscription)
    res = client.request_json(
        "POST",
        f"/api/subscriptions/{summary['id']}/reset-link",
        headers={"If-Match": f'"{summary["token"]}"'},
    )
    out = res.get("subscription") or res
    if not args.show_secrets and isinstance(out, dict):
        out = mask_subscription_obj(out, False)
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_templates(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    if args.id:
        res = client.request_json("GET", f"/api/templates/{urllib.parse.quote(args.id)}")
    else:
        res = client.request_json("GET", f"/api/templates?type={args.tab}")
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_settings(args: argparse.Namespace) -> None:
    cfg = load_runtime_config()
    client = SubBoostClient(cfg["url"], cfg["username"], cfg["password"])
    if args.allow_unsafe is not None:
        val = args.allow_unsafe.lower() in ("true", "1", "yes", "on")
        res = client.request_json("PATCH", "/api/settings/source-import", body={"allowUnsafeSubscriptionSources": val})
    else:
        res = client.request_json("GET", "/api/settings/source-import")
    print(json.dumps(res, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SubBoost Clash/Mihomo Subscription Manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    # init
    sp = sub.add_parser("init", help="Write ~/.config/subboost/config.json and verify login")
    sp.add_argument("--url")
    sp.add_argument("--username")
    sp.add_argument("--password")
    sp.add_argument("--no-verify", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_init)

    # doctor
    sp = sub.add_parser("doctor", help="Check config, health, auth, sync metadata, and list subscriptions")
    sp.add_argument("--refresh-meta", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    # meta
    sp = sub.add_parser("meta", help="Show built-in proxy groups & ruleOrder dynamically fetched from the instance")
    sp.add_argument("--refresh", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_meta)

    # list
    sp = sub.add_parser("list", help="List all subscriptions")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--show-secrets", action="store_true")
    sp.set_defaults(func=cmd_list)

    # show
    sp = sub.add_parser("show", help="Show subscription details (id, name, or @primary)")
    sp.add_argument("subscription")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--show-secrets", action="store_true")
    sp.set_defaults(func=cmd_show)

    # nodes
    sp = sub.add_parser("nodes", help="List/search parsed nodes in a subscription")
    sp.add_argument("subscription")
    sp.add_argument("--search")
    sp.add_argument("--type")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_nodes)

    # yaml
    sp = sub.add_parser("yaml", help="Fetch and inspect generated Clash/Mihomo config.yaml")
    sp.add_argument("subscription")
    sp.add_argument("--grep")
    sp.add_argument("--section", choices=["proxies", "proxy-groups", "rules", "rule-providers", "dns", "listeners"])
    sp.add_argument("--limit", type=int, default=40)
    sp.add_argument("--raw", action="store_true")
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_yaml)

    # refresh
    sp = sub.add_parser("refresh", help="Refresh upstream URLs and parse static sources into nodes")
    sp.add_argument("subscription")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_refresh)

    # rule group
    sp_rule = sub.add_parser("rule", help="Manage custom rules, ruleOrder, and search built-in rules")
    rsub = sp_rule.add_subparsers(dest="rule_cmd", required=True)

    sp = rsub.add_parser("list", help="List rules and effective matching order")
    sp.add_argument("subscription")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_rule_list)

    sp = rsub.add_parser("add", help="Add a custom rule")
    sp.add_argument("subscription")
    sp.add_argument("--type", required=True, help="e.g. DOMAIN-SUFFIX, DOMAIN, DOMAIN-KEYWORD, IP-CIDR, PROCESS-NAME")
    sp.add_argument("--value", required=True)
    sp.add_argument("--target", required=True, help="Target group (e.g. ai, select, DIRECT, REJECT, or custom group name)")
    sp.add_argument("--no-resolve", action="store_true")
    sp.add_argument("--position", help="top, bottom, custom-top, custom-bottom, before:<key>, after:<key>")
    sp.add_argument("--id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_rule_add)

    sp = rsub.add_parser("remove", help="Remove custom rule(s)")
    sp.add_argument("subscription")
    sp.add_argument("--id")
    sp.add_argument("--match")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_rule_remove)

    sp = rsub.add_parser("move", help="Move any rule or rule-set in ruleOrder")
    sp.add_argument("subscription")
    sp.add_argument("--key", required=True, help="e.g. custom-rule:<id>, custom-rule-set:<id>, or module:<mod>:<rule>")
    sp.add_argument("--to", required=True, help="top, bottom, custom-top, custom-bottom, before:<key>, after:<key>")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_rule_move)

    sp = rsub.add_parser("search", help="Search SubBoost built-in 2000+ rule catalog")
    sp.add_argument("keyword")
    sp.add_argument("--type", choices=["domain", "ipcidr", "classical"])
    sp.add_argument("--size", type=int, default=20)
    sp.add_argument("--page", type=int, default=1)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_rule_search)

    sp = rsub.add_parser("detect", help="Detect behavior/format of an external rule-set URL")
    sp.add_argument("url")
    sp.set_defaults(func=cmd_rule_detect)

    # rule-set group
    sp_rs = sub.add_parser("rule-set", help="Manage customRuleSets")
    rssub = sp_rs.add_subparsers(dest="rs_cmd", required=True)

    sp = rssub.add_parser("add", help="Add a custom rule-set")
    sp.add_argument("subscription")
    sp.add_argument("--name", required=True)
    sp.add_argument("--behavior", required=True, choices=["domain", "ipcidr", "classical"])
    sp.add_argument("--format", default="mrs", choices=["mrs", "yaml", "text"])
    sp.add_argument("--path", required=True, help="e.g. geosite/openai.mrs or https://...")
    sp.add_argument("--target", required=True)
    sp.add_argument("--no-resolve", action="store_true")
    sp.add_argument("--position")
    sp.add_argument("--id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_ruleset_add)

    sp = rssub.add_parser("remove", help="Remove a custom rule-set")
    sp.add_argument("subscription")
    sp.add_argument("--id", required=True)
    sp.set_defaults(func=cmd_ruleset_remove)

    # source group
    sp_src = sub.add_parser("source", help="Manage subscription sources (url / nodes / yaml) and auto-refresh")
    ssub = sp_src.add_subparsers(dest="src_cmd", required=True)

    sp = ssub.add_parser("add", help="Add a URL, raw node links, or YAML proxy source and refresh")
    sp.add_argument("subscription")
    sp.add_argument("--type", required=True, choices=["url", "nodes", "yaml"])
    sp.add_argument("--content")
    sp.add_argument("--file")
    sp.add_argument("--tag")
    sp.add_argument("--name-template")
    sp.add_argument("--use-proxy-providers", action="store_true")
    sp.add_argument("--id")
    sp.add_argument("--no-refresh", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_source_add)

    sp = ssub.add_parser("remove", help="Remove a source by id")
    sp.add_argument("subscription")
    sp.add_argument("--id", required=True)
    sp.add_argument("--prune-nodes", action="store_true")
    sp.add_argument("--refresh", action="store_true")
    sp.set_defaults(func=cmd_source_remove)

    sp = ssub.add_parser("probe", help="Test-fetch a subscription URL without modifying anything")
    sp.add_argument("url")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_source_probe)

    # node group
    sp_node = sub.add_parser("node", help="Directly remove or rename parsed nodes")
    nsub = sp_node.add_subparsers(dest="node_cmd", required=True)

    sp = nsub.add_parser("remove", help="Remove nodes by exact name, regex pattern, or sourceId")
    sp.add_argument("subscription")
    sp.add_argument("--name")
    sp.add_argument("--pattern")
    sp.add_argument("--source-id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_node_remove)

    sp = nsub.add_parser("rename", help="Rename a node")
    sp.add_argument("subscription")
    sp.add_argument("--old-name", required=True)
    sp.add_argument("--new-name", required=True)
    sp.set_defaults(func=cmd_node_rename)

    # filter
    sp_flt = sub.add_parser("filter", help="Configure nodeNameFilter")
    fsub = sp_flt.add_subparsers(dest="flt_cmd", required=True)

    sp = fsub.add_parser("set", help="Set or clear nodeNameFilter")
    sp.add_argument("subscription")
    sp.add_argument("--include", nargs="*")
    sp.add_argument("--exclude", nargs="*")
    sp.add_argument("--clear", action="store_true")
    sp.set_defaults(func=cmd_filter_set)

    # group
    sp_grp = sub.add_parser("group", help="Inspect or toggle built-in/custom proxy groups")
    gsub = sp_grp.add_subparsers(dest="grp_cmd", required=True)

    sp = gsub.add_parser("list", help="List built-in, custom, and dialer proxy groups")
    sp.add_argument("subscription")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_group_list)

    sp = gsub.add_parser("toggle", help="Enable or disable built-in proxy group modules")
    sp.add_argument("subscription")
    sp.add_argument("--enable", nargs="*")
    sp.add_argument("--disable", nargs="*")
    sp.set_defaults(func=cmd_group_toggle)

    # patch
    sp = sub.add_parser("patch", help="Safely deep-merge arbitrary JSON patch into subscription")
    sp.add_argument("subscription")
    sp.add_argument("--json", dest="json_str")
    sp.add_argument("--file")
    sp.add_argument("--replace-config", action="store_true", help="Replace config instead of deep-merging")
    sp.add_argument("--show-secrets", action="store_true")
    sp.set_defaults(func=cmd_patch)

    # create / delete / reset-link / templates / settings
    sp = sub.add_parser("create", help="Create a new subscription")
    sp.add_argument("--name", required=True)
    sp.add_argument("--url", action="append")
    sp.add_argument("--nodes-json")
    sp.add_argument("--template", default="standard", choices=["minimal", "standard", "full"])
    sp.add_argument("--no-refresh", action="store_true")
    sp.add_argument("--show-secrets", action="store_true")
    sp.set_defaults(func=cmd_create)

    sp = sub.add_parser("delete", help="Delete a subscription")
    sp.add_argument("subscription")
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("reset-link", help="Rotate subscription YAML token")
    sp.add_argument("subscription")
    sp.add_argument("--show-secrets", action="store_true")
    sp.set_defaults(func=cmd_reset_link)

    sp = sub.add_parser("templates", help="List or inspect templates")
    sp.add_argument("--tab", default="default", choices=["default", "my"])
    sp.add_argument("--id")
    sp.set_defaults(func=cmd_templates)

    sp = sub.add_parser("settings", help="Get or update allowUnsafeSubscriptionSources")
    sp.add_argument("--allow-unsafe", choices=["true", "false"])
    sp.set_defaults(func=cmd_settings)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except SubBoostError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "error": exc.message}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

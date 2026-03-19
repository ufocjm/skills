#!/usr/bin/env python3

from __future__ import annotations

import json
from typing import Any

from common import build_full_url, cookie_headers, request_url


def fetch_play_html(play_page_url: str, phpsessid: str) -> str:
    response = request_url(build_full_url(play_page_url), headers=cookie_headers(phpsessid))
    return response.text()


def parse_play_page(html: str, play_page_url: str) -> dict[str, Any]:
    payload = {
        "play_page_url": play_page_url,
        "play_page_full_url": build_full_url(play_page_url),
        "player": {
            "url": "",
            "url_next": "",
            "from": "",
            "server": "",
            "encrypt": 0,
            "id": "",
            "sid": 0,
            "nid": 0,
            "vod_data": {},
        },
    }

    script_object = _extract_player_object(html)
    if not script_object:
        return payload

    try:
        player = json.loads(script_object)
    except json.JSONDecodeError:
        return payload

    payload["player"] = {
        "url": player.get("url", ""),
        "url_next": player.get("url_next", ""),
        "from": player.get("from", ""),
        "server": player.get("server", ""),
        "encrypt": player.get("encrypt", 0),
        "id": str(player.get("id", "")),
        "sid": int(player.get("sid", 0) or 0),
        "nid": int(player.get("nid", 0) or 0),
        "vod_data": player.get("vod_data", {}) if isinstance(player.get("vod_data", {}), dict) else {},
    }
    return payload


def _extract_player_object(html: str) -> str:
    marker = "var player_aaaa="
    start = html.find(marker)
    if start == -1:
        return ""

    brace_start = html.find("{", start)
    if brace_start == -1:
        return ""

    depth = 0
    in_string = False
    escaped = False
    quote = ""
    for index in range(brace_start, len(html)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue

        if char in ('"', "'"):
            in_string = True
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[brace_start : index + 1]
    return ""

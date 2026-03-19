#!/usr/bin/env python3

from __future__ import annotations

import re
from typing import Any

from common import (
    build_full_url,
    build_search_url,
    cookie_headers,
    extract_numeric_id_from_url,
    normalize_space,
    request_url,
    split_values,
    strip_tags,
)


def fetch_search_html(keyword: str, phpsessid: str) -> str:
    response = request_url(build_search_url(keyword), headers=cookie_headers(phpsessid))
    return response.text()


def is_verify_page(html: str) -> bool:
    lowered = html.lower()
    return "系统安全验证" in html or "请输入验证码" in html or "<title>系统安全验证" in lowered


def parse_search_results(html: str, keyword: str) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    blocks = re.findall(r"(?is)<dl\b.*?</dl>", html)

    for block in blocks:
        if "名称：" not in block:
            continue

        name_match = re.search(
            r'(?is)名称：\s*<strong>\s*<a href="([^"]+)">([^<]+)</a>\s*</strong>\s*(?:<span[^>]*class="ss1"[^>]*>(.*?)</span>)?',
            block,
        )
        if not name_match:
            continue

        detail_url = name_match.group(1).strip()
        name = normalize_space(name_match.group(2))
        tag_text = strip_tags(name_match.group(3) or "")

        alias = _field_text(block, "又名")
        region_raw, types_raw = _region_types(block)
        actors_raw = _field_text(block, "主演")
        update_time = _field_text(block, "时间")
        summary = _field_text(block, "简介")

        results.append(
            {
                "id": extract_numeric_id_from_url(detail_url),
                "name": name,
                "detail_url": detail_url,
                "detail_full_url": build_full_url(detail_url),
                "tag_text": tag_text,
                "alias": alias,
                "region": region_raw,
                "types": split_values(types_raw),
                "actors": split_values(actors_raw),
                "update_time": update_time,
                "summary": summary,
            }
        )

    count_hint = ""
    count_match = re.search(r"为您找到\s*([0-9]+)\s*条与相关的资源", html)
    if count_match:
        count_hint = count_match.group(1)

    return {
        "keyword": keyword,
        "need_verify": False,
        "is_verify_page": False,
        "count_hint": count_hint,
        "results": results,
    }


def _field_text(block: str, field_name: str) -> str:
    match = re.search(
        rf'(?is)<p[^>]*>\s*{re.escape(field_name)}：\s*(.*?)</p>',
        block,
    )
    return strip_tags(match.group(1)) if match else ""


def _region_types(block: str) -> tuple[str, str]:
    match = re.search(
        r"(?is)<p[^>]*>\s*地区：\s*(.*?)\s*[　 ]+\s*类型：\s*(.*?)</p>",
        block,
    )
    if not match:
        return "", ""
    return strip_tags(match.group(1)), strip_tags(match.group(2))

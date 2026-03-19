#!/usr/bin/env python3

from __future__ import annotations

import html
import json
import re
import ssl
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

BASE_URL = "https://www.qwnull.com"
SEARCH_PATH = "/vs/-------------.html"
DEFAULT_TIMEOUT = 30
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


@dataclass
class HttpResponse:
    url: str
    status: int
    headers: Any
    body: bytes

    def text(self, fallback: str = "utf-8") -> str:
        charset = self.headers.get_content_charset() or fallback
        return self.body.decode(charset, errors="replace")


def build_full_url(url: str) -> str:
    return urllib.parse.urljoin(f"{BASE_URL}/", url)


def build_search_url(keyword: str) -> str:
    query = urllib.parse.quote(keyword)
    return f"{build_full_url(SEARCH_PATH)}?wd={query}"


def make_temp_dir(prefix: str = "qwnull-media-search.") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


def request_url(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> HttpResponse:
    request_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method.upper())
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return HttpResponse(
            url=response.geturl(),
            status=response.status,
            headers=response.headers,
            body=response.read(),
        )


def extract_phpsessid_from_headers(headers: Any) -> str:
    for value in headers.get_all("Set-Cookie", []):
        match = re.search(r"PHPSESSID=([^;]+)", value, re.I)
        if match:
            return match.group(1)
    return ""


def cookie_headers(phpsessid: str) -> dict[str, str]:
    return {"Cookie": f"PHPSESSID={phpsessid}"}


def normalize_space(value: str) -> str:
    value = html.unescape(value).replace("\xa0", " ")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def strip_tags(value: str) -> str:
    value = re.sub(r"(?is)<script\b.*?</script>", " ", value)
    value = re.sub(r"(?is)<style\b.*?</style>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return normalize_space(value)


def split_values(value: str) -> list[str]:
    text = normalize_space(value)
    if not text:
        return []
    parts = re.split(r"\s*[\/|,，、]\s*|\s{2,}", text)
    return [item.strip() for item in parts if item.strip()]


def unique_texts(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = normalize_space(value)
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def extract_numeric_id_from_url(url: str) -> str:
    match = re.search(r"/(\d+)\.html", url)
    return match.group(1) if match else ""


def classify_clipboard(value: str) -> str:
    text = value.strip()
    lowered = text.lower()
    if not text:
        return "unknown"
    if lowered.startswith("magnet:"):
        return "magnet"
    if any(
        host in lowered
        for host in (
            "pan.baidu.com",
            "quark.cn",
            "drive.uc.cn",
            "aliyundrive.com",
            "alipan.com",
            "cloud.189.cn",
            "115.com",
            "123pan.com",
            "123684.com",
            "123865.com",
            "pan.xunlei.com",
        )
    ):
        return "pan"
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return "external"
    return "unknown"


def dump_json(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)

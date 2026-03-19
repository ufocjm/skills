#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common import (
    BASE_URL,
    build_search_url,
    dump_json,
    extract_phpsessid_from_headers,
    make_temp_dir,
    request_url,
)


def fetch_captcha() -> dict[str, Any]:
    temp_dir = make_temp_dir()
    response = request_url(f"{BASE_URL}/index.php/verify/index.html")
    phpsessid = extract_phpsessid_from_headers(response.headers)
    if not phpsessid:
        raise RuntimeError("Failed to extract PHPSESSID from captcha response")

    image_path = temp_dir / "verify.png"
    image_path.write_bytes(response.body)
    header_path = temp_dir / "verify_headers.txt"
    header_path.write_text(str(response.headers), encoding="utf-8")

    return {
        "temp_dir": str(temp_dir),
        "image_path": str(image_path),
        "header_path": str(header_path),
        "phpsessid": phpsessid,
    }


def verify_search_session(phpsessid: str, verify_code: str, keyword: str) -> dict[str, Any]:
    referer = build_search_url(keyword)
    response = request_url(
        f"{BASE_URL}/index.php/ajax/verify_check?type=search&verify={verify_code}",
        method="POST",
        data=b"",
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": f"PHPSESSID={phpsessid}",
            "Origin": BASE_URL,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    raw = response.text()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"code": -1, "msg": raw}
    payload["raw"] = raw
    return payload


def write_resume_state(
    *,
    keyword: str,
    phpsessid: str,
    image_path: str,
    session_source: str,
    provided_phpsessid_invalid: bool,
) -> str:
    temp_dir = Path(image_path).parent
    resume_path = temp_dir / "session.json"
    payload = {
        "keyword": keyword,
        "phpsessid": phpsessid,
        "image_path": image_path,
        "session_source": session_source,
        "provided_phpsessid_invalid": provided_phpsessid_invalid,
    }
    resume_path.write_text(dump_json(payload, pretty=True), encoding="utf-8")
    return str(resume_path)


def load_resume_state(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))

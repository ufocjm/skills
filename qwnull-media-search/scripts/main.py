#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
from time import perf_counter
from typing import Any

from common import dump_json
from detail import fetch_detail_html, parse_detail_page
from play import fetch_play_html, parse_play_page
from search import fetch_search_html, is_verify_page, parse_search_results
from session import fetch_captcha, load_resume_state, verify_search_session, write_resume_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search qwnull media and expand structured results.")
    parser.add_argument("--keyword", help="Search keyword. Required unless --resume-path is used.")
    parser.add_argument("--phpsessid", help="Optional qwnull PHPSESSID. If valid it is used directly.")
    parser.add_argument("--verify-code", help="Captcha code for a fetched verification image.")
    parser.add_argument("--resume-path", help="Resume a pending captcha flow from a previous run.")
    parser.add_argument(
        "--resolve-streams",
        choices=("none", "first", "all"),
        default="first",
        help="Resolve play-page stream URLs: none, first entry per source, or all entries.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=0,
        help="Only expand the first N search results. 0 means expand every result.",
    )
    parser.add_argument(
        "--output",
        choices=("json", "pretty"),
        default="pretty",
        help="JSON output formatting.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    pretty = args.output == "pretty"

    try:
        result = run(args)
    except Exception as exc:
        print(dump_json({"status": "error", "error": str(exc)}, pretty=pretty))
        return 1

    print(dump_json(result, pretty=pretty))
    return 0 if result.get("status") != "needs_verify" else 2


def run(args: argparse.Namespace) -> dict[str, Any]:
    started_at = perf_counter()
    timings = _new_timings()

    if args.resume_path:
        if not args.verify_code:
            raise ValueError("--resume-path requires --verify-code")
        state, _ = _measure(
            timings,
            "resume_state_load",
            load_resume_state,
            args.resume_path,
            summary_key="resume_state_load_ms",
        )
        keyword = state["keyword"]
        phpsessid = state["phpsessid"]
        session_source = state["session_source"]
        provided_invalid = bool(state["provided_phpsessid_invalid"])
        verify_response, _ = _measure(
            timings,
            "verify_check",
            verify_search_session,
            phpsessid,
            args.verify_code,
            keyword,
            summary_key="verify_check_ms",
        )
        if verify_response.get("code") != 1:
            return _finalize_result(
                _needs_verify_result(
                    keyword=keyword,
                    session_source=session_source,
                    provided_invalid=provided_invalid,
                    timings=timings,
                ),
                timings,
                started_at,
            )
        return _finalize_result(
            _run_search_pipeline(
                keyword=keyword,
                phpsessid=phpsessid,
                session_source=session_source,
                provided_invalid=provided_invalid,
                max_results=args.max_results,
                resolve_streams=args.resolve_streams,
                timings=timings,
            ),
            timings,
            started_at,
        )

    keyword = (args.keyword or "").strip()
    if not keyword:
        raise ValueError("--keyword is required unless --resume-path is used")

    if args.phpsessid and args.verify_code:
        verify_response, _ = _measure(
            timings,
            "verify_check",
            verify_search_session,
            args.phpsessid,
            args.verify_code,
            keyword,
            summary_key="verify_check_ms",
        )
        if verify_response.get("code") != 1:
            return _finalize_result(
                _needs_verify_result(
                    keyword=keyword,
                    session_source="auto_verified",
                    provided_invalid=False,
                    timings=timings,
                ),
                timings,
                started_at,
            )
        return _finalize_result(
            _run_search_pipeline(
                keyword=keyword,
                phpsessid=args.phpsessid,
                session_source="auto_verified",
                provided_invalid=False,
                max_results=args.max_results,
                resolve_streams=args.resolve_streams,
                timings=timings,
            ),
            timings,
            started_at,
        )

    if args.phpsessid:
        html, _ = _measure(
            timings,
            "search_fetch",
            fetch_search_html,
            keyword,
            args.phpsessid,
            summary_key="search_fetch_ms",
        )
        if not is_verify_page(html):
            return _finalize_result(
                _run_search_pipeline(
                    keyword=keyword,
                    phpsessid=args.phpsessid,
                    session_source="provided",
                    provided_invalid=False,
                    max_results=args.max_results,
                    resolve_streams=args.resolve_streams,
                    prefetched_search_html=html,
                    timings=timings,
                    prefetched_search_html_already_timed=True,
                ),
                timings,
                started_at,
            )
        return _finalize_result(
            _needs_verify_result(
                keyword=keyword,
                session_source="provided_then_refreshed",
                provided_invalid=True,
                timings=timings,
            ),
            timings,
            started_at,
        )

    return _finalize_result(
        _needs_verify_result(
            keyword=keyword,
            session_source="auto_verified",
            provided_invalid=False,
            timings=timings,
        ),
        timings,
        started_at,
    )


def _needs_verify_result(
    *,
    keyword: str,
    session_source: str,
    provided_invalid: bool,
    timings: dict[str, Any],
) -> dict[str, Any]:
    captcha, _ = _measure(
        timings,
        "captcha_fetch",
        fetch_captcha,
        summary_key="captcha_fetch_ms",
    )
    resume_path, _ = _measure(
        timings,
        "resume_state_write",
        write_resume_state,
        keyword=keyword,
        phpsessid=captcha["phpsessid"],
        image_path=captcha["image_path"],
        session_source=session_source,
        provided_phpsessid_invalid=provided_invalid,
        summary_key="resume_state_write_ms",
    )
    return {
        "status": "needs_verify",
        "keyword": keyword,
        "phpsessid": captcha["phpsessid"],
        "image_path": captcha["image_path"],
        "resume_path": resume_path,
        "session_source": session_source,
        "provided_phpsessid_invalid": provided_invalid,
        "message": (
            f"Read the captcha image and rerun with --resume-path {resume_path} --verify-code <4 digits>"
        ),
    }


def _run_search_pipeline(
    *,
    keyword: str,
    phpsessid: str,
    session_source: str,
    provided_invalid: bool,
    max_results: int,
    resolve_streams: str,
    prefetched_search_html: str | None = None,
    timings: dict[str, Any],
    prefetched_search_html_already_timed: bool = False,
) -> dict[str, Any]:
    if prefetched_search_html is not None:
        search_html = prefetched_search_html
        if prefetched_search_html_already_timed:
            _record_timing(
                timings,
                "search_fetch_prefetched",
                0,
                summary_key=None,
                metadata={"cached": True},
            )
    else:
        search_html, _ = _measure(
            timings,
            "search_fetch",
            fetch_search_html,
            keyword,
            phpsessid,
            summary_key="search_fetch_ms",
        )
    if is_verify_page(search_html):
        return _needs_verify_result(
            keyword=keyword,
            session_source="provided_then_refreshed" if session_source == "provided" else session_source,
            provided_invalid=True,
            timings=timings,
        )

    parsed, _ = _measure(
        timings,
        "search_parse",
        parse_search_results,
        search_html,
        keyword,
        summary_key="search_parse_ms",
    )
    results = parsed["results"]

    if max_results > 0:
        expandable = results[:max_results]
    else:
        expandable = results

    for item in expandable:
        detail_html, detail_fetch_ms = _measure(
            timings,
            "detail_fetch",
            fetch_detail_html,
            item["detail_url"],
            phpsessid,
            summary_key="detail_fetch_total_ms",
            metadata={"detail_url": item["detail_url"], "id": item.get("id", "")},
        )
        detail, detail_parse_ms = _measure(
            timings,
            "detail_parse",
            parse_detail_page,
            detail_html,
            item["detail_url"],
            summary_key="detail_parse_total_ms",
            metadata={"detail_url": item["detail_url"], "id": item.get("id", "")},
        )
        play_timing = _resolve_play_streams(detail["play_groups"], phpsessid, resolve_streams, timings)
        item["detail"] = detail
        item["timings"] = {
            "detail_fetch_ms": detail_fetch_ms,
            "detail_parse_ms": detail_parse_ms,
            "play_fetch_total_ms": play_timing["play_fetch_total_ms"],
            "play_parse_total_ms": play_timing["play_parse_total_ms"],
        }

    return {
        "status": "ok",
        "keyword": keyword,
        "phpsessid": phpsessid,
        "session_source": session_source,
        "provided_phpsessid_invalid": provided_invalid,
        "need_verify": False,
        "count_hint": parsed.get("count_hint", ""),
        "results": results,
    }


def _resolve_play_streams(
    play_groups: list[dict[str, Any]],
    phpsessid: str,
    mode: str,
    timings: dict[str, Any],
) -> dict[str, int]:
    totals = {"play_fetch_total_ms": 0, "play_parse_total_ms": 0}
    if mode == "none":
        return totals

    for group in play_groups:
        entries = group.get("entries", [])
        if mode == "first":
            target_entries = entries[:1]
        else:
            target_entries = entries

        for entry in target_entries:
            play_html, play_fetch_ms = _measure(
                timings,
                "play_fetch",
                fetch_play_html,
                entry["play_page_url"],
                phpsessid,
                summary_key="play_fetch_total_ms",
                metadata={"play_page_url": entry["play_page_url"]},
            )
            payload, play_parse_ms = _measure(
                timings,
                "play_parse",
                parse_play_page,
                play_html,
                entry["play_page_url"],
                summary_key="play_parse_total_ms",
                metadata={"play_page_url": entry["play_page_url"]},
            )
            player = payload["player"]
            entry["stream_url"] = player.get("url", "")
            entry["stream_url_next"] = player.get("url_next", "")
            entry["player_from"] = player.get("from", "")
            entry["player_server"] = player.get("server", "")
            entry["player_encrypt"] = player.get("encrypt", 0)
            entry["player_id"] = player.get("id", "")
            entry["player_sid"] = player.get("sid", 0)
            entry["player_nid"] = player.get("nid", 0)
            entry["timings"] = {
                "play_fetch_ms": play_fetch_ms,
                "play_parse_ms": play_parse_ms,
            }
            totals["play_fetch_total_ms"] += play_fetch_ms
            totals["play_parse_total_ms"] += play_parse_ms
    return totals


def _new_timings() -> dict[str, Any]:
    return {"summary": {}, "steps": []}


def _elapsed_ms(started_at: float) -> int:
    return int(round((perf_counter() - started_at) * 1000))


def _record_timing(
    timings: dict[str, Any],
    name: str,
    elapsed_ms: int,
    *,
    summary_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    event: dict[str, Any] = {"name": name, "ms": elapsed_ms}
    if metadata:
        event.update(metadata)
    timings["steps"].append(event)
    if summary_key:
        timings["summary"][summary_key] = timings["summary"].get(summary_key, 0) + elapsed_ms


def _measure(
    timings: dict[str, Any],
    name: str,
    func,
    *args,
    summary_key: str | None = None,
    metadata: dict[str, Any] | None = None,
    **kwargs,
):
    started_at = perf_counter()
    value = func(*args, **kwargs)
    elapsed_ms = _elapsed_ms(started_at)
    _record_timing(timings, name, elapsed_ms, summary_key=summary_key, metadata=metadata)
    return value, elapsed_ms


def _finalize_result(result: dict[str, Any], timings: dict[str, Any], started_at: float) -> dict[str, Any]:
    timings["summary"]["total_ms"] = _elapsed_ms(started_at)
    result["timings"] = timings
    return result


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        "--detail-workers",
        type=int,
        default=4,
        help="Max worker threads for detail-page fetch and parse.",
    )
    parser.add_argument(
        "--play-workers",
        type=int,
        default=6,
        help="Max worker threads for play-page fetch and parse.",
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
                detail_workers=args.detail_workers,
                play_workers=args.play_workers,
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
                detail_workers=args.detail_workers,
                play_workers=args.play_workers,
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
                    detail_workers=args.detail_workers,
                    play_workers=args.play_workers,
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
    detail_workers: int,
    play_workers: int,
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

    if expandable:
        _expand_results_in_parallel(
            expandable=expandable,
            phpsessid=phpsessid,
            resolve_streams=resolve_streams,
            detail_workers=detail_workers,
            play_workers=play_workers,
            timings=timings,
        )

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


def _expand_results_in_parallel(
    *,
    expandable: list[dict[str, Any]],
    phpsessid: str,
    resolve_streams: str,
    detail_workers: int,
    play_workers: int,
    timings: dict[str, Any],
) -> None:
    worker_count = _normalize_workers(detail_workers, len(expandable))
    if worker_count == 1:
        for index, item in enumerate(expandable):
            expanded_item, local_timings = _expand_single_result(
                item,
                phpsessid,
                resolve_streams,
                play_workers,
            )
            expandable[index] = expanded_item
            _merge_timings(timings, local_timings)
        return

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                _expand_single_result,
                item,
                phpsessid,
                resolve_streams,
                play_workers,
            ): index
            for index, item in enumerate(expandable)
        }
        ordered_results: list[dict[str, Any] | None] = [None] * len(expandable)
        for future in as_completed(future_map):
            index = future_map[future]
            expanded_item, local_timings = future.result()
            ordered_results[index] = expanded_item
            _merge_timings(timings, local_timings)

    for index, item in enumerate(ordered_results):
        if item is not None:
            expandable[index] = item


def _expand_single_result(
    item: dict[str, Any],
    phpsessid: str,
    resolve_streams: str,
    play_workers: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    local_timings = _new_timings()
    expanded_item = dict(item)

    detail_html, detail_fetch_ms = _measure(
        local_timings,
        "detail_fetch",
        fetch_detail_html,
        item["detail_url"],
        phpsessid,
        summary_key="detail_fetch_total_ms",
        metadata={"detail_url": item["detail_url"], "id": item.get("id", "")},
    )
    detail, detail_parse_ms = _measure(
        local_timings,
        "detail_parse",
        parse_detail_page,
        detail_html,
        item["detail_url"],
        summary_key="detail_parse_total_ms",
        metadata={"detail_url": item["detail_url"], "id": item.get("id", "")},
    )
    play_timing = _resolve_play_streams(
        detail["play_groups"],
        phpsessid,
        resolve_streams,
        local_timings,
        play_workers,
    )
    expanded_item["detail"] = detail
    expanded_item["timings"] = {
        "detail_fetch_ms": detail_fetch_ms,
        "detail_parse_ms": detail_parse_ms,
        "play_fetch_total_ms": play_timing["play_fetch_total_ms"],
        "play_parse_total_ms": play_timing["play_parse_total_ms"],
    }
    return expanded_item, local_timings


def _resolve_play_streams(
    play_groups: list[dict[str, Any]],
    phpsessid: str,
    mode: str,
    timings: dict[str, Any],
    play_workers: int,
) -> dict[str, int]:
    totals = {"play_fetch_total_ms": 0, "play_parse_total_ms": 0}
    if mode == "none":
        return totals

    targets = _collect_play_targets(play_groups, mode)
    worker_count = _normalize_workers(play_workers, len(targets))
    if worker_count == 1:
        for group_index, entry_index in targets:
            updated_entry, local_timings, play_fetch_ms, play_parse_ms = _resolve_single_play_entry(
                play_groups[group_index]["entries"][entry_index],
                phpsessid,
            )
            play_groups[group_index]["entries"][entry_index] = updated_entry
            totals["play_fetch_total_ms"] += play_fetch_ms
            totals["play_parse_total_ms"] += play_parse_ms
            _merge_timings(timings, local_timings)
        return totals

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                _resolve_single_play_entry,
                play_groups[group_index]["entries"][entry_index],
                phpsessid,
            ): (group_index, entry_index)
            for group_index, entry_index in targets
        }
        for future in as_completed(future_map):
            group_index, entry_index = future_map[future]
            updated_entry, local_timings, play_fetch_ms, play_parse_ms = future.result()
            play_groups[group_index]["entries"][entry_index] = updated_entry
            totals["play_fetch_total_ms"] += play_fetch_ms
            totals["play_parse_total_ms"] += play_parse_ms
            _merge_timings(timings, local_timings)
    return totals


def _collect_play_targets(play_groups: list[dict[str, Any]], mode: str) -> list[tuple[int, int]]:
    targets: list[tuple[int, int]] = []
    for group_index, group in enumerate(play_groups):
        entries = group.get("entries", [])
        if mode == "first":
            limit = min(1, len(entries))
        else:
            limit = len(entries)
        for entry_index in range(limit):
            targets.append((group_index, entry_index))
    return targets


def _resolve_single_play_entry(
    entry: dict[str, Any],
    phpsessid: str,
) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    local_timings = _new_timings()
    updated_entry = dict(entry)

    play_html, play_fetch_ms = _measure(
        local_timings,
        "play_fetch",
        fetch_play_html,
        entry["play_page_url"],
        phpsessid,
        summary_key="play_fetch_total_ms",
        metadata={"play_page_url": entry["play_page_url"]},
    )
    payload, play_parse_ms = _measure(
        local_timings,
        "play_parse",
        parse_play_page,
        play_html,
        entry["play_page_url"],
        summary_key="play_parse_total_ms",
        metadata={"play_page_url": entry["play_page_url"]},
    )
    player = payload["player"]
    updated_entry["stream_url"] = player.get("url", "")
    updated_entry["stream_url_next"] = player.get("url_next", "")
    updated_entry["player_from"] = player.get("from", "")
    updated_entry["player_server"] = player.get("server", "")
    updated_entry["player_encrypt"] = player.get("encrypt", 0)
    updated_entry["player_id"] = player.get("id", "")
    updated_entry["player_sid"] = player.get("sid", 0)
    updated_entry["player_nid"] = player.get("nid", 0)
    updated_entry["timings"] = {
        "play_fetch_ms": play_fetch_ms,
        "play_parse_ms": play_parse_ms,
    }
    return updated_entry, local_timings, play_fetch_ms, play_parse_ms


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


def _merge_timings(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source["summary"].items():
        target["summary"][key] = target["summary"].get(key, 0) + value
    target["steps"].extend(source["steps"])


def _normalize_workers(requested_workers: int, task_count: int) -> int:
    if task_count <= 0:
        return 1
    return max(1, min(requested_workers, task_count))


def _finalize_result(result: dict[str, Any], timings: dict[str, Any], started_at: float) -> dict[str, Any]:
    timings["summary"]["total_ms"] = _elapsed_ms(started_at)
    result["timings"] = timings
    return result


if __name__ == "__main__":
    sys.exit(main())

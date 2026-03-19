---
name: qwnull-media-search
description: Search qwnull.com media by keyword, optionally reuse a provided PHPSESSID, and return structured search results with detail pages, download links, pan links, play-page URLs, and playable stream data from player_aaaa when available. Use when a user wants qwnull search automation, qwnull HTML parsing, detail/download extraction, or needs captcha-assisted session creation for qwnull search.
---

# Qwnull Media Search

Use the Python entrypoint in this skill to search qwnull and expand results into structured JSON. Reuse a provided `PHPSESSID` when available; otherwise fetch a captcha session, read the 4-digit image, verify it, and continue the search.

## Workflow

1. If the user already has a `PHPSESSID`, run:

```bash
python3 scripts/main.py --keyword "复仇者联盟" --phpsessid "<session>"
```

2. If the provided session works, the script returns final structured JSON immediately.
3. If the provided session is invalid, or if no session was supplied, the script returns:
   - `status: "needs_verify"`
   - `image_path`
   - `resume_path`
   - the captcha-bound `phpsessid`
4. Open `image_path`, read the 4 digits, then resume:

```bash
python3 scripts/main.py --resume-path /tmp/qwnull-media-search.xxxxx/session.json --verify-code 5953
```

5. Return the final JSON result set.
6. Inspect the `timings` field in the returned JSON to see which steps are slow.

## Rules

1. If a user provides `PHPSESSID`, do not pre-verify it. Try the search directly first.
2. Only fall back to captcha flow when the search page actually returns the qwnull verification page.
3. Keep the captcha image and the `PHPSESSID` from the same fetch.
4. If the captcha is read incorrectly, rerun the main script with the new `resume_path` and the next 4-digit code.
5. Always use the real detail-page hrefs from search results. Never synthesize `/mv/...` or `/tv/...` paths.
6. Parse detail pages by section, not by sweeping the entire page with broad regexes.
7. For play pages, prefer `player_aaaa.url` over visible DOM guesses.
8. Stream resolution is best effort. Preserve `play_page_url` even when `stream_url` is unavailable.
9. `--resolve-streams first` is the default to keep live requests bounded. Use `all` only when the user explicitly wants every play entry resolved.
10. `--detail-workers` and `--play-workers` control the thread-pool size for detail-page and play-page fetches.
11. In concurrent runs, some `*_total_ms` values are cumulative task time, so they may be larger than `total_ms`, which is the real wall-clock time.

## Commands

Search with a known session:

```bash
python3 scripts/main.py --keyword "权力的游戏" --phpsessid "<session>"
```

Start without a session:

```bash
python3 scripts/main.py --keyword "权力的游戏"
```

Resume after reading the captcha:

```bash
python3 scripts/main.py --resume-path /tmp/qwnull-media-search.xxxxx/session.json --verify-code 7712
```

Resolve every play-page stream instead of the first entry per source:

```bash
python3 scripts/main.py --keyword "复仇者联盟" --phpsessid "<session>" --resolve-streams all
```

Tune concurrency explicitly:

```bash
python3 scripts/main.py --keyword "叶问3" --phpsessid "<session>" --detail-workers 4 --play-workers 6
```

## Output Contract

Needs verification:

```json
{
  "status": "needs_verify",
  "keyword": "复仇者联盟",
  "phpsessid": "...",
  "image_path": "/tmp/qwnull-media-search.xxxxx/verify.png",
  "resume_path": "/tmp/qwnull-media-search.xxxxx/session.json",
  "session_source": "auto_verified",
  "provided_phpsessid_invalid": false,
  "timings": {
    "summary": {
      "captcha_fetch_ms": 0,
      "resume_state_write_ms": 0,
      "total_ms": 0
    },
    "steps": []
  }
}
```

Final result:

```json
{
  "keyword": "复仇者联盟",
  "phpsessid": "...",
  "session_source": "provided",
  "provided_phpsessid_invalid": false,
  "need_verify": false,
  "timings": {
    "summary": {
      "search_fetch_ms": 0,
      "search_parse_ms": 0,
      "detail_fetch_total_ms": 0,
      "detail_parse_total_ms": 0,
      "play_fetch_total_ms": 0,
      "play_parse_total_ms": 0,
      "total_ms": 0
    },
    "steps": []
  },
  "results": [
    {
      "id": "77178",
      "name": "复仇者联盟(2012)",
      "detail_url": "/mv/77178.html",
      "timings": {
        "detail_fetch_ms": 0,
        "detail_parse_ms": 0,
        "play_fetch_total_ms": 0,
        "play_parse_total_ms": 0
      },
      "detail": {
        "play_groups": [],
        "thunder_groups": [],
        "pan_groups": []
      }
    }
  ]
}
```

## Scripts

1. `scripts/main.py`: CLI entrypoint and workflow orchestrator.
2. `scripts/session.py`: captcha fetch, session verification, resume-state helpers.
3. `scripts/search.py`: search-page fetch and result parsing.
4. `scripts/detail.py`: detail-page parsing for metadata, play groups, thunder groups, and pan groups.
5. `scripts/play.py`: play-page parsing via `player_aaaa`.

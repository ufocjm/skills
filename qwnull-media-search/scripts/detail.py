#!/usr/bin/env python3

from __future__ import annotations

import re
from typing import Any

from common import (
    build_full_url,
    classify_clipboard,
    cookie_headers,
    extract_numeric_id_from_url,
    normalize_space,
    request_url,
    split_values,
    unique_texts,
)
from htmltools import ancestor, direct_children, find_all, find_first, matches, parse_html, text_content, Node


def fetch_detail_html(detail_url: str, phpsessid: str) -> str:
    response = request_url(build_full_url(detail_url), headers=cookie_headers(phpsessid))
    return response.text()


def parse_detail_page(html: str, detail_url: str) -> dict[str, Any]:
    root = parse_html(html)
    main_ui_meta = find_first(root, lambda node: matches(node, class_names=("main-ui-meta",)))
    movie_intro = find_first(root, lambda node: matches(node, class_names=("movie-introduce",)))

    detail: dict[str, Any] = {
        "id": extract_numeric_id_from_url(detail_url),
        "name": "",
        "year": "",
        "cover": "",
        "status": "",
        "last_update": "",
        "director": [],
        "writer": [],
        "actors": [],
        "types": [],
        "region": [],
        "language": [],
        "release_date": "",
        "duration": "",
        "alias": "",
        "douban_score": "",
        "imdb_score": "",
        "description_short": "",
        "description_full": "",
        "play_groups": [],
        "thunder_groups": [],
        "pan_groups": [],
    }

    if main_ui_meta:
        _fill_basic_info(detail, root, main_ui_meta)
    if movie_intro:
        detail["description_short"], detail["description_full"] = _extract_descriptions(movie_intro)

    detail["play_groups"] = _extract_play_groups(root)
    detail["thunder_groups"] = _extract_download_groups(root, "迅雷下载")
    detail["pan_groups"] = _extract_download_groups(root, "网盘下载")
    return detail


def _fill_basic_info(detail: dict[str, Any], root: Node, meta: Node) -> None:
    h1 = find_first(meta, lambda node: matches(node, tag="h1"))
    if h1:
        year_node = find_first(h1, lambda node: matches(node, tag="span", class_names=("year",)))
        year_text = text_content(year_node) if year_node else ""
        title_text = text_content(h1)
        detail["year"] = normalize_space(year_text.strip("()（）"))
        detail["name"] = normalize_space(title_text.replace(year_text, "", 1))

    image_box = find_first(root, lambda node: matches(node, tag="div", class_names=("img",)))
    if image_box:
        image = find_first(image_box, lambda node: matches(node, tag="img"))
        if image and image.attr("src"):
            detail["cover"] = build_full_url(image.attr("src"))

    otherbox = find_first(meta, lambda node: matches(node, class_names=("otherbox",)))
    if otherbox:
        em_values = [text_content(node) for node in find_all(otherbox, lambda node: matches(node, tag="em"))]
        if em_values:
            detail["status"] = normalize_space(em_values[0])
        if len(em_values) > 1:
            detail["last_update"] = normalize_space(em_values[1])

    for div in direct_children(meta, lambda node: matches(node, tag="div")):
        span_children = direct_children(div, lambda node: matches(node, tag="span"))
        label_span = span_children[0] if span_children else None
        if not label_span:
            continue
        label = normalize_space(text_content(label_span)).rstrip("：:")
        if not label:
            continue
        full_text = text_content(div)
        label_text = text_content(label_span)
        raw_value = normalize_space(full_text.replace(label_text, "", 1))
        anchor_values = unique_texts(
            text_content(anchor)
            for anchor in find_all(div, lambda node: matches(node, tag="a"))
            if text_content(anchor)
        )

        if label == "导演":
            detail["director"] = anchor_values or split_values(raw_value)
        elif label == "编剧":
            detail["writer"] = anchor_values or split_values(raw_value)
        elif label == "主演":
            detail["actors"] = anchor_values or split_values(raw_value)
        elif label == "类型":
            detail["types"] = anchor_values or split_values(raw_value)
        elif label == "地区":
            detail["region"] = anchor_values or split_values(raw_value)
        elif label == "语言":
            detail["language"] = anchor_values or split_values(raw_value)
        elif label == "上映":
            detail["release_date"] = raw_value
        elif label == "片长":
            detail["duration"] = raw_value
        elif label == "又名":
            detail["alias"] = raw_value
        elif "豆瓣" in label:
            detail["douban_score"] = _extract_score(raw_value, anchor_values)
        elif "imdb" in label.lower():
            detail["imdb_score"] = _extract_score(raw_value, anchor_values)


def _extract_score(raw_value: str, anchor_values: list[str]) -> str:
    for value in anchor_values + [raw_value]:
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)", value)
        if match:
            return match.group(1)
    return ""


def _extract_descriptions(movie_intro: Node) -> tuple[str, str]:
    short_node = find_first(movie_intro, lambda node: matches(node, tag="p", class_names=("zkjj_a",)))
    full_node = find_first(movie_intro, lambda node: matches(node, tag="p", class_names=("sqjj_a",)))

    short_text = _cleanup_description(text_content(short_node) if short_node else "")
    full_text = _cleanup_description(text_content(full_node) if full_node else "")
    if not full_text:
        full_text = short_text
    return short_text, full_text


def _cleanup_description(text: str) -> str:
    text = normalize_space(text)
    text = text.replace("[展开全部]", "").replace("[收起部分]", "")
    return normalize_space(text)


def _extract_play_groups(root: Node) -> list[dict[str, Any]]:
    section = find_first(root, lambda node: matches(node, id_="url"))
    if not section:
        return []

    tabs_container = find_first(section, lambda node: matches(node, class_names=("py-tabs",)))
    tab_names = [text_content(node) for node in direct_children(tabs_container, lambda child: matches(child, tag="li"))] if tabs_container else []
    player_uls = find_all(section, lambda node: matches(node, tag="ul", class_names=("player",)))

    groups: list[dict[str, Any]] = []
    for idx, player_ul in enumerate(player_uls):
        entries: list[dict[str, Any]] = []
        for item in direct_children(player_ul, lambda node: matches(node, tag="li")):
            anchor = find_first(item, lambda node: matches(node, tag="a"))
            if not anchor or not anchor.attr("href"):
                continue
            play_page_url = anchor.attr("href").strip()
            entries.append(
                {
                    "label": text_content(anchor),
                    "play_page_url": play_page_url,
                    "play_page_full_url": build_full_url(play_page_url),
                    "stream_url": "",
                    "stream_url_next": "",
                    "player_from": "",
                    "player_server": "",
                    "player_encrypt": 0,
                    "player_id": "",
                    "player_sid": 0,
                    "player_nid": 0,
                }
            )

        groups.append(
            {
                "source_name": normalize_space(tab_names[idx]) if idx < len(tab_names) else "",
                "entries": entries,
            }
        )
    return groups


def _extract_download_groups(root: Node, title: str) -> list[dict[str, Any]]:
    wrap = _find_wrap_section_by_title(root, title)
    if not wrap:
        return []

    down_link = find_first(wrap, lambda node: matches(node, class_names=("down-link",)))
    if not down_link:
        return []

    tabs = [
        text_content(node)
        for node in find_all(down_link, lambda node: matches(node, tag="li", class_names=("title",)))
    ]
    contents = find_all(down_link, lambda node: matches(node, tag="ul", class_names=("gdt", "content")))

    groups: list[dict[str, Any]] = []
    for idx, content in enumerate(contents):
        items: list[dict[str, Any]] = []
        for item in direct_children(content, lambda node: matches(node, tag="li", class_names=("down-list2",))):
            title_box = find_first(item, lambda node: matches(node, tag="p", class_names=("down-list3",)))
            title_anchor = find_first(title_box, lambda node: matches(node, tag="a")) if title_box else None
            clipboard_anchor = find_first(
                item,
                lambda node: matches(node, tag="a") and bool(node.attr("data-clipboard-text")),
            )
            href = ""
            clipboard = ""
            if clipboard_anchor:
                href = clipboard_anchor.attr("href")
                clipboard = clipboard_anchor.attr("data-clipboard-text")
            elif title_anchor:
                href = title_anchor.attr("href")

            items.append(
                {
                    "title": text_content(title_anchor) if title_anchor else "",
                    "href": href,
                    "full_href": build_full_url(href) if href and href.startswith("/") else href,
                    "clipboard": clipboard,
                    "kind": classify_clipboard(clipboard or href),
                }
            )

        groups.append(
            {
                "group_name": normalize_space(tabs[idx]) if idx < len(tabs) else "",
                "items": items,
            }
        )
    return groups


def _find_wrap_section_by_title(root: Node, title: str) -> Node | None:
    heading = find_first(root, lambda node: matches(node, tag="h2") and text_content(node) == title)
    if not heading:
        heading = find_first(root, lambda node: matches(node, tag="h2") and title in text_content(node))
    if not heading:
        return None
    wrap = ancestor(heading, lambda node: matches(node, tag="div", class_names=("wrap", "row")))
    return wrap or heading.parent

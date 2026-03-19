#!/usr/bin/env python3

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Iterable

VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = None
    data: str = ""
    is_text: bool = False

    def attr(self, name: str, default: str = "") -> str:
        return self.attrs.get(name, default)

    def classes(self) -> list[str]:
        return [item for item in self.attr("class").split() if item]

    def has_class(self, class_name: str) -> bool:
        return class_name in self.classes()

    def iter_descendants(self) -> Iterable["Node"]:
        for child in self.children:
            yield child
            yield from child.iter_descendants()

    def element_children(self) -> list["Node"]:
        return [child for child in self.children if not child.is_text]


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(
            tag=tag.lower(),
            attrs={key: value or "" for key, value in attrs},
            parent=self.stack[-1],
        )
        self.stack[-1].children.append(node)
        if node.tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(
            tag=tag.lower(),
            attrs={key: value or "" for key, value in attrs},
            parent=self.stack[-1],
        )
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].tag == lowered:
                self.stack = self.stack[:idx]
                break

    def handle_data(self, data: str) -> None:
        if not data:
            return
        node = Node(tag="#text", data=data, is_text=True, parent=self.stack[-1])
        self.stack[-1].children.append(node)


def parse_html(text: str) -> Node:
    parser = _TreeBuilder()
    parser.feed(text)
    return parser.root


def text_content(node: Node) -> str:
    pieces: list[str] = []

    def collect(current: Node) -> None:
        if current.is_text:
            pieces.append(current.data)
            return
        for child in current.children:
            collect(child)

    collect(node)
    text = "".join(pieces).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def find_all(node: Node, predicate: Callable[[Node], bool]) -> list[Node]:
    matches: list[Node] = []
    for current in node.iter_descendants():
        if current.is_text:
            continue
        if predicate(current):
            matches.append(current)
    return matches


def find_first(node: Node, predicate: Callable[[Node], bool]) -> Node | None:
    for current in node.iter_descendants():
        if current.is_text:
            continue
        if predicate(current):
            return current
    return None


def direct_children(node: Node, predicate: Callable[[Node], bool] | None = None) -> list[Node]:
    children = [child for child in node.children if not child.is_text]
    if predicate is None:
        return children
    return [child for child in children if predicate(child)]


def matches(node: Node, *, tag: str | None = None, class_names: tuple[str, ...] = (), id_: str | None = None) -> bool:
    if node.is_text:
        return False
    if tag and node.tag != tag:
        return False
    if id_ and node.attr("id") != id_:
        return False
    for class_name in class_names:
        if not node.has_class(class_name):
            return False
    return True


def ancestor(node: Node | None, predicate: Callable[[Node], bool]) -> Node | None:
    current = node.parent if node else None
    while current:
        if predicate(current):
            return current
        current = current.parent
    return None

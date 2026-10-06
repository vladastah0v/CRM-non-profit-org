"""Проверка разметки страниц в тестах: парные теги, без встроенных скриптов и стилей (CSP),
у каждого поля — подпись <label for>, id не повторяются."""

import re
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}


class TagBalance(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.ids: list[str] = []
        self.label_for: set[str] = set()
        self.inputs: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "style" in attrs:
            self.errors.append(f"style= в <{tag}>")
        if any(name.startswith("on") for name in attrs):
            self.errors.append(f"обработчик on* в <{tag}>")
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "label" and attrs.get("for"):
            self.label_for.add(attrs["for"])
        if tag in {"input", "select", "textarea"} and attrs.get("type") not in {"hidden", "submit"}:
            self.inputs.append(attrs.get("id") or "")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"лишний </{tag}>, открыт {self.stack[-1:]}")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()


def check_markup(html: str) -> None:
    parser = TagBalance()
    parser.feed(html)
    assert not parser.errors, parser.errors
    assert parser.stack == [], parser.stack
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "встроенный <script>"
    duplicates = {i for i in parser.ids if parser.ids.count(i) > 1}
    assert not duplicates, f"повторяющиеся id: {duplicates}"
    unlabeled = [i for i in parser.inputs if i and i not in parser.label_for]
    assert not unlabeled, f"поля без <label for>: {unlabeled}"

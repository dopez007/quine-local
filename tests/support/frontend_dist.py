"""Parse committed frontend HTML without browser or server effects."""

from __future__ import annotations

from html.parser import HTMLParser
import pathlib


class FrontendDocument(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []
        self.links: list[dict[str, str | None]] = []
        self.unsafe_attributes: list[tuple[str, str, str | None]] = []
        self._script: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        for name, value in attrs:
            lower_name = name.lower()
            lower_value = "".join(character for character in (value or "") if ord(character) > 0x20).lower()
            if lower_name.startswith("on") or lower_name == "srcdoc":
                self.unsafe_attributes.append((tag, name, value))
            elif lower_name in {"action", "formaction", "href", "src", "xlink:href"} and lower_value.startswith("javascript:"):
                self.unsafe_attributes.append((tag, name, value))
        if tag == "script":
            self._script = {"src": attributes.get("src"), "body": ""}
            self.scripts.append(self._script)
        elif tag == "link":
            self.links.append({"rel": attributes.get("rel"), "href": attributes.get("href")})

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script["body"] = (self._script["body"] or "") + data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._script = None


def parse_frontend_html(path: pathlib.Path) -> FrontendDocument:
    document = FrontendDocument()
    document.feed(path.read_text(encoding="utf-8"))
    return document


def generated_references(document: FrontendDocument) -> list[str]:
    references = [script["src"] for script in document.scripts if script["src"]]
    for link in document.links:
        rel = set((link["rel"] or "").split())
        if rel & {"modulepreload", "stylesheet"} and link["href"]:
            references.append(link["href"])
    return [reference for reference in references if reference is not None]

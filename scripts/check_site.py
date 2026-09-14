"""Check the generated site without accessing the network.

Usage:
    python scripts/check_site.py
"""

from __future__ import annotations

import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from icalendar import Calendar

from common import ROOT, DATA, SiteError, load_yaml, make_url_helpers


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.references = []
        self.nav_links = []
        self.in_nav = 0
        self.h1_count = 0
        self.main_count = 0
        self.title_count = 0
        self.language = None
        self.viewport = False
        self.description = False
        self.canonicals = []
        self.problems = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)

        if "id" in attrs:
            self.ids.append(attrs["id"])

        if tag == "html":
            self.language = attrs.get("lang")
        elif tag == "title":
            self.title_count += 1
        elif tag == "h1":
            self.h1_count += 1
        elif tag == "main":
            self.main_count += 1
        elif tag == "nav":
            self.in_nav += 1
        elif tag == "meta":
            if attrs.get("name") == "viewport":
                self.viewport = bool(attrs.get("content"))
            if attrs.get("name") == "description":
                self.description = bool(attrs.get("content"))
        elif tag == "link":
            if "canonical" in attrs.get("rel", "").split():
                self.canonicals.append(attrs.get("href"))
        elif tag == "img":
            if "alt" not in attrs:
                self.problems.append("Image without alternative text.")
        elif tag == "script":
            if attrs.get("type") != "application/ld+json":
                self.problems.append("Unexpected browser-side JavaScript.")

        if tag == "a" and self.in_nav:
            self.nav_links.append(attrs.get("href", ""))

        for attribute in ("href", "src"):
            value = attrs.get(attribute)
            if value:
                self.references.append((value, tag == "a"))

    def handle_endtag(self, tag):
        if tag == "nav":
            self.in_nav = max(0, self.in_nav - 1)

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)


def page_route(root: Path, path: Path) -> str:
    relative = path.relative_to(root).as_posix()
    if relative == "index.html":
        return "/"
    if relative.endswith("/index.html"):
        return "/" + relative.removesuffix("index.html")
    return "/" + relative


def check(root: Path | None = None) -> list[str]:
    root = (root or ROOT / "_site").resolve()
    site = load_yaml(DATA / "site.yaml")
    local_url, absolute_url = make_url_helpers(site)
    origin = urlsplit(site["url"])
    base = site.get("base_path", "")
    errors = []

    if not root.is_dir():
        return ["Generated site is missing; run the build first."]

    expected_files = [
        "index.html",
        "people/index.html",
        "seminar/index.html",
        "publications/index.html",
        "news/index.html",
        "internal/index.html",
        "legal/index.html",
        "404.html",
        "css/style.css",
        "seminar/calendar.ics",
        "robots.txt",
        "sitemap.xml",
    ]
    for relative in expected_files:
        if not (root / relative).is_file():
            errors.append(f"Missing output: {relative}")

    pages = {}
    for path in sorted(root.rglob("*.html")):
        parser = PageParser()
        parser.feed(path.read_text(encoding="utf-8"))
        pages[path.resolve()] = parser
        label = path.relative_to(root).as_posix()

        for message in parser.problems:
            errors.append(f"{label}: {message}")

        if parser.language != site["language"]:
            errors.append(f"{label}: missing or incorrect HTML language.")
        if parser.title_count != 1:
            errors.append(f"{label}: expected exactly one document title.")
        if parser.h1_count != 1:
            errors.append(f"{label}: expected exactly one h1.")
        if parser.main_count != 1:
            errors.append(f"{label}: expected exactly one main element.")
        if not parser.viewport or not parser.description:
            errors.append(f"{label}: missing viewport or description.")

        expected_canonical = absolute_url(page_route(root, path))
        if parser.canonicals != [expected_canonical]:
            errors.append(f"{label}: incorrect canonical URL.")

        duplicates = [
            identifier
            for identifier, count in Counter(parser.ids).items()
            if count > 1
        ]
        if duplicates:
            errors.append(f"{label}: duplicate IDs: {duplicates}")

        expected_navigation = [
            local_url(path)
            for path in ("/", "/people/", "/seminar/", "/publications/")
        ]
        if parser.nav_links != expected_navigation:
            errors.append(f"{label}: unexpected main navigation.")

    for path, parser in pages.items():
        label = path.relative_to(root).as_posix()

        for reference, check_fragment in parser.references:
            parts = urlsplit(reference)

            if parts.scheme and parts.scheme not in {"http", "https"}:
                if parts.scheme != "mailto":
                    errors.append(f"{label}: unsupported URL {reference!r}")
                continue

            if parts.netloc and parts.netloc != origin.netloc:
                continue

            decoded = unquote(parts.path)
            if not decoded:
                target = path
            elif decoded.startswith("/"):
                if base:
                    if decoded == base:
                        decoded = "/"
                    elif decoded.startswith(base + "/"):
                        decoded = decoded[len(base):]
                    else:
                        errors.append(
                            f"{label}: URL omits base_path: {reference!r}"
                        )
                        continue
                target = root / decoded.lstrip("/")
            else:
                target = path.parent / decoded

            target = target.resolve()
            if not target.is_relative_to(root):
                errors.append(f"{label}: URL escapes site: {reference!r}")
                continue

            if target.is_dir():
                target = target / "index.html"

            if not target.is_file():
                errors.append(f"{label}: broken local link: {reference!r}")
                continue

            fragment = unquote(parts.fragment)
            if check_fragment and fragment and target.suffix == ".html":
                destination = pages.get(target.resolve())
                if destination and fragment not in destination.ids:
                    errors.append(
                        f"{label}: missing anchor: {reference!r}"
                    )

    calendar_path = root / "seminar/calendar.ics"
    if calendar_path.is_file():
        try:
            calendar = Calendar.from_ical(calendar_path.read_bytes())
            uids = set()
            for event in calendar.walk("VEVENT"):
                for field in (
                    "UID", "DTSTART", "DTEND", "DTSTAMP",
                    "LAST-MODIFIED", "SEQUENCE", "SUMMARY", "STATUS",
                ):
                    if field not in event:
                        raise ValueError(f"Event missing {field}")

                uid = str(event["UID"])
                if uid in uids:
                    raise ValueError(f"Duplicate calendar UID: {uid}")
                uids.add(uid)

                if event.decoded("DTEND") <= event.decoded("DTSTART"):
                    raise ValueError(f"Non-positive duration: {uid}")

        except Exception as exc:
            errors.append(f"Invalid calendar: {exc}")

    sitemap_path = root / "sitemap.xml"
    if sitemap_path.is_file():
        try:
            tree = ET.parse(sitemap_path)
            namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            locations = [
                element.text
                for element in tree.findall(".//s:loc", namespace)
            ]
            if absolute_url("/internal/") in locations:
                errors.append("Internal page must not appear in the sitemap.")
            if site["demo"] and locations:
                errors.append("Demo sitemap should be empty.")
        except ET.ParseError as exc:
            errors.append(f"Invalid sitemap: {exc}")

    return errors


def main() -> int:
    try:
        errors = check()
    except (SiteError, OSError, ValueError) as exc:
        errors = [str(exc)]

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Generated-site checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

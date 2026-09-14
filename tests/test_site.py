"""Behavioural tests. No DBLP network access is required."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from icalendar import Calendar

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build
from common import (
    MarkdownRenderer,
    SiteError,
    get_timezone,
    load_yaml,
    make_url_helpers,
    parse_datetime,
)


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.zone = get_timezone("Europe/Rome")
        self.now = datetime(2026, 9, 14, 12, 0, tzinfo=self.zone)
        self.site = {
            "url": "https://example.org",
            "base_path": "",
            "timezone": "Europe/Rome",
            "seminar": {
                "name": "Test seminar",
                "duration_minutes": 60,
            },
            "statuses": [
                {"id": "permanent", "label": "Permanent", "publications": True},
                {"id": "visitor", "label": "Visitors", "publications": False},
            ],
        }
        self.local_url, self.absolute_url = make_url_helpers(self.site)
        self.markdown = MarkdownRenderer(self.local_url)

    def talk(self, identifier, when, **extra):
        return {
            "id": identifier,
            "uid": identifier + "@example.org",
            "last_modified": "2026-09-01 12:00",
            "datetime": when,
            **extra,
        }

    def test_duplicate_yaml_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.yaml"
            path.write_text("name: first\nname: second\n", encoding="utf-8")
            with self.assertRaises(SiteError):
                load_yaml(path)

    def test_dst_gap_and_ambiguity_are_rejected(self):
        for value in ("2026-03-29 02:30", "2026-10-25 02:30"):
            with self.subTest(value=value):
                with self.assertRaises(SiteError):
                    parse_datetime(value, self.zone, "test")

    def test_talk_order_start_boundary_and_no_location_default(self):
        data = [
            self.talk("later", "2026-09-16 14:00"),
            self.talk("earlier", "2026-09-01 14:00"),
            self.talk("boundary", "2026-09-14 12:00"),
            self.talk("yesterday", "2026-09-13 14:00"),
        ]
        with patch.object(build, "load_yaml", return_value=data):
            talks, upcoming, past, next_talk = build.load_talks(
                self.site, self.now, self.markdown
            )

        self.assertEqual([t["id"] for t in upcoming], ["boundary", "later"])
        self.assertEqual([t["id"] for t in past], ["yesterday", "earlier"])
        self.assertEqual(next_talk["id"], "boundary")
        self.assertTrue(all(t["location"] == "" for t in talks))

    def test_cancellation_is_exported_and_not_next(self):
        data = [
            self.talk("cancelled", "2026-09-15 14:00", cancelled=True),
            self.talk("available", "2026-09-16 14:00"),
        ]
        with patch.object(build, "load_yaml", return_value=data):
            talks, _, _, next_talk = build.load_talks(
                self.site, self.now, self.markdown
            )

        self.assertEqual(next_talk["id"], "available")
        payload = build.calendar_bytes(self.site, talks, self.absolute_url)
        events = Calendar.from_ical(payload).walk("VEVENT")

        self.assertEqual(str(events[0]["UID"]), "cancelled@example.org")
        self.assertEqual(str(events[0]["STATUS"]), "CANCELLED")
        self.assertEqual(events[0].decoded("DTSTART").hour, 12)
        self.assertEqual(events[0].decoded("DTEND").hour, 13)

    def test_news_expiry_is_inclusive(self):
        data = [
            {
                "id": "active",
                "date": "2026-09-01",
                "expires": "2026-09-14",
                "title": "Active today",
                "text": "Text",
            },
            {
                "id": "expired",
                "date": "2026-08-01",
                "expires": "2026-09-13",
                "title": "Expired",
                "text": "Text",
            },
            {
                "id": "future",
                "date": "2026-09-15",
                "title": "Future",
                "text": "Text",
            },
        ]
        with patch.object(build, "load_yaml", return_value=data):
            archive, active = build.load_news(
                self.now.date(), self.markdown, 3
            )

        self.assertEqual([n["id"] for n in active], ["active"])
        self.assertEqual([n["id"] for n in archive], ["active", "expired"])

    def test_publications_periods_overrides_and_exclusion_priority(self):
        def record(key, year):
            return {
                "key": key,
                "title": key,
                "authors": ["Example"],
                "venue": "Venue",
                "year": year,
                "electronic_url": None,
                "type": "article",
            }

        records = {
            "journals/test/Old": record("journals/test/Old", 2023),
            "journals/test/Boundary": record("journals/test/Boundary", 2024),
            "journals/test/Visit": record("journals/test/Visit", 2025),
        }
        cache = {
            "schema_version": 1,
            "authors": {
                "x/Example": {
                    "name": "Example",
                    "keys": list(records),
                    "fetched_at": "2026-09-14T10:00:00+00:00",
                }
            },
            "records": records,
        }
        people = [{
            "dblp": "x/Example",
            "periods": [
                {
                    "status": "permanent",
                    "start": date(2024, 6, 1),
                    "end": date(2024, 12, 31),
                },
                {
                    "status": "visitor",
                    "start": date(2025, 1, 1),
                    "end": None,
                },
            ],
        }]
        overrides = {
            "include": [
                {"key": "journals/test/Old"},
                {"key": "journals/test/Boundary"},
            ],
            "exclude": [{"key": "journals/test/Boundary"}],
        }

        with (
            patch.object(build, "load_cache", return_value=cache),
            patch.object(build, "read_overrides", return_value=overrides),
        ):
            publications, _, _ = build.select_publications(
                self.site, people, self.now.date()
            )

        self.assertEqual(
            [p["key"] for p in publications],
            ["journals/test/Old"],
        )

    def test_markdown_escapes_html_and_prefixes_local_links(self):
        site = {**self.site, "base_path": "/research"}
        local_url, _ = make_url_helpers(site)
        renderer = MarkdownRenderer(local_url)
        html = str(renderer.render(
            '<script>alert(1)</script>\n\n[Seminar](/seminar/)',
            "test",
        ))

        self.assertNotIn("<script>", html)
        self.assertIn('href="/research/seminar/"', html)

    def test_math_is_rendered_to_html_and_mathml(self):
        html = str(self.markdown.render(
            r"A judgement: $\Gamma \vdash t : A$.",
            "test",
        ))
        self.assertTrue(self.markdown.has_math)
        self.assertIn('class="katex"', html)
        self.assertIn("<math", html)
        self.assertNotIn("<script", html)


if __name__ == "__main__":
    unittest.main()

"""Build the static website from YAML, Markdown, and the committed DBLP cache.

Usage:
    python scripts/build.py

For reproducible previews or tests:
    python scripts/build.py --now 2026-09-14T12:00:00+02:00

Output:
    _site/

A failed build leaves the previous _site directory intact.
Run fetch_dblp.py separately to refresh bibliographic data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from base64 import b64encode
from urllib.parse import quote

from icalendar import Calendar, Event
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from common import (
    DATA,
    ROOT,
    MarkdownRenderer,
    SiteError,
    display_date,
    display_datetime,
    get_timezone,
    load_cache,
    load_yaml,
    make_url_helpers,
    optional_text,
    optional_url,
    parse_date,
    parse_datetime,
    reject_unknown,
    require_bool,
    require_id,
    require_int,
    require_list,
    require_mapping,
    require_text,
    static_path,
    unique_ids,
)
from fetch_dblp import dblp_identifier, read_overrides
from remote_photos import photo_source


def load_site() -> dict:
    site = require_mapping(load_yaml(DATA / "site.yaml"), "site")
    reject_unknown(
        site,
        {
            "name", "url", "base_path", "timezone", "language",
            "description", "demo", "show_photos", "home_news_limit",
            "publications_visible", "statuses", "seminar", "logos",
        },
        "site",
    )

    for field in ("name", "url", "timezone", "language", "description"):
        require_text(site.get(field), f"site.{field}")

    site.setdefault("base_path", "")
    site.setdefault("demo", False)
    require_bool(site["demo"], "site.demo")
    site.setdefault("show_photos", True)
    require_bool(site["show_photos"], "site.show_photos")

    for field in ("home_news_limit", "publications_visible"):
        require_int(site.get(field), f"site.{field}", minimum=1)

    statuses = require_list(site.get("statuses"), "site.statuses")
    unique_ids(statuses, "site.statuses")
    if not statuses:
        raise SiteError("site.statuses must not be empty.")

    for status in statuses:
        reject_unknown(status, {"id", "label", "publications"}, "status")
        require_text(status.get("label"), "status.label")
        require_bool(status.get("publications"), "status.publications")

    seminar = require_mapping(site.get("seminar"), "site.seminar")
    reject_unknown(
        seminar, {"name", "duration_minutes"}, "site.seminar"
    )
    require_text(seminar.get("name"), "site.seminar.name")
    seminar.setdefault("duration_minutes", 60)
    require_int(
        seminar["duration_minutes"],
        "site.seminar.duration_minutes",
        minimum=1,
    )

    logos = require_mapping(site.get("logos"), "site.logos")
    reject_unknown(logos, {"header", "footer"}, "site.logos")
    header = require_mapping(logos.get("header"), "site.logos.header")
    footer = require_list(logos.get("footer"), "site.logos.footer")

    for index, logo in enumerate([header, *footer]):
        where = f"logo[{index}]"
        require_mapping(logo, where)
        reject_unknown(logo, {"src", "alt", "url"}, where)
        logo["src"] = static_path(logo.get("src"), f"{where}.src")
        logo["alt"] = require_text(logo.get("alt"), f"{where}.alt")
        logo["url"] = optional_url(logo.get("url"), f"{where}.url")

    get_timezone(site["timezone"])
    make_url_helpers(site)
    return site


def load_people(site: dict, today, markdown: MarkdownRenderer):
    people = require_list(load_yaml(DATA / "people.yaml"), "people")
    unique_ids(people, "people")
    status_by_id = {status["id"]: status for status in site["statuses"]}

    groups = [
        {"id": status["id"], "label": status["label"], "people": []}
        for status in site["statuses"]
    ]
    group_by_id = {group["id"]: group for group in groups}
    former = []

    allowed = {
        "id", "first_name", "last_name", "affiliations", "email",
        "webpage", "dblp", "photo", "interests", "periods",
    }

    for person in people:
        where = f"person {person['id']}"
        reject_unknown(person, allowed, where)

        for field in ("first_name", "last_name"):
            require_text(person.get(field), f"{where}.{field}")

        affiliations = require_text(
            person.get("affiliations", ""),
            f"{where}.affiliations",
            allow_empty=True,
        )
        person["affiliations_html"] = markdown.render(
            affiliations, f"{where}.affiliations"
        )
        person["email"] = optional_text(person.get("email"), f"{where}.email")
        if person["email"] and (
            "@" not in person["email"]
            or any(c.isspace() for c in person["email"])
            or any(c in person["email"] for c in "?#")
        ):
            raise SiteError(f"{where}.email: invalid email address.")
        person["email_encoded"] = (
            b64encode(
                ("mailto:" + quote(person["email"], safe="@")).encode("ascii")
            ).decode("ascii")
            if person["email"] else None
        )

        person["webpage"] = optional_url(
            person.get("webpage"), f"{where}.webpage"
        )
        person["photo"] = photo_source(
            person.get("photo"),
            f"{where}.photo",
            check_remote=site["show_photos"],
        )
        person["dblp"] = (
            dblp_identifier(person["dblp"], f"{where}.dblp")
            if person.get("dblp") is not None else None
        )

        person["interests"] = require_list(
            person.get("interests", []), f"{where}.interests"
        )
        for interest in person["interests"]:
            require_text(interest, f"{where}.interests")

        periods = require_list(person.get("periods"), f"{where}.periods")
        if not periods:
            raise SiteError(f"{where}: at least one period is required.")

        for index, period in enumerate(periods):
            context = f"{where}.periods[{index}]"
            require_mapping(period, context)
            reject_unknown(
                period, {"status", "arrival", "departure"}, context
            )
            status = require_text(period.get("status"), f"{context}.status")
            if status not in status_by_id:
                raise SiteError(f"{context}: unknown status {status!r}.")

            period["start"] = parse_date(
                period.get("arrival"), f"{context}.arrival"
            )
            period["end"] = (
                parse_date(period["departure"], f"{context}.departure")
                if period.get("departure") is not None else None
            )
            if period["end"] and period["end"] < period["start"]:
                raise SiteError(f"{context}: departure precedes arrival.")

        periods.sort(key=lambda period: period["start"])
        for previous, following in zip(periods, periods[1:]):
            if previous["end"] is None or previous["end"] >= following["start"]:
                raise SiteError(f"{where}: affiliation periods overlap.")

        active = next(
            (
                period for period in periods
                if period["start"] <= today
                and (period["end"] is None or today <= period["end"])
            ),
            None,
        )
        past = [
            period for period in periods
            if period["end"] is not None and period["end"] < today
        ]

        person["visit_dates"] = ""
        person["period_summary"] = ""

        if active:
            if active["status"] == "visitor":
                end = (
                    display_date(active["end"])
                    if active["end"] else "ongoing"
                )
                person["visit_dates"] = (
                    f"{display_date(active['start'])} – {end}"
                )
            group_by_id[active["status"]]["people"].append(person)

        elif past:
            summaries = []
            for period in past:
                label = status_by_id[period["status"]]["label"]
                years = str(period["start"].year)
                if period["end"].year != period["start"].year:
                    years += f"–{period['end'].year}"
                summaries.append(f"{label}, {years}")

            person["period_summary"] = "; ".join(summaries)
            former.append(person)

    def sort_key(person):
        return (
            person["last_name"].casefold(),
            person["first_name"].casefold(),
            person["id"],
        )

    for group in groups:
        group["people"].sort(key=sort_key)
    former.sort(key=sort_key)

    return people, groups, former


def load_talks(site: dict, now: datetime, markdown: MarkdownRenderer):
    talks = require_list(load_yaml(DATA / "talks.yaml"), "talks")
    unique_ids(talks, "talks")
    zone = get_timezone(site["timezone"])
    seen_uids = set()

    allowed = {
        "id", "uid", "last_modified", "sequence", "datetime",
        "duration_minutes", "location", "warning", "speaker",
        "speaker_url", "affiliation", "title", "abstract", "slides",
        "url", "video", "online_url", "cancelled",
    }

    for talk in talks:
        where = f"talk {talk['id']}"
        reject_unknown(talk, allowed, where)

        uid = require_text(talk.get("uid"), f"{where}.uid")
        if any(c.isspace() for c in uid):
            raise SiteError(f"{where}.uid: whitespace is not allowed.")
        if uid in seen_uids:
            raise SiteError(f"{where}: duplicate calendar UID {uid!r}.")
        seen_uids.add(uid)

        talk["start"] = parse_datetime(
            talk.get("datetime"), zone, f"{where}.datetime"
        )
        talk["modified"] = parse_datetime(
            talk.get("last_modified"), zone, f"{where}.last_modified"
        )
        talk["sequence"] = require_int(
            talk.get("sequence", 0), f"{where}.sequence"
        )
        talk["duration_minutes"] = require_int(
            talk.get("duration_minutes", site["seminar"]["duration_minutes"]),
            f"{where}.duration_minutes",
            minimum=1,
        )
        talk["cancelled"] = require_bool(
            talk.get("cancelled", False), f"{where}.cancelled"
        )

        # Deliberately no shared location default.
        talk["location"] = require_text(
            talk.get("location", ""), f"{where}.location", allow_empty=True
        )
        talk["affiliation"] = require_text(
            talk.get("affiliation", ""),
            f"{where}.affiliation",
            allow_empty=True,
        )

        for field in ("warning", "speaker", "title", "abstract"):
            talk[field] = optional_text(talk.get(field), f"{where}.{field}")

        for field in ("speaker_url", "slides", "url", "video", "online_url"):
            talk[field] = optional_url(talk.get(field), f"{where}.{field}")

        talk["start_iso"] = talk["start"].isoformat()
        talk["display_datetime"] = display_datetime(talk["start"])
        talk["is_upcoming"] = talk["start"] >= now
        talk["abstract_html"] = (
            markdown.render(talk["abstract"], f"{where}.abstract")
            if talk["abstract"] else ""
        )

    talks.sort(key=lambda talk: (talk["start"], talk["id"]))
    upcoming = [talk for talk in talks if talk["is_upcoming"]]
    past = list(reversed([talk for talk in talks if not talk["is_upcoming"]]))
    next_talk = next(
        (talk for talk in upcoming if not talk["cancelled"]), None
    )
    return talks, upcoming, past, next_talk


def load_news(today, markdown: MarkdownRenderer, limit: int):
    news = require_list(load_yaml(DATA / "news.yaml"), "news")
    unique_ids(news, "news")

    for item in news:
        where = f"news {item['id']}"
        reject_unknown(item, {"id", "date", "expires", "title", "text"}, where)

        published = parse_date(item.get("date"), f"{where}.date")
        expiry = (
            parse_date(item["expires"], f"{where}.expires")
            if item.get("expires") is not None else None
        )
        if expiry and expiry < published:
            raise SiteError(f"{where}: expiry precedes publication.")

        require_text(item.get("title"), f"{where}.title")
        require_text(item.get("text"), f"{where}.text")
        item["published"] = published
        item["expiry"] = expiry
        item["display_date"] = display_date(published)
        item["text_html"] = markdown.render(item["text"], f"{where}.text")

    news.sort(key=lambda item: (item["published"], item["id"]), reverse=True)
    archive = [item for item in news if item["published"] <= today]
    active = [
        item for item in archive
        if item["expiry"] is None or today <= item["expiry"]
    ]
    return archive, active[:limit]


def select_publications(site: dict, people: list, today):
    cache = load_cache()
    overrides = read_overrides()
    records = cache["records"]

    # Validate cached records before using them in templates.
    for key, record in records.items():
        where = f"cached publication {key}"
        dblp_identifier(key, where)
        require_mapping(record, where)
        if record.get("key") != key:
            raise SiteError(f"{where}: key mismatch.")
        require_text(record.get("title"), f"{where}.title")
        require_int(record.get("year"), f"{where}.year", minimum=1)
        require_text(record.get("venue"), f"{where}.venue", allow_empty=True)
        require_text(record.get("type"), f"{where}.type")
        require_bool(record.get("informal", False), f"{where}.informal")
        for author in require_list(record.get("authors"), f"{where}.authors"):
            require_text(author, f"{where}.authors")
        optional_url(record.get("electronic_url"), f"{where}.electronic_url")

    author_ids = {person["dblp"] for person in people if person["dblp"]}
    timestamps = []

    for pid in sorted(author_ids):
        entry = cache["authors"].get(pid)
        if not isinstance(entry, dict):
            raise SiteError(
                f"No cached bibliography for {pid}. "
                "Run python scripts/fetch_dblp.py."
            )

        require_text(entry.get("name"), f"cached author {pid}.name")
        keys = require_list(entry.get("keys"), f"cached author {pid}.keys")
        seen = set()
        for key in keys:
            dblp_identifier(key, f"cached author {pid}.key")
            if key in seen or key not in records:
                raise SiteError(
                    f"Cached author {pid}: duplicate or missing record {key!r}."
                )
            seen.add(key)

        stamp = require_text(entry.get("fetched_at"), f"cached author {pid}.fetched_at")
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError as exc:
            raise SiteError(f"Invalid cache timestamp for {pid}.") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise SiteError(f"Cache timestamp for {pid} needs a timezone.")
        timestamps.append(parsed.astimezone(timezone.utc))

    eligible_statuses = {
        status["id"] for status in site["statuses"] if status["publications"]
    }
    selected = set()

    for person in people:
        pid = person["dblp"]
        if not pid:
            continue
        periods = [
            period for period in person["periods"]
            if period["status"] in eligible_statuses and period["start"] <= today
        ]

        for key in cache["authors"][pid]["keys"]:
            year = records[key]["year"]
            if any(
                period["start"].year <= year
                <= min(period["end"] or today, today).year
                for period in periods
            ):
                selected.add(key)

    for operation in ("include", "exclude"):
        for entry in overrides[operation]:
            if entry["key"] not in records:
                raise SiteError(
                    f"Unresolved {operation} override {entry['key']!r}. "
                    "Run python scripts/fetch_dblp.py."
                )

    selected.update(entry["key"] for entry in overrides["include"])
    selected.difference_update(entry["key"] for entry in overrides["exclude"])

    # Exclude informal publications, including preprints.
    # The CoRR key check also works with caches predating the informal field.
    selected = {
        key
        for key in selected
        if not records[key].get("informal", False)
        and not key.startswith("journals/corr/")
    }

    publications = sorted(
        (records[key] for key in selected),
        key=lambda record: (
            -record["year"],
            record["title"].casefold(),
            record["key"],
        ),
    )

    # Conservative date: the oldest author snapshot used by this build.
    stamp = min(timestamps) if timestamps else None
    return (
        publications,
        stamp.isoformat() if stamp else None,
        display_date(stamp.date()) if stamp else None,
    )


def calendar_bytes(site: dict, talks: list, absolute_url) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", "-//TSS Bologna//Research Seminar//EN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("x-wr-calname", site["seminar"]["name"])
    calendar.add("x-wr-timezone", site["timezone"])

    for talk in talks:
        event = Event()
        start = talk["start"].astimezone(timezone.utc)
        # Compute elapsed duration in UTC, including across DST boundaries.
        end = start + timedelta(minutes=talk["duration_minutes"])
        modified = talk["modified"].astimezone(timezone.utc)

        if talk["speaker"]:
            summary = f"{talk['speaker']}: {talk['title'] or 'TBA'}"
        else:
            summary = "TBA"

        event.add("uid", talk["uid"])
        event.add("sequence", talk["sequence"])
        event.add("dtstamp", modified)
        event.add("last-modified", modified)
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("summary", summary)
        event.add("status", "CANCELLED" if talk["cancelled"] else "CONFIRMED")
        event.add("url", absolute_url("/seminar/#" + talk["id"]))

        if talk["location"]:
            event.add("location", talk["location"])

        description = []
        if talk["affiliation"]:
            description.append(talk["affiliation"])
        if talk["warning"]:
            description.append("Note: " + talk["warning"])
        if talk["abstract"]:
            # Calendar descriptions are plain text; retain Markdown/TeX source.
            description.append(talk["abstract"])
        for field, label in (
            ("speaker_url", "Speaker"),
            ("slides", "Slides"),
            ("url", "Link"),
            ("video", "Video"),
            ("online_url", "Online attendance"),
        ):
            if talk[field]:
                description.append(f"{label}: {talk[field]}")

        if description:
            event.add("description", "\n\n".join(description))

        calendar.add_component(event)

    return calendar.to_ical()


def copy_assets(destination: Path, has_math: bool) -> None:
    shutil.copytree(ROOT / "static", destination, dirs_exist_ok=True)

    if not has_math:
        return

    katex = ROOT / "node_modules" / "katex"
    required = [
        katex / "dist" / "katex.min.css",
        katex / "dist" / "fonts",
        katex / "LICENSE",
    ]
    if not all(path.exists() for path in required):
        raise SiteError("KaTeX assets are missing. Run npm install.")

    target = destination / "vendor" / "katex"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(required[0], target / "katex.min.css")
    shutil.copytree(required[1], target / "fonts", dirs_exist_ok=True)
    shutil.copy2(required[2], target / "LICENSE")

    # Preserve additional font licence notices if supplied by the package.
    for path in katex.rglob("*"):
        if (
            path.is_file()
            and path != required[2]
            and path.name.lower().startswith(("license", "licence", "ofl"))
        ):
            relative = path.relative_to(katex)
            notice = target / "notices" / relative
            notice.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, notice)


def write_sitemap(destination: Path, paths: list[str], absolute_url) -> None:
    namespace = "http://www.sitemaps.org/schemas/sitemap/0.9"
    ET.register_namespace("", namespace)
    root = ET.Element(f"{{{namespace}}}urlset")

    for path in paths:
        entry = ET.SubElement(root, f"{{{namespace}}}url")
        ET.SubElement(entry, f"{{{namespace}}}loc").text = absolute_url(path)

    ET.ElementTree(root).write(
        destination / "sitemap.xml",
        encoding="utf-8",
        xml_declaration=True,
    )


def install_output(staging: Path) -> None:
    """Replace only the generated _site directory, keeping rollback possible."""
    target = ROOT / "_site"

    if target.is_symlink():
        raise SiteError("Refusing to replace a symlink at _site.")
    if target.exists() and not target.is_dir():
        raise SiteError("_site exists but is not a directory.")

    backup = None
    if target.exists():
        # Reserve a unique sibling name; remove only that empty reservation.
        backup = Path(tempfile.mkdtemp(prefix=".site-previous-", dir=ROOT))
        backup.rmdir()
        os.replace(target, backup)

    try:
        os.replace(staging, target)
    except OSError:
        if backup is not None:
            os.replace(backup, target)
        raise

    if backup is not None:
        try:
            shutil.rmtree(backup)
        except OSError as exc:
            print(
                f"Warning: previous generated output remains at {backup}: {exc}",
                file=sys.stderr,
            )


def build(now_override: str | None = None) -> None:
    site = load_site()
    zone = get_timezone(site["timezone"])

    if now_override:
        try:
            now = datetime.fromisoformat(now_override)
        except ValueError as exc:
            raise SiteError("--now must be an ISO date-time.") from exc
        if now.tzinfo is None or now.utcoffset() is None:
            raise SiteError("--now must include a UTC offset.")
        now = now.astimezone(zone)
    else:
        now = datetime.now(zone)

    today = now.date()
    local_url, absolute_url = make_url_helpers(site)
    markdown = MarkdownRenderer(local_url)

    people, groups, former = load_people(site, today, markdown)
    talks, upcoming, past, next_talk = load_talks(site, now, markdown)
    archive, home_news = load_news(today, markdown, site["home_news_limit"])
    publications, snapshot, snapshot_display = select_publications(
        site, people, today
    )
    for publication in publications:
        publication["title_html"] = markdown.render_title(
            publication["title"],
            f"publication {publication['key']}.title",
        )
        publication["authors_display"] = [
            re.sub(r" \d{4}(?= \(ed\.\)$|$)", "", name)
            for name in publication["authors"]
        ]

    content = {}
    for name in ("home", "seminar", "internal", "legal"):
        path = ROOT / "content" / f"{name}.md"
        content[name] = markdown.render(
            path.read_text(encoding="utf-8"), str(path)
        )

    environment = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )

    context = {
        "site": site,
        "local_url": local_url,
        "absolute_url": absolute_url,
        "people_groups": groups,
        "former_people": former,
        "upcoming_talks": upcoming,
        "past_talks": past,
        "next_talk": next_talk,
        "home_news": home_news,
        "archived_news": archive,
        "publications": publications,
        "publications_updated": snapshot,
        "publications_updated_display": snapshot_display,
    }

    pages = [
        ("/", "", "home.html", "home"),
        ("/people/", "People", "people.html", None),
        ("/seminar/", "Seminar", "seminar.html", "seminar"),
        ("/publications/", "Publications", "publications.html", None),
        ("/news/", "News archive", "news.html", None),
        ("/internal/", "Internal information", "prose.html", "internal"),
        ("/legal/", "Licensing and privacy", "prose.html", "legal"),
        ("/404.html", "Page not found", "404.html", None),
    ]
    descriptions = {
        "/": site["description"],
        "/people/": "Researchers, students, visitors, and collaborators in Bologna.",
        "/seminar/": "Upcoming and past talks in the Bologna software theory seminar.",
        "/publications/": "Publications associated with researchers' periods in Bologna.",
        "/news/": "News from the Theory of Software Systems community in Bologna.",
        "/internal/": "Practical information for participants.",
        "/legal/": "Website licensing and privacy information.",
        "/404.html": "The requested page could not be found.",
    }

    staging = Path(tempfile.mkdtemp(prefix=".site-build-", dir=ROOT))

    try:
        copy_assets(staging, markdown.has_math)

        for path, title, template, content_name in pages:
            noindex = site["demo"] or path in {"/internal/", "/404.html"}
            page = {
                "path": path,
                "title": title,
                "description": descriptions[path],
                "robots": "noindex, follow" if noindex else "index, follow",
                # Shared pre-rendered fields may contain mathematics.
                "has_math": markdown.has_math,
            }
            html = environment.get_template(template).render(
                **context,
                page=page,
                content_html=content.get(content_name, ""),
            )
            relative = (
                "index.html" if path == "/"
                else path.lstrip("/") + "index.html" if path.endswith("/")
                else path.lstrip("/")
            )
            output = staging / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(html + "\n", encoding="utf-8")

        calendar_path = staging / "seminar" / "calendar.ics"
        calendar_path.write_bytes(calendar_bytes(site, talks, absolute_url))

        public_paths = [
            path for path, *_ in pages
            if path not in {"/internal/", "/404.html"}
        ]
        write_sitemap(
            staging,
            [] if site["demo"] else public_paths,
            absolute_url,
        )

        # Allow crawling so crawlers can see the pages' noindex directives.
        (staging / "robots.txt").write_text(
            "User-agent: *\n"
            "Allow: /\n"
            f"Sitemap: {absolute_url('/sitemap.xml')}\n",
            encoding="utf-8",
        )
        (staging / ".nojekyll").write_text("", encoding="utf-8")

        install_output(staging)

    finally:
        # staging has moved away after a successful installation.
        if staging.exists():
            shutil.rmtree(staging)

    print(
        f"Built {len(pages)} pages, {len(talks)} calendar events, "
        f"and {len(publications)} publications in {ROOT / '_site'}."
    )
    if site["demo"]:
        print("Example-content mode is enabled; pages are marked noindex.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", help="ISO date-time with an explicit UTC offset")
    args = parser.parse_args()

    try:
        build(args.now)
    except (SiteError, OSError, ValueError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

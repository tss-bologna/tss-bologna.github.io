"""Fetch DBLP bibliographies into the committed JSON cache.

Usage:
    python scripts/fetch_dblp.py

The cache stores:
    authors[pid]:
        name: DBLP's display name
        keys: publication keys associated with the author
        fetched_at: UTC timestamp of the stored snapshot

    records[key]:
        key, title, authors, venue, year, electronic_url, type

Affiliation filtering happens during the website build, not here.
This keeps the cache useful when affiliation dates or statuses change.

No partial updates are written. A network or validation failure preserves
the existing cache and returns a nonzero exit code.

Identical bibliographic content does not change the cache or its timestamps.
The timestamp records acquisition of the stored snapshot, rather than every
subsequent check that found identical data.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from defusedxml import ElementTree

from common import (
    CACHE,
    DATA,
    SiteError,
    http_url,
    load_cache,
    load_yaml,
    optional_text,
    reject_unknown,
    require_list,
    require_mapping,
    require_text,
    unique_ids,
)


DBLP_ORIGIN = "https://dblp.org"
USER_AGENT = (
    "TSS-Bologna-Website/0.1 "
    "(https://tss-bologna.github.io; bibliographic site build)"
)

# Only these publication elements are accepted from DBLP XML.
PUBLICATION_TYPES = {
    "article",
    "inproceedings",
    "proceedings",
    "book",
    "incollection",
    "phdthesis",
    "mastersthesis",
    "www",
    "data",
}

# Restrict identifiers before interpolating them into URLs.
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]*")

REQUEST_TIMEOUT = 30
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MIN_REQUEST_INTERVAL = 1.0
MAX_ATTEMPTS = 3


def dblp_identifier(value, where: str) -> str:
    value = require_text(value, where)

    if (
        not IDENTIFIER_PATTERN.fullmatch(value)
        or "/" not in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or value.endswith((".html", ".xml"))
    ):
        raise SiteError(
            f"{where}: expected a DBLP identifier without a URL "
            "prefix or file extension."
        )

    return value


def read_author_ids() -> list[str]:
    """Fetch all listed authors, including former people and collaborators."""
    people = require_list(load_yaml(DATA / "people.yaml"), "people")
    unique_ids(people, "people")

    identifiers = set()

    for person in people:
        value = person.get("dblp")
        if value is not None:
            identifiers.add(
                dblp_identifier(value, f"person {person['id']}.dblp")
            )

    return sorted(identifiers)


def read_overrides() -> dict[str, list[dict]]:
    raw = require_mapping(
        load_yaml(DATA / "publication_overrides.yaml"),
        "publication overrides",
    )
    reject_unknown(raw, {"include", "exclude"}, "publication overrides")

    result = {}

    for operation in ("include", "exclude"):
        entries = require_list(raw.get(operation, []), operation)
        seen = set()
        normalised = []

        for index, entry in enumerate(entries):
            where = f"{operation}[{index}]"
            entry = require_mapping(entry, where)
            reject_unknown(entry, {"key", "reason"}, where)

            key = dblp_identifier(entry.get("key"), f"{where}.key")
            reason = optional_text(entry.get("reason"), f"{where}.reason")

            if key in seen:
                raise SiteError(f"{where}: duplicate publication key {key!r}.")

            seen.add(key)
            normalised.append({"key": key, "reason": reason})

        result[operation] = normalised

    return result


class Client:
    """Small sequential HTTP client with bounded retries and response size."""

    def __init__(self):
        self.last_request = 0.0

    def xml(self, path: str):
        url = DBLP_ORIGIN + path
        last_error = None

        for attempt in range(MAX_ATTEMPTS):
            delay = MIN_REQUEST_INTERVAL - (
                time.monotonic() - self.last_request
            )
            if delay > 0:
                time.sleep(delay)

            self.last_request = time.monotonic()
            request = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/xml, text/xml",
                },
            )

            try:
                with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                    final_url = urlsplit(response.geturl())
                    if final_url.scheme != "https":
                        raise SiteError(f"Non-HTTPS redirect received for {url}.")

                    payload = response.read(MAX_RESPONSE_BYTES + 1)

                if len(payload) > MAX_RESPONSE_BYTES:
                    raise SiteError(f"Response exceeds size limit: {url}.")

            except HTTPError as exc:
                last_error = exc

                # Retry transient failures only. An invalid key must fail.
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise SiteError(
                        f"DBLP returned HTTP {exc.code} for {url}."
                    ) from exc

                retry_after = exc.headers.get("Retry-After", "")
                if retry_after:
                    try:
                        requested_delay = float(retry_after)
                    except ValueError:
                        requested_delay = None

                    # Do not retry sooner than a long server-requested delay.
                    # Let a later workflow run try again instead.
                    if requested_delay is None or requested_delay > 10:
                        raise SiteError(
                            f"DBLP requested a later retry for {url}; "
                            "try again later."
                        ) from exc
                    if requested_delay > 0:
                        time.sleep(requested_delay)

            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc

            else:
                try:
                    # External entities and entity expansion remain disabled.
                    return ElementTree.fromstring(payload)
                except Exception as exc:
                    raise SiteError(
                        f"Could not parse DBLP XML from {url}: {exc}"
                    ) from exc

            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(2 ** attempt)

        raise SiteError(
            f"DBLP request failed after {MAX_ATTEMPTS} attempts: "
            f"{url}: {last_error}"
        )


def element_text(element) -> str:
    """Preserve text nested inside title markup such as <i> or <sub>."""
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def child_text(element, name: str) -> str:
    return element_text(element.find(name))


def parse_publication(element) -> dict:
    key = dblp_identifier(element.get("key"), "DBLP publication key")
    title = child_text(element, "title")
    if not title:
        raise SiteError(f"DBLP record {key!r} has no title.")

    year_text = child_text(element, "year")
    if not re.fullmatch(r"\d{4}", year_text):
        raise SiteError(f"DBLP record {key!r} has an invalid year.")

    authors = [
        element_text(author)
        for author in element.findall("author")
        if element_text(author)
    ]

    # Edited collections may identify editors rather than authors.
    if not authors:
        authors = [
            f"{element_text(editor)} (ed.)"
            for editor in element.findall("editor")
            if element_text(editor)
        ]

    journal = child_text(element, "journal")
    booktitle = child_text(element, "booktitle")
    school = child_text(element, "school")
    publisher = child_text(element, "publisher")
    venue = journal or booktitle or school or publisher

    volume = child_text(element, "volume")
    number = child_text(element, "number")
    pages = child_text(element, "pages")

    if journal and volume:
        venue += f" {volume}"
        if number:
            venue += f"({number})"
    elif journal and number:
        venue += f", no. {number}"

    if pages:
        venue += f", pp. {pages}" if venue else f"pp. {pages}"

    electronic_url = None
    for electronic in element.findall("ee"):
        candidate = element_text(electronic)
        if not candidate:
            continue
        try:
            electronic_url = http_url(candidate, f"DBLP record {key}.ee")
        except SiteError:
            # Some historical records may contain non-HTTP identifiers.
            continue
        break

    return {
        "key": key,
        "title": title,
        "authors": authors,
        "venue": venue,
        "year": int(year_text),
        "electronic_url": electronic_url,
        "type": element.tag,
    }


def publication_elements(root):
    """Handle both author-profile XML and individual-record XML."""
    if root.tag in PUBLICATION_TYPES:
        yield root
        return

    for element in root.iter():
        if element.tag in PUBLICATION_TYPES and element.get("key"):
            yield element


def fetch_author(client: Client, pid: str) -> tuple[str, dict[str, dict]]:
    path = "/pid/" + quote(pid, safe="/") + ".xml"
    root = client.xml(path)

    if root.tag != "dblpperson":
        raise SiteError(f"Unexpected DBLP response for author {pid!r}.")

    name = root.get("name", "").strip()
    if not name:
        raise SiteError(f"DBLP author {pid!r} has no display name.")

    records = {}
    for element in publication_elements(root):
        record = parse_publication(element)
        records[record["key"]] = record

    # Empty bibliographies are accepted: the returned profile is still valid.
    return name, records


def fetch_record(client: Client, key: str) -> dict:
    path = "/rec/" + quote(key, safe="/") + ".xml"
    root = client.xml(path)

    for element in publication_elements(root):
        record = parse_publication(element)
        if record["key"] == key:
            return record

    raise SiteError(
        f"DBLP did not return the requested record {key!r}. "
        "Check the publication override."
    )


def write_cache(cache: dict) -> None:
    """Atomically replace the cache after every request has succeeded."""
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=CACHE.parent,
            prefix="dblp-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = temporary.name
            json.dump(cache, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())

        os.replace(temporary_path, CACHE)
        temporary_path = None

    finally:
        if temporary_path is not None:
            os.unlink(temporary_path)


def refresh() -> None:
    author_ids = read_author_ids()
    overrides = read_overrides()
    old_cache = load_cache()
    client = Client()

    # Start a new snapshot so removed authors and unused records are pruned
    # only after a fully successful refresh.
    new_cache = {
        "schema_version": 1,
        "authors": {},
        "records": {},
    }

    snapshots = {}
    fetched_records = {}

    for pid in author_ids:
        print(f"Fetching author {pid}…", flush=True)
        name, records = fetch_author(client, pid)
        snapshots[pid] = {
            "name": name,
            "keys": sorted(records),
        }
        fetched_records.update(records)

    override_keys = {
        entry["key"]
        for operation in ("include", "exclude")
        for entry in overrides[operation]
    }

    # Validate exclusions too: misspelled exclusions must not silently pass.
    for key in sorted(override_keys):
        if key not in fetched_records:
            print(f"Resolving override {key}…", flush=True)
            fetched_records[key] = fetch_record(client, key)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for pid, snapshot in snapshots.items():
        old_author = old_cache["authors"].get(pid)
        unchanged = (
            isinstance(old_author, dict)
            and old_author.get("name") == snapshot["name"]
            and old_author.get("keys") == snapshot["keys"]
            and bool(old_author.get("fetched_at"))
            and all(
                old_cache["records"].get(key) == fetched_records[key]
                for key in snapshot["keys"]
            )
        )

        snapshot["fetched_at"] = (
            old_author["fetched_at"] if unchanged else now
        )
        new_cache["authors"][pid] = snapshot

    new_cache["records"] = fetched_records

    if new_cache == old_cache:
        print("DBLP content is unchanged; cache left untouched.")
        return

    write_cache(new_cache)
    print(
        f"Updated cache: {len(new_cache['authors'])} authors, "
        f"{len(new_cache['records'])} records."
    )


def main() -> int:
    try:
        refresh()
    except (SiteError, OSError) as exc:
        print(f"DBLP refresh failed: {exc}", file=sys.stderr)
        print("The previous cache has been preserved.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

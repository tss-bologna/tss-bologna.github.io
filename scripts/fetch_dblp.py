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
from urllib.parse import quote, urlsplit, urlencode
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
    """Query DBLP's public SPARQL service, sequentially."""

    ENDPOINT = "https://sparql.dblp.org/sparql"

    def __init__(self):
        self.last_request = 0.0

    def query(self, query: str) -> list[dict]:
        delay = MIN_REQUEST_INTERVAL - (
            time.monotonic() - self.last_request
        )
        if delay > 0:
            time.sleep(delay)

        self.last_request = time.monotonic()

        request = Request(
            self.ENDPOINT,
            data=urlencode({"query": query}).encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/sparql-results+json",
            },
        )

        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                content_type = response.headers.get("Content-Type", "")
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except (URLError, TimeoutError, OSError) as exc:
            raise SiteError(f"DBLP SPARQL request failed: {exc}") from exc

        if len(payload) > MAX_RESPONSE_BYTES:
            raise SiteError("DBLP SPARQL response exceeds the size limit.")

        if "json" not in content_type.lower():
            raise SiteError(
                f"DBLP SPARQL returned {content_type!r}, not JSON. "
                f"Response starts with {payload[:200]!r}"
            )

        try:
            document = json.loads(payload)
            rows = document["results"]["bindings"]
            if not isinstance(rows, list):
                raise ValueError("bindings is not a list")
        except (ValueError, KeyError, TypeError) as exc:
            raise SiteError("Invalid DBLP SPARQL response.") from exc

        return rows


PREFIXES = """
PREFIX dblp: <https://dblp.org/rdf/schema#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""

RECORD_PREFIX = "https://dblp.org/rec/"
SCHEMA_PREFIX = "https://dblp.org/rdf/schema#"


def binding(row: dict, name: str) -> str:
    try:
        value = row[name]["value"]
    except (KeyError, TypeError) as exc:
        raise SiteError(f"Missing SPARQL binding: {name}.") from exc
    return require_text(value, f"SPARQL binding {name}")


def record_key(iri: str) -> str:
    if not iri.startswith(RECORD_PREFIX):
        raise SiteError(f"Unexpected publication identifier: {iri!r}.")
    return dblp_identifier(iri[len(RECORD_PREFIX):], "publication key")


def paginated(client: Client, query: str, order: str) -> list[dict]:
    """Explicit pagination avoids relying on a server's default row limit."""
    size = 1000
    result = []

    for page in range(1000):
        rows = client.query(
            PREFIXES
            + query
            + f"\nORDER BY {order}\nLIMIT {size}\nOFFSET {page * size}"
        )
        result.extend(rows)
        if len(rows) < size:
            return result

    raise SiteError("SPARQL pagination limit reached; cache not updated.")


def fetch_records(client: Client, keys: list[str]) -> dict[str, dict]:
    """Fetch metadata and ordered authorship signatures in small batches."""
    records = {}

    for offset in range(0, len(keys), 25):
        batch = keys[offset:offset + 25]
        values = " ".join(
            f"<{RECORD_PREFIX}{dblp_identifier(key, 'record key')}>"
            for key in batch
        )

        metadata = paginated(
            client,
            """
            SELECT DISTINCT ?publ ?pred ?value WHERE {
              VALUES ?publ { %s }
              VALUES ?pred {
                rdf:type
                dblp:title
                dblp:yearOfPublication
                dblp:bibtexType
                dblp:publishedIn
                dblp:publishedInJournal
                dblp:publishedInBook
                dblp:publishedBy
                dblp:pagination
                dblp:documentPage
                dblp:doi
              }
              ?publ ?pred ?value .
            }
            """ % values,
            "?publ ?pred ?value",
        )

        signatures = paginated(
            client,
            """
            SELECT DISTINCT ?publ ?sig ?kind ?ordinal ?name WHERE {
              VALUES ?publ { %s }
              ?publ dblp:hasSignature ?sig .
              ?sig rdf:type ?kind ;
                   dblp:signatureOrdinal ?ordinal ;
                   dblp:signatureDblpName ?name .
              VALUES ?kind { dblp:AuthorSignature dblp:EditorSignature }
            }
            """ % values,
            "?publ ?kind ?ordinal ?sig ?name",
        )

        fields = {key: {} for key in batch}
        names = {key: {"authors": {}, "editors": {}} for key in batch}

        for row in metadata:
            key = record_key(binding(row, "publ"))
            predicate = binding(row, "pred").removeprefix(SCHEMA_PREFIX)
            fields[key].setdefault(predicate, []).append(binding(row, "value"))

        for row in signatures:
            key = record_key(binding(row, "publ"))
            role = (
                "authors"
                if binding(row, "kind") == SCHEMA_PREFIX + "AuthorSignature"
                else "editors"
            )
            try:
                ordinal = int(binding(row, "ordinal"))
            except ValueError as exc:
                raise SiteError(f"Invalid author order for {key}.") from exc

            signature = binding(row, "sig")
            names[key][role][(ordinal, signature)] = binding(row, "name")

        for key in batch:
            data = fields[key]

            def first(predicate: str) -> str:
                values = data.get(predicate, [])
                return sorted(set(values))[0] if values else ""

            title = first("title")
            year_text = first("yearOfPublication")
            if not title or not re.fullmatch(r"\d{4}", year_text):
                raise SiteError(
                    f"DBLP record {key!r} is missing a title or valid year."
                )

            authors = [
                name for _, name in sorted(names[key]["authors"].items())
            ]
            if not authors:
                authors = [
                    f"{name} (ed.)"
                    for _, name in sorted(names[key]["editors"].items())
                ]

            if not authors:
                raise SiteError(
                    f"DBLP record {key!r} has no ordered creator metadata."
                )

            venue = (
                first("publishedIn")
                or first("publishedInJournal")
                or first("publishedInBook")
                or first("publishedBy")
            )
            pages = first("pagination")
            if pages:
                venue += f", pp. {pages}" if venue else f"pp. {pages}"

            electronic_url = None
            for candidate in (
                sorted(set(data.get("doi", [])))
                + sorted(set(data.get("documentPage", [])))
            ):
                try:
                    electronic_url = http_url(candidate, f"{key}.url")
                except SiteError:
                    continue
                break

            publication_type = first("bibtexType")
            if publication_type:
                publication_type = (
                    publication_type.rsplit("#", 1)[-1]
                    .rsplit("/", 1)[-1]
                    .lower()
                )
            else:
                publication_type = "publication"

            records[key] = {
                "key": key,
                "title": title,
                "authors": authors,
                "venue": venue,
                "year": int(year_text),
                "electronic_url": electronic_url,
                "type": publication_type,
                "informal": (
                    SCHEMA_PREFIX + "Informal"
                    in data.get(
                        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                        [],
                    )
                ),
            }

    return records


def fetch_author(client: Client, pid: str) -> tuple[str, dict[str, dict]]:
    pid = dblp_identifier(pid, "author ID")
    person = f"<https://dblp.org/pid/{pid}>"

    rows = client.query(
        PREFIXES
        + f"""
        SELECT ?name WHERE {{
          {person} rdfs:label ?name .
        }}
        ORDER BY ?name
        LIMIT 1
        """
    )
    if not rows:
        raise SiteError(f"DBLP SPARQL did not recognise author {pid!r}.")

    name = binding(rows[0], "name")

    rows = paginated(
        client,
        f"""
        SELECT DISTINCT ?publ WHERE {{
          ?publ dblp:createdBy {person} .
        }}
        """,
        "?publ",
    )
    keys = sorted({record_key(binding(row, "publ")) for row in rows})
    return name, fetch_records(client, keys)


def fetch_record(client: Client, key: str) -> dict:
    return fetch_records(client, [key])[key]


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

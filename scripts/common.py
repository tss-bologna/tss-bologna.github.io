"""Shared helpers for the website build and DBLP fetcher.

Requires Python 3.11 or later.

Paths are resolved relative to the repository, not the caller's directory.
User-authored prose is Markdown; embedded HTML is disabled.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token
from markupsafe import Markup
from mdit_py_plugins.dollarmath import dollarmath_plugin


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CACHE = ROOT / "cache" / "dblp.json"

ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
DATETIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")


class SiteError(ValueError):
    """An actionable configuration, data, or build error."""


class StrictLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_mapping(
    loader: StrictLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict:
    loader.flatten_mapping(node)
    result = {}

    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)

        try:
            duplicate = key in result
        except TypeError as exc:
            raise SiteError(
                f"YAML line {key_node.start_mark.line + 1}: "
                "mapping keys must be scalar values."
            ) from exc

        if duplicate:
            raise SiteError(
                f"YAML line {key_node.start_mark.line + 1}: "
                f"duplicate key {key!r}."
            )

        result[key] = loader.construct_object(value_node, deep=deep)

    return result


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_yaml(path: Path) -> Any:
    """Read YAML, retaining its normal types and rejecting duplicate keys."""
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=StrictLoader)
    except (OSError, yaml.YAMLError, SiteError) as exc:
        raise SiteError(f"{path}: {exc}") from exc


def require_mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise SiteError(f"{where}: expected a mapping.")
    return value


def require_list(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise SiteError(f"{where}: expected a list.")
    return value


def require_text(
    value: Any,
    where: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise SiteError(f"{where}: expected a string.")
    if not allow_empty and not value.strip():
        raise SiteError(f"{where}: must not be empty.")
    return value


def optional_text(value: Any, where: str) -> str | None:
    if value is None:
        return None
    return require_text(value, where)


def require_bool(value: Any, where: str) -> bool:
    if type(value) is not bool:
        raise SiteError(f"{where}: expected true or false.")
    return value


def require_int(
    value: Any,
    where: str,
    *,
    minimum: int = 0,
) -> int:
    if type(value) is not int or value < minimum:
        raise SiteError(f"{where}: expected an integer >= {minimum}.")
    return value


def reject_unknown(
    record: dict,
    allowed: set[str],
    where: str,
) -> None:
    unknown = set(record) - allowed
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        raise SiteError(f"{where}: unknown field(s): {names}.")


def require_id(value: Any, where: str) -> str:
    value = require_text(value, where)
    if not ID_PATTERN.fullmatch(value):
        raise SiteError(
            f"{where}: use lowercase letters, digits, and hyphens; "
            "start with a letter or digit."
        )
    return value


def unique_ids(records: list[dict], where: str) -> None:
    seen = set()
    for index, record in enumerate(records):
        record = require_mapping(record, f"{where}[{index}]")
        identifier = require_id(record.get("id"), f"{where}[{index}].id")
        if identifier in seen:
            raise SiteError(f"{where}: duplicate id {identifier!r}.")
        seen.add(identifier)


def parse_date(value: Any, where: str) -> date:
    """Require quoted ISO dates rather than YAML's implicit date objects."""
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise SiteError(f'{where}: expected a quoted date, "YYYY-MM-DD".')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SiteError(f"{where}: invalid date {value!r}.") from exc


def get_timezone(name: Any) -> ZoneInfo:
    name = require_text(name, "timezone")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise SiteError(f"Unknown IANA timezone: {name!r}.") from exc


def parse_datetime(
    value: Any,
    zone: ZoneInfo,
    where: str,
) -> datetime:
    """Parse a local date-time, rejecting DST gaps and ambiguous times."""
    if not isinstance(value, str) or not DATETIME_PATTERN.fullmatch(value):
        raise SiteError(
            f'{where}: expected a quoted date-time, "YYYY-MM-DD HH:MM".'
        )

    try:
        naive = datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise SiteError(f"{where}: invalid date-time {value!r}.") from exc

    # Round-tripping through UTC distinguishes real local times from DST gaps.
    utc = ZoneInfo("UTC")
    candidates = []

    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive:
            candidates.append(candidate)

    if not candidates:
        raise SiteError(f"{where}: local time does not exist due to DST.")

    if len({candidate.utcoffset() for candidate in candidates}) > 1:
        raise SiteError(
            f"{where}: local time is ambiguous due to DST; "
            "choose an unambiguous event time."
        )

    return candidates[0]


MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
SHORT_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def display_date(value: date) -> str:
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}"


def display_datetime(value: datetime) -> str:
    """Match the original PHP's 'D j M Y, H:i' format."""
    return (
        f"{WEEKDAYS[value.weekday()]} "
        f"{value.day} {SHORT_MONTHS[value.month - 1]} {value.year}, "
        f"{value:%H:%M}"
    )


def http_url(value: Any, where: str) -> str:
    value = require_text(value, where)
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or any(character.isspace() for character in value)
    ):
        raise SiteError(f"{where}: expected an HTTP(S) URL.")
    return value


def optional_url(value: Any, where: str) -> str | None:
    if value is None:
        return None
    return http_url(value, where)


def static_path(value: Any, where: str) -> str | None:
    """Validate an existing file path relative to static/."""
    if value is None:
        return None

    value = require_text(value, where)
    relative = Path(value)

    if relative.is_absolute() or ".." in relative.parts:
        raise SiteError(f"{where}: expected a path within static/.")

    static_root = (ROOT / "static").resolve()
    target = (static_root / relative).resolve()

    if not target.is_relative_to(static_root) or not target.is_file():
        raise SiteError(f"{where}: static file does not exist: {value!r}.")

    return relative.as_posix()


def make_url_helpers(site: dict):
    """Return helpers for site-root paths, including project-site prefixes."""
    origin = http_url(site.get("url"), "site.url")

    parts = urlsplit(origin)
    if parts.path or parts.query or parts.fragment:
        raise SiteError("site.url must be an origin without a trailing slash.")

    base = require_text(
        site.get("base_path", ""),
        "site.base_path",
        allow_empty=True,
    )

    if base and (
        not base.startswith("/")
        or base.startswith("//")
        or base.endswith("/")
        or "?" in base
        or "#" in base
        or ".." in base.split("/")
        or any(character.isspace() for character in base)
    ):
        raise SiteError("site.base_path must be empty or '/repository-name'.")

    def local_url(path: str) -> str:
        if not path.startswith("/") or path.startswith("//"):
            raise SiteError(f"Expected a site-root path, got {path!r}.")
        return base + path

    def absolute_url(path: str) -> str:
        return origin + local_url(path)

    return local_url, absolute_url


def load_cache() -> dict:
    """Read the committed cache; fetching and schema checks are separate."""
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SiteError(f"Cannot read {CACHE}: {exc}") from exc

    require_mapping(cache, "DBLP cache")
    if cache.get("schema_version") != 1:
        raise SiteError("Unsupported DBLP cache schema version.")
    require_mapping(cache.get("authors"), "DBLP cache authors")
    require_mapping(cache.get("records"), "DBLP cache records")
    return cache


class MarkdownRenderer:
    """Render Markdown and batch its mathematical expressions through KaTeX.

    Each call renders one Markdown field. KaTeX runs once for all formulas
    in that field, with no browser-side JavaScript.

    Rendered HTML is returned as Markup for Jinja2. Do not apply Markup to
    arbitrary raw data elsewhere.
    """

    def __init__(self, local_url):
        self.local_url = local_url
        self.has_math = False

        self.parser = MarkdownIt(
            "commonmark",
            {
                "html": False,
                "breaks": False,
                "typographer": False,
            },
        ).use(
            dollarmath_plugin,
            allow_labels=False,
            allow_space=False,
            allow_digits=False,
        )

        self.parser.add_render_rule("math_inline", self._render_math)
        self.parser.add_render_rule("math_block", self._render_math)

    @staticmethod
    def _render_math(renderer, tokens, index, options, env):
        return env["math_html"][id(tokens[index])]

    @staticmethod
    def _walk(tokens):
        for token in tokens:
            yield token
            if token.children:
                yield from MarkdownRenderer._walk(token.children)

    def render(self, text: str, where: str) -> Markup:
        require_text(text, where, allow_empty=True)

        environment = {}
        tokens = self.parser.parse(text, environment)
        return self._render_tokens(tokens, environment, where)

    def render_title(self, text: str, where: str) -> Markup:
        """Render title formulas inline, without interpreting Markdown or HTML."""
        require_text(text, where, allow_empty=True)
        pattern = re.compile(
            r"(?<!\\)\$\$(.+?)(?<!\\)\$\$"
            r"|(?<![\\$])\$(?!\$)(.+?)(?<!\\)\$(?!\$)"
            r"|\\\((.+?)\\\)"
            r"|\\\[(.+?)\\\]",
            re.DOTALL,
        )
        tokens = []
        position = 0

        for match in pattern.finditer(text):
            plain = Token("text", "", 0)
            plain.content = text[position:match.start()]
            tokens.append(plain)

            formula = Token("math_inline", "math", 0)
            formula.content = next(
                group for group in match.groups() if group is not None
            ).strip()
            tokens.append(formula)
            position = match.end()

        tail = Token("text", "", 0)
        tail.content = text[position:]
        tokens.append(tail)
        return self._render_tokens(tokens, {}, where)

    def _render_tokens(self, tokens, environment, where: str) -> Markup:
        math_tokens = []

        for token in self._walk(tokens):
            if token.type in {"math_inline", "math_block"}:
                math_tokens.append(token)

            # Root-relative Markdown links work on both organisation
            # sites and project sites.
            if token.type == "link_open":
                href = token.attrGet("href")
                if href:
                    if href.lower().startswith(("https://", "http://", "//")):
                        token.attrSet("target", "_blank")
                        token.attrSet("rel", "noopener noreferrer")
                    elif href.startswith("/"):
                        token.attrSet("href", self.local_url(href))

            if token.type == "image":
                src = token.attrGet("src")
                if src and src.startswith("/") and not src.startswith("//"):
                    token.attrSet("src", self.local_url(src))

        environment["math_html"] = {}

        if math_tokens:
            formulas = [
                {
                    "tex": token.content,
                    "display": token.type == "math_block",
                }
                for token in math_tokens
            ]

            try:
                result = subprocess.run(
                    ["node", str(ROOT / "scripts" / "render_math.cjs")],
                    input=json.dumps(formulas),
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=60,
                    cwd=ROOT,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise SiteError(
                    f"{where}: Node.js is required for mathematics."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise SiteError(f"{where}: KaTeX rendering timed out.") from exc

            if result.returncode:
                raise SiteError(
                    f"{where}: {result.stderr.strip() or 'KaTeX failed.'}"
                )

            try:
                rendered = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise SiteError(f"{where}: invalid KaTeX response.") from exc

            if (
                not isinstance(rendered, list)
                or len(rendered) != len(math_tokens)
                or not all(isinstance(item, str) for item in rendered)
            ):
                raise SiteError(f"{where}: unexpected KaTeX output.")

            environment["math_html"] = {
                id(token): html
                for token, html in zip(math_tokens, rendered)
            }
            self.has_math = True

        html = self.parser.renderer.render(
            tokens,
            self.parser.options,
            environment,
        )
        return Markup(html)

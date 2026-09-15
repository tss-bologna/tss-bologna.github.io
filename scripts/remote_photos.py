"""Validate local portraits and check remote HTTPS portraits during the build.

Remote failures produce GitHub Actions warnings and select the SVG placeholder.
Local paths retain strict validation. No images are stored.
"""

from functools import lru_cache
from urllib.error import URLError
from urllib.request import Request, urlopen

from common import SiteError, http_url, optional_text, static_path


@lru_cache(maxsize=None)
def remote_problem(url: str) -> str | None:
    """Check HTTP status, image content type, and a nonempty response.

    Use GET because some servers do not support HEAD. Read only one byte.
    The browser fallback handles corrupt images and later failures.
    """
    request = Request(
        url,
        headers={
            "User-Agent": "TSS-Bologna-photo-check/1.0",
            "Accept": "image/*",
        },
    )

    try:
        with urlopen(request, timeout=10) as response:
            if not response.geturl().lower().startswith("https://"):
                return "redirected to a non-HTTPS address"

            if response.status != 200:
                return f"HTTP status {response.status}"

            content_type = response.headers.get_content_type()
            if not content_type.startswith("image/"):
                return "the response is not labelled as an image"

            if not response.read(1):
                return "empty image response"

    except (URLError, OSError, ValueError) as exc:
        return str(exc)

    return None


def photo_source(value, where: str, *, check_remote: bool = True):
    """Return a local path, an HTTPS URL, or None for the default portrait."""
    value = optional_text(value, where)
    if value is None:
        return None

    if value.lower().startswith(("http://", "https://")):
        value = http_url(value, where)

        if not value.startswith("https://"):
            raise SiteError(f"{where}: remote photos must use HTTPS.")

        problem = remote_problem(value) if check_remote else None

        if problem:
            message = (
                f"{where}: remote photo unavailable ({problem}); "
                "using the SVG placeholder."
            )

            # Escape GitHub workflow-command data.
            message = (
                message.replace("%", "%25")
                .replace("\r", "%0D")
                .replace("\n", "%0A")
            )
            print(f"::warning::{message}")
            return None

        return value

    return static_path(value, where)

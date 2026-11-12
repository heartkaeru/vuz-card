"""Bounded downloads for images embedded in generated presentations."""

from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image, UnidentifiedImageError


MAX_IMAGE_BYTES = 5 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def download_official_portrait(url: str | None, allowed_hosts: set[str]) -> tuple[bytes, tuple[int, int]] | None:
    """Download a small portrait only from the host of accepted rector evidence.

    A bad or unavailable image is treated as optional presentation decoration.
    Redirects are rejected so a trusted source cannot redirect the server elsewhere.
    """
    if not url or not allowed_hosts:
        return None
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return None
        if not any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts):
            return None
        opener = build_opener(_NoRedirect())
        request = Request(url, headers={"User-Agent": "VUZ-Card-Presentation/1.0", "Accept": "image/*"})
        with opener.open(request, timeout=5) as response:
            content_type = response.headers.get_content_type().lower()
            if content_type not in {"image/jpeg", "image/png"}:
                return None
            declared_length = response.headers.get("Content-Length")
            if declared_length and int(declared_length) > MAX_IMAGE_BYTES:
                return None
            content = response.read(MAX_IMAGE_BYTES + 1)
        if len(content) > MAX_IMAGE_BYTES:
            return None
        with Image.open(BytesIO(content)) as image:
            if image.format not in {"JPEG", "PNG"} or image.width * image.height > 40_000_000:
                return None
            image.verify()
        with Image.open(BytesIO(content)) as image:
            return content, image.size
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnidentifiedImageError):
        return None

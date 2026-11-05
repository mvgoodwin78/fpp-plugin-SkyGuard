"""The one place feeds touch the network.

Errors carry only a short fixed description. URLs can hold the home coordinates
or a password, and response bodies come from third parties, so neither is ever
put in an error that might reach the log.
"""

import http.client
import json
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .. import REPO_NAME
from .base import FeedError

DEFAULT_TIMEOUT_S = 3.0
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_RETRY_AFTER_S = 300
_CHUNK_BYTES = 64 * 1024
_ALLOWED_SCHEMES = ("http", "https")


class FeedHttpError(FeedError):
    """The server answered with an HTTP error status."""

    def __init__(self, status, retry_after=None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after  # seconds the server asked us to wait, if it said


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    """A root daemon must not follow a redirect to anything but http(s)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme not in _ALLOWED_SCHEMES:
            raise FeedError("redirected to an unsupported address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# No proxies: receivers are on the LAN, and FPP's environment shouldn't reroute feeds.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SafeRedirects)


def _read_limited(response, deadline):
    """Read the body, enforcing a total time limit and a size limit."""
    chunks = []
    total = 0
    while True:
        # read1 returns after one network read. read() would wait for the full chunk,
        # so a server dripping bytes could hold it open past any deadline.
        chunk = response.read1(_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise FeedError("response is too large")
        if time.monotonic() > deadline:
            raise FeedError("request timed out")
        chunks.append(chunk)


def _retry_after(headers):
    """Seconds from a Retry-After header, capped. The date form isn't used by these services."""
    value = (headers.get("Retry-After") or "").strip()
    return min(int(value), MAX_RETRY_AFTER_S) if value.isdigit() else None


def _describe(error):
    reason = getattr(error, "reason", error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        # FPP boxes often have no clock battery, so a wrong date is the usual cause.
        return "certificate check failed; check this device's date and time"
    if isinstance(reason, TimeoutError):
        return "request timed out"
    return "connection failed"


def fetch_json(url, timeout_s=DEFAULT_TIMEOUT_S):
    """GET a URL and parse it as JSON. Every failure raises FeedError."""
    if urlsplit(url).scheme not in _ALLOWED_SCHEMES:
        raise FeedError("unsupported address")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": REPO_NAME, "Accept": "application/json"},
    )
    deadline = time.monotonic() + timeout_s
    try:
        with _OPENER.open(request, timeout=timeout_s) as response:
            body = _read_limited(response, deadline)
    except urllib.error.HTTPError as error:
        status = error.code
        retry_after = _retry_after(error.headers)
        error.close()
        raise FeedHttpError(status, retry_after) from None
    except (OSError, http.client.HTTPException) as error:
        raise FeedError(_describe(error)) from None
    try:
        return json.loads(body)
    except ValueError:
        raise FeedError("response is not valid JSON") from None

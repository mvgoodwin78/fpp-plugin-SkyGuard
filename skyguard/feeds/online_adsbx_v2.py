"""Online providers with an ADSBx-v2-compatible point/radius query."""

import math
from dataclasses import dataclass

from .base import FeedError, parse_document
from .http_client import fetch_json

MAX_QUERY_RADIUS_NM = 250
DEFAULT_PROVIDER = "adsb.fi"


@dataclass(frozen=True)
class Provider:
    name: str
    url_template: str
    min_interval_s: float


PROVIDERS = {
    provider.name: provider
    for provider in (
        # Publishes a limit of 1 request a second; every 2 s leaves a margin and was tested.
        Provider("adsb.fi", "https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}", 2.0),
        # Limits are dynamic. It refused polling at 1 s and at 5 s, and accepted 10 s.
        Provider("adsb.lol", "https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}", 15.0),
    )
}


def query_radius_nm(display_range_nm):
    """Whole nautical miles, rounded up so the query never covers less than asked, capped at 250."""
    return max(1, min(MAX_QUERY_RADIUS_NM, math.ceil(display_range_nm)))


def build_url(provider_name, lat, lon, display_range_nm):
    provider = PROVIDERS[provider_name]
    return provider.url_template.format(
        lat=f"{lat:.4f}",
        lon=f"{lon:.4f}",
        nm=query_radius_nm(display_range_nm),
    )


def parse_online(doc, provider_name):
    if isinstance(doc, dict):
        message = doc.get("msg")
        if message is not None and str(message).strip().lower() != "no error":
            # The text comes from the provider, so it is not repeated into the log.
            raise FeedError("provider reported an error")
    return parse_document(doc, "ac", f"online:{provider_name}")


class OnlineAdapter:
    """A point/radius query to one provider."""

    def __init__(self, provider_name, lat, lon, display_range_nm, fetch=fetch_json):
        self.provider_name = provider_name
        self.url = build_url(provider_name, lat, lon, display_range_nm)
        self._fetch = fetch

    def fetch(self):
        return parse_online(self._fetch(self.url), self.provider_name)

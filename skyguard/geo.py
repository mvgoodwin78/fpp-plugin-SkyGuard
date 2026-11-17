"""Great-circle distance and unit conversion. All internal distances are nautical miles."""

import math

EARTH_RADIUS_NM = 6371.0088 / 1.852

NM_PER_UNIT = {
    "nm": 1.0,
    "mi": 1609.344 / 1852.0,
    "km": 1000.0 / 1852.0,
}


def to_nm(value, unit):
    """Convert a distance in `unit` (mi, nm, km) to nautical miles."""
    return value * NM_PER_UNIT[unit]


def from_nm(value_nm, unit):
    """Convert a distance in nautical miles to `unit` (mi, nm, km)."""
    return value_nm / NM_PER_UNIT[unit]


def haversine_nm(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points in decimal degrees, in nautical miles."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    # Rounding can push `a` just past 1 for antipodal points, which would fail in asin.
    return 2 * EARTH_RADIUS_NM * math.asin(math.sqrt(min(1.0, a)))


def offset_nm(center_lat, center_lon, lat, lon):
    """How far a point is north and east of the center, in nautical miles; negative is south
    or west. A flat-earth approximation, which is well within a poll's movement at the
    distances a zone covers. Longitude wraps, so a zone can cross the 180th meridian."""
    d_lon = (lon - center_lon + 180.0) % 360.0 - 180.0
    mid_lat = math.radians((lat + center_lat) / 2)
    north = math.radians(lat - center_lat) * EARTH_RADIUS_NM
    east = math.radians(d_lon) * EARTH_RADIUS_NM * math.cos(mid_lat)
    return north, east


def in_box(north_nm, east_nm, bounds_nm):
    """Whether an offset is inside a box given as (north, south, east, west) reaches, edges included."""
    north, south, east, west = bounds_nm
    return -south <= north_nm <= north and -west <= east_nm <= east


def zone_reach_nm(bounds_nm):
    """The distance from the center to a box's farthest corner."""
    north, south, east, west = bounds_nm
    return math.hypot(max(north, south), max(east, west))

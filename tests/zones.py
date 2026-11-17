"""Zones for tests: a square box the size of an old radius, so a test written for one
radius reads the same."""

from skyguard.config import FEED_ONLINE, FEED_RECEIVER, ZONE_ONLINE, ZONE_RECEIVER_1, Settings, Zone


def box(reach, floor=0.0, ceiling=10000.0, zone_id=ZONE_RECEIVER_1, unit="nm", feed=FEED_RECEIVER, label="Receiver zone 1"):
    """A square zone reaching `reach` on every side."""
    return Zone(zone_id, feed, label, reach, reach, reach, reach, floor, ceiling, unit)


def zone_settings(**values):
    """Settings, taking the old radius, radius_receiver and altitude keys as square zones."""
    unit = values.get("radius_unit", "mi")
    online = values.pop("radius", None)
    receiver = values.pop("radius_receiver", None)
    floor = values.pop("alt_floor_ft", 0.0)
    ceiling = values.pop("alt_ceiling_ft", 10000.0)
    zones = []
    if receiver is not None:
        zones.append(box(receiver, floor, ceiling, unit=unit))
    if online is not None:
        zones.append(box(online, floor, ceiling, ZONE_ONLINE, unit, FEED_ONLINE, "Online zone"))
    values.setdefault("zones", tuple(zones))
    return Settings(**values)

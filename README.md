# SkyGuard

An FPP plugin that blacks out moving-head light output whenever an aircraft is inside a radius you set, so beams aren't pointed at aircraft. Only each head's dimmer and shutter channels are forced to off values; pan, tilt, color and every other channel keep following the sequence.

**Status: in development.** It has been tested on one FPP 10.3 box, but not yet with real moving heads, a real receiver or on FPP 9.5. Don't rely on it at a show yet.

## How it works

- **Aircraft data** comes from a local ADS-B receiver (on another device, or on the FPP box itself), an online provider (adsb.fi or adsb.lol), or both. With both, the receiver has priority: an aircraft it has located goes by the receiver radius. During a show, an aircraft it misses goes by the online data and online radius, and if the receiver stops, online data takes over.
- **SkyGuard is for personal use only**, and it does nothing until you confirm that.
- **Online data can lag behind real aircraft**, so increase your radius by 3 miles when using it. You enter one radius for online data and another for your own receiver, and SkyGuard stays idle until each one in use has been entered.
- **Heads** are picked by their FPP model (uploaded from xLights with FPP Connect) or a start channel, with the dimmer and shutter channels from the fixture's DMX chart. SkyGuard follows a model that is re-patched.
- **Altitudes** are heights above your show: enter its elevation, or look it up.
- **During a show**, any qualifying aircraft inside the radius and altitude band blacks out the heads on that poll. They're released once the zone has been empty for the release delay.
- **It fails dark.** If aircraft data stops, or SkyGuard hits an unexpected error during a show, the heads black out. If the program itself crashes during a blackout, they stay dark until it restarts.
- **It doesn't replace your own judgment or any legal duty.** Coverage depends on what aircraft broadcast and on your data source, and not every aircraft can be seen.

## What it sends, and to whom

- **Online flight data:** your show's location and search radius go to the provider you choose, during a show only, unless you turn on checking between shows. The default data source is your own receiver, so nothing goes online until you choose online data.
- **Map:** while a map is open, your browser loads images from OpenStreetMap, which shows it roughly where your show is. Clear the map address under Advanced to stop this.
- **Address lookup:** a typed address goes to OpenStreetMap's Nominatim, only when you press Look up.
- **Local receiver:** requests go to the address you enter.

There's no telemetry. Your location and any API key are stored in `plugindata/fpp-plugin-SkyGuard/private.json`. Anyone who can reach FPP can read the plugin's status, which includes your location.

## Development

Run the tests with:

```
python -m unittest discover -s tests -t .
```

`SKYGUARD_SPEC.md` is the build spec and records what has been verified. Tests need only the Python standard library.

## License

GPLv2. See `LICENSE`.

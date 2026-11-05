# SkyGuard

An FPP plugin that blacks out moving-head light output whenever an aircraft is inside a radius you set, so beams aren't pointed at aircraft. Only each head's dimmer and shutter channels are forced to off values; pan, tilt, color and every other channel keep following the sequence.

**Status: in development.** The backend is built and its settings pages aren't. It has been tested against FPP's API on one FPP 10.3 box, but not yet with real moving heads. Don't rely on it at a show yet.

## How it works

- **Aircraft data** comes from a local ADS-B receiver, an online provider (adsb.lol or adsb.fi), or the receiver with online as a fallback.
- **During a show**, any qualifying aircraft inside the radius and altitude band blacks out the heads on that poll. They're released once the zone has been empty for the release delay.
- **It fails dark.** If aircraft data stops, or SkyGuard hits an unexpected error during a show, the heads black out. If the program itself crashes during a blackout, they stay dark until it restarts.
- **It doesn't replace your own judgment or any legal duty.** Coverage depends on what aircraft broadcast and on your data source, and not every aircraft can be seen.

## What it sends, and to whom

- **Online flight data:** your show's location and search radius go to the provider you choose.
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

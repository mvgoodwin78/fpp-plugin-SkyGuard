# SkyGuard — FPP Plugin Build Spec

Nov 5, 2026

## Purpose and scope

SkyGuard blacks out the light output of moving heads whenever any aircraft is inside a user-defined radius, so beams are never pointed at aircraft. Pan, tilt, color, gobo and every other channel keep following the sequence; only each head's dimmer and shutter channels are forced to a user-set off value. When the zone clears, those channels are released back to the sequence.

This document is the build spec for Claude Code. It covers phase 1 backend behavior. The user interface is specified in its own section and built after the backend.

**Phase 1 (this build):** data feeds with fallback, trigger engine, per-fixture blackout, schedule awareness, matrix message, FPP warnings, FPP commands, status API, public-listing compliance.

**Later phases:** phase 1b is the UI (see User interface), built once the backend passes its tests; text/push alerts and MQTT come later (see Deferred features).

**Plugin identity:** `repoName` = `fpp-plugin-SkyGuard`, display name SkyGuard, license GPLv2, intended for public listing in fpp-data.

## Platform and compliance requirements

The plugin must run on FPP 9.5 and later, on Raspberry Pi 3, 4 and 5 and on BeagleBone, and must pass the fpp-data automated plugin check with no Blocker or Best-practice findings.

| Area | Requirement |
| --- | --- |
| Language | Python 3, using FPP's system `python3`; no per-plugin venv |
| Dependencies | Install from `scripts/fpp_install.sh` with `pip install --break-system-packages` or `apt-get` only. Also declare them in `pluginInfo.json` `dependencies` for FPP 10+. Prefer the standard library (`urllib`, `json`, `math`, `threading`) to keep installs light |
| Logging | Exactly one log: `<logdir>/plugin-fpp-plugin-SkyGuard.log`, logdir from the `LOGDIR` env var (fallback `/home/fpp/media/logs`). Append mode, no self-rotation, no secrets |
| Settings | `WriteSettingToFile` / FPP plugin config, stored as `config/plugin.fpp-plugin-SkyGuard`. Larger data (fixture list) as JSON under `<mediadir>/plugindata/fpp-plugin-SkyGuard/` |
| FPP access | HTTP API on `http://localhost/api/...` only. Never port 32322, never read or edit FPP's config files directly |
| Restarts | Never restart fppd or reboot. Set `restartFlag` only if truly needed |
| Filesystem | Write only to the plugin dir, the one log, the plugin config and `plugindata/` |
| Forbidden | No sudo, no piped remote installs (curl into bash), no telemetry, ads or donation links |
| Menu | At most one `menu.inc` entry per type (status, content, output, help) |
| Template files | Delete `PLUGIN_GUIDELINES.md` and `PLUGININFO_FORMAT.md` from the fork |

Claude Code should read [PLUGIN_GUIDELINES.md](https://github.com/FalconChristmas/fpp-plugin-Template/blob/master/PLUGIN_GUIDELINES.md) in full before writing code, and [PLUGINS.md](https://github.com/FalconChristmas/fpp-data/blob/master/PLUGINS.md) before submission.

## Architecture and file layout

SkyGuard is one long-running Python daemon started by FPP's lifecycle hooks; a trigger engine inside it is the only part that decides blackout, and every output follows its state.

```
INPUTS                      SKYGUARD DAEMON                 OUTPUTS

Receiver Pi        ─┐                                  ┌─> Overlay controller
(aircraft.json, LAN) ├─> Feed manager                  │   (dimmer + shutter only,
Online feed        ─┘   (10 s health check,            │    one model per channel)
(chosen provider)        fallback, max data age)       ├─> Matrix message
                              │ active aircraft list   │   (LED panels + Virtual Display)
                              v                        ├─> Notifier
FPP status API     ───> Trigger engine ────────────────┤   (FPP banner + log)
(schedule window)       (radius + altitude band,       └─> Status endpoint
                         IDLE / CLEAR / BLACKOUT)          (api.php, later map view)
```

The feed manager hands one active aircraft list to the trigger engine each poll; the engine combines it with FPP's schedule state and drives the overlays, matrix, notifier and status file.

### File layout

```
fpp-plugin-SkyGuard/
  pluginInfo.json          # metadata + privacy block
  apiDocs.json             # documents status + geocode routes
  LICENSE                  # GPLv2
  README.md
  icon.png
  menu.inc                 # max one entry per type (UI phase)
  api.php                  # status + geocode endpoints
  callbacks                # minimal, enables live command registration
  commands/
    descriptions.json      # Enable, Disable, Test Trigger
    skyguard_cmd.py        # sends command to daemon
  scripts/
    fpp_install.sh
    fpp_uninstall.sh
    postStart.sh           # launches daemon (+ supervisor)
    preStop.sh             # releases overlays, stops daemon
  skyguard/
    daemon.py              # main loop
    config.py              # settings load/reload
    feeds/
      base.py              # adapter interface + normalized record
      receiver.py          # readsb/dump1090/tar1090/dump978 JSON
      online_adsbx_v2.py   # adsb.lol, adsb.fi, airplanes.live
    feed_manager.py        # polling, health, fallback, data age
    geo.py                 # haversine, unit conversion, geocode
    trigger.py             # zone evaluation + state machine
    fpp_api.py             # all calls to http://localhost/api
    overlay.py             # fixture -> overlay model control
    matrix.py              # text overlay messages
    notifier.py            # events + sinks
    status.py              # atomic status JSON writer
  tests/                   # unit tests + mock feed server
```

UI pages (`content.php`, `status.php`, `about.php`) stay as template stubs until phase 1b (User interface).

## Data sources, fallback and freshness

The user picks one of three source modes: **Receiver only**, **Online only**, or **Both (receiver primary, online fallback)**. Feeds are never merged; exactly one feed is active at a time.

### Feed adapters

Each source is an adapter returning a normalized aircraft list, so new receivers or providers are one new module.

| Adapter | Input | Notes |
| --- | --- | --- |
| Local receiver | `aircraft.json` from a user-entered URL on a separate Pi | Must accept readsb, dump1090-fa, tar1090 and dump978-fa formats (receiver software is undecided). Optional second URL for a separate 978 MHz decoder; aircraft lists combined by ICAO hex |
| Online, ADSBx-v2-compatible | Point/radius query to the chosen provider | Provider picked from a list (initially adsb.lol, adsb.fi, airplanes.live — verify current endpoints and terms) |
| Online, keyed provider | Same, with a user API key | Optional; key stored in plugin config, never logged |

Normalized aircraft record: `hex`, `callsign`, `lat`, `lon` (nullable), `alt_ft` (barometric, or `ground`), `gs_kt`, `track_deg`, `seen_pos_s`, `source`.

### Polling

- Poll the active feed every 1–2 s (user setting, default 1 s).
- Each online provider has its own minimum interval; the plugin enforces the larger of the user setting and the provider minimum.
- HTTP timeout per request: 3 s.

### Receiver health and fallback (Both mode)

1. Health-check the receiver every 10 s (an HTTP GET of its `aircraft.json`, checking that the file's `now` timestamp advances).
2. Two consecutive failed checks (about 20 s) → switch the active feed to online, log it, raise the fallback warning.
3. While on online, keep health-checking the receiver every 10 s. When it passes, switch back automatically and clear the warning.

### Data freshness (fail-safe)

Separately from health checks, track the time since the last successful poll of the active feed.

- **Max data age** setting, default 5 s. If no successful poll within that time, force a blackout immediately (reason: `no_data`).
- Setting 0 = blackout on the first failed poll.
- Blackout holds until a feed (either one) delivers a successful poll again.
- If both feeds are down, the heads stay blacked out and the both-down warning shows until a feed returns.

No aircraft records are discarded for age or ground status; see the trigger engine.

## Trigger engine

The zone is one circle: a center lat/lon plus a radius, filtered by an altitude floor and ceiling. Any qualifying aircraft inside it puts the plugin in BLACKOUT; the zone must be empty for the release delay before channels are released.

### Center point

- Stored as decimal degrees (`center_lat`, `center_lon`).
- Set by manual entry, by address geocoding (backend calls a geocoding service on request only, result shown for the user to confirm), or by the browser's location (UI side; may need HTTPS, fall back to manual).

### Per-poll evaluation

For each aircraft in the active feed's list:

1. **Distance:** great-circle (haversine) distance from center to `lat/lon`. In zone if distance ≤ `radius`.
2. **Altitude:** in zone if `alt_floor_ft` ≤ altitude ≤ `alt_ceiling_ft`. An aircraft reporting `ground` counts as 0 ft. Default floor 0 so ground traffic counts.
3. **No position** (altitude but no lat/lon): governed by the `count_no_position` toggle. ON = treat as in zone if its altitude passes the altitude filter; OFF = skip it but log it. Default ON (fail-safe).
4. Stale positions are not discarded; the last known position is used.

`aircraft_in_zone` = count of aircraft passing all checks.

### States

| State | Entered when | Heads |
| --- | --- | --- |
| IDLE | Outside the show schedule, or plugin disabled | Released; no overlays held |
| CLEAR | In schedule, data fresh, `aircraft_in_zone` = 0 for ≥ release delay | Released to the sequence |
| BLACKOUT | In schedule and (`aircraft_in_zone` ≥ 1, or data older than max data age) | Forced to off values |

- Entering BLACKOUT happens on the same poll the condition is seen; no delay.
- Leaving BLACKOUT requires the zone empty and data fresh continuously for `release_delay_s` (user setting, default 5 s; 0 = release immediately).
- Every transition is logged with reason (`aircraft`, `no_data`, `test`, `schedule`) and the aircraft that caused it.

Units: radius in miles or nautical miles or km (user choice, default statute miles), altitude in feet.

## Fixture model and blackout output

Each moving head is entered individually; the plugin overrides only that head's dimmer and shutter channels and nothing else, so other DMX props are never touched. It must work with any DMX moving head.

### Fixture record (stored in `plugindata/fpp-plugin-SkyGuard/fixtures.json`)

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | User label, e.g. "Roof head left" |
| `enabled` | bool | Include this head in blackouts |
| `universe` | int | Universe the head is on |
| `dimmer_ch` | int 1–512, optional | DMX channel of the dimmer within that universe (absolute address, not offset) |
| `dimmer_off` | int 0–255, default 0 | Value forced during blackout |
| `shutter_ch` | int 1–512, optional | DMX channel of the shutter within that universe |
| `shutter_off` | int 0–255, default 0 | Value forced during blackout |

At least one of `dimmer_ch` or `shutter_ch` is required. A head on a 16-bit dimmer can list both bytes later; phase 1 supports one channel each.

### Universe to FPP channel

The plugin converts (universe, DMX channel) to an absolute FPP channel by reading FPP's channel output configuration through the API (universe start channels), never from files. Re-resolve on startup and when FPP's outputs change. If a universe isn't found, mark that fixture invalid, log it, and raise a warning.

`fpp_channel = universe_start_channel + dmx_channel − 1`

### How the override is applied

Preferred approach: one FPP Pixel Overlay Model per fixture channel (or per fixture, if dimmer and shutter are contiguous), created and owned by the plugin, named `SkyGuard-<fixture>-<dimmer|shutter>`, one channel per node.

- **Blackout:** set the model to an opaque/active state and fill it with the off value, so it overrides the running sequence for that channel only.
- **Release:** set the model state to disabled so the sequence's values pass through.
- **Cleanup:** remove the plugin's models on uninstall (via the API), and release them on fppd stop.

The exact API calls for creating overlay models and setting state/fill on 9.5 must be verified first (see Items to verify). If overlay models can't be created via the API on 9.5, stop and report options rather than editing FPP's model file.

## Schedule awareness

SkyGuard protects the whole scheduled show window, including gaps between songs, and sits IDLE outside it.

- "In schedule" = FPP's scheduler reports a scheduled playlist active now, or a playlist/sequence is playing (manual starts count too, so a show started by hand is protected).
- Read this from the FPP status API every poll cycle (or at least every 5 s); verify the exact status fields on 9.5.
- Entering the window: start evaluating immediately; if an aircraft is already in the zone, black out on the first poll.
- Leaving the window: release all overlays and go IDLE. Keep polling the feed (cheaply) so status and the future map view stay current.
- Feed polling and health checks continue while IDLE so warnings are accurate before the show starts.

## Matrix message

When a blackout starts, SkyGuard can optionally show a message on one or more LED matrices; the message clears when the zone clears. Because it is drawn on FPP Pixel Overlay Models, the same text also appears on FPP's Virtual Display.

- **Targets:** a list of existing Pixel Overlay Model names (the user's LED panels), chosen by the user. The plugin never creates or edits these models.
- **Mechanism:** FPP's text overlay effect on the model (the "Overlay Model Effect" command with a Text effect, or the equivalent API call — verify on 9.5). On clear, stop the effect and return the model to its prior state (disabled unless the user had it enabled).
- **Message:** a custom string with optional placeholders, filled from the closest in-zone aircraft:

| Placeholder | Value |
| --- | --- |
| `{callsign}` | Flight/callsign, or hex if none |
| `{altitude}` | Altitude in ft, or "ground" |
| `{speed}` | Ground speed in kt |
| `{distance}` | Distance from center, in the user's unit, 1 decimal |
| `{heading}` | Track in degrees |
| `{count}` | Number of aircraft in zone |

- **Updates:** refresh the text when its values change, no more than once every 2 s to avoid flicker.
- **Options:** text color, font, size, scroll speed/position are plain settings with defaults; their UI is deferred.
- **No-data blackouts:** use a separate message (default "Aircraft data offline – lights paused"), optional.
- If no matrix is configured, skip silently.

## Warnings and notification events

Feed problems surface in FPP's warning banner at the top of every FPP page; all events go through one internal notification layer so text/push alerts can be added in phase 2 without touching the trigger engine.

### Events

| Event | Fired when | Phase 1 action |
| --- | --- | --- |
| `receiver_lost` | 2 failed receiver health checks | Banner warning + log |
| `fallback_active` | Switched to online feed | Banner warning + log |
| `receiver_restored` | Receiver passes a health check after loss | Clear warnings + log |
| `all_feeds_down` | No feed delivering data | Banner warning + log |
| `feeds_restored` | A feed delivers data after all were down | Clear warning + log |
| `blackout_started` | CLEAR → BLACKOUT | Log (plus matrix message) |
| `blackout_cleared` | BLACKOUT → CLEAR | Log |
| `fixture_invalid` | A fixture's universe can't be resolved | Banner warning + log |

### Notifier interface

- `Notifier.emit(event, details)` dispatches to registered sinks.
- Phase 1 sinks: `LogSink`, `FPPWarningSink`.
- Phase 2 adds `SmsSink` / push sinks with user-chosen events; nothing else changes.
- Rate-limit repeated identical events (e.g. one `all_feeds_down` per outage, not per poll).

How a Python plugin raises and clears FPP banner warnings on 9.5 must be verified. If no supported API exists, show the warning in the plugin's status data and log it, and report back.

## FPP commands and status API

SkyGuard registers three FPP commands and serves one read-only status endpoint that the later UI and map view will use.

### Commands (`commands/descriptions.json`)

| Command | Args | Effect |
| --- | --- | --- |
| SkyGuard Enable | none | Plugin evaluates normally (persisted setting) |
| SkyGuard Disable | none | Releases all overlays, goes IDLE, raises a "SkyGuard disabled" warning so it's never off silently |
| SkyGuard Test Trigger | seconds (default 10) | Forces BLACKOUT for N seconds with reason `test`, including matrix message, then normal evaluation resumes |

Command scripts pass the request to the running daemon over a local-only channel inside the plugin's own directory (e.g. a Unix socket). They must return quickly.

A `callbacks` script is required so commands live-register on install without an fppd restart (guidelines §2.8).

### Status endpoint

Served through the template's `api.php` at `/api/plugin/fpp-plugin-SkyGuard/status`, reading a status JSON the daemon rewrites every poll (atomic write in `plugindata/`).

Fields: `state`, `reason`, `enabled`, `in_schedule`, `active_feed`, `receiver_ok`, `last_data_age_s`, `aircraft_in_zone`, `center`, `radius`, `aircraft[]` (normalized records with `distance` and `in_zone`), `fixtures[]` (name, resolved channels, valid), `warnings[]`, `updated_at`.

The `aircraft[]` list covers everything the feed returns within a display range (default 2× radius), so the phase-2 map has context. Document the route in `apiDocs.json`.

### Geocode endpoint

`/api/plugin/fpp-plugin-SkyGuard/geocode?q=<address>` returns candidate lat/lon results. Called only when the user presses the lookup button; never automatically.

## Settings reference

Every setting below lives in `config/plugin.fpp-plugin-SkyGuard` except the fixture list (`fixtures.json`). The daemon reloads settings on change without an fppd restart.

| Key | Default | Notes |
| --- | --- | --- |
| `enabled` | true | Set by Enable/Disable commands too |
| `source_mode` | both | `receiver`, `online`, `both` |
| `receiver_url` | empty | e.g. `http://<receiver-ip>/data/aircraft.json` |
| `receiver_978_url` | empty | Optional second decoder |
| `online_provider` | adsb.lol | From the provider list |
| `online_api_key` | empty | Keyed providers only; never logged |
| `poll_interval_s` | 1 | 1–2; floored at provider minimum for online |
| `health_interval_s` | 10 | Receiver health check |
| `health_fail_count` | 2 | Misses before fallback |
| `max_data_age_s` | 5 | 0 = blackout on first failed poll |
| `center_lat` / `center_lon` | empty | Plugin stays IDLE with a warning until set |
| `radius` / `radius_unit` | 10 / mi | `mi`, `nm`, `km` |
| `alt_floor_ft` / `alt_ceiling_ft` | 0 / 10000 | Inclusive |
| `count_no_position` | true | Aircraft with altitude but no position count as in zone |
| `release_delay_s` | 5 | 0 = immediate release |
| `matrix_enabled` | false | |
| `matrix_models` | empty | List of overlay model names |
| `matrix_message` | "Aircraft overhead: {callsign} at {altitude} ft" | Custom text with placeholders |
| `matrix_nodata_message` | "Aircraft data offline – lights paused" | Empty = none |
| `matrix_color` / `matrix_font` / `matrix_size` / `matrix_speed` | sensible defaults | Verify available fonts via API |
| `geocoder` | Nominatim (OpenStreetMap) | Verify usage policy; send a proper User-Agent |

## pluginInfo.json and privacy

A `privacy` block is required for listing; generate it with the [Privacy disclosure builder](https://falconchristmas.github.io/fpp-data/plugin_privacy_builder/) and check the result in the [Plugin preview](https://falconchristmas.github.io/fpp-data/plugin_preview/). Follow `PLUGININFO_FORMAT.md` for every field.

### Fields to set

- `repoName`: `fpp-plugin-SkyGuard`; `name`: SkyGuard.
- `description`: one or two sentences saying it blacks out moving-head light output when aircraft are within a set radius, using a local ADS-B receiver or online flight data.
- `versions[]`: minimum FPP 9.5; platforms Raspberry Pi and BeagleBone.
- `dependencies`: declare any pip/apt packages (FPP 10+ only; also install them in `fpp_install.sh` for 9.5).
- Category: pick from fpp-data's `pluginCategories.json`.
- `bugURL`, `homeURL`, license (GPLv2), icon.

### What the privacy block must disclose

| Data / connection | Detail |
| --- | --- |
| Stored location | Home latitude/longitude stored locally in plugin config |
| Online flight feed | Center point and radius sent to the chosen provider on every poll when online mode is active |
| Geocoding | Typed address sent to the geocoding service, only when the user presses lookup |
| Map tiles | The viewer's browser loads map tiles from the tile provider (e.g. OpenStreetMap) while the status or location map is open |
| Local receiver | Requests to a user-entered address on the LAN |
| API key | Optional third-party key stored locally |
| No telemetry | Nothing sent anywhere else; no usage data collected |

Re-run the builder whenever a phase adds a connection (map tiles, SMS).

## Lifecycle scripts

The daemon starts with fppd and stops with it; every hook returns in well under a second and both install and uninstall are safe to run twice.

| Script | Does |
| --- | --- |
| `scripts/fpp_install.sh` | Source `${FPPDIR}/scripts/common`; install deps with `apt-get`/`pip --break-system-packages` if missing; create `plugindata/fpp-plugin-SkyGuard/`; write default settings only where unset. Idempotent |
| `scripts/postStart.sh` | If not already running (PID file check), launch the daemon in the background with nohup, output appended to `$PLUGIN_LOG`, and return |
| `scripts/preStop.sh` | Tell the daemon to release all overlays and exit; wait at most ~2 s, then kill by PID |
| `scripts/fpp_uninstall.sh` | Stop the daemon; delete the plugin's overlay models via the API; remove `plugindata/fpp-plugin-SkyGuard/`. Every step tolerates "already gone" |
| `callbacks` | Minimal script so commands live-register on install (guidelines §2.8) |

- No systemd unit, cron or files outside the plugin dir, `plugindata/`, the plugin config and the log.
- Daemon crash safety: overlay state lives in fppd, so if the daemon dies the heads keep their last state; a crash during CLEAR leaves them unprotected. To keep the fail-safe, the daemon should restart itself via a lightweight supervisor loop in `postStart.sh`'s background process (with backoff), and log each restart. Flag this design choice in the review.

## User interface

The UI is built after the backend passes its tests (phase 1b). It is phone-first on every page, uses only FPP's own UI helpers, and adds nothing to FPP's own pages: everything lives in SkyGuard's three menu entries.

### General rules

- Use FPP's `PrintSetting*` helpers, Bootstrap 5.3 utility classes and `$.jGrowl` toasts.
- Must read correctly in light and dark themes: no hard-coded colors, only Bootstrap semantic classes and CSS variables.
- Phone-first: works from a 320 px phone to desktop with no horizontal page scroll; finger-sized tap targets, no hover-only actions.
- Settings save FPP-style, each field on change.
- Units: imperial by default (mi, ft, kt), metric selectable (`units` setting).
- Bundle any JavaScript library (e.g. a map library) inside the plugin rather than loading it from a CDN at runtime.

### Menu entries (`menu.inc`, one per type)

| Type | Title | Page |
| --- | --- | --- |
| content | SkyGuard Settings | `content.php` |
| status | SkyGuard | `status.php` |
| help | SkyGuard | `about.php` (help + about combined) |

### First-run setup

- Shown at the top of the Settings page until every step is completed or skipped.
- Steps in order: 1 Data feed, 2 Location & zone, 3 Fixtures, 4 Matrix message, 5 Test run (fires SkyGuard Test Trigger). Each step has a Skip option.
- Progress stored in a `setup_state` setting. No "run setup again" link afterwards; skipped items are set from the tabs.

### Settings page tabs

| Tab | Contents |
| --- | --- |
| Data Source | Source mode; receiver URL and optional 978 URL with a Test button (shows aircraft seen or the error); online provider dropdown, masked API key field, Test button |
| Location & Zone | Lat/lon fields; address field + Look up button (shows candidates to pick from); Use my location button (disabled with a note when the page isn't HTTPS); display-only map of the center point and radius circle (no draggable pin); radius + unit; altitude floor and ceiling |
| Fixtures | Table, one row per head: name, universe, dimmer ch, dimmer off, shutter ch, shutter off, enabled. Add, duplicate, delete. Per-row Test button blacks out that head only for 3 s |
| Matrix Message | Enable switch; panel picker from the FPP overlay model list; message field with click-to-insert placeholder buttons; live preview with sample values; no-data message; color, font, size, speed |
| Advanced (collapsed) | Poll interval, health interval, fail count, max data age, release delay, count-no-position toggle, units |

**Fixture row validation:** a row takes effect only when its required fields are filled and valid. Until then it shows an "Incomplete" badge and the trigger engine ignores it. A row whose universe can't be resolved shows "Universe not found".

### Status page (Status → SkyGuard)

Top to bottom on a phone:

1. **State badge:** CLEAR, BLACKOUT or IDLE with its reason, in Bootstrap semantic colors.
2. **Controls:** Enable/Disable switch (confirmation before disabling) and Test Trigger button.
3. **Map:** center point, radius circle, aircraft icons rotated to heading and labeled with callsign, altitude and speed; in-zone aircraft highlighted. Refreshes every 5 s from the status endpoint.
4. **No-internet fallback:** if map tiles don't load, a theme-aware radar plot (range rings, radius circle, aircraft dots) drawn from the same data.
5. **Aircraft list button:** opens a full-screen list (callsign, altitude, speed, distance, in zone) with a back button; same 5 s refresh.
6. **Health:** active feed, receiver health, data age, aircraft count, per-fixture status, current warnings.
7. **Recent events:** last 20 blackout transitions with time, reason and aircraft.

### Help page

Setup walkthrough, how the blackout works (which channels, fail-safe behavior), troubleshooting (feed down, universe not found, matrix not showing), and where to find the plugin log. No ads or donation links.

### Backend additions for the UI

- SkyGuard Test Trigger gains an optional `fixture` argument (blacks out one head for 3 s).
- Status JSON gains `events[]` (last 20 transitions).
- `api.php` gains fixture endpoints: list, add, update and delete one row, so rows save on change.
- New settings: `units`, `setup_state`.

## Items Claude Code must verify before building

These assumptions were not confirmed during planning. Verify each against FPP 9.5 source/API docs (and current FPP master) first, and stop to report if one fails rather than working around a guideline.

1. **Overlay models via API:** can a plugin create, update and delete a Pixel Overlay Model over an arbitrary channel range (1 channel per node) through the HTTP API on 9.5? Exact endpoints and payloads for set state (opaque vs. transparent vs. disabled) and fill with a value.
2. **Override behavior:** does an opaque overlay fill truly override sequence data for only those channels while the rest of the universe follows the sequence? Test on hardware with a running sequence.
3. **Universe lookup:** which API returns universe-to-start-channel mapping for the user's DMX path (E1.31/Art-Net universes vs. a controller or cape with a native DMX port)? Handle both if they differ.
4. **Schedule status:** which fields of the FPP status API indicate "inside a scheduled window" vs. "playing," on 9.5 and current releases.
5. **Banner warnings:** a supported way for a Python plugin to raise and clear FPP banner warnings.
6. **Text overlay:** the command/API for the Text overlay effect on a model, its arguments (font, size, color, scroll), and how to stop it.
7. **Online providers:** current endpoints, response format, rate limits and terms for adsb.lol, adsb.fi, airplanes.live; set per-provider minimum poll intervals from them.
8. **Receiver formats:** field names in `aircraft.json` for readsb, dump1090-fa, tar1090 and dump978-fa (`lat/lon`, `alt_baro` incl. "ground", `gs`, `track`, `flight`, `seen_pos`, `now`).
9. **Geocoder:** Nominatim usage policy (rate limit, User-Agent), or a better free option.
10. **Commands without a native plugin:** confirm the `commands/descriptions.json` + script format for a script plugin on 9.5 and that the `callbacks` script enables live registration.
11. **Map:** a map library that can be bundled in the plugin under a GPL-compatible license, and the tile provider's usage policy (attribution, rate limits).
12. **Plugin API saves:** how `api.php` endpoints in a plugin accept POST/PUT on 9.5, for the fixture endpoints.

## Acceptance tests

Phase 1 is done when every test below passes on a Pi 4 running FPP 9.5 and on current FPP release and nightly. Use a mock feed server (replays recorded `aircraft.json` files) so tests are repeatable without real traffic.

- [ ] Aircraft enters radius within altitude band → dimmer/shutter forced to off values on the next poll; pan/tilt/color keep following the sequence
- [ ] Aircraft above ceiling or below floor → no blackout
- [ ] Aircraft reporting "ground" inside radius with floor 0 → blackout
- [ ] Aircraft with altitude but no position: blackout when `count_no_position` on; logged only when off
- [ ] Zone clears → channels released after `release_delay_s`; 0 releases immediately
- [ ] Two fixtures in different universes, one in the same universe as a non-head prop → only the listed channels change
- [ ] Receiver stops → after 2 failed 10 s checks, online feed takes over and banner warning shows; receiver returns → switches back and warning clears
- [ ] Feed silent longer than `max_data_age_s` → blackout with reason `no_data`; recovers when data returns
- [ ] Both feeds down → blackout held, both-down warning shown
- [ ] Outside schedule → IDLE, nothing overridden; schedule starts with an aircraft present → blackout on first poll
- [ ] Matrix message appears with placeholders filled, updates, clears on release; shows on Virtual Display
- [ ] Enable, Disable and Test Trigger commands work from a playlist and from the Command API
- [ ] Status endpoint returns all documented fields
- [ ] fppd restart mid-blackout → daemon restarts and re-evaluates; no stuck overlays after uninstall
- [ ] Install and uninstall each run twice cleanly; only one log file exists
- [ ] fpp-data plugin check: no Blocker or Best-practice findings

### UI acceptance (phase 1b)

- [ ] Every page reads correctly in light and dark themes and at 320 px width with no horizontal scroll
- [ ] First-run setup appears on a fresh install; each step can be skipped; it disappears once all steps are done or skipped
- [ ] A half-filled fixture row shows "Incomplete" and is ignored by the trigger engine
- [ ] Per-row Test blacks out only that head for 3 s
- [ ] Status map refreshes every 5 s; with internet blocked, the radar plot appears instead
- [ ] Aircraft list opens full screen and returns with the back button
- [ ] Disabling from the status page asks for confirmation

## Deferred features

These are out of scope for phase 1; the backend above leaves hooks for each.

| Feature | Hook in phase 1 | Open question |
| --- | --- | --- |
| Text / push alerts | Notifier events and sink interface | Provider: Twilio, email-to-SMS, ntfy or Pushover; which events |
| MQTT publishing | Notifier events | Topics and payloads |
| 16-bit dimmers, multiple shutter channels | Fixture record is extensible | Needed? |

## Sources

- [fpp-plugin-Template](https://github.com/FalconChristmas/fpp-plugin-Template)
- [PLUGIN_GUIDELINES.md](https://github.com/FalconChristmas/fpp-plugin-Template/blob/master/PLUGIN_GUIDELINES.md)
- [fpp-data](https://github.com/FalconChristmas/fpp-data)
- [PLUGINS.md](https://github.com/FalconChristmas/fpp-data/blob/master/PLUGINS.md)

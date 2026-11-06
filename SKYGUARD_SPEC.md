# SkyGuard — FPP Plugin Build Spec

Nov 5, 2026

## Purpose and scope

SkyGuard blacks out the light output of moving heads whenever any aircraft is inside a user-defined radius, so beams are never pointed at aircraft. Pan, tilt, color, gobo and every other channel keep following the sequence; only each head's dimmer and shutter channels are forced to a user-set off value. When the zone clears, those channels are released back to the sequence.

This document is the build spec for Claude Code. It covers phase 1 backend behavior. The user interface is specified in its own section and built after the backend.

**Revision, Nov 5, 2026:** updated with the verification results and the owner's decisions. See Verification results for what changed and why, and for what still has to be tested on hardware.

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
| Private data | The API key and home coordinates live in `plugindata/fpp-plugin-SkyGuard/private.json`, mode 0600, never in `config/` (guidelines §14.11) |
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
Online feed        ─┘   (10 s health check,            │    one channel range each)
(chosen provider)        fallback, max data age)       ├─> Matrix message
                              │ active aircraft list   │   (LED panels + Virtual Display)
                              v                        ├─> Notifier
FPP status API     ───> Trigger engine ────────────────┤   (warnings + log)
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
  callbacks                # enables live command registration; lifecycle hook starts the daemon
  .gitattributes           # keeps scripts LF on any machine
  commands/
    descriptions.json      # Enable, Disable, Test Trigger, Test Message, Stop Test Message
    skyguard_enable.py     # one executable per command (FPP passes no fixed argument)
    skyguard_disable.py
    skyguard_test.py
  scripts/
    fpp_install.sh
    fpp_uninstall.sh
    skyguard_start.sh      # shared by postStart.sh and callbacks
    postStart.sh           # launches daemon (+ supervisor)
    preStop.sh             # releases overlays, stops daemon
  skyguard/
    daemon.py              # main loop
    supervisor.py          # restarts the daemon with capped backoff
    commands.py            # command spool between FPP commands and the daemon
    release.py             # releases everything the ledger holds, for uninstall
    config.py              # settings load/reload
    feeds/
      base.py              # normalized record + shared parsing
      http_client.py       # the only network code for feeds
      receiver.py          # readsb/dump1090/tar1090/dump978 JSON
      online_adsbx_v2.py   # adsb.lol, adsb.fi
    feed_manager.py        # polling, health, fallback, data age
    geo.py                 # haversine, unit conversion, geocode
    trigger.py             # zone evaluation + state machine
    fpp_api.py             # all calls to http://localhost/api
    overlay.py             # fixture -> channel range control + active-range ledger
    matrix.py              # text overlay messages
    notifier.py            # events + sinks
    status.py              # atomic status JSON writer
  skyguard_assets.inc      # writes the versioned tags for assets/
  assets/
    skyguard-settings.js   # settings page
    skyguard-status.js     # status page
    skyguard-map.js        # the map and radar plot both pages use
    skyguard.css
    leaflet/               # Leaflet 1.9.4, unchanged, with its license
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
| Online, ADSBx-v2-compatible | Point/radius query to the chosen provider | Provider picked from a list; see Online providers. The radius is in nautical miles and the aircraft list is under `ac`, with `now` in milliseconds |
| Online, keyed provider | Same, with a user API key | Optional; key stored in plugin config, never logged |

Normalized aircraft record: `hex`, `callsign`, `lat`, `lon` (nullable), `alt_ft` (barometric, or `ground`, or null when not reported), `gs_kt`, `track_deg`, `seen_pos_s`, `source`.

Receiver format notes: readsb, dump1090-fa and dump978-fa share one schema (`now` in seconds, `aircraft[]`, `alt_baro` a number or `"ground"`, `~` prefix on non-ICAO hex). readsb drops `lat`/`lon` once they are over 60 s old and may provide `lastPosition` instead, which is used as the last known position. `rr_lat`/`rr_lon` are rough receiver-based estimates and are not used as positions. Entries with no `hex` cannot be tracked; they are skipped and counted in the log.

### Online providers

| Provider | Endpoint | Minimum interval | Terms and status |
| --- | --- | --- | --- |
| adsb.fi (default) | `https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}` | 2 s | Publishes a limit of 1 request/s; 2 s was tested without a refusal. Personal, non-commercial use only; the UI must credit and link adsb.fi. Repeated 4xx responses can restrict the IP |
| adsb.lol | `https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}` | 15 s | Limits are dynamic and an API key is planned. Tested: refused polling at 1 s after about 19 s, and at 5 s after 8 requests; accepted 10 s for 60 s. Its Retry-After said 3 s |

airplanes.live was removed from the list on Nov 5, 2026. Its API returned 403 from the dev machine with a message asking developers to contact the operator at contact@airplanes.live. Don't use the internal data URLs behind its `globe.airplanes.live` map instead. It can return as one row here if the operator approves access for end users' devices.

The query radius is capped at 250 nm. Any 4xx or 429 response counts as a failed poll and is never retried faster than the normal interval. While a provider keeps refusing, the online interval doubles per refusal up to 30 s and returns to normal on the first success, because repeated 4xx responses can get an address restricted. An immediate poll at show start still happens during this backoff, unless the provider sent `Retry-After`, which is always honored (capped at 300 s).

### Personal use and network lag

- **Confirmation:** SkyGuard as a whole is for personal use only, on every setup including receiver-only. `personal_use_confirmed` starts false. Until it is true, the plugin stays IDLE with reason `not_confirmed`, holds no channels, makes no online requests, doesn't run Test Trigger, and `personal_use_required` shows a warning. Receiver polling continues so status stays useful, and setting it takes effect without a restart. The confirmation is the first step of first-run setup.
- **Lag:** whenever `online` or `both` mode is selected, `notices[]` in the status, the Location & Zone tab and a log line say: "Online flight data can lag behind real aircraft. Increase your radius by 3 miles to allow for it." It is a notice rather than a warning because it would otherwise light FPP's header icon permanently and teach people to ignore it.

### Polling

- **Receiver:** always poll every 1–2 s (user setting, default 1 s), in or out of the show window, and also while the plugin is disabled.
- **Online, inside the show window:** poll at the same setting, but never faster than the provider's minimum interval (see Online providers), which in practice sets the rate.
- **Online, outside the show window:** poll every `online_idle_interval_s` (default 30 s) so free services aren't hit once a second all year.
- **Online, plugin disabled, center or online radius not set, or personal use not confirmed:** no online requests at all.
- **Show window starts:** poll the active feed immediately, without waiting for the next interval.
- FPP's status is read every 1 s regardless of the feed interval, so the start of a show is noticed quickly even at the 30 s online interval.
- HTTP timeout per request: 3 s. Polling, health checks and the 978 decoder run on separate threads, so a dead host that takes the whole timeout to fail doesn't delay the others.
- Requests send the User-Agent `fpp-plugin-SkyGuard`, ignore proxy settings, follow redirects only to http(s), and refuse responses over 16 MB. Errors and logs never contain a URL, because a URL can hold the home coordinates or a password.

### Receiver health and fallback (Both mode)

1. Health-check the receiver every 10 s (an HTTP GET of its `aircraft.json`, checking that the file's `now` timestamp advances).
2. Two consecutive failed checks (about 20 s) → switch the active feed to online, log it, raise the fallback warning.
3. While on online, keep health-checking the receiver every 10 s. When it passes, switch back automatically and clear the warning.

No fallback happens while online requests aren't allowed (plugin disabled or center not set): the active feed stays the receiver and `receiver_lost` still shows. If online becomes allowed with the receiver still down, it falls back at once.

### Online check on a working receiver (Both mode)

The receiver has priority: it stays the active feed, and its radius judges every aircraft it has located, wherever online data puts that aircraft. Decided by the owner on Nov 6, 2026.

- **During a show only**, with online requests allowed, the online feed is polled alongside the receiver, at the normal online rate and never faster than the provider's minimum. Outside a show nothing is sent while the receiver is healthy, because heads only black out during a show. Refusals and `Retry-After` are honored and follow the online feed through a fallback, and never hold up the receiver.
- **An aircraft the receiver misses** is one in the online data, matched by hex, that the receiver doesn't report or reports with no position. It's judged by the online radius and the same altitude and ground rules, and can cause a blackout. Status marks it `online_only`.
- **If the check fails**, it never causes a blackout, because the receiver's data is still good. After three intervals with nothing new its data is dropped and `online_check_lost` warns that missed aircraft won't be caught; `online_check_ok` clears it.

### Second decoder (978 MHz)

- The optional `receiver_978_url` is polled at the normal rate only while the receiver is the active feed. Its aircraft are merged into the receiver's list by hex.
- Its failure never counts against the receiver. Data older than the max data age (at least 5 s) is dropped from the list.
- If it fails for `health_interval_s` × `health_fail_count`, `receiver_978_lost` raises a warning, because UAT aircraft are no longer being seen. It clears when the decoder recovers.
- If only `receiver_978_url` is set, it acts as the receiver.

### Data freshness (fail-safe)

Separately from health checks, track the time since the last successful poll of the active feed.

A poll is successful only if the feed's `now` has changed since the previous one. A decoder can die while its web server keeps serving the last `aircraft.json`, and a frozen file must not look like a healthy, possibly empty, sky. An unchanged `now` is not a failure for the first 3 s, since decoders write about once a second; after that it counts as a failed poll. Only change is checked, not the time itself, so a wrong clock on either device doesn't matter.

- **Max data age** setting, default 5 s. If no successful poll within that time, force a blackout immediately (reason: `no_data`).
- On an online feed the limit is the larger of this setting and two poll intervals, because a provider limited to one poll in 15 s could never meet a 5 s limit. That makes it 5 s on adsb.fi and 30 s on adsb.lol, so on adsb.lol an aircraft can be seen up to 15 s late and an outage can take about 30 s to darken the heads.
- Setting 0 = blackout on the first failed poll.
- Blackout holds until a feed (either one) delivers a successful poll again.
- Data age only drives a blackout inside the show window. If a show starts while online data is older than max data age, the heads black out with reason `no_data` until the immediate poll succeeds and the release delay has passed. This fails dark on purpose, and can darken the first seconds of a show on an online feed.
- If both feeds are down, the heads stay blacked out and the both-down warning shows until a feed returns.
- With the default settings, a receiver failure in Both mode reaches max data age at about 5 s but doesn't fall back until about 20 s. The heads are therefore dark for roughly 15 s or more, plus the release delay, before the online feed can release them. This is the intended fail-dark behavior.

No aircraft records are discarded for age or ground status; see the trigger engine.

## Trigger engine

The zone is one circle: a center lat/lon plus a radius, filtered by an altitude floor and ceiling. `radius` applies with online data and `radius_receiver` applies while a receiver is the active feed, so in `both` mode the radius changes automatically on fallback. The user must enter both: neither has a default, and neither borrows the other. Until a radius is entered for every data source in use, SkyGuard stays IDLE with reason `no_radius` and a warning, and makes no online requests. Any qualifying aircraft inside it puts the plugin in BLACKOUT; the zone must be empty for the release delay before channels are released.

### Center point

- Stored as decimal degrees (`center_lat`, `center_lon`) in `plugindata/fpp-plugin-SkyGuard/private.json`.
- Set by manual entry, by address geocoding (backend calls a geocoding service on request only, result shown for the user to confirm), or by the browser's location (UI side; may need HTTPS, fall back to manual).

### Per-poll evaluation

For each aircraft in the active feed's list:

1. **Distance:** great-circle (haversine) distance from center to `lat/lon`. In zone if distance ≤ `radius`.
2. **Altitude:** in zone if `alt_floor_ft` ≤ altitude ≤ `alt_ceiling_ft`. An aircraft reporting `ground` counts as 0 ft. Default floor 0 so ground traffic counts. An aircraft inside the radius that reports no altitude counts as in zone (fail-safe).
3. **No position** (altitude but no lat/lon): governed by the `count_no_position` toggle. ON = treat as in zone if its altitude passes the altitude filter; OFF = skip it but log it. Default ON (fail-safe). The owner will review this default against real receiver traffic during acceptance, because a receiver can hear position-less aircraft far outside the radius.
4. An aircraft with neither position nor altitude tells us nothing and is skipped, whatever the toggle says.
5. Stale positions are not discarded; the last known position is used.

`aircraft_in_zone` = count of aircraft passing all checks.

### Ignoring ground traffic

`ignore_ground` is off by default, so the public default still counts everything. When it is on, an aircraft is skipped only if both of these are true. This is applied before the rules above and also covers aircraft with no position.

1. The feed reports it on the ground (`alt_baro` is `ground`).
2. Its reported ground speed is below `ground_speed_limit_kt`, or it reports no speed at all.

At the limit the aircraft counts as taking off and counts as in the radius on that same poll, even while it still reports `ground`. A numeric altitude plays no part in this rule, and nothing depends on aircraft type. An aircraft that doesn't report `ground` always goes through the normal rules, however slowly it is moving, so a helicopter below the limit is counted unless it reports `ground`. A `ground` report with no speed is omitted because airport surface vehicles report this way and would otherwise hold a blackout for the whole show.

A skipped aircraft is not part of `aircraft_in_zone`, the release delay or the matrix message. It stays in the status data and on the map, marked `ground (ignored)`. It is logged with the `ground_aircraft_ignored` event when it is first skipped inside the zone, not on every poll.

**Limits:** this trusts the feed's `ground` report. An airborne aircraft that wrongly reports `ground` below the limit or with no speed is skipped, and so is an aircraft on its takeoff roll that reports no speed, until it reports a speed at the limit or a numeric altitude.

### States

| State | Entered when | Heads |
| --- | --- | --- |
| IDLE | Outside the show schedule, plugin disabled, personal use not confirmed, center not set, or a required radius not entered | Released; no overlays held |
| CLEAR | In schedule, data fresh, `aircraft_in_zone` = 0 for ≥ release delay | Released to the sequence |
| BLACKOUT | In schedule and (`aircraft_in_zone` ≥ 1, or data older than max data age) | Forced to off values |

- Entering BLACKOUT happens on the same poll the condition is seen; no delay.
- Leaving BLACKOUT requires the zone empty and data fresh continuously for `release_delay_s` (user setting, default 5 s; 0 = release immediately).
- Entering the window with fresh data and an empty zone goes straight to CLEAR. The release delay applies only to leaving BLACKOUT.
- Precedence, highest first: disabled, personal use not confirmed, active test, center not set, radius not entered, outside the window, stale data, aircraft in zone. A test therefore works outside the show window and before the center is set, but not while disabled or before personal use is confirmed.
- When a test ends, normal evaluation resumes at once with no release delay.
- If the daemon restarts and its ledger shows ranges still held (see How the override is applied), it starts in BLACKOUT with reason `resume` and applies the normal rules from there.
- Every transition is logged with reason (`aircraft`, `no_data`, `test`, `schedule`, `disabled`, `not_confirmed`, `no_center`, `no_radius`, `resume`, `clear`) and the aircraft that caused it. A reason change within BLACKOUT is logged too.

Units: radius in miles or nautical miles or km (user choice, default statute miles), altitude in feet.

## Fixture model and blackout output

Each moving head is entered individually; the plugin overrides only that head's dimmer and shutter channels and nothing else, so other DMX props are never touched. It must work with any DMX moving head.

### Fixture record (stored in `plugindata/fpp-plugin-SkyGuard/fixtures.json`)

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | User label, e.g. "Roof head left" |
| `enabled` | bool | Include this head in blackouts |
| `universe` | int, optional | Universe the head is on |
| `start_channel` | int ≥ 1, optional | Absolute FPP channel of DMX channel 1 on this head's output, for outputs with no universe number (USB DMX, DDP, some capes) |
| `dimmer_ch` | int 1–512, optional | DMX channel of the dimmer within that universe (absolute address, not offset) |
| `dimmer_off` | int 0–255, default 0 | Value forced during blackout |
| `shutter_ch` | int 1–512, optional | DMX channel of the shutter within that universe |
| `shutter_off` | int 0–255, default 0 | Value forced during blackout |

Exactly one of `universe` or `start_channel` is required, and at least one of `dimmer_ch` or `shutter_ch`. A row that doesn't meet this is incomplete and ignored. A head on a 16-bit dimmer can list both bytes later; phase 1 supports one channel each.

### Universe to FPP channel

The plugin converts (universe, DMX channel) to an absolute FPP channel by reading FPP's channel output configuration through the API, never from files: `GET /api/channel/output/universeOutputs`, then `channelOutputs[].universes[]` with `id`, `startChannel`, `channelCount`, `universeCount` and `active`. One entry can span several universes, so a universe `u` in an entry starts at `startChannel + (u − id) × channelCount`. Re-resolve on startup and when FPP's outputs change. If a universe isn't found, mark that fixture invalid, log it, and raise a warning. If the same universe appears on more than one active entry, use the first and log it.

`fpp_channel = universe_start_channel + dmx_channel − 1`

USB DMX outputs (`GET /api/channel/output/co-other`) have a start channel but no universe number, so those heads use the fixture's `start_channel`, giving `fpp_channel = start_channel + dmx_channel − 1`. Cape DMX ports have not been traced; use `start_channel` for them until they are.

### How the override is applied

Each head channel is forced with an FPP overlay channel range. Ranges were chosen over overlay models because creating a model means replacing FPP's whole model list and restarting fppd, which would leave a newly added head unprotected until the next restart. Ranges need no restart, touch no FPP configuration, and apply after overlay models on every output frame.

- **Blackout:** `PUT /api/overlays/range/<ch>-<ch>` with body `{"Value":<off value>}`. Channels with the same value share one request (`10-10,11-11`), up to 100 per request.
- **Hold:** repeat that PUT about every 2 s during BLACKOUT. It is idempotent, ranges can't be read back, and this restores them after an fppd restart or another plugin's `deleteAll`.
- **Release:** always PUT the value first, then PUT `{"delete":true}`. **This order is a safety requirement.** On FPP 9.5 through 10.2, deleting a range that doesn't exist adds it with value −1, which writes 0xFF (full on). It is fixed in 10.3. A small risk remains if fppd restarts between the two calls.
- **Never** use `deleteAll`, which would also remove other plugins' ranges.
- **Cleanup:** release on fppd stop and on uninstall, again set then delete.
- **Failures:** a failed set is retried on the next loop and never followed by a delete. A failed delete leaves the channel dark and retries. Either one raises `overlay_failed`, because the heads may not be doing what SkyGuard intends.
- **Channel checks:** a DMX channel past the end of its universe, or an absolute channel past fppd's limit of 8,388,608, makes that channel invalid instead of landing on another prop. FPP 9.5 doesn't check that limit itself.
- **Shared channels:** two heads may share a channel with the same off value. With different values, the first head keeps it and the second is flagged invalid.

Limits: Channel Tester, other plugins' `modifyChannelData` and output processors all run after overlays, so they can still change these channels. SkyGuard must run on the FPP that outputs to the heads; a blackout on a player doesn't affect a remote playing its own copy of the sequence.

### Active-range ledger

The daemon records the channels it holds and their values, plus each matrix model it changed and that model's original state, in `plugindata/fpp-plugin-SkyGuard/active_ranges.json` (atomic write). A channel is added before its range is set and removed only after its delete succeeds, so the ledger can overstate what is held but never understate it. On restart the ledger, not the current fixture list, says what to release, so channels from a fixture edited or removed while the daemon was down are still released.

### If the daemon crashes

Ranges live in fppd's memory, not in the daemon, so they outlast it.

| Situation | What the heads do |
| --- | --- |
| Crash during BLACKOUT | Stay at their off values, with pan, tilt, color and everything else still following the sequence. The heads fail dark |
| Supervisor restarts the daemon | It reads the ledger, resumes in BLACKOUT with reason `resume`, re-sends the ranges, and releases only after the normal rules are met (zone empty and data fresh for `release_delay_s`, or IDLE) |
| Daemon keeps crashing | Heads stay dark for as long as it stays down. The supervisor retries forever with a capped backoff |
| fppd restarts while the daemon is down | Ranges are lost, so heads follow the sequence until the daemon is back. `postStart.sh` starts it if it isn't running |
| Crash during CLEAR | Nothing is held, so the heads are unprotected until the daemon returns. This is the gap the supervisor exists to shorten |
| Supervisor killed, daemon still running | Protection continues, but nothing would restart the daemon after a crash. The daemon notices its parent has changed and raises `supervisor_lost`, a warning that stays until the next fppd start, which replaces both |
| Daemon and supervisor both killed | Held ranges stay and nothing restarts until the next fppd start |
| Ledger corrupt | Resume in BLACKOUT holding the channels of all current fixtures, release them under the normal rules, and warn that a removed fixture's range could stay until fppd restarts |
| Ledger missing | Treated as a first start: nothing has been held. A file deleted by hand could leave a range held until fppd restarts |

While the daemon is down it can't report anything itself, so the `headerIndicator` endpoint in `api.php` checks the age of the status file and shows a danger icon when it is stale.

## Schedule awareness

SkyGuard protects the whole scheduled show window, including gaps between songs, and sits IDLE outside it.

- "In schedule" = FPP's scheduler reports a scheduled playlist active now, or a playlist/sequence is playing (manual starts count too, so a show started by hand is protected).
- Read `GET /api/fppd/status` every 1 s. In schedule when `status` is non-zero, or `bridging` is true (live E1.31 or Art-Net input driving the outputs with no playlist), or `scheduler.status` is `playing` or `manual`, or `status_name` is anything other than `idle`. Master reports `playing background` with `status` still 0, and `status_name` covers it. Treat an unreachable or unparseable status as in schedule (fail-safe).
- **`bridging` never clears by itself.** FPP sets it on the first packet and resets it only when the bridge restarts: seen on a real 10.3 box still true 45 s after the last packet, and cleared by a restart of fppd. So while it's set, SkyGuard also reads `GET /api/fppd/e131stats`, and bridged data counts as a show only while the `packetsReceived` total moves, plus 30 s (`BRIDGE_IDLE_S`). Stats that can't be read count as a show. The same stats exist on 9.5, from its source.
- Bridged data counts as a show, so heads can black out during an xLights test session if an aircraft is nearby. `bridging` comes from FPP's `HasBridgeData()`, on 9.5 and master; seen going true on a real 10.3 box with DDP packets.
- Entering the window: start evaluating immediately and poll the active feed at once; if an aircraft is already in the zone, black out on the first poll.
- Leaving the window: release all overlays and go IDLE. Keep polling the feed (see Polling) so status and the future map view stay current.
- Feed polling and health checks continue while IDLE so warnings are accurate before the show starts.

## Matrix message

When a blackout starts, SkyGuard can optionally show a message on one or more LED matrices; the message clears when the zone clears. Because it is drawn on FPP Pixel Overlay Models, the same text also appears on FPP's Virtual Display.

- **Targets:** a list of existing Pixel Overlay Model names (the user's LED panels), chosen by the user. The plugin never creates or edits these models.
- **Mechanism:** `POST /api/command/Overlay Model Effect` with a JSON array: `[model, "False", "Text", color, font, size, "false", position, speed, 0, text]`. Arguments are positional and identical on 9.5 and master. Fonts come from `GET /api/overlays/fonts`.
- **Showing:** read the model's state from `isActive` on `GET /api/overlays/model/<name>` and record it in the ledger. If it was disabled, set it enabled with `PUT .../state` first. Auto-enable isn't used, because it makes Center text stop after 25 ms.
- **Clearing:** send `[model, "Enabled", "Stop Effects"]` for a model that was disabled, which stops the effect and disables the model, or `[model, "False", "Stop Effects"]` for one the user had on, which keeps its state. No state call follows, because on current FPP that would cancel a pending stop.
- These details come from FPP's source and still need a box test.
- **Message:** a custom string with optional placeholders, filled from the closest in-zone aircraft:

| Placeholder | Value |
| --- | --- |
| `{callsign}` | Flight/callsign, or hex if none |
| `{altitude}` | Altitude in ft, or "ground" |
| `{speed}` | Ground speed in kt |
| `{distance}` | Distance from center, in the user's unit, 1 decimal |
| `{heading}` | Track in degrees |
| `{count}` | Number of aircraft in zone |

- **Updates:** Center text refreshes when its values change, no more than once every 2 s to avoid flicker. A scrolling message appears to make one pass and end, and restarting it mid-pass would mean a changing `{distance}` kept it from ever finishing. It is therefore replaced with the latest text only when `effectRunning` shows the pass has ended, which also keeps it repeating.
- **Missing values** show as `--`. During the release delay, the zone is empty, so the placeholders keep describing the last aircraft.
- **Failures** are logged once per model and never affect the blackout.
- **Options:** text color, font, size, scroll speed/position are plain settings with defaults; their UI is deferred.
- **No-data blackouts:** use a separate message (default "Aircraft data offline – lights paused"), optional.
- If no matrix is configured, skip silently.

## Warnings and notification events

FPP has no way for a plugin to raise a banner warning: warnings are raised only inside fppd, with no HTTP route on 9.5 or master. Feed problems therefore surface through FPP's plugin header indicator, an icon shown on every FPP page, plus the status data and the log. All events go through one internal notification layer so text/push alerts can be added in phase 2 without touching the trigger engine.

### Events

| Event | Fired when | Phase 1 action |
| --- | --- | --- |
| `receiver_lost` | 2 failed receiver health checks | Warning + log |
| `fallback_active` | Switched to online feed | Warning + log |
| `receiver_restored` | Receiver passes a health check after loss | Clear warnings + log |
| `fallback_ended` | Left the online feed with the receiver still down, because online is no longer allowed | Clear the fallback warning + log |
| `receiver_978_lost` / `receiver_978_restored` | The 978 decoder fails for the health window / recovers | Raise / clear warning + log |
| `all_feeds_down` | The last feed left to try is failing and its data is older than max data age. Not raised while online is switched off by design, or while Both mode is still waiting on health checks | Warning + log |
| `feeds_restored` | A feed delivers data after all were down | Clear warning + log |
| `blackout_started` | CLEAR → BLACKOUT | Log (plus matrix message) |
| `blackout_cleared` | BLACKOUT → CLEAR | Log |
| `fixture_invalid` | A fixture's universe can't be resolved | Warning + log |
| `fixture_resolved` | That fixture resolves again or is removed | Clear warning + log |
| `overlay_failed` / `overlay_restored` | FPP refuses or misses a channel set or delete / all succeed again | Raise / clear warning + log |
| `supervisor_lost` | The daemon's supervisor has died | Warning + log. Cleared by the fppd restart it recommends |
| `internal_error` / `internal_ok` | The main loop hits an unexpected error / a step completes normally | Raise / clear warning + log |
| `plugin_disabled` / `plugin_enabled` | Disable / Enable command | Raise / clear "SkyGuard disabled" + log |
| `center_unset` / `center_set` | Center missing / provided | Raise / clear warning + log |
| `radius_unset` / `radius_set` | A required radius is missing / all are entered | Raise / clear warning + log |
| `personal_use_required` / `personal_use_confirmed` | Enabled and not confirmed / confirmed | Raise / clear warning + log |
| `online_check_lost` / `online_check_ok` | The online check on a working receiver has delivered nothing new for three intervals during a show / it delivers again, or isn't needed | Raise / clear warning + log. Never a blackout by itself |
| `ground_aircraft_ignored` | An aircraft in the zone is first skipped as on the ground | Log |

### Notifier interface

- `Notifier.emit(event, details)` dispatches to registered sinks.
- Phase 1 sinks: `LogSink`, `WarningSink` (keeps the active warnings for the status file and header indicator).
- Phase 2 adds `SmsSink` / push sinks with user-chosen events; nothing else changes.
- Rate-limit repeated identical events (e.g. one `all_feeds_down` per outage, not per poll).

### Unexpected errors

The main loop never stops on an unexpected error. It logs the error and raises `internal_error`, because what the heads are doing is then unknown. During a show with the plugin enabled, it holds every resolved fixture dark until a step completes normally, when `internal_ok` clears the warning and the normal rules apply. Outside a show or while disabled, it holds nothing.

The channel ranges are applied before anything slower in each step, and the matrix runs on its own thread, so a slow panel call can't delay a blackout.

The header indicator is served from `api.php` at `/api/plugin/fpp-plugin-SkyGuard/headerIndicator`, which FPP calls with a 1 s timeout on its status updates, so it must only read the status file. It returns `visible`, `icon`, `color` (a hex color, because FPP 10.3 ignores anything but a hex color or a color name), `tooltip`, `link` and `animate`. The icon is always shown, and its text starts by saying SkyGuard is ON, OFF or NOT RUNNING (owner's request, Nov 6, 2026). Green: on, nothing wrong. Yellow: off, waiting on setup, or a warning. Red: blacked out or not running. FPP sets the text with `.attr` on 10.x and writes it into an attribute unescaped on 9.5, so the text leaves out `" < > &` and uses a typographic apostrophe instead of escaping. FPP refreshes it on its own schedule, measured at 22–29 s on 10.3. Header indicators exist on 9.5 and master but haven't been tested on a box yet.

## FPP commands and status API

SkyGuard registers five FPP commands and serves one read-only status endpoint that the later UI and map view will use.

### Commands (`commands/descriptions.json`)

| Command | Args | Effect |
| --- | --- | --- |
| SkyGuard Enable | none | Plugin evaluates normally (persisted setting) |
| SkyGuard Disable | none | Releases all overlays, goes IDLE, raises a "SkyGuard disabled" warning so it's never off silently |
| SkyGuard Test Trigger | seconds (default 10), optional fixture ID | Forces BLACKOUT for N seconds with reason `test`, including matrix message, then normal evaluation resumes. With a fixture ID, holds only that head dark for N seconds, without changing the state or showing a message |
| SkyGuard Test Message | none | Shows the message on the chosen panels with sample values until stopped, without touching the heads or the state. Needs personal use confirmed. Works while disabled and while the matrix switch is off, because it only touches the panels and sends nothing out. A real blackout message wins, and the test returns afterwards. While it runs, the status has `message_test: true` and a warning shows, so it can't be forgotten before a show. It isn't saved: a daemon restart or an FPP restart ends it and clears the panel |
| SkyGuard Stop Test Message | none | Ends the message test |

Command scripts pass the request to the daemon through a spool folder, `plugindata/fpp-plugin-SkyGuard/commands/`: each drops a small JSON file and exits, and the daemon reads the folder every 0.25 s. A spool opens no port, so nothing new is reachable on the network. They must return quickly: fppd waits for each one to exit.

- Enable and Disable apply at once in memory and are saved through `PUT /api/plugin/fpp-plugin-SkyGuard/settings/enabled`, retried if FPP doesn't answer. They also take effect if sent while the daemon is down.
- A Test Trigger or Test Message older than 30 s is ignored, so one requested during an outage can't fire later. Its length must be 1–3600 s, otherwise the default of 10 s is used.
- Unreadable command files are logged and removed.

- FPP runs a command script directly with `execve`, so each needs a shebang and an exec bit, committed explicitly (`git update-index --chmod=+x`) because development is on Windows.
- Its environment contains only `MEDIADIR`, `FPPDIR` and `SCRIPTDIR`.
- Each command in `descriptions.json` names its own script and no fixed argument can be passed, hence one small script per command.

A `callbacks` script is required so commands live-register on install without an fppd restart (guidelines §2.8). Live load exists from FPP 10.0. FPP 9.5 has no load route and reads commands only at fppd startup, so on versions below 10.0 `fpp_install.sh` and `fpp_uninstall.sh` must set `restartFlag`.

### Status endpoint

Served through the template's `api.php` at `/api/plugin/fpp-plugin-SkyGuard/status`, reading a status JSON the daemon rewrites (atomic write in `plugindata/`). It writes at once when the state, reason, enabled setting, show window, active feed, in-zone count or warnings change, and otherwise every 5 s. That matches the 5 s map refresh and spares the SD card a write every second all year.

Fields: `state`, `reason`, `enabled`, `in_schedule`, `source_mode`, `cross_check_active`, `active_feed`, `receiver_ok`, `last_data_age_s`, `aircraft_in_zone`, `center`, `radius`, `aircraft[]` (normalized records with `distance` and `in_zone`), `fixtures[]` (name, resolved channels, valid), `warnings[]`, `notices[]`, `message_test`, `events[]`, `held_channels[]`, `updated_at`. `held_channels` lists the channels being forced right now; FPP has no call that reads an overlay range back, so this is how the page and a test can tell. `events` is the last 20 blackout starts and ends, each with `at`, `type` (`started` or `ended`), `reason` and at most 10 `aircraft`, kept across a daemon restart by reading them back from the status file. `radius` carries the `value` in force for the active feed, its `unit`, and the `online` and `receiver` settings.

Each `aircraft[]` record also carries `ground_ignored`, and a `note` of `ground (ignored)` when that is true.

`api.php` adds `running`, false when the status file is missing or over 30 s old. It serves the status file as is and never reads `private.json`.

`center` is needed by the map, so the home coordinates remain readable by anyone who can reach FPP's API, even though they are stored privately. The API key is never returned.

The `aircraft[]` list covers everything the feed returns within a display range (default 2× radius), so the phase-2 map has context. Document the route in `apiDocs.json`.

### Geocode endpoint

`/api/plugin/fpp-plugin-SkyGuard/geocode?q=<address>` returns candidate lat/lon results. Called only when the user presses the lookup button; never automatically.

## Settings reference

Every setting below lives in `config/plugin.fpp-plugin-SkyGuard` except the fixture list (`fixtures.json`) and the private values marked below (`private.json`, mode 0600). The daemon reloads settings on change without an fppd restart. The file format is FPP's `key = "value"`.

| Key | Default | Notes |
| --- | --- | --- |
| `enabled` | true | Set by Enable/Disable commands too |
| `source_mode` | both | `receiver`, `online`, `both` |
| `receiver_url` | empty | e.g. `http://<receiver-ip>/data/aircraft.json` |
| `receiver_978_url` | empty | Optional second decoder |
| `online_provider` | adsb.fi | From the provider list |
| `personal_use_confirmed` | false | The plugin stays IDLE until true, on every setup. Set by first-run setup |
| `online_api_key` | empty | **Private.** Loaded but not sent yet: none of the current providers takes a key. Never logged, never in a URL |
| `poll_interval_s` | 1 | 1–2; floored at provider minimum for online |
| `online_idle_interval_s` | 30 | Online feed outside the show window, 5–300. Receiver polling isn't affected |
| `health_interval_s` | 10 | Receiver health check |
| `health_fail_count` | 2 | Misses before fallback |
| `max_data_age_s` | 5 | 0 = blackout on first failed poll |
| `center_lat` / `center_lon` | empty | **Private.** Plugin stays IDLE with a warning until set |
| `radius` | none | User must enter it. Used with online data |
| `radius_receiver` | none | User must enter it. Used while a receiver is the active feed |
| `radius_unit` | mi | `mi`, `nm`, `km`. Applies to both |
| `alt_floor_ft` / `alt_ceiling_ft` | 0 / 10000 | Inclusive |
| `count_no_position` | true | Aircraft with altitude but no position count as in zone |
| `ignore_ground` | false | Skip aircraft on the ground; see Ignoring ground traffic |
| `ground_speed_limit_kt` | 40 | With `ignore_ground` on, aircraft reporting `ground` are skipped below this speed or with no speed. 5–100 |
| `release_delay_s` | 5 | 0 = immediate release |
| `matrix_enabled` | false | |
| `matrix_models` | empty | List of overlay model names |
| `matrix_message` | "Aircraft overhead: {callsign} at {altitude} ft" | Custom text with placeholders |
| `matrix_nodata_message` | "Aircraft data offline – lights paused" | Empty = none |
| `matrix_color` / `matrix_font` / `matrix_size` / `matrix_position` / `matrix_speed` | `#FF0000` / empty, meaning `FreeSansBold` or another plain font if FPP lists one, otherwise the first it lists / 18 / `Right to Left` / 40 | Fonts from `GET /api/overlays/fonts` |
| `map_tile_url` | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` | Map background, used by the browser. Empty, or anything but an `https` address with `{z}`, `{x}` and `{y}`, shows only the radar plot and contacts no map service. Another host also needs allowing in FPP's content policy, or the tiles fail and the radar plot shows |
| `geocoder` | `nominatim` | `nominatim` or `off`. The policy requires a way to switch it off without a software update. Send an identifying User-Agent, at most 1 request/s, no autocomplete, and don't repeat identical queries |

## pluginInfo.json and privacy

A `privacy` block is required for listing; generate it with the [Privacy disclosure builder](https://falconchristmas.github.io/fpp-data/plugin_privacy_builder/) and check the result in the [Plugin preview](https://falconchristmas.github.io/fpp-data/plugin_preview/). Follow `PLUGININFO_FORMAT.md` for every field.

### Fields to set

- `repoName`: `fpp-plugin-SkyGuard`; `name`: SkyGuard.
- `description`: one or two sentences saying it blacks out moving-head light output when aircraft are within a set radius, using a local ADS-B receiver or online flight data.
- `versions[]`: minimum FPP 9.5, with no `platforms` key, so it installs on any platform FPP runs on. SkyGuard needs no particular hardware, and listing boards kept it off virtual machines. It relies on Linux's `/proc` to find its own processes, so macOS isn't expected to work properly.
- `dependencies`: declare any pip/apt packages (FPP 10+ only; also install them in `fpp_install.sh` for 9.5).
- Category: chosen in the fpp-data listing request, not in this file. Use `Hardware` (Hardware, Controls & Animatronics) unless a reviewer prefers otherwise.
- `bugURL`, `homeURL`, license (GPLv2), icon.
- **`api.php` risk:** FPP 9 has no protection against a plugin's `api.php` failing to parse, so one syntax error there takes down every plugin's API routes. Run `php -l api.php` on a real FPP box before anything reaches a 9.x install.

### What the privacy block must disclose

| Data / connection | Detail |
| --- | --- |
| Stored location | Home latitude/longitude stored locally in `plugindata/` and returned by the status endpoint to anyone who can reach FPP |
| Online flight feed | Center point and radius sent to the chosen provider on every online poll: every 1–2 s during a show and about every 30 s otherwise, and never while disabled or with no center set. Not alwaysOn |
| Geocoding | Typed address sent to the geocoding service, only when the user presses lookup |
| Map tiles | The viewer's browser loads map tiles from `tile.openstreetmap.org` while the status page or Location & Zone tab is open. `fpp_install.sh` runs `ManageApacheContentPolicy.sh add img-src`, `fpp_uninstall.sh` removes it, and the `sends` entry says "your browser's address and the map area around your show", because the tiles requested reveal roughly where the show is |
| Local receiver | Requests to a user-entered address on the LAN |
| API key | Optional third-party key stored locally |
| No telemetry | Nothing sent anywhere else; no usage data collected |

Re-run the builder whenever a phase adds a connection (map tiles, SMS).

## Lifecycle scripts

The daemon starts with fppd and stops with it; every hook returns in well under a second and both install and uninstall are safe to run twice.

| Script | Does |
| --- | --- |
| `scripts/fpp_install.sh` | Source `${FPPDIR}/scripts/common`; confirm `python3` exists (there are no other dependencies, so nothing is installed); create `plugindata/fpp-plugin-SkyGuard/` and its `commands/` folder, and `private.json` at mode 0600 if missing; set `restartFlag` if FPP is below 10 or its version can't be read. Defaults live in the code, so no settings are written. Idempotent |
| `scripts/postStart.sh` | If not already running (PID file check), launch the supervisor and daemon in the background with nohup and return. The daemon opens the log itself with `WatchedFileHandler`, because a shell redirect would keep writing to the rotated file (guidelines §1.2) |
| `scripts/preStop.sh` | Run `python3 -m skyguard.supervisor stop`: send SIGTERM to the verified supervisor PID, which stops the daemon so it can release all overlays; wait at most ~2 s, then kill. A PID is signalled only if `/proc/<pid>/cmdline` shows it is ours |
| `scripts/fpp_install.sh`, on an update | Stops and restarts a running daemon, because FPP's quick restart (the Restart FPPD button) restarts fppd in place without running `preStop.sh` or `postStart.sh`. Seen on the spare Pi: an updated plugin kept running the old daemon. Does nothing if the daemon isn't running |
| The version check in both install hooks | Reads `majorVersion` from `/api/fppd/version`. `-V` is an option of `fppd`, not of the `fpp` tool, so the first version of these hooks couldn't read it and asked for a restart on every install |
| `scripts/fpp_uninstall.sh` | Stop the daemon; release every ledger range via the API (set then delete); remove the CSP entry; set `restartFlag` if FPP is below 10 or its version can't be read; unless `FPP_PLUGIN_REINSTALL` is `1`, remove `plugindata/fpp-plugin-SkyGuard/` and the plugin config. Every step tolerates "already gone" |
| `callbacks` | Answers `--list` with `lifecycle`, so commands live-register on install (guidelines §2.8). Its `startup` hook starts the daemon if it isn't running, because `postStart.sh` doesn't run on a live install. Returns immediately. A file lock means a second start exits, since both can run at fppd startup |

- No systemd unit, cron or files outside the plugin dir, `plugindata/`, the plugin config and the log.
- Daemon crash safety: range state lives in fppd, so if the daemon dies the heads keep their last state (see If the daemon crashes). To keep the fail-safe, `skyguard/supervisor.py` restarts the daemon with a backoff of 1, 2, 5, 10, then 30 s, never gives up, logs each restart, and starts the backoff over after a 60 s run. Any daemon exit is restarted, including a clean one; only an explicit stop ends it. If the supervisor itself is killed, the next start stops any daemon it left behind, because two daemons would fight over the same ranges. A clean stop releases the channels before anything else, since the stop has a short time budget. Flag this design choice in the review.
- `plugindata/` files must be readable and writable by both the daemon (root) and the PHP pages (user `fpp`), so `fpp_install.sh` sets their owner to `fpp`, keeping `private.json` at mode 0600.

## User interface

The UI is built after the backend passes its tests (phase 1b). It is phone-first on every page, uses only FPP's own UI helpers, and adds nothing to FPP's own pages: everything lives in SkyGuard's three menu entries.

### General rules

- Use FPP's `PrintSetting*` helpers, Bootstrap 5.3 utility classes and `$.jGrowl` toasts.
- Must read correctly in light and dark themes: no hard-coded colors, only Bootstrap semantic classes and CSS variables.
- Phone-first: works from a 320 px phone to desktop with no horizontal page scroll; finger-sized tap targets, no hover-only actions.
- Settings save FPP-style, each field on change.
- Units: imperial by default (mi, ft, kt), metric selectable (`units` setting).
- **No `js/` or `css/` folder.** FPP sends plugin files with `Cache-Control: max-age=31536000`, and the tags it writes for those two folders have no version, so an update wouldn't reach a browser that has been here before. Seen on the spare Pi: a computer kept an old script while a phone had the new one. Pages load from `assets/` through `skyguard_assets.inc`, which adds the file's modification time to the address.
- Bundle any JavaScript library inside the plugin rather than loading it from a CDN at runtime. The map uses Leaflet (BSD-2-Clause; keep its license notice).
- Map tiles: show the OpenStreetMap attribution visibly on the map, keep the tile URL configurable, and don't prefetch or store tiles. Confirm FPP's pages send a valid Referer, which the tile policy requires. When adsb.fi is the selected provider, credit and link it.

### Menu entries (`menu.inc`, one per type)

| Type | Title | Page |
| --- | --- | --- |
| content | SkyGuard Settings | `content.php` |
| status | SkyGuard | `status.php` |
| help | SkyGuard | `about.php` (help + about combined) |

### First-run setup

- Shown at the top of the Settings page until every step is completed or skipped.
- It opens with a confirmation that SkyGuard is for personal use only, which sets `personal_use_confirmed`. It has no Skip, because the plugin stays idle without it.
- Steps in order: 1 Data feed, 2 Location & zone, 3 Fixtures, 4 Matrix message, 5 Test run (fires SkyGuard Test Trigger). Each of these has a Skip option.
- The Data feed step starts by asking whether the user has their own ADS-B receiver. No selects `online` mode, and receiver settings and the receiver radius aren't shown or required anywhere. Yes then asks whether to use online data as a fallback (`both`) or the receiver alone (`receiver`).
- Progress stored in a `setup_state` setting. No "run setup again" link afterwards; skipped items are set from the tabs.

### Settings page tabs

| Tab | Contents |
| --- | --- |
| Data Source | Source mode; receiver URL and optional 978 URL with a Test button (shows aircraft seen or the error); online provider dropdown, masked API key field, Test button |
| Location & Zone | Lat/lon fields; address field + Look up button (shows candidates to pick from); Use my location button (disabled with a note when the page isn't HTTPS); display-only map of the center point and radius circle (no draggable pin); online radius and receiver radius as required entries with no pre-filled value, shown according to the data sources in use, and their unit; the lag notice when online data is selected; altitude floor and ceiling; "Ignore aircraft on the ground" switch |
| Fixtures | Table, one row per head: name, universe or start channel, dimmer ch, dimmer off, shutter ch, shutter off, enabled. Add, duplicate, delete. Per-row Test button blacks out that head only for 3 s |
| Matrix Message | Enable switch; panel picker from the FPP overlay model list; message field with click-to-insert placeholder buttons; live preview with sample values; Test on panels and Stop buttons, with a live view of the first ticked panel's real pixels during the test, also shown when the page is opened during one; no-data message; color, font, size, speed |
| Advanced (collapsed) | Poll interval, online idle interval, health interval, fail count, max data age, release delay, count-no-position toggle, ground speed limit, units |

**Fixture row validation:** a row takes effect only when its required fields are filled and valid. Until then it shows an "Incomplete" badge and the trigger engine ignores it. A row whose universe can't be resolved shows "Universe not found".

### Status page (Status → SkyGuard)

Top to bottom on a phone:

1. **State badge:** CLEAR, BLACKOUT or IDLE with its reason, in Bootstrap semantic colors.
2. **Controls:** Enable/Disable button (confirmation before disabling), Test Trigger button (off while disabled) and the aircraft list button.
3. **Map:** center point, a circle for each radius that applies to the data sources chosen (online red, receiver blue; the one in use is solid and lightly filled, the other dashed, with a legend), aircraft icons rotated to heading; in-zone aircraft highlighted and labeled with callsign, altitude and speed, and any other aircraft shows its label when tapped or clicked, which lasts through each refresh, because labels on every aircraft hide each other; skipped ones marked `ground (ignored)`. Refreshes every 5 s from the status endpoint. When the newest data is older than twice the online interval outside a show plus 10 s, no aircraft are drawn or listed and a note says why: online data stops while disabled, and the status still holds the last aircraft seen.
4. **No-internet fallback:** if map tiles don't load, a theme-aware radar plot (range rings, radius circle, aircraft dots) drawn from the same data.
5. **Aircraft list:** a full-screen list (callsign, altitude, speed, distance, in zone, or `ground (ignored)`) with a Back button, which the browser's own back also closes; same 5 s refresh.
6. **Health:** active feed, receiver health, data age, aircraft count, whether a show is running, per-fixture status (including "Held dark" from `held_channels`). Current warnings sit under the state badge.
7. **Recent events:** last 20 blackout transitions with time, reason and aircraft.

### Help page

Setup walkthrough, how the blackout works (which channels, fail-safe behavior), troubleshooting (feed down, universe not found, matrix not showing), and where to find the plugin log. No ads or donation links.

### Backend additions for the UI

- SkyGuard Test Trigger gains an optional `fixture` argument (blacks out one head for 3 s).
- Status JSON gains `events[]` (last 20 transitions).
- `api.php` gains fixture endpoints: list, add, update and delete one row, so rows save on change. Rows carry a stable `id`, which the status also reports.
- `api.php` gains `GET`/`PUT private` for the center and API key, which never returns the key, only `api_key_set`; and `POST test-receiver` and `POST test-online`, which report a count or an error and never pass a response on. `test-online` needs personal use confirmed and a center, and is limited to one request every 5 s.
- Every write route requires `Content-Type: application/json`, so a page on another site can't send one as a simple request. Free-text settings refuse `"` and `\\`, because FPP 9.5 writes settings into an inline script without escaping them.
- A `no_fixtures` warning shows while enabled and confirmed with no head that resolves to a channel, cleared by `fixtures_set`.
- New settings: `units`, `setup_state`, `map_tile_url`.

## Verification results

Checked Nov 5, 2026 by reading FPP 9.5 and master source, FPP tags 10.0–10.3 for the two version-specific behaviors, provider and policy pages, and receiver source. API requests, including approved write tests through SkyGuard's own controllers, were then run against a spare Pi 4 on FPP 10.3 (Debian 13) with no heads, panel or sequence. **Box test** marks what still needs one.

Seen on the real 10.3 box:

- **A PHP route answers 404 to a request with a JSON `Content-Type` and no body** (`/api/channel/...`, `/api/plugin/...`). The client therefore sends that header only with a JSON body. Settings are saved as a bare value, as FPP's own pages do. Routes proxied to fppd (`/api/overlays/...`, `/api/command/...`, `/api/fppd/...`) aren't affected.
- **A sequence started on its own reports `status` 0 and `scheduler.status` `idle`; only `status_name` says `playing`.** `status_name` is therefore essential to detecting a show started by hand. When stopped, it returned to `idle` within 0.2 s. A scheduled playlist, and the gap between its items, hasn't been observed yet. `warnings` is left out of the status when there are none.
- `universeOutputs` returns `{"enabled": 0, "channelCount": -1, "universes": []}` when no universes are set up.
- The Text effect's first eight arguments match this spec; 10.3 adds optional color arguments after them. `isActive` and `effectRunning` are on `GET /api/overlays/model/<name>`.
- `/api/plugin/headerIndicators` exists, and the range, state and settings routes are in FPP's API documentation.
- **Channel ranges:** FPP accepted set, repeat set and set-then-delete for one-channel ranges, including the batched form, both idle and during a playing sequence. Their effect on real output wasn't observable on this box, whose channel outputs are off.
- **Center text:** with the model enabled first and auto-enable off, the text was still in the model's buffer after 3 s. Stop Effects with `Enabled` returned the model to disabled and emptied the buffer in about 0.1 s. A model that was already enabled stayed enabled with its effect stopped.
- **Scrolling text:** `effectRunning` was true during the pass and false after it, with the model still enabled, confirming that a pass ends by itself. Sending the text again started a new pass, and stopping mid-pass disabled the model.

| # | Item | Result |
| --- | --- | --- |
| 1 | Overlay models via API | **Failed as originally specified.** Creating a model takes a whole-list `POST /api/models` and an fppd restart. Replaced by channel ranges, which FPP 10.3 accepted. **Box test** on 9.5 |
| 2 | Override behavior | Source confirms a range writes only its own channels on each output frame, after the sequence and overlay models. **Box test** with a running sequence and real heads |
| 3 | Universe lookup | See Universe to FPP channel. **Box check** of the user's actual output configuration; cape DMX ports not traced |
| 4 | Schedule status | See Schedule awareness. Idle and a sequence started by hand confirmed on 10.3. **Box check** of a scheduled playlist and the gap between its items |
| 5 | Banner warnings | **No supported API.** Replaced by header indicator, status data and log. **Box test** of the indicator after the build |
| 6 | Text overlay | See Matrix message. Display state, scrolling, stop and restore confirmed on 10.3. **Box test** on 9.5, and of how it looks on a real panel, non-ASCII text, and Virtual Display |
| 7 | Online providers | Polling tested live: see Online providers. **adsb.lol's original 1 s minimum failed on a real box**, which is why the default changed to adsb.fi. airplanes.live removed |
| 8 | Receiver formats | Confirmed from documentation or source for readsb, dump1090-fa and dump978-fa. tar1090 reuses the decoder's JSON (not independently checked). **Box check** with a real file from the user's receiver |
| 9 | Geocoder | Nominatim is acceptable for a user-pressed lookup under its [usage policy](https://operations.osmfoundation.org/policies/nominatim/); see the `geocoder` setting |
| 10 | Commands | Format confirmed. Live load exists from 10.0, not on 9.5. **Box test** after the build |
| 11 | Map | Leaflet (BSD-2-Clause). [OSM tile policy](https://operations.osmfoundation.org/policies/tiles/) reviewed. **Box check** of tile loading and Referer in phase 1b |
| 12 | Plugin API saves | Confirmed on 9.5: `api.php` endpoints may use GET, POST, PUT or DELETE. **Box test** after the build |

- **Crash recovery on the real box:** a daemon killed with `kill -9` during a blackout was restarted by the supervisor, resumed in BLACKOUT and went on to release and black out normally. With the supervisor killed, the daemon kept running; an fppd restart then left exactly one new supervisor and one new daemon. Blackout state here means SkyGuard's own state, since this box's channel outputs are off.

### Owner decisions, Nov 5, 2026

1. Use channel ranges instead of overlay models.
2. Add an optional fixture `start_channel` for outputs with no universe number.
3. Add a header indicator alongside status data and the log.
4. Keep the API key and the home coordinates in `plugindata/` (`private.json`, mode 0600).
5. Slow only the online feed outside the show window (30 s), and stop it when disabled. Receiver polling stays fast. Poll immediately when the show window starts.
6. Keep `count_no_position` on by default, and review it against real traffic during acceptance.
6a. Count live bridged data as a show.
7. Remove airplanes.live from the provider list until its operator approves access.
7a. Make adsb.fi the default at 2 s, and keep adsb.lol at 15 s.
7b. Add a second radius for use with a receiver, a notice to increase the radius by 3 miles for online lag, and a personal-use confirmation covering the whole plugin.
7c. Both radii are user entered, with no defaults and no fallback between them.
8. Add an optional ground-traffic filter (`ignore_ground`, `ground_speed_limit_kt`), off by default. It requires a `ground` report, with a speed below the limit or no speed, and uses no numeric altitude, airport elevation or aircraft type. Helicopters below the limit are counted unless they report `ground`.

### Known risks to review during acceptance

- **Airport inside the radius:** with the default floor of 0, ground traffic counts. In one live sample around Heathrow, 28 of 42 aircraft were on the ground, so a nearby airport could keep the heads dark for the whole show. `ignore_ground` is the control for this, and raising `alt_floor_ft` also excludes `ground` aircraft.
- **False `ground` reports with `ignore_ground` on:** see the limits under Ignoring ground traffic.
- **Far-away aircraft with no position:** see `count_no_position` above.
- **Unprotected cases:** Channel Tester and the crash-during-CLEAR gap.

## Acceptance tests

Phase 1 is done when every test below passes on a Pi 4 running FPP 9.5 and on current FPP release and nightly. Use a mock feed server (replays recorded `aircraft.json` files) so tests are repeatable without real traffic.

- [ ] Aircraft enters radius within altitude band → dimmer/shutter forced to off values on the next poll; pan/tilt/color keep following the sequence
- [ ] Aircraft above ceiling or below floor → no blackout
- [ ] Aircraft reporting "ground" inside radius with floor 0 → blackout
- [ ] `ignore_ground` on: aircraft reporting "ground" below `ground_speed_limit_kt` inside radius → no blackout; shown as `ground (ignored)` in status and logged
- [ ] `ignore_ground` on: aircraft reporting "ground" reaches `ground_speed_limit_kt` on its takeoff roll → blackout on that poll
- [ ] `ignore_ground` on: slow aircraft reporting a numeric altitude, such as a hovering helicopter → blackout
- [ ] `ignore_ground` on: aircraft or surface vehicle reporting "ground" with no speed → no blackout; shown as `ground (ignored)`
- [ ] `ignore_ground` on: helicopter below `ground_speed_limit_kt` with no "ground" report → blackout; the same helicopter reporting "ground" → no blackout
- [ ] Aircraft with altitude but no position: blackout when `count_no_position` on; logged only when off
- [ ] Zone clears → channels released after `release_delay_s`; 0 releases immediately
- [ ] Two fixtures in different universes, one in the same universe as a non-head prop → only the listed channels change
- [ ] Receiver stops → after 2 failed 10 s checks, online feed takes over and the warning shows; receiver returns → switches back and warning clears
- [ ] Feed silent longer than `max_data_age_s` → blackout with reason `no_data`; recovers when data returns
- [ ] Both feeds down → blackout held, both-down warning shown
- [ ] Outside schedule → IDLE, nothing overridden; schedule starts with an aircraft present → blackout on first poll
- [ ] Matrix message appears with placeholders filled, updates, clears on release; shows on Virtual Display
- [ ] Enable, Disable, Test Trigger, Test Message and Stop Test Message commands work from a playlist and from the Command API
- [ ] Status endpoint returns all documented fields
- [ ] fppd restart mid-blackout → daemon restarts and re-evaluates; no stuck overlays after uninstall
- [ ] Daemon killed mid-blackout → heads stay dark; supervisor restarts it; it resumes with reason `resume` and releases only once the normal rules are met
- [ ] Daemon killed mid-blackout with a fixture removed while it is down → that fixture's channel is still released
- [ ] On FPP 9.5 and 10.2, release after an fppd restart never produces a full-on channel
- [ ] Online feed outside the show window polls about every 30 s, inside it at the provider's rate with no refusals for a whole show, with an immediate poll at show start; no online requests while disabled or before personal use is confirmed; receiver polling stays fast throughout
- [ ] `both` mode with both radii entered: the radius in force changes when the feed falls back and when it returns
- [ ] A required radius not entered: IDLE with reason `no_radius` and a warning, and no online requests; entering it starts protection without a restart
- [ ] First-run: on every setup the plugin stays IDLE with a warning until personal use is confirmed, then starts without a restart; the lag notice shows whenever online data is selected
- [ ] A USB DMX head using `start_channel` blacks out correctly
- [ ] Header indicator always shows, says ON, OFF or NOT RUNNING, and adds any warnings
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
- [ ] The ground switch is on Location & Zone and the ground speed limit is under Advanced; ignored aircraft show as `ground (ignored)` on the map, radar plot and aircraft list

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
- [FPP source](https://github.com/FalconChristmas/fpp)
- [adsb.lol API](https://github.com/adsblol/api), [adsb.fi open data](https://github.com/adsbfi/opendata)
- [readsb JSON](https://github.com/wiedehopf/readsb/blob/dev/README-json.md), [dump1090-fa JSON](https://github.com/flightaware/dump1090/blob/master/README-json.md), [dump978 writer](https://github.com/flightaware/dump978/blob/master/skyaware_writer.cc)

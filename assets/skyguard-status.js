/* SkyGuard's status page. Refreshes every 5 seconds from the status file the daemon writes. */
(function () {
    'use strict';

    var API = 'api/plugin/fpp-plugin-SkyGuard/';
    var REFRESH_MS = 5000;
    var COMMAND_SETTLE_MS = 1500;

    var STATE_CLASSES = {
        CLEAR: 'text-bg-success',
        BLACKOUT: 'text-bg-danger',
        IDLE: 'text-bg-secondary'
    };

    var REASONS = {
        aircraft: 'Aircraft in the zone.',
        no_data: 'No fresh aircraft data, so the heads are held dark.',
        test: 'A test is running.',
        schedule: 'No show is running.',
        disabled: 'SkyGuard is disabled.',
        not_confirmed: 'Waiting for the personal use confirmation in SkyGuard Settings.',
        no_center: 'Waiting for a center point in SkyGuard Settings.',
        no_radius: 'Waiting for a radius in SkyGuard Settings.',
        resume: 'Held dark after a restart, until fresh data arrives.',
        clear: 'No aircraft in the zone.'
    };

    var FEEDS = { receiver: 'My receiver', online: 'Online data' };

    function byId(id) {
        return document.getElementById(id);
    }

    function toast(message, state) {
        $.jGrowl(message, { themeState: state || 'success' });
    }

    function showError(error) {
        toast(error.message, 'danger');
    }

    /* A JSON header goes only with a body: FPP's PHP routes answer 404 to one without. */
    function request(method, path, body) {
        var options = { method: method, credentials: 'same-origin' };
        if (body !== undefined) {
            options.headers = { 'Content-Type': 'application/json' };
            options.body = JSON.stringify(body);
        }
        return fetch(path, options).then(function (response) {
            return response.json().catch(function () {
                return {};
            }).then(function (doc) {
                if (!response.ok) {
                    throw new Error(doc.message || ('The request failed (HTTP ' + response.status + ').'));
                }
                return doc;
            });
        });
    }

    function readSetting(key) {
        return request('GET', API + 'settings/' + encodeURIComponent(key)).then(function (doc) {
            return (doc[key] === false || doc[key] === undefined) ? null : doc[key];
        }).catch(function () {
            return null;
        });
    }

    function runCommand(name, args) {
        return request('POST', 'api/command/' + encodeURIComponent(name), args);
    }

    function cell(row, text, className) {
        var td = document.createElement('td');
        td.textContent = text;
        if (className) {
            td.className = className;
        }
        row.appendChild(td);
        return td;
    }

    function badge(text, className) {
        var span = document.createElement('span');
        span.className = 'badge ' + className;
        span.textContent = text;
        return span;
    }

    /* ---- the pieces of the page ---- */

    var units = 'imperial';
    var staleAfterS = 70;
    var map = null;
    var last = null;

    function showState(status) {
        var state = byId('skyGuardState');
        var reason = byId('skyGuardReason');
        if (status.running === false || !status.state) {
            state.className = 'badge fs-5 text-bg-danger';
            state.textContent = 'NOT RUNNING';
            reason.textContent = 'SkyGuard is not running, so the heads are not protected.';
            return;
        }
        state.className = 'badge fs-5 ' + (STATE_CLASSES[status.state] || 'text-bg-secondary');
        state.textContent = status.state;
        reason.textContent = REASONS[status.reason] || '';
    }

    function showControls(status) {
        var toggle = byId('skyGuardToggle');
        var running = status.running !== false && !!status.state;
        toggle.disabled = !running;
        toggle.textContent = status.enabled ? 'Disable SkyGuard' : 'Enable SkyGuard';
        toggle.className = status.enabled ? 'btn btn-danger' : 'btn btn-success';
        byId('skyGuardTest').disabled = !(running && status.enabled);
    }

    function showWarnings(status) {
        var box = byId('skyGuardStatusWarnings');
        var messages = status.warnings || [];
        box.textContent = messages.join(' ');
        box.hidden = messages.length === 0;
        byId('skyGuardNotices').textContent = (status.notices || []).join(' ');
    }

    function healthRow(box, label, value) {
        var term = document.createElement('dt');
        term.className = 'col-6 col-md-3 fw-semibold';
        term.textContent = label;
        var detail = document.createElement('dd');
        detail.className = 'col-6 col-md-9 mb-1';
        detail.textContent = value;
        box.appendChild(term);
        box.appendChild(detail);
    }

    function showHealth(status) {
        var box = byId('skyGuardHealth');
        var receiver = 'Not used';
        if (status.receiver_ok === true) {
            receiver = 'OK';
        } else if (status.receiver_ok === false) {
            receiver = 'Not responding';
        }
        box.replaceChildren();
        healthRow(box, 'Active data feed', FEEDS[status.active_feed] || 'None');
        healthRow(box, 'Receiver', receiver);
        healthRow(box, 'Data age', typeof status.last_data_age_s === 'number' ? status.last_data_age_s + ' seconds' : 'No data yet');
        healthRow(box, 'Aircraft in the zone', String(status.aircraft_in_zone || 0));
        healthRow(box, 'Show running', status.in_schedule ? 'Yes' : 'No');
    }

    function showHeads(status) {
        var body = byId('skyGuardHeads');
        var held = status.held_channels || [];
        body.replaceChildren();
        (status.fixtures || []).forEach(function (fixture) {
            var row = document.createElement('tr');
            var channels = (fixture.channels || []).map(function (channel) {
                return channel.channel;
            });
            var dark = channels.length > 0 && channels.every(function (channel) {
                return held.indexOf(channel) !== -1;
            });
            cell(row, fixture.name || 'Head');
            var state = cell(row, '');
            if (fixture.problem) {
                state.appendChild(badge(fixture.problem, 'text-bg-warning'));
            } else if (dark) {
                state.appendChild(badge('Held dark', 'text-bg-danger'));
            } else {
                state.appendChild(badge('Following the sequence', 'text-bg-success'));
            }
            cell(row, channels.join(', '));
            body.appendChild(row);
        });
    }

    function showEvents(status) {
        var body = byId('skyGuardEvents');
        var events = (status.events || []).slice().reverse();
        body.replaceChildren();
        if (!events.length) {
            var row = document.createElement('tr');
            cell(row, 'No blackouts yet.', 'text-body-secondary').colSpan = 4;
            body.appendChild(row);
            return;
        }
        events.forEach(function (event) {
            var line = document.createElement('tr');
            cell(line, new Date(event.at * 1000).toLocaleString());
            cell(line, event.type === 'started' ? 'Blackout started' : 'Blackout ended');
            cell(line, REASONS[event.reason] || '');
            cell(line, (event.aircraft || []).join(', '));
            body.appendChild(line);
        });
    }

    function showList(status) {
        var aircraft = status.aircraft || [];
        var body = byId('skyGuardListRows');
        var radiusUnit = (status.radius || {}).unit;
        byId('skyGuardListOpen').textContent = 'Aircraft list (' + aircraft.length + ')';
        byId('skyGuardListEmpty').hidden = aircraft.length > 0;
        body.replaceChildren();
        aircraft.forEach(function (plane) {
            var info = SkyGuardMap.describe(plane, units, radiusUnit);
            var row = document.createElement('tr');
            cell(row, info.name);
            cell(row, info.altitude);
            cell(row, info.speed);
            cell(row, info.distance);
            cell(row, info.zone);
            body.appendChild(row);
        });
    }

    /* Online data stops while SkyGuard is disabled, and the last aircraft stay in the status.
       Drawing them would show aircraft that left long ago, so old data means none are shown. */
    function withoutOldAircraft(status) {
        var age = status.last_data_age_s;
        var old = typeof age === 'number' && age > staleAfterS;
        var note = byId('skyGuardOldData');
        note.hidden = !old;
        if (!old) {
            return status;
        }
        note.textContent = 'No aircraft are shown, because the newest data is ' + Math.round(age / 60) +
            ' minutes old.' + (status.enabled ? '' : ' SkyGuard does not request online data while it is disabled.');
        return Object.assign({}, status, { aircraft: [], aircraft_in_zone: 0 });
    }

    function refresh() {
        return request('GET', API + 'status').then(function (reported) {
            var status = withoutOldAircraft(reported);
            last = status;
            showState(status);
            showControls(status);
            showWarnings(status);
            showHealth(status);
            showHeads(status);
            showEvents(status);
            showList(status);
            if (map) {
                map.update(status);
            }
        }, function () {
            /* Only a failed read lands here: that says FPP can't be reached, not that SkyGuard stopped. */
            var state = byId('skyGuardState');
            state.className = 'badge fs-5 text-bg-warning';
            state.textContent = 'UNKNOWN';
            byId('skyGuardReason').textContent = 'FPP cannot be reached right now, so this may be out of date.';
            showControls({ running: false });
        });
    }

    /* ---- controls ---- */

    function setupControls() {
        byId('skyGuardToggle').addEventListener('click', function () {
            var enabled = last && last.enabled;
            if (enabled && !window.confirm('Disable SkyGuard? The heads will not black out for aircraft until you enable it again.')) {
                return;
            }
            runCommand(enabled ? 'SkyGuard Disable' : 'SkyGuard Enable', []).then(function () {
                toast(enabled ? 'SkyGuard disabled.' : 'SkyGuard enabled.');
                setTimeout(refresh, COMMAND_SETTLE_MS);
            }).catch(showError);
        });
        byId('skyGuardTest').addEventListener('click', function () {
            runCommand('SkyGuard Test Trigger', ['5']).then(function () {
                toast('Blacking out every head for 5 seconds.');
                setTimeout(refresh, COMMAND_SETTLE_MS);
            }).catch(showError);
        });
    }

    /* The list covers the whole screen, so a phone's back button should close it, not leave the page. */
    function setupList() {
        var element = byId('skyGuardList');
        var modal = bootstrap.Modal.getOrCreateInstance(element);
        var historyEntry = false;

        byId('skyGuardListOpen').addEventListener('click', function () {
            history.pushState({ skyGuardList: true }, '');
            historyEntry = true;
            modal.show();
        });
        byId('skyGuardListBack').addEventListener('click', function () {
            modal.hide();
        });
        element.addEventListener('hidden.bs.modal', function () {
            if (historyEntry) {
                historyEntry = false;
                history.back();
            }
        });
        window.addEventListener('popstate', function () {
            if (historyEntry) {
                historyEntry = false;
                modal.hide();
            }
        });
    }

    function start() {
        if (!byId('skyGuardStatus')) {
            return;
        }
        setupControls();
        setupList();
        Promise.all([readSetting('units'), readSetting('map_tile_url'), readSetting('online_provider'), readSetting('online_idle_interval_s')]).then(function (results) {
            units = results[0] === 'metric' ? 'metric' : 'imperial';
            /* Two missed polls at the slowest rate in use, outside a show. */
            staleAfterS = 2 * (parseFloat(results[3]) || 30) + 10;
            map = SkyGuardMap.create({
                box: byId('skyGuardMap'),
                note: byId('skyGuardMapNote'),
                legend: byId('skyGuardMapLegend'),
                tiles: SkyGuardMap.usableTiles(results[1]),
                units: function () {
                    return units;
                }
            });
            byId('skyGuardCredit').hidden = (results[2] || 'adsb.fi') !== 'adsb.fi';
            return refresh();
        });
        setInterval(function () {
            if (!document.hidden) {
                refresh();
            }
        }, REFRESH_MS);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
}());

/* SkyGuard's settings page. content.php loads it, with a version in its address. */

/* Called by FPP's PrintSetting helpers after a value saves, so it must be global. */
function skyGuardSettingSaved(key) {
    if (window.skyGuardPage) {
        window.skyGuardPage.settingSaved(key);
    }
}

(function () {
    'use strict';

    var REPO = 'fpp-plugin-SkyGuard';
    var API = 'api/plugin/' + REPO + '/';
    var SETUP_DONE = 'done';

    var DEFAULTS = {
        matrix_message: 'Aircraft overhead: {callsign} at {altitude} ft',
        matrix_nodata_message: 'Aircraft data offline – lights paused',
        matrix_color: '#FF0000',
        map_tile_url: window.SkyGuardMap ? SkyGuardMap.DEFAULT_TILES : ''
    };

    var PLACEHOLDERS = {
        callsign: 'N123AB',
        altitude: '3200',
        speed: '142',
        distance: '4.7',
        heading: '270',
        count: '2'
    };

    function byId(id) {
        return document.getElementById(id);
    }

    function toast(message, state) {
        $.jGrowl(message, { themeState: state || 'success' });
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

    var lastSave = Promise.resolve();

    function saveSetting(key, value) {
        lastSave = fetch(API + 'settings/' + encodeURIComponent(key), {
            method: 'PUT',
            credentials: 'same-origin',
            body: String(value)
        }).then(function (response) {
            if (!response.ok) {
                throw new Error('Could not save that setting.');
            }
        });
        return lastSave;
    }

    function readSetting(key) {
        return request('GET', API + 'settings/' + encodeURIComponent(key)).then(function (doc) {
            return (doc[key] === false || doc[key] === undefined) ? null : doc[key];
        });
    }

    /* FPP 9.5 writes settings into an inline script without escaping them, so one of
       these in a saved value would break every page of this plugin. */
    function hasUnsafeCharacters(text) {
        return /["\\\r\n]/.test(text);
    }

    function showError(error) {
        toast(error.message, 'danger');
    }

    function sourceMode() {
        var field = byId('source_mode');
        return field ? field.value : 'both';
    }

    /* ---- showing only what applies to the data sources chosen ---- */

    function updateVisibility() {
        var mode = sourceMode();
        var usesReceiver = mode === 'receiver' || mode === 'both';
        var usesOnline = mode === 'online' || mode === 'both';
        document.querySelectorAll('[data-show-with="receiver"]').forEach(function (element) {
            element.hidden = !usesReceiver;
        });
        document.querySelectorAll('[data-show-with="online"]').forEach(function (element) {
            element.hidden = !usesOnline;
        });
        var provider = byId('online_provider');
        var name = provider ? provider.value : 'adsb.fi';
        byId('skyGuardProviderCredit').hidden = name !== 'adsb.fi';
        byId('skyGuardProviderSlow').hidden = name !== 'adsb.lol';
    }

    /* ---- warnings from the running plugin ---- */

    var fixtureProblems = {};
    var latestStatus = null;
    var locationMap = null;

    function refreshStatus() {
        return request('GET', API + 'status').then(function (status) {
            var box = byId('skyGuardWarnings');
            var messages = (status.warnings || []).slice();
            if (status.running === false) {
                messages.unshift('SkyGuard is not running, so the heads are not protected.');
            }
            box.textContent = messages.join(' ');
            box.hidden = messages.length === 0;

            /* Covers a page opened or reloaded while a test is running. */
            if (Date.now() >= messageTestSettleUntil) {
                showMessageTest(status.message_test === true);
            }

            fixtureProblems = {};
            (status.fixtures || []).forEach(function (fixture) {
                fixtureProblems[fixture.id] = fixture.problem;
            });
            updateFixtureBadges();
            latestStatus = status;
            if (locationMap) {
                locationMap.update(status);
            }
        }).catch(function () {
            /* The next refresh tries again. */
        });
    }

    /* ---- receiver and provider tests ---- */

    function runTest(buttonId, resultId, path, makeBody) {
        byId(buttonId).addEventListener('click', function () {
            var result = byId(resultId);
            var body = makeBody();
            if (body === null) {
                return;
            }
            result.className = 'small ms-2 text-body-secondary';
            result.textContent = 'Testing…';
            request('POST', path, body).then(function (doc) {
                result.className = 'small ms-2 text-success';
                result.textContent = 'Working: ' + doc.aircraft + ' aircraft seen.';
            }).catch(function (error) {
                result.className = 'small ms-2 text-danger';
                result.textContent = error.message;
            });
        });
    }

    function urlBody(fieldId, resultId) {
        return function () {
            var value = byId(fieldId).value.trim();
            if (!value) {
                byId(resultId).className = 'small ms-2 text-danger';
                byId(resultId).textContent = 'Enter an address first.';
                return null;
            }
            return { url: value };
        };
    }

    /* ---- location ---- */

    function saveCenter(lat, lon) {
        return request('PUT', API + 'private', { center_lat: lat, center_lon: lon }).then(function (doc) {
            byId('skyGuardLat').value = doc.center_lat;
            byId('skyGuardLon').value = doc.center_lon;
            toast('Center point saved.');
            refreshStatus();
        });
    }

    function setupLocation() {
        byId('skyGuardCenterSave').addEventListener('click', function () {
            var lat = byId('skyGuardLat').value.trim();
            var lon = byId('skyGuardLon').value.trim();
            saveCenter(lat === '' ? null : lat, lon === '' ? null : lon).catch(showError);
        });

        var locate = byId('skyGuardUseLocation');
        if (!window.isSecureContext || !navigator.geolocation) {
            locate.disabled = true;
            byId('skyGuardLocationNote').textContent =
                'Browsers only share your location over HTTPS. Enter it by hand or look up an address.';
        }
        locate.addEventListener('click', function () {
            navigator.geolocation.getCurrentPosition(function (position) {
                saveCenter(position.coords.latitude, position.coords.longitude).catch(showError);
            }, function () {
                toast('Your browser did not share a location.', 'danger');
            });
        });

        byId('skyGuardLookup').addEventListener('click', function () {
            var results = byId('skyGuardLookupResults');
            var note = byId('skyGuardLookupNote');
            var query = byId('skyGuardAddress').value.trim();
            results.replaceChildren();
            note.textContent = 'Looking up…';
            request('GET', API + 'geocode?q=' + encodeURIComponent(query)).then(function (doc) {
                note.textContent = doc.results.length
                    ? 'Choose one. ' + doc.attribution + '.'
                    : 'Nothing found for that address.';
                doc.results.forEach(function (place) {
                    var item = document.createElement('button');
                    item.type = 'button';
                    item.className = 'list-group-item list-group-item-action';
                    item.textContent = place.name;
                    item.addEventListener('click', function () {
                        saveCenter(place.lat, place.lon).then(function () {
                            results.replaceChildren();
                            note.textContent = 'Saved. ' + doc.attribution + '.';
                        }).catch(showError);
                    });
                    results.appendChild(item);
                });
            }).catch(function (error) {
                note.textContent = error.message;
            });
        });
    }

    function setupApiKey() {
        var state = byId('skyGuardApiKeyState');
        var field = byId('skyGuardApiKey');
        function showState(isSet) {
            state.textContent = isSet ? 'A key is saved.' : 'No key is saved.';
        }
        function save(value, message) {
            request('PUT', API + 'private', { online_api_key: value }).then(function (doc) {
                field.value = '';
                showState(doc.api_key_set);
                toast(message);
            }).catch(showError);
        }
        byId('skyGuardApiKeySave').addEventListener('click', function () {
            if (field.value) {
                save(field.value, 'API key saved.');
            }
        });
        byId('skyGuardApiKeyClear').addEventListener('click', function () {
            save(null, 'API key cleared.');
        });
        return showState;
    }

    /* ---- fixtures ---- */

    var FIXTURE_NUMBERS = [
        ['universe', 0, 63999], ['start_channel', 1, 8388608],
        ['dimmer_ch', 1, 512], ['dimmer_off', 0, 255],
        ['shutter_ch', 1, 512], ['shutter_off', 0, 255]
    ];
    var fixtureBadges = {};

    function isBlank(value) {
        return value === null || value === undefined || value === '';
    }

    /* The daemon's rules, plus FPP's channel limits, so a row can be marked at once. */
    function rowIsComplete(row) {
        if (!row.name) {
            return false;
        }
        if (isBlank(row.universe) === isBlank(row.start_channel)) {
            return false;
        }
        if (isBlank(row.dimmer_ch) && isBlank(row.shutter_ch)) {
            return false;
        }
        return FIXTURE_NUMBERS.every(function (rule) {
            var value = row[rule[0]];
            return isBlank(value) || (Number(value) >= rule[1] && Number(value) <= rule[2]);
        });
    }

    function setBadge(id, row) {
        var badge = fixtureBadges[id];
        if (!badge) {
            return;
        }
        var problem = fixtureProblems[id];
        if (!rowIsComplete(row)) {
            badge.className = 'badge text-bg-warning';
            badge.textContent = 'Incomplete';
        } else if (row.enabled === false) {
            badge.className = 'badge text-bg-secondary';
            badge.textContent = 'Off';
        } else if (problem) {
            badge.className = 'badge text-bg-danger';
            badge.textContent = problem;
        } else {
            badge.className = 'badge text-bg-success';
            badge.textContent = 'OK';
        }
    }

    var fixtureRows = {};

    function updateFixtureBadges() {
        Object.keys(fixtureRows).forEach(function (id) {
            setBadge(id, fixtureRows[id]);
        });
    }

    function makeCell(child) {
        var cell = document.createElement('td');
        cell.appendChild(child);
        return cell;
    }

    function makeButton(label, className, onClick) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = className;
        button.textContent = label;
        button.addEventListener('click', onClick);
        return button;
    }

    function addFixtureRow(row) {
        fixtureRows[row.id] = row;
        var tr = document.createElement('tr');

        function saveRow() {
            request('PUT', API + 'fixtures/' + encodeURIComponent(row.id), row).then(function () {
                setBadge(row.id, row);
                refreshStatus();
            }).catch(showError);
        }

        var name = document.createElement('input');
        name.type = 'text';
        name.className = 'form-control form-control-sm';
        name.maxLength = 64;
        name.value = row.name || '';
        name.setAttribute('aria-label', 'Name');
        name.addEventListener('change', function () {
            row.name = name.value.trim();
            saveRow();
        });
        tr.appendChild(makeCell(name));

        var enabled = document.createElement('input');
        enabled.type = 'checkbox';
        enabled.className = 'form-check-input m-0';
        enabled.checked = row.enabled !== false;
        enabled.setAttribute('aria-label', 'On');
        enabled.addEventListener('change', function () {
            row.enabled = enabled.checked;
            saveRow();
        });
        tr.appendChild(makeCell(enabled));

        FIXTURE_NUMBERS.forEach(function (rule) {
            var input = document.createElement('input');
            input.type = 'number';
            input.className = 'form-control form-control-sm';
            input.min = rule[1];
            input.max = rule[2];
            input.value = isBlank(row[rule[0]]) ? '' : row[rule[0]];
            input.setAttribute('aria-label', rule[0]);
            input.addEventListener('change', function () {
                row[rule[0]] = input.value === '' ? null : input.value;
                saveRow();
            });
            tr.appendChild(makeCell(input));
        });

        var badge = document.createElement('span');
        fixtureBadges[row.id] = badge;
        tr.appendChild(makeCell(badge));
        setBadge(row.id, row);

        var actions = document.createElement('div');
        actions.className = 'd-flex flex-wrap gap-1';
        actions.appendChild(makeButton('Test', 'btn btn-sm btn-secondary', function () {
            runCommand('SkyGuard Test Trigger', ['3', row.id]).then(function () {
                toast('Testing this head for 3 seconds.');
            }).catch(showError);
        }));
        actions.appendChild(makeButton('Duplicate', 'btn btn-sm btn-secondary', function () {
            var copy = Object.assign({}, row);
            delete copy.id;
            copy.name = (row.name || 'Head') + ' copy';
            request('POST', API + 'fixtures', copy).then(addFixtureRow).then(refreshStatus).catch(showError);
        }));
        actions.appendChild(makeButton('Delete', 'btn btn-sm btn-outline-danger', function () {
            request('DELETE', API + 'fixtures/' + encodeURIComponent(row.id)).then(function () {
                tr.remove();
                delete fixtureRows[row.id];
                delete fixtureBadges[row.id];
                refreshStatus();
            }).catch(showError);
        }));
        tr.appendChild(makeCell(actions));

        byId('skyGuardFixtureRows').appendChild(tr);
    }

    function setupFixtures() {
        byId('skyGuardFixtureAdd').addEventListener('click', function () {
            request('POST', API + 'fixtures', { name: 'Head ' + (Object.keys(fixtureRows).length + 1) })
                .then(addFixtureRow).then(refreshStatus).catch(showError);
        });
        return request('GET', API + 'fixtures').then(function (doc) {
            doc.fixtures.forEach(addFixtureRow);
        }).catch(showError);
    }

    function runCommand(name, args) {
        return request('POST', 'api/command/' + encodeURIComponent(name), args);
    }

    /* ---- matrix message ---- */

    function updatePreview() {
        var text = byId('skyGuardMessage').value;
        Object.keys(PLACEHOLDERS).forEach(function (key) {
            text = text.split('{' + key + '}').join(PLACEHOLDERS[key]);
        });
        byId('skyGuardPreview').textContent = text;
    }

    function setupFreeText(id, key) {
        var field = byId(id);
        field.addEventListener('change', function () {
            if (hasUnsafeCharacters(field.value)) {
                toast('Please leave out double quotes and backslashes.', 'danger');
                return;
            }
            saveSetting(key, field.value).then(function () {
                toast('Saved.');
            }).catch(showError);
        });
        return readSetting(key).then(function (value) {
            field.value = value === null ? (DEFAULTS[key] || '') : value;
            updatePreview();
        });
    }

    function setupPlaceholders() {
        var box = byId('skyGuardPlaceholders');
        var field = byId('skyGuardMessage');
        Object.keys(PLACEHOLDERS).forEach(function (key) {
            box.appendChild(makeButton('{' + key + '}', 'btn btn-sm btn-secondary', function () {
                var start = field.selectionStart === null ? field.value.length : field.selectionStart;
                var end = field.selectionEnd === null ? field.value.length : field.selectionEnd;
                field.value = field.value.slice(0, start) + '{' + key + '}' + field.value.slice(end);
                updatePreview();
                field.dispatchEvent(new Event('change'));
                field.focus();
            }));
        });
        field.addEventListener('input', updatePreview);
    }

    function setupModels() {
        var box = byId('skyGuardModels');
        return Promise.all([
            request('GET', 'api/overlays/models'),
            readSetting('matrix_models')
        ]).then(function (results) {
            var chosen = (results[1] || '').split(',').map(function (name) {
                return name.trim();
            }).filter(Boolean);
            var models = Array.isArray(results[0]) ? results[0] : [];
            if (!models.length) {
                box.textContent = 'FPP has no overlay models.';
                return;
            }
            models.forEach(function (model, index) {
                var wrap = document.createElement('div');
                wrap.className = 'd-flex align-items-center gap-2 mb-1';
                var input = document.createElement('input');
                input.type = 'checkbox';
                input.className = 'form-check-input m-0 flex-shrink-0';
                input.id = 'skyGuardModel' + index;
                input.value = model.Name;
                input.checked = chosen.indexOf(model.Name) !== -1;
                input.addEventListener('change', function () {
                    var names = [];
                    box.querySelectorAll('input:checked').forEach(function (item) {
                        names.push(item.value);
                    });
                    saveSetting('matrix_models', names.join(',')).catch(showError);
                    if (messageTestOn) {
                        startLiveView();
                    }
                });
                var label = document.createElement('label');
                label.className = 'form-check-label mb-0';
                label.htmlFor = input.id;
                label.textContent = model.Name;
                wrap.appendChild(input);
                wrap.appendChild(label);
                box.appendChild(wrap);
            });
            if (messageTestOn) {
                startLiveView();
            }
        }).catch(function () {
            box.textContent = 'Could not read the overlay models from FPP.';
        });
    }

    /* ---- live view of a panel during a test ---- */

    var LIVE_VIEW_POLL_MS = 250;
    /* A status read just after pressing a button can be from before the daemon acted. */
    var MESSAGE_TEST_SETTLE_MS = 3000;
    var liveViewRun = 0;
    var messageTestOn = false;
    var messageTestSettleUntil = 0;

    function pixelsInFrame(data, perPixel) {
        var total = 0;
        for (var i = 0; i < data.length; i += perPixel + 1) {
            total += data[i];
        }
        return total;
    }

    /* FPP's run-length form: a count, then that pixel's channels, repeated. FPP 9.5 always
       sends three channels, whatever the model has. */
    function drawFrame(canvas, data, modelChannels) {
        var total = canvas.width * canvas.height;
        var perPixel = pixelsInFrame(data, modelChannels) === total ? modelChannels : 3;
        var context = canvas.getContext('2d');
        var image = context.createImageData(canvas.width, canvas.height);
        var out = image.data;
        var pixel = 0;
        for (var i = 0; i + perPixel < data.length; i += perPixel + 1) {
            var red = data[i + 1];
            var green = perPixel >= 3 ? data[i + 2] : red;
            var blue = perPixel >= 3 ? data[i + 3] : red;
            for (var n = 0; n < data[i] && pixel < total; n++, pixel++) {
                out[pixel * 4] = red;
                out[pixel * 4 + 1] = green;
                out[pixel * 4 + 2] = blue;
            }
        }
        for (var alpha = 3; alpha < out.length; alpha += 4) {
            out[alpha] = 255;
        }
        context.putImageData(image, 0, 0);
    }

    function stopLiveView() {
        liveViewRun++;
        byId('skyGuardLive').hidden = true;
    }

    /* Reads the panel's real pixels, so it shows what FPP holds, for only as long as the test. */
    function startLiveView() {
        var panel = byId('skyGuardModels').querySelector('input:checked');
        stopLiveView();
        if (!panel) {
            return;
        }
        var name = panel.value;
        var run = liveViewRun;
        var box = byId('skyGuardLive');
        var canvas = byId('skyGuardLiveCanvas');
        var path = 'api/overlays/model/' + encodeURIComponent(name);

        request('GET', path).then(function (model) {
            if (run !== liveViewRun || !model.width || !model.height) {
                return;
            }
            var channels = parseInt(model.ChannelCountPerNode, 10) || 3;
            canvas.width = model.width;
            canvas.height = model.height;
            byId('skyGuardLiveLabel').textContent = 'Live view of ' + name;
            box.hidden = false;

            function poll() {
                if (run !== liveViewRun) {
                    return;
                }
                if (document.hidden) {
                    setTimeout(poll, LIVE_VIEW_POLL_MS);
                    return;
                }
                request('GET', path + '/data/rle').then(function (frame) {
                    if (run === liveViewRun && Array.isArray(frame.data)) {
                        drawFrame(canvas, frame.data, channels);
                    }
                }).catch(function () {
                    /* The next poll tries again. */
                }).then(function () {
                    setTimeout(poll, LIVE_VIEW_POLL_MS);
                });
            }
            poll();
        }).catch(function () {
            /* The test itself still ran; there's just no picture of it. */
        });
    }

    function showMessageTest(on) {
        if (on === messageTestOn) {
            return;
        }
        messageTestOn = on;
        byId('skyGuardMatrixStop').hidden = !on;
        byId('skyGuardMatrixTest').disabled = on;
        if (on) {
            startLiveView();
        } else {
            stopLiveView();
        }
    }

    function setupMessageTest() {
        byId('skyGuardMatrixTest').addEventListener('click', function () {
            var message = byId('skyGuardMessage').value;
            if (!byId('skyGuardModels').querySelector('input:checked')) {
                toast('Tick at least one panel first.', 'danger');
            } else if (!message.trim()) {
                toast('Enter a message first.', 'danger');
            } else if (hasUnsafeCharacters(message)) {
                toast('Please leave out double quotes and backslashes.', 'danger');
            } else {
                /* Leaving the message box to press this button saves it, so wait for that. */
                lastSave.catch(function () {
                    return null;
                }).then(function () {
                    return runCommand('SkyGuard Test Message', []);
                }).then(function () {
                    toast('Showing the message until you press Stop.');
                    messageTestSettleUntil = Date.now() + MESSAGE_TEST_SETTLE_MS;
                    showMessageTest(true);
                }).catch(showError);
            }
        });
        byId('skyGuardMatrixStop').addEventListener('click', function () {
            runCommand('SkyGuard Stop Test Message', []).then(function () {
                toast('Test message stopped.');
                messageTestSettleUntil = Date.now() + MESSAGE_TEST_SETTLE_MS;
                showMessageTest(false);
            }).catch(showError);
        });
    }

    function setupColorAndFont() {
        var color = byId('skyGuardColor');
        var font = byId('skyGuardFont');
        color.addEventListener('change', function () {
            saveSetting('matrix_color', color.value).catch(showError);
        });
        font.addEventListener('change', function () {
            saveSetting('matrix_font', font.value).catch(showError);
        });
        var automatic = document.createElement('option');
        automatic.value = '';
        automatic.textContent = 'Automatic';
        font.appendChild(automatic);
        return Promise.all([
            request('GET', 'api/overlays/fonts'),
            readSetting('matrix_font'),
            readSetting('matrix_color')
        ]).then(function (results) {
            (Array.isArray(results[0]) ? results[0] : []).forEach(function (name) {
                var option = document.createElement('option');
                option.value = name;
                option.textContent = name;
                font.appendChild(option);
            });
            font.value = results[1] || '';
            color.value = results[2] || DEFAULTS.matrix_color;
        }).catch(function () {
            /* The Automatic font and the default color still work. */
        });
    }

    /* ---- the map on the Location & Zone tab ---- */

    /* Made when the tab first opens, so no map tiles load until someone looks at it. */
    function setupLocationMap() {
        var link = document.querySelector('a[href="#skyGuardTabLocation"]');
        link.addEventListener('shown.bs.tab', function () {
            if (locationMap) {
                locationMap.resize();
                return;
            }
            readSetting('map_tile_url').then(function (value) {
                locationMap = SkyGuardMap.create({
                    box: byId('skyGuardLocationMap'),
                    note: byId('skyGuardLocationMapNote'),
                    legend: byId('skyGuardLocationMapLegend'),
                    tiles: SkyGuardMap.usableTiles(value),
                    aircraft: false,
                    units: function () {
                        return 'imperial';
                    }
                });
                if (latestStatus) {
                    locationMap.update(latestStatus);
                }
            }).catch(showError);
        });
    }

    /* ---- first-run setup ---- */

    function showTab(id) {
        var link = document.querySelector('a[href="#' + id + '"]');
        if (link && window.bootstrap) {
            bootstrap.Tab.getOrCreateInstance(link).show();
        }
    }

    function setupWizard(startState) {
        var box = byId('skyGuardSetup');
        var actions = byId('skyGuardSetupActions');

        function chooseSource(mode, next) {
            var field = byId('source_mode');
            saveSetting('source_mode', mode).then(function () {
                if (field) {
                    field.value = mode;
                }
                updateVisibility();
                next();
            }).catch(showError);
        }

        var steps = [
            {
                title: 'Personal use only',
                text: 'SkyGuard is for personal use only. Its flight data services allow personal, non-commercial use. SkyGuard stays idle until you confirm this.',
                skippable: false,
                buttons: [{
                    label: 'I confirm this is for personal use',
                    primary: true,
                    run: function (next) {
                        saveSetting('personal_use_confirmed', '1').then(function () {
                            refreshStatus();
                            next();
                        }).catch(showError);
                    }
                }]
            },
            {
                title: 'Data feed',
                text: 'Do you have your own ADS-B receiver? Online data alone depends on a free service: if it has a problem during a show, the heads stay dark until data returns.',
                tab: 'skyGuardTabData',
                skippable: true,
                buttons: [
                    { label: 'No, use online data', primary: true, run: function (next) { chooseSource('online', next); } },
                    { label: 'Yes, with online data as a check and a backup', run: function (next) { chooseSource('both', next); } },
                    { label: 'Yes, my receiver only', run: function (next) { chooseSource('receiver', next); } }
                ]
            },
            {
                title: 'Location & zone',
                text: 'Set the center point and enter a radius for each data source you use. SkyGuard stays idle until you do. You can also fill in anything else on this tab.',
                tab: 'skyGuardTabLocation',
                skippable: true
            },
            {
                title: 'Fixtures',
                text: 'Add one row per moving head, with its dimmer and shutter channels.',
                tab: 'skyGuardTabFixtures',
                skippable: true
            },
            {
                title: 'Matrix message',
                text: 'Optionally show a message on your LED panels during a blackout.',
                tab: 'skyGuardTabMatrix',
                skippable: true
            },
            {
                title: 'Test run',
                text: 'This blacks out every head for 5 seconds, so you can check they respond. Watch your heads, then finish.',
                skippable: true,
                buttons: [{
                    label: 'Run the test',
                    run: function () {
                        runCommand('SkyGuard Test Trigger', ['5']).then(function () {
                            toast('Testing for 5 seconds.');
                        }).catch(showError);
                    }
                }]
            }
        ];

        function finish() {
            saveSetting('setup_state', SETUP_DONE).catch(showError);
            box.hidden = true;
        }

        function show(index) {
            if (index >= steps.length) {
                finish();
                return;
            }
            var step = steps[index];
            saveSetting('setup_state', String(index)).catch(showError);
            box.hidden = false;
            byId('skyGuardSetupProgress').textContent = 'Step ' + (index + 1) + ' of ' + steps.length;
            byId('skyGuardSetupTitle').textContent = step.title;
            byId('skyGuardSetupText').textContent = step.text;
            if (step.tab) {
                showTab(step.tab);
            }
            actions.replaceChildren();
            function next() {
                show(index + 1);
            }
            (step.buttons || []).forEach(function (button) {
                actions.appendChild(makeButton(
                    button.label,
                    button.primary ? 'btn btn-primary' : 'btn btn-outline-primary',
                    function () {
                        button.run(next);
                    }
                ));
            });
            if (!step.buttons || index === steps.length - 1) {
                actions.appendChild(makeButton(
                    index === steps.length - 1 ? 'Finish' : 'Next',
                    'btn btn-primary',
                    next
                ));
            }
            if (step.skippable && index !== steps.length - 1) {
                actions.appendChild(makeButton('Skip', 'btn btn-link', next));
            }
        }

        var saved = parseInt(startState, 10);
        return readSetting('personal_use_confirmed').then(function (confirmed) {
            /* The plugin stays idle without the confirmation, whatever step was saved. */
            if (confirmed !== '1') {
                show(0);
            } else if (startState !== SETUP_DONE) {
                show(isNaN(saved) ? 1 : Math.max(saved, 1));
            }
        });
    }

    /* ---- start ---- */

    function start() {
        var root = byId('skyGuardSettings');
        if (!root) {
            return;
        }

        window.skyGuardPage = {
            settingSaved: function () {
                updateVisibility();
                refreshStatus();
            }
        };

        updateVisibility();
        setupLocation();
        var showKeyState = setupApiKey();
        setupFixtures();
        setupPlaceholders();
        setupFreeText('skyGuardReceiverUrl', 'receiver_url');
        setupFreeText('skyGuardReceiver978Url', 'receiver_978_url');
        setupFreeText('skyGuardMessage', 'matrix_message');
        setupFreeText('skyGuardNoData', 'matrix_nodata_message');
        setupModels();
        setupColorAndFont();
        setupMessageTest();
        setupFreeText('skyGuardTileUrl', 'map_tile_url');
        setupLocationMap();

        runTest('skyGuardTestReceiver', 'skyGuardTestReceiverResult', API + 'test-receiver',
            urlBody('skyGuardReceiverUrl', 'skyGuardTestReceiverResult'));
        runTest('skyGuardTest978', 'skyGuardTest978Result', API + 'test-receiver',
            urlBody('skyGuardReceiver978Url', 'skyGuardTest978Result'));
        runTest('skyGuardTestOnline', 'skyGuardTestOnlineResult', API + 'test-online', function () {
            return { provider: byId('online_provider').value };
        });

        request('GET', API + 'private').then(function (doc) {
            byId('skyGuardLat').value = doc.center_lat === null ? '' : doc.center_lat;
            byId('skyGuardLon').value = doc.center_lon === null ? '' : doc.center_lon;
            showKeyState(doc.api_key_set);
        }).catch(showError);

        refreshStatus();
        setInterval(refreshStatus, 5000);
        setupWizard(root.getAttribute('data-setup-state'));
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
}());

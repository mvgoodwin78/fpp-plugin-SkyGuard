/* The map shared by the status page and the Location & Zone tab: Leaflet over map tiles when
   they load, otherwise a radar plot drawn from the same data. Aircraft callsigns come from
   outside, so everything is written as text or built as elements, never as HTML. */
(function () {
    'use strict';

    var DEFAULT_TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
    var TILE_WAIT_MS = 10000;
    var NO_TILES_NOTE = 'The map could not load, so this is a radar plot instead.';
    var METERS = { mi: 1609.344, nm: 1852, km: 1000 };
    var FEET_TO_METERS = 0.3048;
    var KNOTS_TO_KMH = 1.852;
    var METERS_PER_DEGREE = 111320;
    var SVG = 'http://www.w3.org/2000/svg';

    function svgElement(name, attributes) {
        var element = document.createElementNS(SVG, name);
        Object.keys(attributes || {}).forEach(function (key) {
            element.setAttribute(key, attributes[key]);
        });
        return element;
    }

    function formatNumber(value) {
        return Math.round(value).toLocaleString();
    }

    /* What the page shows for one aircraft, in the units chosen. */
    function describe(aircraft, units, radiusUnit) {
        var metric = units === 'metric';
        var altitude = 'Unknown';
        if (aircraft.alt_ft === 'ground') {
            altitude = 'Ground';
        } else if (typeof aircraft.alt_ft === 'number') {
            altitude = metric ? formatNumber(aircraft.alt_ft * FEET_TO_METERS) + ' m' : formatNumber(aircraft.alt_ft) + ' ft';
        }
        var speed = 'Unknown';
        if (typeof aircraft.gs_kt === 'number') {
            speed = metric ? formatNumber(aircraft.gs_kt * KNOTS_TO_KMH) + ' km/h' : formatNumber(aircraft.gs_kt) + ' kt';
        }
        var distance = 'No position';
        if (typeof aircraft.distance === 'number' && METERS[radiusUnit]) {
            var meters = aircraft.distance * METERS[radiusUnit];
            distance = metric ? (meters / METERS.km).toFixed(1) + ' km' : (meters / METERS.mi).toFixed(1) + ' mi';
        }
        var zone = 'No';
        if (aircraft.ground_ignored) {
            zone = 'ground (ignored)';
        } else if (aircraft.in_zone) {
            zone = 'Yes';
        }
        if (aircraft.online_only) {
            zone += ', receiver missed it';
        }
        return {
            name: aircraft.callsign || aircraft.hex || 'Unknown',
            altitude: altitude,
            speed: speed,
            distance: distance,
            zone: zone
        };
    }

    function hasPosition(aircraft) {
        return typeof aircraft.lat === 'number' && typeof aircraft.lon === 'number';
    }

    /* Each radius that applies to the data sources chosen, whichever feed is in use right now.
       The one in use is what decides a blackout, so it's the solid one. */
    function radii(status) {
        var radius = status.radius || {};
        var list = [];
        function add(kind, label, value) {
            if (typeof value === 'number' && METERS[radius.unit]) {
                list.push({
                    kind: kind,
                    label: label,
                    text: value + ' ' + radius.unit,
                    meters: value * METERS[radius.unit],
                    active: status.active_feed === kind
                });
            }
        }
        if (status.source_mode !== 'receiver') {
            add('online', 'Online radius', radius.online);
        }
        if (status.source_mode !== 'online') {
            add('receiver', 'Receiver radius', radius.receiver);
        }
        return list;
    }

    function largestMeters(list) {
        return list.reduce(function (largest, item) {
            return Math.max(largest, item.meters);
        }, 0);
    }

    function showLegend(box, list) {
        if (!box) {
            return;
        }
        box.replaceChildren();
        list.forEach(function (item) {
            var entry = document.createElement('span');
            entry.className = 'skyguard-legend-item';
            var swatch = document.createElement('span');
            swatch.className = 'skyguard-swatch skyguard-swatch-' + item.kind + (item.active ? '' : ' skyguard-swatch-standby');
            entry.appendChild(swatch);
            entry.appendChild(document.createTextNode(item.label + ': ' + item.text + (item.active ? ' (in use now)' : '')));
            box.appendChild(entry);
        });
    }

    function hasCenter(status) {
        var center = status.center || {};
        return typeof center.lat === 'number' && typeof center.lon === 'number';
    }

    function planeClass(aircraft, zoneClass, ignoredClass) {
        if (aircraft.ground_ignored) {
            return ignoredClass;
        }
        return aircraft.in_zone ? zoneClass : '';
    }

    /* A7 is the ADS-B category for rotorcraft. An aircraft that doesn't report one gets the plane. */
    function isHelicopter(aircraft) {
        return aircraft.category === 'A7';
    }

    /* Seen from above, nose up: crossed rotor blades over the body and tail. */
    var HELICOPTER_SHAPE = 'M-8.85 -9.15 L-7.15 -10.85 L8.85 5.15 L7.15 6.85 Z ' +
        'M7.15 -10.85 L8.85 -9.15 L-7.15 6.85 L-8.85 5.15 Z ' +
        'M0 -7 C2.5 -7 3.5 -4.5 3.5 -2 C3.5 0.5 2 2.5 1 3.5 V8.5 H4 V11 H-4 V8.5 H-1 V3.5 C-2 2.5 -3.5 0.5 -3.5 -2 C-3.5 -4.5 -2.5 -7 0 -7 Z';

    /* Labels on every aircraft hide each other, so only the ones that matter have one to start with. */
    function labelShown(aircraft, opened) {
        return aircraft.in_zone || opened[aircraft.hex] === true;
    }

    /* ---- the radar plot ---- */

    function drawRadar(box, status, options, opened, toggle) {
        var plot = svgElement('svg', {
            'class': 'skyguard-radar',
            viewBox: '-110 -110 220 220',
            role: 'img',
            'aria-label': 'Radar plot of aircraft near the center point'
        });
        var list = radii(status);
        var largest = largestMeters(list);
        var center = status.center;
        /* The largest radius is halfway out, leaving room for aircraft approaching it. */
        var scale = largest ? 50 / largest : null;

        function text(x, y, value) {
            var label = svgElement('text', { 'class': 'skyguard-radar-text', x: x, y: y });
            label.textContent = value;
            plot.appendChild(label);
        }

        plot.appendChild(svgElement('line', { 'class': 'skyguard-radar-axis', x1: -100, y1: 0, x2: 100, y2: 0 }));
        plot.appendChild(svgElement('line', { 'class': 'skyguard-radar-axis', x1: 0, y1: -100, x2: 0, y2: 100 }));
        plot.appendChild(svgElement('circle', { 'class': 'skyguard-radar-ring', r: 100 }));
        list.forEach(function (item) {
            var ring = item.meters * scale;
            plot.appendChild(svgElement('circle', {
                'class': 'skyguard-radar-zone skyguard-zone-' + item.kind + (item.active ? '' : ' skyguard-radar-zone-standby'),
                r: ring
            }));
            text(2, -ring - 2, item.text);
        });
        plot.appendChild(svgElement('circle', { 'class': 'skyguard-radar-center', r: 2 }));
        text(-8, -104, 'N');

        if (scale && options.aircraft !== false) {
            var perLon = METERS_PER_DEGREE * Math.cos(center.lat * Math.PI / 180);
            (status.aircraft || []).filter(hasPosition).forEach(function (aircraft) {
                var x = (aircraft.lon - center.lon) * perLon * scale;
                var y = (center.lat - aircraft.lat) * METERS_PER_DEGREE * scale;
                if (Math.sqrt(x * x + y * y) > 104) {
                    return;
                }
                var track = typeof aircraft.track_deg === 'number' ? aircraft.track_deg : 0;
                var cls = planeClass(aircraft, 'skyguard-radar-plane-zone', 'skyguard-radar-plane-ignored');
                var helicopter = isHelicopter(aircraft);
                plot.appendChild(svgElement('path', {
                    'class': ('skyguard-radar-plane ' + cls).trim(),
                    d: helicopter ? HELICOPTER_SHAPE : 'M0 -4 L3 3 L0 1.5 L-3 3 Z',
                    transform: 'translate(' + x + ' ' + y + ') rotate(' + track + ')' + (helicopter ? ' scale(0.5)' : '')
                }));
                /* Larger than the shape, so a finger can hit it. */
                var target = svgElement('circle', { 'class': 'skyguard-radar-target', cx: x, cy: y, r: 8 });
                target.addEventListener('click', function () {
                    toggle(aircraft.hex);
                });
                plot.appendChild(target);
                if (labelShown(aircraft, opened)) {
                    var info = describe(aircraft, options.units(), status.radius.unit);
                    text(x + 5, y + 2, info.name + (aircraft.ground_ignored ? ' ground (ignored)' : ''));
                }
            });
        }
        box.replaceChildren(plot);
    }

    /* ---- the Leaflet map ---- */

    function planeIcon(aircraft) {
        var wrap = document.createElement('div');
        wrap.className = ('skyguard-plane ' + planeClass(aircraft, 'skyguard-plane-zone', 'skyguard-plane-ignored')).trim();
        var drawing = svgElement('svg', { viewBox: '-12 -12 24 24' });
        var track = typeof aircraft.track_deg === 'number' ? aircraft.track_deg : 0;
        drawing.appendChild(svgElement('path', {
            'class': 'skyguard-plane-shape' + (isHelicopter(aircraft) ? ' skyguard-helicopter-shape' : ''),
            d: isHelicopter(aircraft) ? HELICOPTER_SHAPE : 'M0 -11 L2 -3 L10 3 L10 5 L2 2 L1.5 7 L4 9 L4 10.5 L0 9.5 L-4 10.5 L-4 9 L-1.5 7 L-2 2 L-10 5 L-10 3 L-2 -3 Z',
            transform: 'rotate(' + track + ')'
        }));
        wrap.appendChild(drawing);
        return L.divIcon({ html: wrap, className: '', iconSize: [24, 24], iconAnchor: [12, 12] });
    }

    function planeLabel(aircraft, info) {
        var label = document.createElement('div');
        var name = document.createElement('div');
        name.className = 'fw-semibold';
        name.textContent = info.name;
        var detail = document.createElement('div');
        detail.textContent = info.altitude + ' · ' + info.speed;
        label.appendChild(name);
        label.appendChild(detail);
        if (aircraft.ground_ignored) {
            var note = document.createElement('div');
            note.textContent = 'ground (ignored)';
            label.appendChild(note);
        }
        if (aircraft.online_only) {
            var missed = document.createElement('div');
            missed.textContent = 'receiver missed it';
            label.appendChild(missed);
        }
        return label;
    }

    /* options: box (element), note and legend (elements or null), tiles (address, or '' for radar only),
       aircraft (false to leave them off), units (function returning 'imperial' or 'metric'). */
    function create(options) {
        var box = options.box;
        var map = null;
        var layers = null;
        var viewKey = null;
        var last = null;
        var usingRadar = false;
        var tileTimer = null;
        var opened = {}; /* aircraft whose label was opened by hand, kept through each refresh */

        function toggleLabel(hex) {
            opened[hex] = !opened[hex];
            if (last) {
                draw(last);
            }
        }

        function setNote(text) {
            if (options.note) {
                options.note.textContent = text;
            }
        }

        function useRadar(reason) {
            if (usingRadar) {
                return;
            }
            usingRadar = true;
            clearTimeout(tileTimer);
            if (map) {
                map.remove();
                map = null;
            }
            setNote(reason);
            if (last) {
                draw(last);
            }
        }

        function startMap() {
            map = L.map(box, {
                scrollWheelZoom: false,
                dragging: !L.Browser.mobile /* so a finger on the map still scrolls the page */
            });
            map.attributionControl.setPrefix('');
            layers = L.layerGroup().addTo(map);
            var loaded = false;
            var tiles = L.tileLayer(options.tiles, {
                maxZoom: 19,
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'
            });
            tiles.on('tileload', function () {
                loaded = true;
                clearTimeout(tileTimer);
            });
            tiles.on('tileerror', function () {
                if (!loaded) {
                    useRadar(NO_TILES_NOTE);
                }
            });
            tileTimer = setTimeout(function () {
                if (!loaded) {
                    useRadar(NO_TILES_NOTE);
                }
            }, TILE_WAIT_MS);
            return tiles;
        }

        function drawMap(status) {
            var center = [status.center.lat, status.center.lon];
            var list = radii(status);
            var largest = largestMeters(list);
            var key = center.join(',') + '/' + largest;
            if (key !== viewKey) {
                viewKey = key;
                if (largest) {
                    map.fitBounds(L.latLng(center).toBounds(largest * 2.4));
                } else {
                    map.setView(center, 10);
                }
            }
            layers.clearLayers();
            list.forEach(function (item) {
                L.circle(center, {
                    radius: item.meters,
                    className: 'skyguard-zone skyguard-zone-' + item.kind,
                    weight: 2,
                    dashArray: item.active ? null : '6 6',
                    fillOpacity: item.active ? 0.08 : 0,
                    interactive: false
                }).addTo(layers);
            });
            L.circleMarker(center, { radius: 5, className: 'skyguard-center-dot', weight: 2, fillOpacity: 1, interactive: false }).addTo(layers);
            if (options.aircraft !== false) {
                (status.aircraft || []).filter(hasPosition).forEach(function (aircraft) {
                    var info = describe(aircraft, options.units(), status.radius.unit);
                    var marker = L.marker([aircraft.lat, aircraft.lon], { icon: planeIcon(aircraft), keyboard: false });
                    if (labelShown(aircraft, opened)) {
                        marker.bindTooltip(planeLabel(aircraft, info), { permanent: true, direction: 'right', offset: [12, 0], className: 'skyguard-label' });
                    }
                    marker.on('click', function () {
                        toggleLabel(aircraft.hex);
                    });
                    marker.addTo(layers);
                });
            }
        }

        function draw(status) {
            if (!hasCenter(status)) {
                if (map) {
                    map.remove();
                    map = null;
                    viewKey = null;
                }
                clearTimeout(tileTimer);
                box.replaceChildren();
                showLegend(options.legend, []);
                setNote('Set a center point to see the map.');
                return;
            }
            showLegend(options.legend, radii(status));
            var newTiles = null;
            if (!usingRadar && !map) {
                if (!options.tiles || !window.L) {
                    usingRadar = true;
                    setNote('Showing the radar plot, because the map address is empty.');
                } else {
                    box.replaceChildren();
                    setNote('');
                    newTiles = startMap();
                }
            }
            if (usingRadar) {
                drawRadar(box, status, options, opened, toggleLabel);
            } else {
                drawMap(status);
                /* After the view is set, so tiles load only for where the map really is. */
                if (newTiles) {
                    newTiles.addTo(map);
                }
            }
        }

        return {
            update: function (status) {
                last = status;
                draw(status);
            },
            resize: function () {
                if (map) {
                    map.invalidateSize();
                }
            }
        };
    }

    window.SkyGuardMap = {
        DEFAULT_TILES: DEFAULT_TILES,
        create: create,
        describe: describe,
        /* Never saved means the default. Empty, or not a usable tile address, means no outside map. */
        usableTiles: function (value) {
            if (value === null || value === undefined) {
                return DEFAULT_TILES;
            }
            var usable = /^https:\/\/[^"'<>\s]+$/.test(value) && value.indexOf('{z}') !== -1 &&
                value.indexOf('{x}') !== -1 && value.indexOf('{y}') !== -1;
            return usable ? value : '';
        }
    };
}());

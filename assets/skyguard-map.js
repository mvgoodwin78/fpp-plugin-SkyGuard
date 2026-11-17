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
            /* With a show elevation set, also the height above the show, which the limits use. */
            if (typeof aircraft.height_ft === 'number' && aircraft.height_ft !== aircraft.alt_ft) {
                altitude += ' (' + (metric ? formatNumber(aircraft.height_ft * FEET_TO_METERS) + ' m' : formatNumber(aircraft.height_ft) + ' ft') + ' above show)';
            }
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
        var zones = (aircraft.zones || []).map(function (id) {
            return ZONE_NAMES[id] || id;
        });
        if (zones.length) {
            zone += ' (' + zones.join(', ') + ')';
        }
        return {
            name: aircraft.callsign || aircraft.hex || 'Unknown',
            altitude: altitude,
            speed: speed,
            distance: distance,
            zone: zone
        };
    }

    var ZONE_NAMES = { receiver1: 'receiver zone 1', receiver2: 'receiver zone 2', online: 'online zone' };

    function hasPosition(aircraft) {
        return typeof aircraft.lat === 'number' && typeof aircraft.lon === 'number';
    }

    var ZONE_KINDS = { receiver1: 'receiver', receiver2: 'receiver2', online: 'online' };

    function formatFeet(value) {
        return Math.round(value).toLocaleString() + ' ft';
    }

    /* Each zone that applies to the data sources chosen, whichever feed is in use right now: a box
       reaching its own distance north, south, east and west of center. The ones in use decide a
       blackout, so they're solid. */
    function zoneList(status) {
        var unit = status.unit;
        var list = [];
        if (!METERS[unit]) {
            return list;
        }
        (status.zones || []).forEach(function (zone) {
            if ((zone.feed === 'online' && status.source_mode === 'receiver') ||
                (zone.feed === 'receiver' && status.source_mode === 'online')) {
                return;
            }
            var m = METERS[unit];
            list.push({
                kind: ZONE_KINDS[zone.id] || 'receiver',
                label: zone.label,
                text: 'N ' + zone.north + ', S ' + zone.south + ', E ' + zone.east + ', W ' + zone.west + ' ' + unit +
                    '; ' + formatFeet(zone.alt_floor_ft) + ' to ' + formatFeet(zone.alt_ceiling_ft),
                north: zone.north * m,
                south: zone.south * m,
                east: zone.east * m,
                west: zone.west * m,
                active: zone.active === true
            });
        });
        return list;
    }

    /* How far the farthest corner of any zone is from center, in meters. */
    function largestMeters(list) {
        return list.reduce(function (largest, item) {
            return Math.max(largest, Math.sqrt(Math.pow(Math.max(item.north, item.south), 2) + Math.pow(Math.max(item.east, item.west), 2)));
        }, 0);
    }

    /* How far out aircraft are shown, in meters, or 0. A map without aircraft ignores it. */
    function rangeMeters(status, options) {
        var unit = status.unit;
        if (options.aircraft === false || typeof status.map_range !== 'number' || !METERS[unit]) {
            return 0;
        }
        return status.map_range * METERS[unit];
    }

    /* Range rings: a round step that gives three or four rings out to the map range, which is
       always the outer ring. Distances are in the radius unit. */
    var RING_STEPS = [0.25, 0.5, 1, 2, 2.5, 5, 10, 15, 20, 25, 50, 100];

    function rangeRings(status, options) {
        if (!rangeMeters(status, options)) {
            return null;
        }
        var range = status.map_range;
        var step = RING_STEPS[RING_STEPS.length - 1];
        for (var i = 0; i < RING_STEPS.length; i++) {
            if (RING_STEPS[i] >= range / 4) {
                step = RING_STEPS[i];
                break;
            }
        }
        var rings = [];
        for (var d = step; d < range - step / 4; d += step) {
            rings.push(d);
        }
        rings.push(range);
        return { unit: status.unit, step: step, range: range, rings: rings };
    }

    function legendEntry(box, swatchClass, text) {
        var entry = document.createElement('span');
        entry.className = 'skyguard-legend-item';
        var swatch = document.createElement('span');
        swatch.className = swatchClass;
        entry.appendChild(swatch);
        entry.appendChild(document.createTextNode(text));
        box.appendChild(entry);
    }

    /* The zones, and on the scope what each blip and ring means. */
    function showLegend(box, list, scope) {
        if (!box) {
            return;
        }
        box.replaceChildren();
        box.classList.toggle('skyguard-scope-legend', !!scope);
        var zonesRow = box;
        if (scope) {
            var blips = document.createElement('div');
            legendEntry(blips, 'skyguard-key-blip', 'Aircraft');
            legendEntry(blips, 'skyguard-key-blip skyguard-key-blip-zone', 'In the zone');
            legendEntry(blips, 'skyguard-key-blip skyguard-key-blip-ignored', 'On the ground, ignored');
            legendEntry(blips, 'skyguard-key-blip skyguard-key-helicopter', 'Helicopter');
            legendEntry(blips, 'skyguard-key-missed', 'Receiver missed it (online data only)');
            legendEntry(blips, 'skyguard-key-heading', 'Heading');
            box.appendChild(blips);
            zonesRow = document.createElement('div');
            box.appendChild(zonesRow);
        }
        list.forEach(function (item) {
            legendEntry(zonesRow, 'skyguard-swatch skyguard-swatch-' + item.kind + (item.active ? '' : ' skyguard-swatch-standby'),
                item.label + ': ' + item.text + (item.active ? ' (in use now)' : ''));
        });
        if (scope && scope.rings) {
            legendEntry(zonesRow, 'skyguard-swatch skyguard-swatch-range',
                'Range rings every ' + scope.rings.step + ' ' + scope.rings.unit + ', out to ' + scope.rings.range + ' ' + scope.rings.unit);
        }
    }

    /* A point the given meters north and east of center. Close enough for drawing at these distances. */
    function offsetLatLng(center, north, east) {
        return [
            center[0] + north / METERS_PER_DEGREE,
            center[1] + east / (METERS_PER_DEGREE * Math.cos(center[0] * Math.PI / 180))
        ];
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

    /* One blip, drawn at 0,0 in the given units: a dot (a diamond for a helicopter), a line
       toward its heading when it reports one, and a ring when only online data saw it. */
    function drawBlip(parent, aircraft, size, classes) {
        if (aircraft.online_only) {
            parent.appendChild(svgElement('circle', { 'class': 'skyguard-missed-ring', r: size * 2.6 }));
        }
        if (typeof aircraft.track_deg === 'number') {
            parent.appendChild(svgElement('line', {
                'class': 'skyguard-blip-heading', x1: 0, y1: 0, x2: 0, y2: -size * 3,
                transform: 'rotate(' + aircraft.track_deg + ')'
            }));
        }
        if (isHelicopter(aircraft)) {
            parent.appendChild(svgElement('path', {
                'class': classes + ' skyguard-helicopter-shape',
                d: 'M0 ' + (-size * 1.3) + ' L' + (size * 1.3) + ' 0 L0 ' + (size * 1.3) + ' L' + (-size * 1.3) + ' 0 Z'
            }));
        } else {
            parent.appendChild(svgElement('circle', { 'class': classes, r: size }));
        }
    }

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
        var list = zoneList(status);
        var largest = largestMeters(list);
        var center = status.center;
        /* The outer ring is the map range. Without one, the farthest zone corner is halfway out,
           leaving room for aircraft approaching it. */
        var outer = Math.max(largest * 2, rangeMeters(status, options));
        var scale = outer ? 100 / outer : null;

        function text(x, y, value) {
            var label = svgElement('text', { 'class': 'skyguard-radar-text', x: x, y: y });
            label.textContent = value;
            plot.appendChild(label);
        }

        plot.appendChild(svgElement('line', { 'class': 'skyguard-radar-axis', x1: -100, y1: 0, x2: 100, y2: 0 }));
        plot.appendChild(svgElement('line', { 'class': 'skyguard-radar-axis', x1: 0, y1: -100, x2: 0, y2: 100 }));
        plot.appendChild(svgElement('circle', { 'class': 'skyguard-radar-ring', r: 100 }));
        var rings = rangeRings(status, options);
        if (rings && scale) {
            rings.rings.slice(0, -1).forEach(function (d) {
                var r = d * METERS[rings.unit] * scale;
                plot.appendChild(svgElement('circle', { 'class': 'skyguard-radar-ring', r: r }));
                text(-r + 1, -1.5, d + ' ' + rings.unit);
            });
            text(-99, -1.5, rings.range + ' ' + rings.unit);
        }
        if (options.aircraft !== false) {
            var sweep = svgElement('g', { 'class': 'skyguard-radar-sweep' });
            sweep.appendChild(svgElement('path', { 'class': 'skyguard-radar-sweep-wedge', d: 'M0 0 L0 -100 A100 100 0 0 1 50 -86.6 Z' }));
            plot.appendChild(sweep);
        }
        list.forEach(function (item) {
            var top = -item.north * scale;
            var left = -item.west * scale;
            plot.appendChild(svgElement('rect', {
                'class': 'skyguard-radar-zone skyguard-zone-' + item.kind + (item.active ? '' : ' skyguard-radar-zone-standby'),
                x: left,
                y: top,
                width: (item.west + item.east) * scale,
                height: (item.north + item.south) * scale
            }));
            text(left + 1, top - 1.5, item.label);
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
                var cls = planeClass(aircraft, 'skyguard-radar-plane-zone', 'skyguard-radar-plane-ignored');
                var blip = svgElement('g', { transform: 'translate(' + x + ' ' + y + ')' });
                drawBlip(blip, aircraft, 2.2, ('skyguard-radar-plane ' + cls).trim());
                plot.appendChild(blip);
                /* Larger than the shape, so a finger can hit it. */
                var target = svgElement('circle', { 'class': 'skyguard-radar-target', cx: x, cy: y, r: 8 });
                target.addEventListener('click', function () {
                    toggle(aircraft.hex);
                });
                plot.appendChild(target);
                if (labelShown(aircraft, opened)) {
                    var info = describe(aircraft, options.units(), status.unit);
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
        drawBlip(drawing, aircraft, 3.5, 'skyguard-blip-dot');
        wrap.appendChild(drawing);
        return L.divIcon({ html: wrap, className: '', iconSize: [24, 24], iconAnchor: [12, 12] });
    }

    function rangeLabel(text) {
        var label = document.createElement('span');
        label.textContent = text;
        return L.divIcon({ html: label, className: 'skyguard-range-label', iconSize: null });
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
        var sweep = null;
        var sweepAt = null; /* [center, meters] the sweep is drawn for */
        var opened = {}; /* aircraft whose label was opened by hand, kept through each refresh */
        /* The status map is a radar scope: dark, with the map faint underneath. The Location & Zone
           map has no aircraft and stays a plain map, for placing the center. */
        var scope = options.aircraft !== false;
        box.classList.toggle('skyguard-scope', scope);

        /* The sweep turns over the map range. It lives in its own pane so it pans with the map, and
           is placed again after a zoom. */
        function placeSweep() {
            if (!sweep) {
                return;
            }
            if (!sweepAt) {
                sweep.hidden = true;
                return;
            }
            var middle = map.latLngToLayerPoint(sweepAt[0]);
            var edge = map.latLngToLayerPoint(offsetLatLng(sweepAt[0], sweepAt[1], 0));
            var r = Math.max(0, middle.y - edge.y);
            sweep.hidden = false;
            sweep.style.left = (middle.x - r) + 'px';
            sweep.style.top = (middle.y - r) + 'px';
            sweep.style.width = sweep.style.height = (2 * r) + 'px';
        }

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
                sweep = null;
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
            if (scope) {
                var pane = map.createPane('skyguardSweep');
                pane.style.zIndex = 350;
                pane.style.pointerEvents = 'none';
                sweep = document.createElement('div');
                sweep.className = 'skyguard-sweep leaflet-zoom-hide';
                sweep.hidden = true;
                pane.appendChild(sweep);
                map.on('zoomend viewreset resize', placeSweep);
            }
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
            var list = zoneList(status);
            var largest = largestMeters(list);
            var range = rangeMeters(status, options);
            var key = center.join(',') + '/' + list.map(function (item) {
                return [item.north, item.south, item.east, item.west].join(',');
            }).join('/') + '/' + range;
            if (key !== viewKey) {
                viewKey = key;
                if (largest || range) {
                    map.fitBounds(L.latLng(center).toBounds(Math.max(largest * 2.4, range * 2)));
                } else {
                    map.setView(center, 10);
                }
            }
            layers.clearLayers();
            var rings = rangeRings(status, options);
            if (rings) {
                var outer = rings.range * METERS[rings.unit];
                [[outer, 0, -outer, 0], [0, outer, 0, -outer]].forEach(function (axis) {
                    L.polyline([offsetLatLng(center, axis[0], axis[1]), offsetLatLng(center, axis[2], axis[3])],
                        { className: 'skyguard-range-axis', weight: 1, interactive: false }).addTo(layers);
                });
                rings.rings.forEach(function (d) {
                    var meters = d * METERS[rings.unit];
                    L.circle(center, { radius: meters, className: 'skyguard-range-ring', weight: 1, fill: false, interactive: false }).addTo(layers);
                    L.marker(offsetLatLng(center, meters, 0), { icon: rangeLabel(d + ' ' + rings.unit), interactive: false, keyboard: false }).addTo(layers);
                });
            }
            sweepAt = rings ? [center, rings.range * METERS[rings.unit]] : null;
            placeSweep();
            list.forEach(function (item) {
                L.polygon([
                    offsetLatLng(center, item.north, item.east),
                    offsetLatLng(center, -item.south, item.east),
                    offsetLatLng(center, -item.south, -item.west),
                    offsetLatLng(center, item.north, -item.west)
                ], {
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
                    var info = describe(aircraft, options.units(), status.unit);
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
                    sweep = null;
                    viewKey = null;
                }
                clearTimeout(tileTimer);
                box.replaceChildren();
                showLegend(options.legend, [], null);
                setNote('Set a center point to see the map.');
                return;
            }
            showLegend(options.legend, zoneList(status), scope ? { rings: rangeRings(status, options) } : null);
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

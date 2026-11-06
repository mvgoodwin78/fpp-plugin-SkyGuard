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

    function radiusMeters(status) {
        var radius = status.radius || {};
        return (typeof radius.value === 'number' && METERS[radius.unit]) ? radius.value * METERS[radius.unit] : null;
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

    /* ---- the radar plot ---- */

    function drawRadar(box, status, options) {
        var plot = svgElement('svg', {
            'class': 'skyguard-radar',
            viewBox: '-110 -110 220 220',
            role: 'img',
            'aria-label': 'Radar plot of aircraft near the center point'
        });
        var meters = radiusMeters(status);
        var center = status.center;
        /* The status lists aircraft out to twice the radius, so that's the edge of the plot. */
        var scale = meters ? 50 / meters : null;

        function text(x, y, value) {
            var label = svgElement('text', { 'class': 'skyguard-radar-text', x: x, y: y });
            label.textContent = value;
            plot.appendChild(label);
        }

        plot.appendChild(svgElement('line', { 'class': 'skyguard-radar-axis', x1: -100, y1: 0, x2: 100, y2: 0 }));
        plot.appendChild(svgElement('line', { 'class': 'skyguard-radar-axis', x1: 0, y1: -100, x2: 0, y2: 100 }));
        plot.appendChild(svgElement('circle', { 'class': 'skyguard-radar-ring', r: 100 }));
        if (meters) {
            plot.appendChild(svgElement('circle', { 'class': 'skyguard-radar-zone', r: 50 }));
            text(2, -52, status.radius.value + ' ' + status.radius.unit);
            text(2, -102, (status.radius.value * 2) + ' ' + status.radius.unit);
        }
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
                plot.appendChild(svgElement('path', {
                    'class': ('skyguard-radar-plane ' + cls).trim(),
                    d: 'M0 -4 L3 3 L0 1.5 L-3 3 Z',
                    transform: 'translate(' + x + ' ' + y + ') rotate(' + track + ')'
                }));
                var info = describe(aircraft, options.units(), status.radius.unit);
                text(x + 5, y + 2, info.name + (aircraft.ground_ignored ? ' ground (ignored)' : ''));
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
            'class': 'skyguard-plane-shape',
            d: 'M0 -11 L2 -3 L10 3 L10 5 L2 2 L1.5 7 L4 9 L4 10.5 L0 9.5 L-4 10.5 L-4 9 L-1.5 7 L-2 2 L-10 5 L-10 3 L-2 -3 Z',
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
        return label;
    }

    /* options: box (element), note (element or null), tiles (address, or '' for radar only),
       aircraft (false to leave them off), units (function returning 'imperial' or 'metric'). */
    function create(options) {
        var box = options.box;
        var map = null;
        var layers = null;
        var viewKey = null;
        var last = null;
        var usingRadar = false;
        var tileTimer = null;

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
            var meters = radiusMeters(status);
            var key = center.join(',') + '/' + meters;
            if (key !== viewKey) {
                viewKey = key;
                if (meters) {
                    map.fitBounds(L.latLng(center).toBounds(meters * 2.4));
                } else {
                    map.setView(center, 10);
                }
            }
            layers.clearLayers();
            if (meters) {
                L.circle(center, { radius: meters, className: 'skyguard-zone', weight: 2, fillOpacity: 0.08, interactive: false }).addTo(layers);
            }
            L.circleMarker(center, { radius: 5, className: 'skyguard-center-dot', weight: 2, fillOpacity: 1, interactive: false }).addTo(layers);
            if (options.aircraft !== false) {
                (status.aircraft || []).filter(hasPosition).forEach(function (aircraft) {
                    var info = describe(aircraft, options.units(), status.radius.unit);
                    L.marker([aircraft.lat, aircraft.lon], { icon: planeIcon(aircraft), keyboard: false })
                        .bindTooltip(planeLabel(aircraft, info), { permanent: true, direction: 'right', offset: [12, 0], className: 'skyguard-label' })
                        .addTo(layers);
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
                setNote('Set a center point to see the map.');
                return;
            }
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
                drawRadar(box, status, options);
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

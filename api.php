<?php
// SkyGuard's API routes. FPP loads this file on every /api request, so it only
// declares functions, every name is prefixed, and nothing runs at load time.

function getEndpointsfpppluginSkyGuard() {
    return array(
        array('method' => 'GET', 'endpoint' => 'status', 'callback' => 'fppPluginSkyGuardStatus'),
        array('method' => 'GET', 'endpoint' => 'headerIndicator', 'callback' => 'fppPluginSkyGuardHeaderIndicator'),
        array('method' => 'GET', 'endpoint' => 'geocode', 'callback' => 'fppPluginSkyGuardGeocode'),
    );
}

function fppPluginSkyGuardDataDir() {
    global $settings;
    return $settings['mediaDirectory'] . '/plugindata/fpp-plugin-SkyGuard';
}

// The daemon's status file, or null if there isn't a readable one.
function fppPluginSkyGuardReadStatus() {
    $path = fppPluginSkyGuardDataDir() . '/status.json';
    if (!is_readable($path)) {
        return null;
    }
    $doc = json_decode(file_get_contents($path), true);
    return is_array($doc) ? $doc : null;
}

// The daemon writes at least every 5 s, so much older than that means it isn't running.
function fppPluginSkyGuardIsRunning($doc) {
    if ($doc === null || !isset($doc['updated_at'])) {
        return false;
    }
    return abs(time() - floatval($doc['updated_at'])) <= 30;
}

// GET /api/plugin/fpp-plugin-SkyGuard/status
function fppPluginSkyGuardStatus() {
    $doc = fppPluginSkyGuardReadStatus();
    if ($doc === null) {
        return json(array('state' => null, 'running' => false));
    }
    $doc['running'] = fppPluginSkyGuardIsRunning($doc);
    return json($doc);
}

// GET /api/plugin/fpp-plugin-SkyGuard/headerIndicator
// FPP calls this with a 1 s timeout on its status updates, so it only reads the status file.
function fppPluginSkyGuardHeaderIndicator() {
    $doc = fppPluginSkyGuardReadStatus();
    $running = fppPluginSkyGuardIsRunning($doc);
    $warnings = ($running && isset($doc['warnings']) && is_array($doc['warnings'])) ? $doc['warnings'] : array();

    if (!$running) {
        $tooltip = 'SkyGuard is not running, so the heads are not protected.';
        $color = 'var(--bs-danger)';
    } else {
        $tooltip = implode(' ', $warnings);
        $color = 'var(--bs-warning)';
    }

    return json(array(
        'visible' => (!$running || count($warnings) > 0),
        'pluginName' => 'fpp-plugin-SkyGuard',
        'icon' => 'fa-plane',
        'color' => $color,
        // FPP places this inside an HTML attribute, and warnings can hold fixture names.
        'tooltip' => htmlspecialchars($tooltip, ENT_QUOTES),
        'link' => 'plugin.php?plugin=fpp-plugin-SkyGuard&page=status.php',
    ));
}

function fppPluginSkyGuardError($code, $message) {
    http_response_code($code);
    return json(array('status' => 'ERROR', 'message' => $message));
}

// GET /api/plugin/fpp-plugin-SkyGuard/geocode?q=<address>
// Nominatim's usage policy allows at most 1 request a second and asks that repeated
// queries be avoided, hence the throttle and the one-entry cache. The address isn't logged.
function fppPluginSkyGuardGeocode() {
    if (ReadSettingFromFile('geocoder', 'fpp-plugin-SkyGuard') === 'off') {
        return fppPluginSkyGuardError(403, 'Address lookup is switched off.');
    }
    $query = isset($_GET['q']) ? trim(strval($_GET['q'])) : '';
    if (strlen($query) < 3 || strlen($query) > 200) {
        return fppPluginSkyGuardError(400, 'Enter an address.');
    }

    $dir = fppPluginSkyGuardDataDir();
    $cachePath = $dir . '/geocode_cache.json';
    $key = hash('sha256', $query);
    if (is_readable($cachePath)) {
        $cached = json_decode(file_get_contents($cachePath), true);
        if (is_array($cached) && isset($cached['key'], $cached['results']) && $cached['key'] === $key) {
            return json(array('status' => 'OK', 'results' => $cached['results'], 'attribution' => 'Data © OpenStreetMap contributors'));
        }
    }
    if (is_readable($cachePath) && time() - filemtime($cachePath) < 1) {
        return fppPluginSkyGuardError(429, 'Please wait a moment and try again.');
    }

    $url = 'https://nominatim.openstreetmap.org/search?' . http_build_query(array(
        'q' => $query,
        'format' => 'jsonv2',
        'limit' => 5,
    ));
    $curl = curl_init($url);
    curl_setopt($curl, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($curl, CURLOPT_CONNECTTIMEOUT, 4);
    curl_setopt($curl, CURLOPT_TIMEOUT, 8);
    curl_setopt($curl, CURLOPT_USERAGENT, 'fpp-plugin-SkyGuard (https://github.com/mvgoodwin78/fpp-plugin-SkyGuard)');
    $body = curl_exec($curl);
    $errorNumber = curl_errno($curl);
    $status = curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
    curl_close($curl);

    if ($errorNumber === 60) {
        return fppPluginSkyGuardError(502, 'Certificate check failed. Check this device\'s date and time.');
    }
    if ($body === false || $status !== 200) {
        return fppPluginSkyGuardError(502, 'Could not reach the address lookup service.');
    }
    $places = json_decode($body, true);
    if (!is_array($places)) {
        return fppPluginSkyGuardError(502, 'The address lookup service sent an unreadable answer.');
    }

    $results = array();
    foreach ($places as $place) {
        if (is_array($place) && isset($place['lat'], $place['lon'])) {
            $results[] = array(
                'lat' => floatval($place['lat']),
                'lon' => floatval($place['lon']),
                'name' => isset($place['display_name']) ? strval($place['display_name']) : '',
            );
        }
    }
    if (is_writable($dir)) {
        file_put_contents($cachePath, json_encode(array('key' => $key, 'results' => $results)), LOCK_EX);
    }
    return json(array('status' => 'OK', 'results' => $results, 'attribution' => 'Data © OpenStreetMap contributors'));
}

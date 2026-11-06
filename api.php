<?php
// SkyGuard's API routes. FPP loads this file on every /api request, so it only
// declares functions, every name is prefixed, and nothing runs at load time.

function getEndpointsfpppluginSkyGuard() {
    return array(
        array('method' => 'GET', 'endpoint' => 'status', 'callback' => 'fppPluginSkyGuardStatus'),
        array('method' => 'GET', 'endpoint' => 'headerIndicator', 'callback' => 'fppPluginSkyGuardHeaderIndicator'),
        array('method' => 'GET', 'endpoint' => 'geocode', 'callback' => 'fppPluginSkyGuardGeocode'),
        array('method' => 'GET', 'endpoint' => 'private', 'callback' => 'fppPluginSkyGuardPrivateGet'),
        array('method' => 'PUT', 'endpoint' => 'private', 'callback' => 'fppPluginSkyGuardPrivatePut'),
        array('method' => 'GET', 'endpoint' => 'fixtures', 'callback' => 'fppPluginSkyGuardFixtureList'),
        array('method' => 'POST', 'endpoint' => 'fixtures', 'callback' => 'fppPluginSkyGuardFixtureAdd'),
        array('method' => 'PUT', 'endpoint' => 'fixtures/:id', 'callback' => 'fppPluginSkyGuardFixtureUpdate'),
        array('method' => 'DELETE', 'endpoint' => 'fixtures/:id', 'callback' => 'fppPluginSkyGuardFixtureDelete'),
        array('method' => 'POST', 'endpoint' => 'test-receiver', 'callback' => 'fppPluginSkyGuardTestReceiver'),
        array('method' => 'POST', 'endpoint' => 'test-online', 'callback' => 'fppPluginSkyGuardTestOnline'),
    );
}

function fppPluginSkyGuardDataDir() {
    global $settings;
    return $settings['mediaDirectory'] . '/plugindata/fpp-plugin-SkyGuard';
}

function fppPluginSkyGuardError($code, $message) {
    http_response_code($code);
    return json(array('status' => 'ERROR', 'message' => $message));
}

// The body of a write. Requiring the JSON header means a page on another site can't
// send one as a simple request. Returns null if the request doesn't qualify.
function fppPluginSkyGuardJsonBody() {
    $type = isset($_SERVER['CONTENT_TYPE']) ? $_SERVER['CONTENT_TYPE'] : '';
    if (strpos($type, 'application/json') !== 0) {
        return null;
    }
    $body = json_decode(file_get_contents('php://input'), true);
    return is_array($body) ? $body : null;
}

// Read a JSON file, or return $default if it is missing or unusable.
function fppPluginSkyGuardReadJson($path, $default) {
    if (!is_readable($path)) {
        return $default;
    }
    $doc = json_decode(file_get_contents($path), true);
    return is_array($doc) ? $doc : $default;
}

// Replace a file all at once, so the daemon can never read half of one.
function fppPluginSkyGuardWriteJson($path, $doc, $mode) {
    $temp = tempnam(dirname($path), '.skyguard-');
    if ($temp === false) {
        return false;
    }
    if (file_put_contents($temp, json_encode($doc)) === false || !chmod($temp, $mode) || !rename($temp, $path)) {
        if (is_file($temp)) {
            @unlink($temp);
        }
        return false;
    }
    return true;
}

// Run $change($doc) on a JSON file while holding a lock. Returns true if it was saved.
function fppPluginSkyGuardUpdateJson($path, $default, $mode, $change) {
    $lock = fopen(fppPluginSkyGuardDataDir() . '/.write.lock', 'c');
    if ($lock === false || !flock($lock, LOCK_EX)) {
        return false;
    }
    $doc = $change(fppPluginSkyGuardReadJson($path, $default));
    $saved = ($doc === null) ? false : fppPluginSkyGuardWriteJson($path, $doc, $mode);
    flock($lock, LOCK_UN);
    fclose($lock);
    return $saved;
}

// The daemon's status file, or null if there isn't a readable one.
function fppPluginSkyGuardReadStatus() {
    return fppPluginSkyGuardReadJson(fppPluginSkyGuardDataDir() . '/status.json', null);
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

// Text for the header icon. FPP 10 sets it as text and FPP 9.5 writes it into an HTML
// attribute unescaped, so escaping shows as codes on one and is needed on the other.
// Leaving out the characters that matter is right on both.
function fppPluginSkyGuardIndicatorText($text) {
    $text = str_replace(array(chr(34), '<', '>', '&'), '', $text);
    return str_replace(chr(39), chr(0xE2) . chr(0x80) . chr(0x99), $text);
}

// GET /api/plugin/fpp-plugin-SkyGuard/headerIndicator
// FPP calls this with a 1 s timeout on its status updates, so it only reads the status file.
// Always shown, so hovering always says whether SkyGuard is on or off.
function fppPluginSkyGuardHeaderIndicator() {
    $doc = fppPluginSkyGuardReadStatus();
    $running = fppPluginSkyGuardIsRunning($doc);
    $warnings = array();
    if ($running && isset($doc['warnings']) && is_array($doc['warnings'])) {
        foreach ($doc['warnings'] as $warning) {
            // The first words of the text already say this.
            if (strpos($warning, 'SkyGuard is disabled') !== 0) {
                $warnings[] = $warning;
            }
        }
    }
    $state = isset($doc['state']) ? $doc['state'] : '';
    $reason = isset($doc['reason']) ? $doc['reason'] : '';

    $green = '#198754';
    $yellow = '#ffc107';
    $red = '#dc3545';

    if (!$running) {
        $tooltip = 'SkyGuard is NOT RUNNING, so the heads are not protected.';
        $color = $red;
    } elseif (empty($doc['enabled'])) {
        $tooltip = 'SkyGuard is OFF, so the heads are not protected.';
        $color = $yellow;
    } elseif ($state === 'BLACKOUT') {
        $tooltip = 'SkyGuard is ON. The heads are blacked out.';
        $color = $red;
    } elseif ($state === 'CLEAR') {
        $tooltip = 'SkyGuard is ON. No aircraft are in the zone.';
        $color = $green;
    } elseif ($reason === 'schedule') {
        $tooltip = 'SkyGuard is ON. No show is running.';
        $color = $green;
    } else {
        $tooltip = 'SkyGuard is ON, but is waiting for its setup to finish.';
        $color = $yellow;
    }

    if (count($warnings) > 0) {
        $tooltip .= ' ' . implode(' ', $warnings);
        if ($color === $green) {
            $color = $yellow;
        }
    }

    return json(array(
        'visible' => true,
        'pluginName' => 'fpp-plugin-SkyGuard',
        'icon' => 'fa-plane',
        // FPP 10.3 ignores anything but a hex color or a color name here.
        'color' => $color,
        'tooltip' => fppPluginSkyGuardIndicatorText($tooltip),
        'link' => 'plugin.php?plugin=fpp-plugin-SkyGuard&page=status.php',
    ));
}

// GET /api/plugin/fpp-plugin-SkyGuard/private
// Says whether a key is saved, and never returns the key itself.
function fppPluginSkyGuardPrivateGet() {
    $doc = fppPluginSkyGuardReadJson(fppPluginSkyGuardDataDir() . '/private.json', array());
    return json(array(
        'center_lat' => isset($doc['center_lat']) ? $doc['center_lat'] : null,
        'center_lon' => isset($doc['center_lon']) ? $doc['center_lon'] : null,
        'api_key_set' => !empty($doc['online_api_key']),
    ));
}

// PUT /api/plugin/fpp-plugin-SkyGuard/private
// Takes center_lat and center_lon together, and/or online_api_key. Null clears a value.
function fppPluginSkyGuardPrivatePut() {
    $body = fppPluginSkyGuardJsonBody();
    if ($body === null) {
        return fppPluginSkyGuardError(400, 'Expected JSON.');
    }

    $hasCenter = array_key_exists('center_lat', $body) || array_key_exists('center_lon', $body);
    $lat = null;
    $lon = null;
    if ($hasCenter) {
        $lat = isset($body['center_lat']) ? $body['center_lat'] : null;
        $lon = isset($body['center_lon']) ? $body['center_lon'] : null;
        if (($lat === null) !== ($lon === null)) {
            return fppPluginSkyGuardError(400, 'Enter both latitude and longitude.');
        }
        if ($lat !== null) {
            if (!is_numeric($lat) || !is_numeric($lon)) {
                return fppPluginSkyGuardError(400, 'Latitude and longitude must be numbers.');
            }
            $lat = floatval($lat);
            $lon = floatval($lon);
            if ($lat < -90 || $lat > 90 || $lon < -180 || $lon > 180) {
                return fppPluginSkyGuardError(400, 'Latitude must be -90 to 90, and longitude -180 to 180.');
            }
        }
    }

    $hasKey = array_key_exists('online_api_key', $body);
    $key = $hasKey ? $body['online_api_key'] : null;
    if ($key !== null && (!is_string($key) || strlen($key) > 200 || preg_match('/[\x00-\x1f]/', $key))) {
        return fppPluginSkyGuardError(400, 'That API key is not valid.');
    }

    $saved = fppPluginSkyGuardUpdateJson(
        fppPluginSkyGuardDataDir() . '/private.json',
        array(),
        0600,
        function ($doc) use ($hasCenter, $lat, $lon, $hasKey, $key) {
            if ($hasCenter) {
                $doc['center_lat'] = $lat;
                $doc['center_lon'] = $lon;
            }
            if ($hasKey) {
                $doc['online_api_key'] = $key;
            }
            return $doc;
        }
    );
    if (!$saved) {
        return fppPluginSkyGuardError(500, 'Could not save that.');
    }
    return fppPluginSkyGuardPrivateGet();
}

// Keep only known fields, in the right types. A row may be incomplete: the daemon ignores
// it and the page marks it. Returns a message string if a value can't be stored.
function fppPluginSkyGuardCleanFixture($row) {
    $clean = array();

    $name = isset($row['name']) ? $row['name'] : '';
    if (!is_string($name) || strlen($name) > 64 || preg_match('/[\x00-\x1f]/', $name)) {
        return 'A name can have up to 64 characters.';
    }
    $clean['name'] = trim($name);
    $clean['enabled'] = !isset($row['enabled']) || filter_var($row['enabled'], FILTER_VALIDATE_BOOLEAN);

    foreach (array('universe', 'start_channel', 'dimmer_ch', 'dimmer_off', 'shutter_ch', 'shutter_off') as $field) {
        $value = isset($row[$field]) ? $row[$field] : null;
        if ($value === null || $value === '') {
            $clean[$field] = null;
        } elseif (is_int($value) || (is_string($value) && preg_match('/^[0-9]{1,8}$/', $value))) {
            $clean[$field] = intval($value);
        } else {
            return 'Channel numbers and values must be whole numbers.';
        }
    }
    return $clean;
}

function fppPluginSkyGuardFixturePath() {
    return fppPluginSkyGuardDataDir() . '/fixtures.json';
}

// GET /api/plugin/fpp-plugin-SkyGuard/fixtures
// Rows saved before rows had ids get one here, because the page edits rows by id.
function fppPluginSkyGuardFixtureList() {
    $rows = fppPluginSkyGuardReadJson(fppPluginSkyGuardFixturePath(), array());
    $missing = false;
    foreach ($rows as $row) {
        if (is_array($row) && empty($row['id'])) {
            $missing = true;
        }
    }
    if ($missing) {
        fppPluginSkyGuardUpdateJson(
            fppPluginSkyGuardFixturePath(),
            array(),
            0644,
            function ($current) {
                foreach ($current as $index => $row) {
                    if (is_array($row) && empty($row['id'])) {
                        $current[$index]['id'] = bin2hex(random_bytes(8));
                    }
                }
                return $current;
            }
        );
        $rows = fppPluginSkyGuardReadJson(fppPluginSkyGuardFixturePath(), array());
    }
    return json(array('fixtures' => array_values(array_filter($rows, 'is_array'))));
}

// POST /api/plugin/fpp-plugin-SkyGuard/fixtures
function fppPluginSkyGuardFixtureAdd() {
    $body = fppPluginSkyGuardJsonBody();
    if ($body === null) {
        return fppPluginSkyGuardError(400, 'Expected JSON.');
    }
    $row = fppPluginSkyGuardCleanFixture($body);
    if (is_string($row)) {
        return fppPluginSkyGuardError(400, $row);
    }
    $row['id'] = bin2hex(random_bytes(8));

    $saved = fppPluginSkyGuardUpdateJson(
        fppPluginSkyGuardFixturePath(),
        array(),
        0644,
        function ($rows) use ($row) {
            if (count($rows) >= 200) {
                return null;
            }
            $rows[] = $row;
            return $rows;
        }
    );
    if (!$saved) {
        return fppPluginSkyGuardError(500, 'Could not save that row.');
    }
    return json($row);
}

// PUT /api/plugin/fpp-plugin-SkyGuard/fixtures/:id
function fppPluginSkyGuardFixtureUpdate() {
    $body = fppPluginSkyGuardJsonBody();
    if ($body === null) {
        return fppPluginSkyGuardError(400, 'Expected JSON.');
    }
    $row = fppPluginSkyGuardCleanFixture($body);
    if (is_string($row)) {
        return fppPluginSkyGuardError(400, $row);
    }
    $id = strval(params('id'));
    $row['id'] = $id;

    $saved = fppPluginSkyGuardUpdateJson(
        fppPluginSkyGuardFixturePath(),
        array(),
        0644,
        function ($rows) use ($row, $id) {
            foreach ($rows as $index => $existing) {
                if (is_array($existing) && isset($existing['id']) && $existing['id'] === $id) {
                    $rows[$index] = $row;
                    return $rows;
                }
            }
            return null;
        }
    );
    if (!$saved) {
        return fppPluginSkyGuardError(404, 'That row no longer exists.');
    }
    return json($row);
}

// DELETE /api/plugin/fpp-plugin-SkyGuard/fixtures/:id
function fppPluginSkyGuardFixtureDelete() {
    $id = strval(params('id'));
    $saved = fppPluginSkyGuardUpdateJson(
        fppPluginSkyGuardFixturePath(),
        array(),
        0644,
        function ($rows) use ($id) {
            $kept = array();
            foreach ($rows as $existing) {
                if (!is_array($existing) || !isset($existing['id']) || $existing['id'] !== $id) {
                    $kept[] = $existing;
                }
            }
            return $kept;
        }
    );
    if (!$saved) {
        return fppPluginSkyGuardError(500, 'Could not delete that row.');
    }
    return json(array('status' => 'OK'));
}

// Fetch a URL for a test. Returns array(decoded JSON or null, a message for the user or null).
function fppPluginSkyGuardFetchForTest($url, $timeout) {
    $curl = curl_init($url);
    curl_setopt($curl, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($curl, CURLOPT_FOLLOWLOCATION, false);
    curl_setopt($curl, CURLOPT_CONNECTTIMEOUT, $timeout);
    curl_setopt($curl, CURLOPT_TIMEOUT, $timeout);
    curl_setopt($curl, CURLOPT_USERAGENT, 'fpp-plugin-SkyGuard');
    $body = curl_exec($curl);
    $errorNumber = curl_errno($curl);
    $status = curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
    curl_close($curl);

    if ($errorNumber === 60) {
        return array(null, 'Certificate check failed. Check this device\'s date and time.');
    }
    if ($errorNumber === 28) {
        return array(null, 'No answer within ' . $timeout . ' seconds.');
    }
    if ($body === false) {
        return array(null, 'Could not connect.');
    }
    if ($status !== 200) {
        return array(null, 'It answered with HTTP ' . intval($status) . '.');
    }
    $doc = json_decode($body, true);
    if (!is_array($doc)) {
        return array(null, 'The answer was not aircraft data.');
    }
    return array($doc, null);
}

// POST /api/plugin/fpp-plugin-SkyGuard/test-receiver
// Reports a count or an error and never passes on what the address sent back, so this
// can't be used to read other devices through FPP.
function fppPluginSkyGuardTestReceiver() {
    $body = fppPluginSkyGuardJsonBody();
    if ($body === null || !isset($body['url']) || !is_string($body['url'])) {
        return fppPluginSkyGuardError(400, 'Enter an address.');
    }
    $url = trim($body['url']);
    $scheme = strtolower(strval(parse_url($url, PHP_URL_SCHEME)));
    if (strlen($url) > 300 || ($scheme !== 'http' && $scheme !== 'https') || !parse_url($url, PHP_URL_HOST)) {
        return fppPluginSkyGuardError(400, 'Enter an address starting with http:// or https://.');
    }
    $result = fppPluginSkyGuardFetchForTest($url, 3);
    if ($result[1] !== null) {
        return fppPluginSkyGuardError(502, $result[1]);
    }
    $doc = $result[0];
    if (!isset($doc['aircraft']) || !is_array($doc['aircraft']) || !isset($doc['now'])) {
        return fppPluginSkyGuardError(502, 'The answer was not aircraft data.');
    }
    return json(array('status' => 'OK', 'aircraft' => count($doc['aircraft'])));
}

// POST /api/plugin/fpp-plugin-SkyGuard/test-online
// Sends the saved location to the provider chosen, so it needs the personal-use confirmation.
function fppPluginSkyGuardTestOnline() {
    $body = fppPluginSkyGuardJsonBody();
    if ($body === null) {
        return fppPluginSkyGuardError(400, 'Expected JSON.');
    }
    $provider = isset($body['provider']) ? $body['provider'] : '';
    $templates = array(
        'adsb.fi' => 'https://opendata.adsb.fi/api/v3/lat/%s/lon/%s/dist/5',
        'adsb.lol' => 'https://api.adsb.lol/v2/point/%s/%s/5',
    );
    if (!is_string($provider) || !isset($templates[$provider])) {
        return fppPluginSkyGuardError(400, 'Choose a provider.');
    }
    if (ReadSettingFromFile('personal_use_confirmed', 'fpp-plugin-SkyGuard') !== '1') {
        return fppPluginSkyGuardError(403, 'Confirm personal use first.');
    }
    $doc = fppPluginSkyGuardReadJson(fppPluginSkyGuardDataDir() . '/private.json', array());
    if (!isset($doc['center_lat'], $doc['center_lon'])) {
        return fppPluginSkyGuardError(400, 'Set a center point first.');
    }

    // These are free services that refuse fast clients, so a held-down button can't hammer them.
    $stamp = fppPluginSkyGuardDataDir() . '/.test-online';
    if (is_file($stamp) && time() - filemtime($stamp) < 5) {
        return fppPluginSkyGuardError(429, 'Please wait a few seconds and try again.');
    }
    if (is_writable(fppPluginSkyGuardDataDir())) {
        touch($stamp);
    }

    $url = sprintf(
        $templates[$provider],
        number_format(floatval($doc['center_lat']), 4, '.', ''),
        number_format(floatval($doc['center_lon']), 4, '.', '')
    );
    $result = fppPluginSkyGuardFetchForTest($url, 5);
    if ($result[1] !== null) {
        return fppPluginSkyGuardError(502, $result[1]);
    }
    if (!isset($result[0]['ac']) || !is_array($result[0]['ac'])) {
        return fppPluginSkyGuardError(502, 'The answer was not aircraft data.');
    }
    return json(array('status' => 'OK', 'aircraft' => count($result[0]['ac'])));
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

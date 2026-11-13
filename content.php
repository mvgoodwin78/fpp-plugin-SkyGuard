<?php
require_once __DIR__ . '/skyguard_assets.inc';
skyGuardStyles(array('leaflet/leaflet.css', 'skyguard.css'));
$skyGuardRepo = 'fpp-plugin-SkyGuard';

// One labeled row. Stacks on a phone and sits side by side from medium screens up.
function skyGuardRowOpen($label, $id = '', $help = '') {
    $rowId = ($id !== '') ? " id='" . htmlspecialchars($id, ENT_QUOTES) . "'" : '';
    echo "<div class='row mb-3 align-items-center'" . $rowId . ">";
    echo "<div class='col-12 col-md-4 fw-semibold'>" . htmlspecialchars($label);
    if ($help !== '') {
        echo "<div class='form-text fw-normal'>" . htmlspecialchars($help) . "</div>";
    }
    echo "</div><div class='col-12 col-md-8'>";
}

function skyGuardRowClose() {
    echo "</div></div>";
}

// A number box, which also keeps out the characters FPP 9.5 can't print safely.
// For this input type FPP's helper uses its length and size arguments as max and min.
function skyGuardNumber($setting, $min, $max, $default = '', $unit = '') {
    global $skyGuardRepo;
    if ($unit !== '') {
        echo "<div class='input-group'>";
    }
    PrintSettingTextSaved($setting, 0, 0, $max, $min, $skyGuardRepo, $default, 'skyGuardSettingSaved', '', 'number', array('step' => 'any'));
    if ($unit !== '') {
        echo "<span class='input-group-text'>" . htmlspecialchars($unit) . "</span></div>";
    }
}

function skyGuardSelect($title, $setting, $default, $values) {
    global $skyGuardRepo;
    PrintSettingSelect($title, $setting, 0, 0, $default, $values, $skyGuardRepo, 'skyGuardSettingSaved');
}

function skyGuardCheckbox($title, $setting, $default) {
    global $skyGuardRepo;
    PrintSettingCheckbox($title, $setting, 0, 0, '1', '0', $skyGuardRepo, 'skyGuardSettingSaved', $default);
}
?>
<div id="skyGuardSettings" class="mt-2" data-setup-state="<?= htmlspecialchars(strval(ReadSettingFromFile('setup_state', $skyGuardRepo)), ENT_QUOTES) ?>">

    <div id="skyGuardSetup" class="card mb-3" hidden>
        <div class="card-header fw-semibold">SkyGuard setup</div>
        <div class="card-body">
            <div id="skyGuardSetupProgress" class="small text-body-secondary mb-2"></div>
            <h5 id="skyGuardSetupTitle"></h5>
            <p id="skyGuardSetupText"></p>
            <div id="skyGuardSetupActions" class="d-flex flex-wrap gap-2"></div>
        </div>
    </div>

    <div id="skyGuardWarnings" class="alert alert-warning skyguard-alert" role="alert" hidden></div>

    <ul class="nav nav-pills mb-3 flex-wrap" role="tablist">
        <li class="nav-item"><a class="nav-link active" data-bs-toggle="tab" href="#skyGuardTabData" role="tab">Data Source</a></li>
        <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#skyGuardTabLocation" role="tab">Location &amp; Zone</a></li>
        <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#skyGuardTabFixtures" role="tab">Fixtures</a></li>
        <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#skyGuardTabMatrix" role="tab">Matrix Message</a></li>
    </ul>

    <div class="tab-content">

        <div class="tab-pane fade show active" id="skyGuardTabData" role="tabpanel">
            <?php skyGuardRowOpen('Aircraft data from'); ?>
            <?php skyGuardSelect('Aircraft data from', 'source_mode', 'receiver', array(
                'My receiver only' => 'receiver',
                'My receiver, with online data as a check and a backup' => 'both',
                'Online data only (I have no receiver)' => 'online',
            )); ?>
            <?php skyGuardRowClose(); ?>

            <div data-show-with="receiver">
                <?php skyGuardRowOpen('Where is your receiver?'); ?>
                <?php skyGuardSelect('Where is your receiver?', 'receiver_location', 'other', array(
                    'On another device on my network' => 'other',
                    'On this FPP box' => 'this_box',
                )); ?>
                <?php skyGuardRowClose(); ?>
            </div>

            <div data-show-with="receiver-here">
                <?php skyGuardRowOpen('Decoder on this box', '', 'SkyGuard reads the file readsb, dump1090-fa or dump978-fa writes on this box, 1090 MHz and 978 MHz together. No address or web server is needed.'); ?>
                <button type="button" class="btn btn-sm btn-secondary" id="skyGuardTestLocal">Test</button>
                <span class="small ms-2" id="skyGuardTestLocalResult"></span>
                <?php skyGuardRowClose(); ?>
            </div>

            <div data-show-with="receiver-other">
                <?php skyGuardRowOpen('Receiver address', '', 'For example http://192.168.1.50/data/aircraft.json'); ?>
                <input type="url" class="form-control" id="skyGuardReceiverUrl" data-free-text="receiver_url" maxlength="300" aria-label="Receiver address">
                <button type="button" class="btn btn-sm btn-secondary mt-1" id="skyGuardTestReceiver">Test</button>
                <span class="small ms-2" id="skyGuardTestReceiverResult"></span>
                <?php skyGuardRowClose(); ?>

                <?php skyGuardRowOpen('978 MHz decoder address', '', 'Optional, for a separate UAT decoder'); ?>
                <input type="url" class="form-control" id="skyGuardReceiver978Url" data-free-text="receiver_978_url" maxlength="300" aria-label="978 MHz decoder address">
                <button type="button" class="btn btn-sm btn-secondary mt-1" id="skyGuardTest978">Test</button>
                <span class="small ms-2" id="skyGuardTest978Result"></span>
                <?php skyGuardRowClose(); ?>
            </div>

            <div data-show-with="online">
                <?php skyGuardRowOpen('Online provider'); ?>
                <?php skyGuardSelect('Online provider', 'online_provider', 'adsb.fi', array(
                    'adsb.fi (updates every 2 seconds)' => 'adsb.fi',
                    'adsb.lol (updates every 15 seconds)' => 'adsb.lol',
                )); ?>
                <button type="button" class="btn btn-sm btn-secondary mt-1" id="skyGuardTestOnline">Test</button>
                <span class="small ms-2" id="skyGuardTestOnlineResult"></span>
                <div class="form-text" id="skyGuardProviderCredit" hidden>
                    Flight data from <a href="https://adsb.fi" target="_blank" rel="noopener">adsb.fi</a>.
                </div>
                <div class="form-text" id="skyGuardProviderSlow" hidden>
                    With adsb.lol, an aircraft can be seen up to 15 seconds late, and an outage can take about 30 seconds to black out the heads.
                </div>
                <?php skyGuardRowClose(); ?>

                <?php skyGuardRowOpen('API key', '', 'Optional. None of the current providers needs one.'); ?>
                <div class="input-group">
                    <input type="password" class="form-control" id="skyGuardApiKey" maxlength="200" autocomplete="off">
                    <button type="button" class="btn btn-secondary" id="skyGuardApiKeySave">Save</button>
                    <button type="button" class="btn btn-secondary" id="skyGuardApiKeyClear">Clear</button>
                </div>
                <div class="form-text" id="skyGuardApiKeyState"></div>
                <?php skyGuardRowClose(); ?>

                <p class="text-body-secondary small">
                    With online data, your show's location and search radius go to the provider chosen above.
                    With your receiver and online data together, that happens during a show even while your receiver is working, to catch aircraft it misses.
                    If the provider has a problem during a show, the heads stay dark until data returns.
                </p>
            </div>
        </div>

        <div class="tab-pane fade" id="skyGuardTabLocation" role="tabpanel">
            <?php skyGuardRowOpen('Center point', '', 'Decimal degrees. Stored privately on this device, but readable by anyone who can reach FPP.'); ?>
            <div class="row g-2">
                <div class="col-12 col-sm-6"><input type="number" step="any" min="-90" max="90" class="form-control" id="skyGuardLat" placeholder="Latitude" aria-label="Latitude"></div>
                <div class="col-12 col-sm-6"><input type="number" step="any" min="-180" max="180" class="form-control" id="skyGuardLon" placeholder="Longitude" aria-label="Longitude"></div>
            </div>
            <div class="mt-2 d-flex flex-wrap gap-2">
                <button type="button" class="btn btn-sm btn-primary" id="skyGuardCenterSave">Save center point</button>
            </div>
            <div class="form-text">Type your latitude and longitude, then press Save center point. To find them, right-click your house in Google Maps: the first line of the menu is the two numbers. Or look up your address below.</div>
            <?php skyGuardRowClose(); ?>

            <?php skyGuardRowOpen('Find an address', '', 'Sent to OpenStreetMap only when you press Look up.'); ?>
            <div class="input-group">
                <input type="text" class="form-control" id="skyGuardAddress" maxlength="200" aria-label="Address">
                <button type="button" class="btn btn-secondary" id="skyGuardLookup">Look up</button>
            </div>
            <div id="skyGuardLookupResults" class="list-group mt-2"></div>
            <div class="form-text" id="skyGuardLookupNote"></div>
            <?php skyGuardRowClose(); ?>

            <div id="skyGuardLagNotice" class="alert alert-info" role="status" data-show-with="online">
                Online flight data can lag behind real aircraft. Increase your radius by 3 miles to allow for it.
            </div>

            <div class="mb-3">
                <div id="skyGuardLocationMap" class="skyguard-map-area skyguard-map-area-small"></div>
                <div class="form-text" id="skyGuardLocationMapLegend"></div>
                <div class="form-text" id="skyGuardLocationMapNote"></div>
            </div>

            <div data-show-with="online">
                <?php skyGuardRowOpen('Online radius', '', 'Required. Used while online data is the active feed, and during a show for aircraft your receiver misses.'); ?>
                <?php skyGuardNumber('radius', 0.01, 99999); ?>
                <?php skyGuardRowClose(); ?>
            </div>
            <div data-show-with="receiver">
                <?php skyGuardRowOpen('Receiver radius', '', 'Required. Used while your own receiver is the active feed.'); ?>
                <?php skyGuardNumber('radius_receiver', 0.01, 99999); ?>
                <?php skyGuardRowClose(); ?>
            </div>
            <?php skyGuardRowOpen('Radius unit'); ?>
            <?php skyGuardSelect('Radius unit', 'radius_unit', 'mi', array('Miles' => 'mi', 'Nautical miles' => 'nm', 'Kilometers' => 'km')); ?>
            <?php skyGuardRowClose(); ?>

            <?php skyGuardRowOpen('Lowest altitude', '', '0 includes aircraft on the ground.'); ?>
            <?php skyGuardNumber('alt_floor_ft', -2000, 100000, '0', 'feet'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Highest altitude'); ?>
            <?php skyGuardNumber('alt_ceiling_ft', -2000, 100000, '10000', 'feet'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Ignore aircraft on the ground', '', 'Skips aircraft reporting they are on the ground until they reach the speed limit under Advanced.'); ?>
            <?php skyGuardCheckbox('Ignore aircraft on the ground', 'ignore_ground', 0); ?>
            <?php skyGuardRowClose(); ?>
        </div>

        <div class="tab-pane fade" id="skyGuardTabFixtures" role="tabpanel">
            <p class="text-body-secondary small">
                One row per moving head. Only the channels listed here are forced during a blackout.
                Use a universe for E1.31 or Art-Net, or a start channel for an output with no universe number, such as USB DMX.
                Changes save as you make them.
            </p>
            <div class="table-responsive">
                <table class="table table-sm align-middle">
                    <thead>
                        <tr>
                            <th>Name</th><th>On</th><th>Universe</th><th>Start ch</th>
                            <th>Dimmer ch</th><th>Dimmer off</th><th>Shutter ch</th><th>Shutter off</th>
                            <th>Status</th><th></th>
                        </tr>
                    </thead>
                    <tbody id="skyGuardFixtureRows"></tbody>
                </table>
            </div>
            <button type="button" class="btn btn-primary" id="skyGuardFixtureAdd">Add a head</button>
        </div>

        <div class="tab-pane fade" id="skyGuardTabMatrix" role="tabpanel">
            <?php skyGuardRowOpen('Show a message during a blackout'); ?>
            <?php skyGuardCheckbox('Show a message during a blackout', 'matrix_enabled', 0); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Panels', '', 'Your existing Pixel Overlay Models.'); ?>
            <div id="skyGuardModels"></div>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Message'); ?>
            <input type="text" class="form-control" id="skyGuardMessage" data-free-text="matrix_message" maxlength="200">
            <div class="mt-2 d-flex flex-wrap gap-1" id="skyGuardPlaceholders"></div>
            <div class="form-text">Preview: <span id="skyGuardPreview"></span></div>
            <div class="mt-2">
                <button type="button" class="btn btn-sm btn-primary" id="skyGuardMatrixTest">Test on panels</button>
                <button type="button" class="btn btn-sm btn-danger" id="skyGuardMatrixStop" hidden>Stop</button>
                <div class="form-text">Shows the message on the panels ticked above, with the sample values, until you press Stop. The heads aren't affected.</div>
                <div id="skyGuardLive" class="mt-2" hidden>
                    <div class="form-text" id="skyGuardLiveLabel"></div>
                    <canvas id="skyGuardLiveCanvas" class="w-100 border skyguard-live-canvas"></canvas>
                </div>
            </div>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Message when data is offline', '', 'Leave empty for none.'); ?>
            <input type="text" class="form-control" id="skyGuardNoData" data-free-text="matrix_nodata_message" maxlength="200">
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Text color'); ?>
            <input type="color" class="form-control form-control-color" id="skyGuardColor" data-setting="matrix_color" aria-label="Text color">
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Font'); ?>
            <select class="form-select" id="skyGuardFont" data-setting="matrix_font" aria-label="Font"></select>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Text size'); ?>
            <?php skyGuardNumber('matrix_size', 4, 100, '18'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Text position'); ?>
            <?php skyGuardSelect('Text position', 'matrix_position', 'Right to Left', array(
                'Scroll right to left' => 'Right to Left',
                'Scroll left to right' => 'Left to Right',
                'Scroll bottom to top' => 'Bottom to Top',
                'Scroll top to bottom' => 'Top to Bottom',
                'Centered' => 'Center',
            )); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Scroll speed'); ?>
            <?php skyGuardNumber('matrix_speed', 0, 200, '40'); ?>
            <?php skyGuardRowClose(); ?>
        </div>
    </div>

    <div class="mt-4">
        <button class="btn btn-secondary btn-sm" type="button" data-bs-toggle="collapse" data-bs-target="#skyGuardAdvanced" aria-expanded="false" aria-controls="skyGuardAdvanced">Advanced</button>
        <div class="collapse mt-3" id="skyGuardAdvanced">
            <?php skyGuardRowOpen('Poll interval', '', 'How often SkyGuard asks for aircraft positions, from 1 to 2 seconds. Shorter notices an aircraft sooner. Your receiver is always asked this often. Online data follows this only during a show, and never faster than the provider allows: every 2 seconds for adsb.fi, every 15 seconds for adsb.lol.'); ?>
            <?php skyGuardNumber('poll_interval_s', 1, 2, '1', 'seconds'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Check online data between shows', '', 'Off: online data is only asked for during a show, so your location isn\'t sent all day. The map then shows online aircraft only during a show. On: it is also asked between shows, at the interval below.'); ?>
            <?php skyGuardCheckbox('Check online data between shows', 'online_between_shows', 0); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Online interval between shows', '', 'Used only when checking online data between shows is on. From 5 to 300 seconds; slower is kinder to the free services.'); ?>
            <?php skyGuardNumber('online_idle_interval_s', 5, 300, '30', 'seconds'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Receiver check interval'); ?>
            <?php skyGuardNumber('health_interval_s', 1, 3600, '10', 'seconds'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Failed checks before backup', '', 'Used when online data is the backup.'); ?>
            <?php skyGuardNumber('health_fail_count', 1, 100, '2'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Maximum data age', '', 'Older data blacks out the heads during a show. 0 means on the first failed poll.'); ?>
            <?php skyGuardNumber('max_data_age_s', 0, 3600, '5', 'seconds'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Release delay', '', 'How long the zone must stay clear. 0 releases at once.'); ?>
            <?php skyGuardNumber('release_delay_s', 0, 3600, '5', 'seconds'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Count aircraft with no position', '', 'Fail-safe: these count as in the zone if their altitude is in range.'); ?>
            <?php skyGuardCheckbox('Count aircraft with no position', 'count_no_position', 1); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Ground speed limit', '', 'With "Ignore aircraft on the ground" on, an aircraft reporting it is on the ground is skipped until it reaches this speed. 5 to 100 knots; 40 knots is about 46 mph or 74 km/h.'); ?>
            <?php skyGuardNumber('ground_speed_limit_kt', 5, 100, '40', 'knots'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Display units'); ?>
            <?php skyGuardSelect('Display units', 'units', 'imperial', array('Imperial (mi, ft, kt)' => 'imperial', 'Metric (km, m, km/h)' => 'metric')); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Map tile address', '', 'Where the map background comes from. Empty shows only a radar plot and contacts no map service. Another server also has to be allowed in FPP\'s content policy.'); ?>
            <input type="url" class="form-control" id="skyGuardTileUrl" data-free-text="map_tile_url" maxlength="300" aria-label="Map tile address">
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Address lookup'); ?>
            <?php skyGuardSelect('Address lookup', 'geocoder', 'nominatim', array('OpenStreetMap Nominatim' => 'nominatim', 'Off' => 'off')); ?>
            <?php skyGuardRowClose(); ?>
        </div>
    </div>
</div>
<?php skyGuardScripts(array('leaflet/leaflet.js', 'skyguard-map.js', 'skyguard-settings.js')); ?>

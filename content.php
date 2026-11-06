<?php
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
function skyGuardNumber($setting, $min, $max, $default = '') {
    global $skyGuardRepo;
    PrintSettingTextSaved($setting, 0, 0, $max, $min, $skyGuardRepo, $default, 'skyGuardSettingSaved', '', 'number', array('step' => 'any'));
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

    <div id="skyGuardWarnings" class="alert alert-warning" role="alert" hidden></div>

    <ul class="nav nav-pills mb-3 flex-wrap" role="tablist">
        <li class="nav-item"><a class="nav-link active" data-bs-toggle="tab" href="#skyGuardTabData" role="tab">Data Source</a></li>
        <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#skyGuardTabLocation" role="tab">Location &amp; Zone</a></li>
        <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#skyGuardTabFixtures" role="tab">Fixtures</a></li>
        <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#skyGuardTabMatrix" role="tab">Matrix Message</a></li>
    </ul>

    <div class="tab-content">

        <div class="tab-pane fade show active" id="skyGuardTabData" role="tabpanel">
            <?php skyGuardRowOpen('Aircraft data from'); ?>
            <?php skyGuardSelect('Aircraft data from', 'source_mode', 'both', array(
                'My receiver, with online data as a backup' => 'both',
                'My receiver only' => 'receiver',
                'Online data only (I have no receiver)' => 'online',
            )); ?>
            <?php skyGuardRowClose(); ?>

            <div data-show-with="receiver">
                <?php skyGuardRowOpen('Receiver address', '', 'For example http://192.168.1.50/data/aircraft.json'); ?>
                <input type="url" class="form-control" id="skyGuardReceiverUrl" data-free-text="receiver_url" maxlength="300" aria-label="Receiver address">
                <button type="button" class="btn btn-sm btn-outline-secondary mt-1" id="skyGuardTestReceiver">Test</button>
                <span class="small ms-2" id="skyGuardTestReceiverResult"></span>
                <?php skyGuardRowClose(); ?>

                <?php skyGuardRowOpen('978 MHz decoder address', '', 'Optional, for a separate UAT decoder'); ?>
                <input type="url" class="form-control" id="skyGuardReceiver978Url" data-free-text="receiver_978_url" maxlength="300" aria-label="978 MHz decoder address">
                <button type="button" class="btn btn-sm btn-outline-secondary mt-1" id="skyGuardTest978">Test</button>
                <span class="small ms-2" id="skyGuardTest978Result"></span>
                <?php skyGuardRowClose(); ?>
            </div>

            <div data-show-with="online">
                <?php skyGuardRowOpen('Online provider'); ?>
                <?php skyGuardSelect('Online provider', 'online_provider', 'adsb.fi', array(
                    'adsb.fi (updates every 2 seconds)' => 'adsb.fi',
                    'adsb.lol (updates every 15 seconds)' => 'adsb.lol',
                )); ?>
                <button type="button" class="btn btn-sm btn-outline-secondary mt-1" id="skyGuardTestOnline">Test</button>
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
                    <button type="button" class="btn btn-outline-secondary" id="skyGuardApiKeySave">Save</button>
                    <button type="button" class="btn btn-outline-secondary" id="skyGuardApiKeyClear">Clear</button>
                </div>
                <div class="form-text" id="skyGuardApiKeyState"></div>
                <?php skyGuardRowClose(); ?>

                <p class="text-body-secondary small">
                    With online data, your show's location and search radius go to the provider chosen above.
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
                <button type="button" class="btn btn-sm btn-outline-secondary" id="skyGuardUseLocation">Use my location</button>
            </div>
            <div class="form-text" id="skyGuardLocationNote"></div>
            <?php skyGuardRowClose(); ?>

            <?php skyGuardRowOpen('Find an address', '', 'Sent to OpenStreetMap only when you press Look up.'); ?>
            <div class="input-group">
                <input type="text" class="form-control" id="skyGuardAddress" maxlength="200" aria-label="Address">
                <button type="button" class="btn btn-outline-secondary" id="skyGuardLookup">Look up</button>
            </div>
            <div id="skyGuardLookupResults" class="list-group mt-2"></div>
            <div class="form-text" id="skyGuardLookupNote"></div>
            <?php skyGuardRowClose(); ?>

            <div id="skyGuardLagNotice" class="alert alert-info" role="status" data-show-with="online">
                Online flight data can lag behind real aircraft. Increase your radius by 3 miles to allow for it.
            </div>

            <div data-show-with="online">
                <?php skyGuardRowOpen('Online radius', '', 'Required. Used while online data is the active feed.'); ?>
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

            <?php skyGuardRowOpen('Lowest altitude (ft)', '', '0 includes aircraft on the ground.'); ?>
            <?php skyGuardNumber('alt_floor_ft', -2000, 100000, '0'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Highest altitude (ft)'); ?>
            <?php skyGuardNumber('alt_ceiling_ft', -2000, 100000, '10000'); ?>
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
                <div class="form-text">Shows the message on the panels ticked above for 15 seconds, with the sample values. The heads aren't affected.</div>
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
        <button class="btn btn-outline-secondary btn-sm" type="button" data-bs-toggle="collapse" data-bs-target="#skyGuardAdvanced" aria-expanded="false" aria-controls="skyGuardAdvanced">Advanced</button>
        <div class="collapse mt-3" id="skyGuardAdvanced">
            <?php skyGuardRowOpen('Poll interval (seconds)', '', '1 to 2. An online provider\'s own minimum applies on top of this.'); ?>
            <?php skyGuardNumber('poll_interval_s', 1, 2, '1'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Online interval outside a show (seconds)', '', '5 to 300.'); ?>
            <?php skyGuardNumber('online_idle_interval_s', 5, 300, '30'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Receiver check interval (seconds)'); ?>
            <?php skyGuardNumber('health_interval_s', 1, 3600, '10'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Failed checks before backup', '', 'Used when online data is the backup.'); ?>
            <?php skyGuardNumber('health_fail_count', 1, 100, '2'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Maximum data age (seconds)', '', 'Older data blacks out the heads during a show. 0 means on the first failed poll.'); ?>
            <?php skyGuardNumber('max_data_age_s', 0, 3600, '5'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Release delay (seconds)', '', 'How long the zone must stay clear. 0 releases at once.'); ?>
            <?php skyGuardNumber('release_delay_s', 0, 3600, '5'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Count aircraft with no position', '', 'Fail-safe: these count as in the zone if their altitude is in range.'); ?>
            <?php skyGuardCheckbox('Count aircraft with no position', 'count_no_position', 1); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Ground speed limit (knots)', '', 'With "Ignore aircraft on the ground" on, they count once they reach this speed. 5 to 100.'); ?>
            <?php skyGuardNumber('ground_speed_limit_kt', 5, 100, '40'); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Display units'); ?>
            <?php skyGuardSelect('Display units', 'units', 'imperial', array('Imperial (mi, ft, kt)' => 'imperial', 'Metric (km, m, km/h)' => 'metric')); ?>
            <?php skyGuardRowClose(); ?>
            <?php skyGuardRowOpen('Address lookup'); ?>
            <?php skyGuardSelect('Address lookup', 'geocoder', 'nominatim', array('OpenStreetMap Nominatim' => 'nominatim', 'Off' => 'off')); ?>
            <?php skyGuardRowClose(); ?>
        </div>
    </div>
</div>

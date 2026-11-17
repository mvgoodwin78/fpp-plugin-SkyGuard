<?php require_once __DIR__ . '/skyguard_assets.inc'; ?>
<?php skyGuardStyles(array('leaflet/leaflet.css', 'skyguard.css')); ?>
<div id="skyGuardStatus" class="mt-2">

    <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
        <span id="skyGuardState" class="badge fs-5 text-bg-secondary">…</span>
        <span id="skyGuardReason"></span>
    </div>

    <div id="skyGuardStatusWarnings" class="alert alert-warning skyguard-alert" role="alert" hidden></div>

    <div class="d-flex flex-wrap gap-2 mb-3">
        <button type="button" class="btn btn-primary" id="skyGuardToggle" disabled>…</button>
        <button type="button" class="btn btn-secondary" id="skyGuardTest" disabled>Test all heads (5 seconds)</button>
        <button type="button" class="btn btn-secondary" id="skyGuardListOpen">Aircraft list (0)</button>
    </div>

    <div id="skyGuardMap" class="skyguard-map-area mb-1"></div>
    <div class="form-text" id="skyGuardMapLegend"></div>
    <div class="form-text fw-semibold" id="skyGuardRadiusCount"></div>
    <div class="form-text">Aircraft in the zone are labeled. Tap or click any other aircraft to show or hide its label.</div>
    <div class="form-text" id="skyGuardMapNote"></div>
    <div class="form-text" id="skyGuardOldData" hidden></div>
    <div class="form-text mb-3" id="skyGuardCredit" hidden>
        Flight data from <a href="https://adsb.fi" target="_blank" rel="noopener">adsb.fi</a>.
    </div>

    <h5 class="mt-3">Health</h5>
    <dl class="row mx-0 mb-2" id="skyGuardHealth"></dl>
    <div class="table-responsive">
        <table class="table table-sm align-middle">
            <thead><tr><th>Head</th><th>Status</th><th>Channels</th></tr></thead>
            <tbody id="skyGuardHeads"></tbody>
        </table>
    </div>
    <div class="form-text" id="skyGuardNotices"></div>

    <h5 class="mt-3">Zone log</h5>
    <div class="form-text mb-2">Every aircraft that passed through a zone: inside the box and within that zone's altitudes, whether or not a show was running. Kept for 90 days.</div>
    <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
        <select class="form-select form-select-sm w-auto" id="skyGuardVisitZone" aria-label="Zone to show">
            <option value="">All zones</option>
            <option value="receiver1">Receiver zone 1</option>
            <option value="receiver2">Receiver zone 2</option>
            <option value="online">Online zone</option>
        </select>
        <button type="button" class="btn btn-sm btn-secondary" id="skyGuardVisitCsv">Download CSV</button>
    </div>
    <div class="table-responsive skyguard-visit-table">
        <table class="table table-sm align-middle">
            <thead><tr><th>Zone</th><th>Callsign</th><th>Entered</th><th>Time inside</th><th>Altitude in / low / high</th><th>Data</th></tr></thead>
            <tbody id="skyGuardVisits"></tbody>
        </table>
    </div>

    <h5 class="mt-3">Recent events</h5>
    <div class="table-responsive">
        <table class="table table-sm align-middle">
            <thead><tr><th>Time</th><th>Event</th><th>Why</th><th>Aircraft</th></tr></thead>
            <tbody id="skyGuardEvents"></tbody>
        </table>
    </div>

    <div class="modal fade" id="skyGuardList" tabindex="-1" aria-labelledby="skyGuardListTitle" aria-hidden="true">
        <div class="modal-dialog modal-fullscreen">
            <div class="modal-content">
                <div class="modal-header">
                    <button type="button" class="btn btn-secondary me-3" id="skyGuardListBack">Back</button>
                    <h5 class="modal-title" id="skyGuardListTitle">Aircraft</h5>
                </div>
                <div class="modal-body">
                    <div class="table-responsive">
                        <table class="table table-sm align-middle">
                            <thead><tr><th>Callsign</th><th>Altitude</th><th>Speed</th><th>Distance</th><th>In zone</th></tr></thead>
                            <tbody id="skyGuardListRows"></tbody>
                        </table>
                    </div>
                    <div class="form-text" id="skyGuardListEmpty" hidden>No aircraft nearby.</div>
                </div>
            </div>
        </div>
    </div>
</div>
<?php skyGuardScripts(array('leaflet/leaflet.js', 'skyguard-map.js', 'skyguard-status.js')); ?>

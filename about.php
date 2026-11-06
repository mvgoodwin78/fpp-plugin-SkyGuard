<div id="skyGuardHelp" class="mt-2">

    <fieldset class="border rounded p-3">
        <legend>What SkyGuard does</legend>
        <p>
            SkyGuard blacks out your moving heads while an aircraft is inside a radius you set, so beams aren't pointed at it.
            It forces only each head's dimmer and shutter channels to off values. Pan, tilt, color and everything else keep following the sequence, and other props aren't touched.
        </p>
        <p class="mb-0">
            <strong>It doesn't replace your own judgment or any legal duty.</strong>
            Coverage depends on what aircraft broadcast and on your data source, and not every aircraft can be seen.
        </p>
    </fieldset>

    <fieldset class="border rounded p-3 mt-3">
        <legend>Setting up</legend>
        <p>Open <strong>Content Setup, SkyGuard Settings</strong>. A guided setup appears the first time, and everything in it can be changed later on the tabs.</p>
        <ol class="mb-0">
            <li><strong>Confirm personal use.</strong> SkyGuard is for personal use only and stays idle until you confirm this.</li>
            <li><strong>Choose your data.</strong> Use your own ADS-B receiver, online data, or both. On the Data Source tab, the Test buttons show whether each source answers.</li>
            <li><strong>Set the location and zone.</strong> Enter the center point and a radius for each source in use, and set the lowest and highest altitudes that count. SkyGuard stays idle until the center and radii are entered. Online data can lag behind real aircraft, so increase your online radius by 3 miles to allow for it.</li>
            <li><strong>Add your heads.</strong> On the Fixtures tab, add one row per head with its dimmer and shutter channels, and the value that means off for each. Use a universe for E1.31 or Art-Net, or a start channel for an output with no universe number, such as USB DMX. Channel numbers are the head's actual DMX addresses, not offsets. Check your fixture's manual for its off values.</li>
            <li><strong>Add a message, if you want one.</strong> The Matrix Message tab can show text on your LED panels during a blackout. Test on panels runs until you press Stop, and shows a live view of the first panel you ticked.</li>
            <li><strong>Test it.</strong> Press Test on a row in Fixtures to black out that head for 3 seconds, or Test all heads on the status page for 5 seconds. Watch the real heads: only you can confirm they go dark.</li>
        </ol>
    </fieldset>

    <fieldset class="border rounded p-3 mt-3">
        <legend>How a blackout works</legend>
        <ul>
            <li><strong>It only happens during a show.</strong> A show means a playlist or sequence playing, including one you started by hand, or live E1.31 or Art-Net data driving the outputs. That last one means an xLights test session can black out the heads too. Outside a show the state is IDLE and the heads aren't touched, apart from your own tests.</li>
            <li><strong>It starts on the first poll</strong> that finds an aircraft in the zone, and ends once the zone has been clear for the release delay.</li>
            <li><strong>Altitude limits apply.</strong> An aircraft must be between your lowest and highest altitudes to count. If its altitude is unknown and it's inside the radius, it counts.</li>
            <li><strong>Aircraft with no position</strong> count as in the zone if their altitude is in range, unless you turn that off under Advanced.</li>
            <li><strong>Ground traffic:</strong> with "Ignore aircraft on the ground" on, an aircraft reporting it's on the ground is skipped until it reaches the ground speed limit under Advanced, which is 40 knots by default. The map and list show these as <code>ground (ignored)</code>. A helicopter hovering low still counts unless it reports being on the ground.</li>
        </ul>
        <h6>With your receiver and online data together</h6>
        <ul>
            <li>Your receiver has priority. An aircraft it has located goes by your receiver radius, wherever online data puts that aircraft.</li>
            <li>During a show, online data is also checked. An aircraft your receiver misses, or reports with no position, goes by the online data and online radius and can cause a blackout. The map and list mark it <code>receiver missed it</code>.</li>
            <li>If online data stops while your receiver works, there's no blackout, but a warning says missed aircraft won't be caught.</li>
            <li>If your receiver stops, online data takes over with the online radius until the receiver returns.</li>
        </ul>
        <h6>If something goes wrong: it fails dark</h6>
        <ul class="mb-0">
            <li>If aircraft data stops during a show, the heads black out and stay dark until data returns. An outage on a free online service can therefore darken your show.</li>
            <li>If SkyGuard's program crashes during a blackout, the heads stay dark and it restarts itself, releasing them only when it's safe.</li>
            <li>If it crashes while the zone is clear, nothing is held, so the heads aren't protected until it comes back. The icon in FPP's header warns you if it has stopped.</li>
        </ul>
    </fieldset>

    <fieldset class="border rounded p-3 mt-3">
        <legend>What SkyGuard can't control</legend>
        <ul class="mb-0">
            <li>SkyGuard must run on the FPP that outputs to the heads. A blackout on a player doesn't affect a remote playing its own copy of the sequence.</li>
            <li>FPP's Channel Tester, output processors and some other plugins act after SkyGuard, so they can still change these channels.</li>
            <li>SkyGuard isn't expected to work properly on an FPP install on macOS. It needs Linux.</li>
        </ul>
    </fieldset>

    <fieldset class="border rounded p-3 mt-3">
        <legend>Troubleshooting</legend>
        <dl class="mb-0">
            <dt>The status page says NOT RUNNING</dt>
            <dd>SkyGuard's program has stopped, so the heads aren't protected. Restart FPPD. If it still says this, reboot. If it stops again, check the log.</dd>

            <dt>The state stays IDLE</dt>
            <dd>The words beside the state say why. It may be that no show is running, SkyGuard is disabled, or the personal use confirmation, center point or a radius is missing.</dd>

            <dt>No aircraft on the map, but the online Test finds some</dt>
            <dd>The Test makes its own request. The map uses SkyGuard's regular polling, and online data isn't requested while SkyGuard is disabled. Enable it. With no show running, positions then update about every 30 seconds.</dd>

            <dt>The map shows a radar plot instead</dt>
            <dd>Map images couldn't load, usually because this device or your browser can't reach the internet. The plot uses the same data. An empty map address under Advanced also gives you the plot.</dd>

            <dt>"No aircraft data from any feed"</dt>
            <dd>During a show the heads stay dark until data returns. Use the Test buttons on the Data Source tab to find out which source has stopped.</dd>

            <dt>"The receiver isn't responding" or "Using the online feed"</dt>
            <dd>Check that your receiver is on and that its address is right. SkyGuard goes back to it by itself when it answers.</dd>

            <dt>A head says "Universe not found"</dt>
            <dd>FPP has no active output with that universe number. Check Input/Output Setup, or use a start channel if the output has no universe number. That head won't black out until this is fixed.</dd>

            <dt>A head says "Incomplete"</dt>
            <dd>The row needs a universe or a start channel, plus a dimmer or shutter channel. SkyGuard ignores it until then.</dd>

            <dt>A head doesn't go dark during a test</dt>
            <dd>Check the channel numbers and off values against your fixture's manual. The status page shows "Held dark" while SkyGuard is holding a head's channels. If it says that and the head is still lit, the channels or off values are wrong for that fixture.</dd>

            <dt>The message doesn't appear on a panel</dt>
            <dd>Use Test on panels on the Matrix Message tab. If the live view shows the text but the panel doesn't, the problem is between FPP and the panel, so check that its channel output is on. Tick only the panels you want: the live view shows only the first one.</dd>

            <dt>Blackouts with no aircraft in sight</dt>
            <dd>Open the aircraft list on the status page to see what's counting. Online data can lag, so an aircraft may have just left. With both sources, a receiver with gaps in its coverage will miss more aircraft, and online data then decides for them.</dd>

            <dt>A setting won't save</dt>
            <dd>Text settings can't contain double quotes or backslashes, because older versions of FPP can't store them safely.</dd>
        </dl>
    </fieldset>

    <fieldset class="border rounded p-3 mt-3">
        <legend>Logs and updates</legend>
        <p>
            SkyGuard's log is <code>plugin-fpp-plugin-SkyGuard.log</code>, on the Logs tab of FPP's File Manager under Content Setup.
            It records each blackout, feed change and error. Aircraft are logged, but your location isn't.
        </p>
        <p class="mb-0">After updating SkyGuard you don't need to restart FPPD on FPP 10 or later: the update restarts SkyGuard's program by itself. During the update the heads are released briefly, so don't update during a show.</p>
    </fieldset>

    <fieldset class="border rounded p-3 mt-3">
        <legend>What leaves this device</legend>
        <ul class="mb-0">
            <li><strong>Online flight data:</strong> your show's location and search radius go to the provider you choose. Nothing goes while SkyGuard is disabled or before you confirm personal use.</li>
            <li><strong>Map:</strong> while a map is open, your browser loads images from OpenStreetMap, which shows it roughly where your show is. Clear the map address under Advanced to stop this.</li>
            <li><strong>Address lookup:</strong> a typed address goes to OpenStreetMap only when you press Look up.</li>
            <li><strong>Your receiver:</strong> requests go to the address you enter.</li>
            <li><strong>On your network:</strong> anyone who can reach FPP can read SkyGuard's status, including your location, and change its settings.</li>
        </ul>
    </fieldset>

    <fieldset class="border rounded p-3 mt-3">
        <legend>About</legend>
        <a href="https://github.com/mvgoodwin78/fpp-plugin-SkyGuard">Source code</a><br>
        <a href="https://github.com/mvgoodwin78/fpp-plugin-SkyGuard/issues">Report a bug</a><br>
        The map uses <a href="https://leafletjs.com">Leaflet</a> and <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> data.
    </fieldset>
</div>

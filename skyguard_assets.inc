<?php
// FPP tells browsers to keep plugin files for a year, and the tags it writes for js/ and
// css/ have no version, so an update would never reach a browser that has been here before.
// SkyGuard's files live in assets/ instead, which FPP doesn't load by itself, and each
// address here changes whenever its file does.
function skyGuardAssetUrl($file) {
    $path = __DIR__ . '/assets/' . $file;
    $version = is_file($path) ? filemtime($path) : 0;
    return 'plugin.php?plugin=fpp-plugin-SkyGuard&file=assets/' . $file . '&nopage=1&v=' . $version;
}

function skyGuardStyles($files) {
    foreach ($files as $file) {
        printf("<link rel='stylesheet' type='text/css' href='%s'>\n", htmlspecialchars(skyGuardAssetUrl($file), ENT_QUOTES));
    }
}

function skyGuardScripts($files) {
    foreach ($files as $file) {
        printf("<script type='text/javascript' src='%s'></script>\n", htmlspecialchars(skyGuardAssetUrl($file), ENT_QUOTES));
    }
}

<?php

# One entry per menu area, as FPP's plugin guidelines require.
$menuEntries = Array(
	Array(
		'text' => 'SkyGuard',
		'type' => 'status',
		'page' => 'status.php',
		'wrap' => 1
	),
	Array(
		'text' => 'SkyGuard Settings',
		'type' => 'content',
		'page' => 'content.php',
		'wrap' => 1
	),
	Array(
		'text' => 'SkyGuard',
		'type' => 'help',
		'page' => 'about.php',
		'wrap' => 1
	)
);

# $plugin (the plugin's directory) and $menu (the menu area) are set by FPP.
foreach ($menuEntries as $entry)
{
	if ($entry['type'] != $menu)
		continue;

	printf("<li><a href='plugin.php?plugin=%s&page=%s'>%s</a></li>\n",
		$plugin, $entry['page'], $entry['text']);
}
?>

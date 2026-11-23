use strict;
use warnings;
use Test::More;
BEGIN {
	unshift @INC, 'tests/perl/stubs';
	if ($ENV{OPENKORE_SRC}) {
		my $source_dir = -f "$ENV{OPENKORE_SRC}/src/WarpPortal.pm"
			? "$ENV{OPENKORE_SRC}/src" : $ENV{OPENKORE_SRC};
		push @INC, $source_dir;
	}
}
use WarpPortal qw(parseMemoEntry updateServerMemos recordArrival);

$Globals::maps_lut{"prontera.rsw"} = 1;
$Globals::maps_lut{"gef_fild01.rsw"} = 1;

my $legacy = parseMemoEntry('gef_fild01');
is($legacy->{map}, 'gef_fild01', 'legacy map parses');
ok($legacy->{valid} && !$legacy->{has_coords}, 'legacy coordinates remain unknown');
my $extended = parseMemoEntry('gef_fild01 100 120');
is_deeply([@{$extended}{qw(map x y)}], ['gef_fild01', 100, 120], 'extended map and coordinates parse');
my $zero = parseMemoEntry('prontera 0 0');
ok($zero->{valid} && $zero->{has_coords}, 'explicit zero coordinates are distinct from missing coordinates');
ok(!parseMemoEntry('unknown_map 4 5')->{valid}, 'unknown map rejected');
ok(!parseMemoEntry('prontera -1 2')->{valid}, 'negative coordinate rejected');
ok(!parseMemoEntry('prontera 1')->{valid}, 'partial coordinate pair rejected');

@Misc::writes = ();
%Globals::config = (
	map1 => 'legacy', memo1 => 'gef_fild01 100 120', memo2 => 'stale_map 10 20',
	memo3 => 'prontera', memo4 => '', warpPortal_autoUpdateMemo => 0,
);
$Globals::char = {};
updateServerMemos({type => 27, memo1 => 'prontera.gat', memo2 => 'gef_fild01.gat', memo3 => '', memo4 => ''});
is($Globals::char->{warp}{savepoint}, 'prontera', 'save point is kept separate');
is_deeply($Globals::char->{warp}{memo_slots}, ['gef_fild01', '', ''], 'runtime memo slots preserve empty native slots');
is_deeply($Globals::char->{warp}{memo}, ['prontera', 'gef_fild01'], 'legacy menu view preserves menu ordering');
is($Globals::config{memo1}, 'gef_fild01 100 120', 'automatic update disabled preserves manual configuration');
is(scalar @Misc::writes, 0, 'no config writes when automatic updates are disabled');

$Globals::config{warpPortal_autoUpdateMemo} = 1;
updateServerMemos({type => 27, memo1 => 'prontera.gat', memo2 => 'gef_fild01.gat', memo3 => '', memo4 => ''});
is($Globals::config{memo1}, 'gef_fild01 100 120', 'unchanged map preserves coordinates');
is($Globals::config{memo2}, '', 'changed destination invalidates stale map and coordinates');
is($Globals::config{memo3}, '', 'empty memo slot clears stale entry');
is(scalar @Misc::writes, 2, 'only changed entries are written');

delete $Globals::config{memo3};
updateServerMemos({type => 27, memo1 => 'prontera.gat', memo2 => 'gef_fild01.gat',
	memo3 => 'prontera.gat', memo4 => 'gef_fild01.gat'});
is($Globals::config{memo3}, 'gef_fild01', 'missing optional config key is initialized when auto-update is enabled');

$Globals::config{memo1} = 'gef_fild01';
recordArrival('gef_fild01', 104, 121, 'confirmed_portal_traversal', 1);
is($Globals::config{memo1}, 'gef_fild01 104 121', 'confirmed arrival fills missing coordinates');
my $writes = scalar @Misc::writes;
recordArrival('gef_fild01', 104, 121, 'confirmed_portal_traversal', 1);
is(scalar @Misc::writes, $writes, 'repeat arrival avoids redundant config writes');
is_deeply($Globals::char->{warp}{observed}{gef_fild01}, {x => 104, y => 121, source => 'confirmed_portal_traversal', current_session => 1, time => $Globals::char->{warp}{observed}{gef_fild01}{time}}, 'runtime observation records provenance');

$Globals::config{memo1} = 'gef_fild01 100 120';
recordArrival('gef_fild01', 105, 120, 'confirmed_portal_traversal', 1);
is($Globals::config{memo1}, 'gef_fild01 105 120', 'automatic synchronization replaces different confirmed coordinates');
my $writes_after_coordinate_change = scalar @Misc::writes;
recordArrival('gef_fild01', 105, 120, 'confirmed_portal_traversal', 1);
is($Globals::config{memo1}, 'gef_fild01 105 120', 'matching confirmed coordinates remain unchanged');
is(scalar @Misc::writes, $writes_after_coordinate_change, 'matching coordinates do not cause a redundant write');
$Globals::config{warpPortal_autoUpdateMemo} = 0;
$Globals::config{memo1} = 'gef_fild01 100 120';
$writes = scalar @Misc::writes;
recordArrival('gef_fild01', 106, 120, 'manual_coordinates_preserved', 1);
is($Globals::config{memo1}, 'gef_fild01 100 120', 'manual coordinates remain unchanged when automatic update is disabled');
is(scalar @Misc::writes, $writes, 'manual mode emits no config write after traversal');

# initMapChangeVars clears the legacy warp hash. The selected memo slot is
# captured before travel, then supplied to arrival learning after that reset.
$Globals::char = { warp => { memo_slots => ['gef_fild01', '', ''] } };
$Globals::config{warpPortal_autoUpdateMemo} = 1;
$Globals::config{memo1} = 'gef_fild01';
delete $Globals::char->{warp};
recordArrival('gef_fild01', 120, 62, 'server_map_change_after_selected_warp_portal', 1);
is($Globals::config{memo1}, 'gef_fild01 120 62', 'confirmed arrival learns a memo after map-change state reset');

$Globals::config{memo1} = 'gef_fild01 100 120';
$Globals::char->{warp}{memo_slots} = ['gef_fild01', 'prontera', 'gef_fild01'];
$Globals::config{memo3} = 'gef_fild01';
recordArrival('gef_fild01', 210, 211, 'ambiguous_duplicate_memo_map');
is($Globals::config{memo3}, 'gef_fild01', 'duplicate maps are not assigned to an unknown memo slot');
recordArrival('gef_fild01', 210, 211, 'selected_memo_slot', 3);
is($Globals::config{memo3}, 'gef_fild01 210 211', 'confirmed selected memo slot receives its own coordinate observation');
updateServerMemos({type => 26, memo1 => 'prontera.gat', memo2 => '', memo3 => '', memo4 => ''});
is($Globals::config{memo1}, 'gef_fild01 100 120', 'Teleport menu is not mistaken for Warp Portal memos');

done_testing();

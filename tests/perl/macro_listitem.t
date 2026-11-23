use strict;
use warnings;
use Test::More;
my $source = $ENV{OPENKORE_SRC};
die "OPENKORE_SRC must point to the pinned OpenKore checkout\n" unless $source;
unshift @INC, "$source/src", "$source/src/deps", "$source/plugins/macro";

require Macro::Parser;

is(
	Macro::Parser::parseCmd('do look @listitem(0, north, south)'),
	'do look north',
	'macro command expansion selects the first value from a list',
);
is(
	Macro::Parser::parseCmd('do look @listitem(1, north, south)'),
	'do look south',
	'macro command expansion retains values after the first list entry',
);
is(
	Macro::Parser::parseCmd('do look @listitem(0, 0, 4)'),
	'do look 0',
	'macro command expansion preserves a zero-valued first list item',
);
is(
	Macro::Parser::parseCmd('do look @listitem(1, 4, 0)'),
	'do look 0',
	'macro command expansion preserves zero-valued later list item',
);
is(
	Macro::Parser::parseCmd('do look @listitem(-1, north, south)'),
	'do look -1',
	'negative indices remain invalid instead of selecting from the end',
);
is(
	Macro::Parser::parseCmd('do look @listitem(2, north, south)'),
	'do look -1',
	'out-of-range indices remain invalid',
);

done_testing();

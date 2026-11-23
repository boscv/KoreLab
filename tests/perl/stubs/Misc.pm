package Misc;
use strict;
use Exporter 'import';
our @EXPORT_OK = qw(configModify);
our @writes;
sub configModify {
	my ($key, $value) = @_;
	return if defined $Globals::config{$key} && $Globals::config{$key} eq $value;
	$Globals::config{$key} = $value;
	push @writes, [$key, $value];
}
1;

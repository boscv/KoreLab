from pathlib import Path
import os

import pytest

OPENKORE = Path(os.environ.get("KORELAB_OPENKORE", "/__korelab_openkore_unset__")).expanduser()


@pytest.mark.source_compatibility
@pytest.mark.skipif(not OPENKORE.is_dir(), reason="set KORELAB_OPENKORE")
def test_installed_openkore_exposes_completed_inventory_list_hook():
    source = OPENKORE / "src"
    parser = (source / "Network/PacketParser.pm").read_text()
    receiver = (source / "Network/Receive.pm").read_text()
    profile = (source / "Network/Receive/ServerType0.pm").read_text()

    handler = parser.index("$handleContainer->$callback(\\%args, @handleArguments);")
    dispatch = parser.index('Plugins::callHook("packet/$handler->[0]", \\%args);')
    finalize = receiver.index("sub item_list_end {")
    finalize_end = receiver.index("sub login_error {", finalize)
    inventory_finalizer = receiver[finalize:finalize_end]
    assert dispatch > handler
    assert "if ( $args->{type} == INVTYPE_INVENTORY )" in inventory_finalizer
    assert "$char->inventory->onitemListEnd();" in inventory_finalizer
    assert "'0B0B' => ['item_list_end', 'C2', [qw(type flag)]]" in profile

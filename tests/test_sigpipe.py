"""A client hanging up must not kill the editor."""

import signal
from unittest.mock import patch

import pytest


@pytest.mark.skipif(not hasattr(signal, "SIGPIPE"), reason="POSIX only")
def test_ignore_sigpipe_hands_sigpipe_back_to_python():
    from classes import timeline

    with patch.object(timeline.signal, "signal") as sig:
        timeline.ignore_sigpipe()
    sig.assert_called_once_with(signal.SIGPIPE, signal.SIG_IGN)

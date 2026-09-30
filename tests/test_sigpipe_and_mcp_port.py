"""A client hanging up must not kill the editor; harnesses find the MCP port from a file."""

import signal
from unittest.mock import patch

import pytest


@pytest.mark.skipif(not hasattr(signal, "SIGPIPE"), reason="POSIX only")
def test_ignore_sigpipe_hands_sigpipe_back_to_python():
    from classes import timeline

    with patch.object(timeline.signal, "signal") as sig:
        timeline.ignore_sigpipe()
    sig.assert_called_once_with(signal.SIGPIPE, signal.SIG_IGN)


def test_mcp_server_writes_its_port_next_to_the_token(tmp_path):
    from classes import agent_mcp_server, info

    with patch.object(info, "USER_PATH", str(tmp_path)):
        agent_mcp_server._write_port_file(54321)
        assert (tmp_path / "mcp_port").read_text() == "54321"

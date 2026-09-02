"""Focused, fully offline test for the D4C packaging console-entry-point
contract: espn_fantasy_server.main().

Standard library only (unittest + unittest.mock + io). Never starts a
long-lived MCP process - mcp.run() is patched out entirely. No network
calls.
"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import espn_fantasy_server as srv


class TestPackagingEntryPoint(unittest.TestCase):
    def test_main_exists_and_is_callable(self):
        self.assertTrue(hasattr(srv, "main"))
        self.assertTrue(callable(srv.main))

    def test_main_calls_mcp_run_exactly_once(self):
        with patch.object(srv.mcp, "run") as mock_run:
            srv.main()
            mock_run.assert_called_once_with()

    def test_main_writes_nothing_to_stdout(self):
        buf = io.StringIO()
        with patch.object(srv.mcp, "run"):
            with redirect_stdout(buf):
                srv.main()
        self.assertEqual(buf.getvalue(), "")

    def test_tool_count_and_registration_unaffected_by_main(self):
        before_tools = dict(srv.mcp._tool_manager._tools) if hasattr(srv.mcp, "_tool_manager") else None
        with patch.object(srv.mcp, "run"):
            srv.main()
        after_tools = dict(srv.mcp._tool_manager._tools) if hasattr(srv.mcp, "_tool_manager") else None
        if before_tools is not None:
            self.assertEqual(set(before_tools.keys()), set(after_tools.keys()))
            self.assertEqual(len(after_tools), 37)


if __name__ == "__main__":
    unittest.main()

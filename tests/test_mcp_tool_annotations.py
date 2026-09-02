import asyncio
import json
import os
import subprocess
import sys
import unittest


STATE_MUTATING_TOOL_NAMES = {
    "authenticate",
    "logout",
    "sync_my_espn_leagues",
    "refresh_fantasypros_cache",
    "prepare_draft_strategy",
}


class MCPToolAnnotationTests(unittest.TestCase):
    def test_unified_tools_publish_quick_read_write_annotations(self):
        code = r'''
import asyncio, json
import fantasy_football_server as unified

tools = asyncio.run(unified.mcp.list_tools())
payload = {
    tool.name: (
        None if tool.annotations is None
        else getattr(tool.annotations, "readOnlyHint", None)
    )
    for tool in tools
}
print(json.dumps(payload, sort_keys=True))
'''
        env = os.environ.copy()
        env.pop("ESPN_S2", None)
        env.pop("ESPN_SWID", None)
        env.pop("SWID", None)
        env.pop("FANTASYPROS_API_KEY", None)
        env.pop("SPORTSGAMEODDS_API_KEY", None)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        annotations = json.loads(proc.stdout.strip().splitlines()[-1])

        self.assertEqual(len(annotations), 52)
        self.assertNotIn(None, annotations.values())

        write_tools = {name for name, read_only in annotations.items() if read_only is False}
        read_tools = {name for name, read_only in annotations.items() if read_only is True}

        self.assertEqual(write_tools, STATE_MUTATING_TOOL_NAMES)
        self.assertEqual(len(read_tools), 47)
        self.assertEqual(read_tools | write_tools, set(annotations))

        # Transparent provider/cache-backed retrieval remains semantically read-only.
        self.assertTrue(annotations["find_sportsbook_team"])
        self.assertTrue(annotations["get_sportsbook_slate"])
        self.assertTrue(annotations["compare_sportsbook_market"])
        self.assertTrue(annotations["find_sportsbook_market_disagreements"])
        self.assertTrue(annotations["find_sportsbook_player_prop_disagreements"])
        self.assertTrue(annotations["get_player_prop_market_context"])
        self.assertTrue(annotations["commissioner_investigate"])
        self.assertTrue(annotations["evaluate_trade"])

    def test_annotation_policy_is_fail_closed_for_unclassified_tools(self):
        from mcp_tool_annotations import apply_unified_tool_annotations

        class FakeTool:
            def __init__(self, name):
                self.name = name
                self.annotations = None

        class FakeManager:
            def list_tools(self):
                return [FakeTool("unexpected_new_tool")]

        class FakeMCP:
            _tool_manager = FakeManager()

        with self.assertRaisesRegex(RuntimeError, "annotation policy mismatch"):
            apply_unified_tool_annotations(FakeMCP())


if __name__ == "__main__":
    unittest.main()

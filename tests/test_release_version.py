import json
import os
import subprocess
import sys
import unittest


class ReleaseVersionTests(unittest.TestCase):
    def test_unified_server_reports_project_distribution_version(self):
        code = r'''
import importlib.metadata as metadata
import json
import fantasy_football_server as unified
options = unified.mcp._mcp_server.create_initialization_options()
print(json.dumps({
    "package_version": metadata.version("fantasy-football-mcp"),
    "server_name": options.server_name,
    "server_version": options.server_version,
}))
'''
        env = os.environ.copy()
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["package_version"], "0.4.1")
        self.assertEqual(payload["server_name"], "fantasy-football-mcp")
        self.assertEqual(payload["server_version"], payload["package_version"])


if __name__ == "__main__":
    unittest.main()

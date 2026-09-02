import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

ACTIVE_DOCS = [
    ROOT / "README.md",
    ROOT / "CLAUDE.md",
    ROOT / "SECURITY.md",
    ROOT / "SPORTSGAMEODDS.md",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "docs" / "CONFIGURATION.md",
    ROOT / "docs" / "PROVIDER_CREDENTIALS.md",
    ROOT / "docs" / "TOOL_REFERENCE.md",
    ROOT / "docs" / "DEVELOPMENT.md",
]

LINK_DOCS = [
    ROOT / "README.md",
    ROOT / "CLAUDE.md",
    ROOT / "SECURITY.md",
    ROOT / "SPORTSGAMEODDS.md",
    ROOT / "PROVENANCE.md",
    ROOT / "CHANGELOG.md",
    *sorted((ROOT / "docs").glob("*.md")),
]


class DocumentationContractTests(unittest.TestCase):
    def _read(self, path: Path) -> str:
        self.assertTrue(path.exists(), f"expected documentation file is missing: {path.relative_to(ROOT)}")
        return path.read_text(encoding="utf-8")

    def test_current_tool_counts_are_documented_consistently(self):
        readme = self._read(ROOT / "README.md")
        claude = self._read(ROOT / "CLAUDE.md")
        tools = self._read(ROOT / "docs" / "TOOL_REFERENCE.md")
        sportsbook = self._read(ROOT / "SPORTSGAMEODDS.md")

        self.assertIn("**Current `main` tool surface:** **52 MCP tools**", readme)
        self.assertIn("Current unified MCP surface on `main`: **52 tools**", claude)
        self.assertIn("+ 12 SportsGameOdds", claude)
        self.assertIn("+ 1 cross-provider market context", claude)
        self.assertIn("| SportsGameOdds | 12 |", tools)
        self.assertIn("| Cross-provider Market Context | 1 |", tools)
        self.assertIn("| **Total** | **52** |", tools)
        self.assertIn("**Current contract: 47 Read / 5 Write.**", tools)
        self.assertIn("## SportsGameOdds Tools — 12", sportsbook)
        self.assertIn("get_player_prop_market_context", sportsbook)

    def test_public_project_identity_release_and_license_contract(self):
        readme = self._read(ROOT / "README.md")
        claude = self._read(ROOT / "CLAUDE.md")
        provenance = self._read(ROOT / "PROVENANCE.md")
        security = self._read(ROOT / "SECURITY.md")
        gitignore = self._read(ROOT / ".gitignore")
        license_text = self._read(ROOT / "LICENSE")

        self.assertTrue(readme.startswith("# ESPN Fantasy Football MCP\n"))
        self.assertIn("**Current release:** `0.4.1`", readme)
        self.assertIn("ESPN is the only supported fantasy-league platform", readme)
        self.assertIn("mpsthedude/ESPN-Fantasy-Football-MCP.git", readme)
        self.assertIn("Current release: `0.4.1`", claude)
        self.assertIn("License: MIT", claude)
        self.assertIn("current 52-tool surface", provenance)
        self.assertIn("the current project-authored source is distributed under the MIT License", provenance)
        self.assertIn("MIT License", license_text)
        self.assertIn("Copyright (c) 2026 mpsthedude", license_text)
        self.assertNotIn("currently shared privately with invited collaborators", security)
        self.assertIn("sgo_cache/", gitignore)
        self.assertIn(".sgo_cache/", gitignore)

    def test_provider_credential_setup_is_public_and_complete(self):
        readme = self._read(ROOT / "README.md")
        guide = self._read(ROOT / "docs" / "PROVIDER_CREDENTIALS.md")

        self.assertIn("[Provider Credentials Setup](docs/PROVIDER_CREDENTIALS.md)", readme)
        for fragment in [
            "ESPN_S2",
            "ESPN_SWID",
            "FANTASYPROS_API_KEY",
            "SPORTSGAMEODDS_API_KEY",
            "Application",
            "Storage",
            "https://www.fantasypros.com/api-data/",
            "https://secure.fantasypros.com/api-keys/request/",
            "https://sportsgameodds.com/pricing",
            "Never commit real ESPN cookies or API keys",
        ]:
            self.assertIn(fragment, guide)

        self.assertIn("Keep the surrounding `{}` braces", guide)
        self.assertIn("Do **not** URL-decode", guide)
        self.assertIn("call `authenticate` with **no arguments**", guide)

    def test_active_docs_do_not_restore_known_stale_architecture_claims(self):
        stale_fragments = [
            "Current release: **v0.2.0 / 47 MCP tools**",
            "sportsgameodds_tools.py` — 8 MCP tools",
            "- `espn-api>=0.44.1`",
            "owns ESPN league caching",
            "47-tool unified wrapper",
        ]

        for path in ACTIVE_DOCS:
            text = self._read(path)
            for fragment in stale_fragments:
                self.assertNotIn(
                    fragment,
                    text,
                    f"stale documentation claim in {path.relative_to(ROOT)}: {fragment!r}",
                )

    def test_public_active_docs_do_not_name_private_host_product(self):
        for path in [ROOT / "README.md", ROOT / "docs" / "TOOL_REFERENCE.md"]:
            self.assertNotIn("Amazon Quick", self._read(path))

    def test_runtime_metadata_does_not_reintroduce_espn_api(self):
        pyproject = self._read(ROOT / "pyproject.toml")
        self.assertNotIn("espn-api", pyproject.lower())
        self.assertIn('version = "0.4.1"', pyproject)
        self.assertIn('license = { file = "LICENSE" }', pyproject)
        self.assertIn('"mcp[cli]>=1.7.0,<2"', pyproject)
        self.assertIn('"requests>=2.32.3"', pyproject)
        self.assertIn('"LICENSE"', pyproject)
        self.assertIn('"mcp_tool_annotations.py"', pyproject)
        self.assertIn('"player_market_context.py"', pyproject)
        self.assertIn('"player_market_context_tools.py"', pyproject)

    def test_relative_markdown_links_resolve(self):
        link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        failures = []

        for doc in LINK_DOCS:
            text = self._read(doc)
            for raw_target in link_pattern.findall(text):
                target = raw_target.strip()
                if not target or target.startswith(("http://", "https://", "mailto:", "tel:", "#")):
                    continue

                file_part = target.split("#", 1)[0]
                if not file_part:
                    continue

                resolved = (doc.parent / file_part).resolve()
                if not resolved.exists():
                    failures.append(
                        f"{doc.relative_to(ROOT)} -> {target} (resolved {resolved})"
                    )

        self.assertEqual([], failures, "broken relative documentation links:\n" + "\n".join(failures))


if __name__ == "__main__":
    unittest.main()

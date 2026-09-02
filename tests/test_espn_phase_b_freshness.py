import ast
from pathlib import Path
import unittest

import espn_fantasy_server as espn
from espn_reference import PLAYER_STATS_MAP, POSITION_MAP, PRO_TEAM_MAP, SETTINGS_SCORING_FORMAT_MAP


class ESPNWrapperRetirementTests(unittest.TestCase):
    def test_no_production_module_imports_espn_api(self):
        offenders = []
        for path in sorted(Path('.').glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or '').startswith('espn_api'):
                    offenders.append((str(path), node.lineno, node.module))
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith('espn_api'):
                            offenders.append((str(path), node.lineno, alias.name))
        self.assertEqual(offenders, [])

    def test_no_cached_wrapper_league_surface_remains(self):
        self.assertFalse(hasattr(espn.ESPNFantasyFootballAPI, 'get_league'))

    def test_project_owned_constant_tables_preserve_critical_contract_values(self):
        self.assertEqual(POSITION_MAP[0], 'QB')
        self.assertEqual(POSITION_MAP['FLEX'], 23)
        self.assertEqual(PRO_TEAM_MAP[7], 'DEN')
        self.assertEqual(PLAYER_STATS_MAP[41], 'receivingReceptions')
        self.assertEqual(SETTINGS_SCORING_FORMAT_MAP[41]['abbr'], 'RECS')


if __name__ == '__main__':
    unittest.main()

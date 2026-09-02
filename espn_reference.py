"""Project-defined ESPN Fantasy reference identifiers.

ESPN's fantasy payload uses numeric identifiers for roster slots, pro teams and
statistics. These mappings contain the factual identifiers needed by this
project and use project-owned labels. Unknown identifiers are preserved by the
parsers rather than requiring an exhaustive third-party table.
"""

from __future__ import annotations


_SLOT_NAMES = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE",
    6: "TE", 7: "OP", 8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB",
    13: "S", 14: "DB", 15: "DP", 16: "D/ST", 17: "K", 18: "P", 19: "HC",
    20: "BE", 21: "IR", 22: "", 23: "RB/WR/TE", 24: "ER", 25: "Rookie",
}
_POSITION_INPUTS = {
    "QB": 0, "RB": 2, "WR": 4, "TE": 6, "D/ST": 16, "K": 17,
    "FLEX": 23, "DT": 8, "DE": 9, "LB": 10, "DL": 11, "CB": 12,
    "S": 13, "DB": 14, "DP": 15, "HC": 19,
}
POSITION_MAP = {**_SLOT_NAMES, **_POSITION_INPUTS}

PRO_TEAM_MAP = {
    0: "None", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE",
    6: "DAL", 7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND",
    12: "KC", 13: "LV", 14: "LAR", 15: "MIA", 16: "MIN", 17: "NE",
    18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT",
    24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WSH", 29: "CAR",
    30: "JAX", 33: "BAL", 34: "HOU",
}

PLAYER_STATS_MAP = {
    0: "passingAttempts", 1: "passingCompletions", 2: "passingIncompletions",
    3: "passingYards", 4: "passingTouchdowns", 20: "passingInterceptions",
    23: "rushingAttempts", 24: "rushingYards", 25: "rushingTouchdowns",
    41: "receivingReceptions", 42: "receivingYards", 43: "receivingTouchdowns",
    53: "receivingReceptions", 58: "receivingTargets", 59: "receivingYardsAfterCatch",
    60: "receivingYardsPerReception", 62: "twoPointConversions", 68: "fumbles",
    72: "lostFumbles", 73: "turnovers", 74: "madeFieldGoals50Plus",
    77: "madeFieldGoals40To49", 80: "madeFieldGoalsUnder40", 83: "madeFieldGoals",
    84: "attemptedFieldGoals", 85: "missedFieldGoals", 86: "madeExtraPoints",
    87: "attemptedExtraPoints", 88: "missedExtraPoints", 89: "defensive0PointsAllowed",
    90: "defensive1To6PointsAllowed", 91: "defensive7To13PointsAllowed",
    92: "defensive14To17PointsAllowed", 94: "defensiveTouchdowns",
    95: "defensiveInterceptions", 97: "defensiveBlockedKicks", 98: "defensiveSafeties",
    99: "defensiveSacks", 101: "kickoffReturnTouchdowns", 102: "puntReturnTouchdowns",
    105: "defenseSpecialTeamsTouchdowns", 106: "defensiveForcedFumbles",
    120: "defensivePointsAllowed", 127: "defensiveYardsAllowed",
    187: "defensivePointsAllowed", 201: "madeFieldGoals60Plus",
    202: "attemptedFieldGoals60Plus", 203: "missedFieldGoals60Plus",
    205: "defensiveTwoPointReturns", 206: "defensiveTwoPointReturns",
}


def _rule(abbr: str, label: str) -> dict[str, str]:
    return {"abbr": abbr, "label": label}


SETTINGS_SCORING_FORMAT_MAP = {
    0: _rule("PA", "Pass attempts"),
    1: _rule("PC", "Pass completions"),
    2: _rule("INC", "Incomplete passes"),
    3: _rule("PY", "Passing yards"),
    4: _rule("PTD", "Passing touchdowns"),
    20: _rule("INT", "Interceptions thrown"),
    23: _rule("RA", "Rushing attempts"),
    24: _rule("RY", "Rushing yards"),
    25: _rule("RTD", "Rushing touchdowns"),
    41: _rule("RECS", "Receptions"),
    42: _rule("REY", "Receiving yards"),
    43: _rule("RETD", "Receiving touchdowns"),
    53: _rule("REC", "Per reception"),
    58: _rule("TGT", "Receiving targets"),
    68: _rule("FUM", "Fumbles"),
    72: _rule("FUML", "Fumbles lost"),
    74: _rule("FG50+", "Field goals made from 50+ yards"),
    77: _rule("FG40-49", "Field goals made from 40-49 yards"),
    80: _rule("FG<40", "Field goals made under 40 yards"),
    83: _rule("FGM", "Field goals made"),
    85: _rule("FGMISS", "Field goals missed"),
    86: _rule("XPM", "Extra points made"),
    88: _rule("XPMISS", "Extra points missed"),
    89: _rule("PA0", "Defense: 0 points allowed"),
    90: _rule("PA1-6", "Defense: 1-6 points allowed"),
    91: _rule("PA7-13", "Defense: 7-13 points allowed"),
    92: _rule("PA14-17", "Defense: 14-17 points allowed"),
    94: _rule("DTD", "Defensive touchdowns"),
    95: _rule("DINT", "Defensive interceptions"),
    97: _rule("BLKK", "Blocked kicks"),
    98: _rule("SAFE", "Safeties"),
    99: _rule("SACK", "Sacks"),
    101: _rule("KRTD", "Kick return touchdowns"),
    102: _rule("PRTD", "Punt return touchdowns"),
    105: _rule("DSTTD", "Defense/special-teams touchdowns"),
    106: _rule("FF", "Forced fumbles"),
    120: _rule("PA", "Points allowed"),
    127: _rule("YA", "Yards allowed"),
    187: _rule("PA", "Points allowed"),
    201: _rule("FG60+", "Field goals made from 60+ yards"),
    205: _rule("D2PT", "Defensive two-point returns"),
    206: _rule("D2PT", "Defensive two-point returns"),
}

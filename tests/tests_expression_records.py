#!/usr/bin/env python3
"""Coverage test for every ExpressionAbilityVariable in extracted Records.

This deliberately exercises the production ``_variable_value`` path with a
small synthetic ability context.  The underlying variables are assigned a
non-zero value so arithmetic, including division, is observable; ESC uses the
same escalation state that live resolution uses.
"""

import ast
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abilities.framework.statics import _variable_value
from gamedata.records import deserialize_line


ROOT = Path(__file__).resolve().parents[1]
ABILITY_RECORDS = ROOT / "Records" / "AbilityTemplate.jsonl"


def _expression_variables(value):
    found = []
    if isinstance(value, dict):
        if str(value.get("_t", "")).split(".")[-1] == \
                "ExpressionAbilityVariable":
            found.append(value)
        for child in value.values():
            found.extend(_expression_variables(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_expression_variables(child))
    return found


def _names(expression):
    tree = ast.parse(expression, mode="eval")
    return {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }


def test_all_records_expressions():
    db = sqlite3.connect(":memory:")
    failures = []
    occurrences = 0

    with ABILITY_RECORDS.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if line.startswith('"$$$---'):
                continue
            record = deserialize_line(line).to_dict(include_metadata=True)
            for expression in _expression_variables(record):
                occurrences += 1
                name = expression["m_Name"]
                text = expression["m_ExpressionText"]
                variables = [{
                    "_t": "Game.Shared.Mechanics.Abilities."
                          "ExpressionAbilityVariable",
                    "m_Name": name,
                    "m_ExpressionText": text,
                }]
                # Supply deterministic values for every referenced base name.
                # ESC is resolved from bstate by the production evaluator.
                for referenced in sorted(_names(text) - {name, "ESC"}):
                    variables.append({
                        "_t": "Game.Shared.Mechanics.Abilities."
                              "AbilityConstant",
                        "m_Name": referenced,
                        "m_DefaultValue": 7,
                    })
                raw = json.dumps({"m_Variables": variables})
                value = _variable_value(
                    db, 1, {"player_escalation_uses": 2}, raw, name, 5, 0)
                if value is None:
                    failures.append((line_number, name, text))

    db.close()
    assert not failures, "Unsupported Records expressions: %s" % failures
    print("evaluated %d ExpressionAbilityVariable occurrences" % occurrences)


if __name__ == "__main__":
    test_all_records_expressions()

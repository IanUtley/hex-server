"""Static parity audit of card mechanics: Python RulesPort vs HexClient C#.

Card behavior is data-driven: every card's abilities are Records BOM graphs
interpreted by the shared C# effect classes and by the Python RulesPort leaf
set.  This script compares the two interpreters per effect type and then walks
every card graph, reporting:

  1. effect / nested template types with no Python implementation reference,
  2. C# fields read by an effect class that the Python implementation never
     mentions, when cards actually carry the field (likely missing behavior),
  3. per-card graph problems: missing/dangling Records references, unmapped
     CardModifier properties, effect nodes carrying a field gap,
  4. per-card value gaps: IntAttrModifier attribute names nothing consumes,
     AttributeModifier flag strings the production bit decoder cannot map,
     modifier operations outside the handled set, and ActivateTriggered
     keywords ``ability_matches_keyword`` cannot match.

The value checks are the sharpest: an effect can reference a valid type and
field yet name an attribute/keyword no Python code ever consults, which is
invisible to manual card-by-card reading but shows up here.

Read-only: it opens ``hconnect.db`` for the card list and reads Records via
``rules_port.bom_fields``.  It never mutates game state.

Usage:
    python3 card_audit.py                 # summary to stdout
    python3 card_audit.py --json out.json # write the full report
    python3 card_audit.py --card "Name"   # audit one card
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from gamedata import DEFAULT_RECORD_STORE
from gamedata.semantics import card_ability_graphs
from rules_port.bom_fields import effect_template
from rules_port.coverage import NATIVE_EFFECTS, STRUCTURAL_EFFECTS

CS_DIRS = (
    ROOT / "HexClient/Assembly-CSharp-firstpass/Game/Shared/Mechanics/Abilities",
    ROOT / "HexClient/Assembly-CSharp-firstpass/Game/Shared/Mechanics",
    ROOT / "HexClient/Assembly-CSharp-firstpass/Reckoning/Game",
)

PRODUCTION_FILES = (
    sorted((ROOT / "rules_port").glob("*.py"))
    + sorted((ROOT / "abilities").rglob("*.py"))
    + [ROOT / "gamedata/semantics.py", ROOT / "game_engine.py"]
)

CONSUMER_FILES = (
    sorted((ROOT / "rules_port").glob("*.py"))
    + sorted((ROOT / "abilities").rglob("*.py"))
    + [ROOT / "game_engine.py", ROOT / "hconnect_server.py"]
)


def _reference_text() -> str:
    """Executable string constants, for intattr/keyword consumption checks.

    Stored intattrs are always looked up by their literal name, so only code
    that actually branches on the string can consume one.  Comments, display
    payloads (``domain/events.py`` wire armor fields) and AI heuristics are
    deliberately excluded: mentioning a name there is not a rules consumer.
    """
    chunks = []
    for path in CONSUMER_FILES:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings):
                chunks.append(node.value)
    return "\n".join(chunks)

ZERO_GUID = "00000000-0000-0000-0000-000000000000"

# Self-identifier used only in client log formatting, plus the logger handle.
CSHARP_NOISE = {"m_Logger", "m_TemplateId"}

# Explicit GUID references plus the flag fields that make an empty reference a
# legitimate random/filter selection instead of a dangling one.
ABILITY_REF_FIELDS = {
    "ActivateAbilityEffectTemplate": (
        "m_AbilityToInvoke", ("m_RandomlyLuckyOrUnlucky",)),
    "GrantAbilityEffectTemplate": (
        "m_GrantedAbilityTemplateId",
        ("m_RandomInspirePower", "m_RandomChampionChargePower",
         "m_AllSocketedPowersOfSource", "m_AllSocketedPowersOfTarget",
         "m_AllSocketedPowersOfMyMaster", "m_AllPaymentPowersOfSource",
         "m_AllPaymentPowersOfTarget", "m_AllRememberedPowers")),
}

CARD_REF_FIELDS = {
    "SummonTokenTroopAbilityEffectTemplate": (
        "m_CardTemplateId", ("m_CardFilter", "m_Terminus")),
    "SummonXTokenTroopsAbilityEffectTemplate": (
        "m_CardTemplateId", ("m_CardFilter", "m_Terminus")),
}

def _csharp_consumed_intattrs() -> set[str]:
    """IntAttrs string names read by HexClient game code.

    ``new IntAttrs("Name", ...)`` declares the name; any ``IntAttrs.Field``
    reference in ``Game/**`` (outside the declaration file and the client's
    card-text RulesParser) means the runtime consults it.  A marker only the
    client UI or card-text parser mentions is not a server behavior gap.
    """
    declaration = (ROOT / "HexClient/Assembly-CSharp-firstpass/Game/Shared/"
                   "Mechanics/IntAttrs.cs")
    if not declaration.is_file():
        return set()
    text = declaration.read_text(encoding="utf-8", errors="replace")
    field_to_name = dict(re.findall(
        r"(\w+)\s*=\s*new IntAttrs\(\"([^\"]+)\"", text))
    game_dir = ROOT / "HexClient/Assembly-CSharp-firstpass/Game"
    consumed: set[str] = set()
    for path in game_dir.rglob("*.cs"):
        if path.name == "IntAttrs.cs" or "AI" in path.parts:
            # The C# AI heuristics read markers (Toxified, Enthralled) for
            # search pruning only; the Python AI is a separate implementation,
            # so those are not shared rule consumers.
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        for member in re.findall(r"IntAttrs\.(\w+)", body):
            name = field_to_name.get(member)
            if name:
                consumed.add(name)
    return consumed


def _short_type(value: Any) -> str:
    if value is None:
        return ""
    type_name = getattr(value, "type_name", None)
    if not type_name and isinstance(value, Mapping):
        type_name = value.get("_t")
    return str(type_name or "").rsplit(".", 1)[-1]


def _field_is_active(value: Any) -> bool:
    return value is not None and value is not False and value != 0 and value != ""


def _string_literals() -> tuple[set[str], set[str]]:
    all_literals: set[str] = set()
    field_literals: set[str] = set()
    for path in PRODUCTION_FILES:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                all_literals.add(node.value)
                if re.fullmatch(r"m_[A-Za-z0-9_]+", node.value):
                    field_literals.add(node.value)
    return all_literals, field_literals


def _decorated_registrations() -> set[str]:
    names: set[str] = set()
    for path in sorted((ROOT / "abilities").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                func = deco.func
                name = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else "")
                if name not in ("effect", "leaf_register"):
                    continue
                for arg in deco.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        names.add(arg.value)
    return names


def _csharp_class_file(class_name: str) -> Path | None:
    for directory in CS_DIRS:
        candidate = directory / f"{class_name}.cs"
        if candidate.is_file():
            return candidate
    return None


def _clean_csharp(text: str) -> str:
    """Drop field declarations and property accessors, keep method bodies."""
    lines = []
    declaration = re.compile(
        r"^\s*(public|private|protected|internal)\s[^(){};]*"
        r"\bm_[A-Za-z0-9_]+\s*(=[^;]*)?;\s*$")
    for line in text.splitlines():
        if declaration.match(line):
            continue
        lines.append(line)
    body = "\n".join(lines)
    body = re.sub(r"\breturn\s+this\.m_[A-Za-z0-9_]+\s*;", "", body)
    body = re.sub(r"\bthis\.m_[A-Za-z0-9_]+\s*=\s*value\s*;", "", body)
    return body


def _csharp_reads(class_name: str, seen: set[str] | None = None) -> tuple[set[str], list[str]]:
    seen = seen if seen is not None else set()
    path = _csharp_class_file(class_name)
    if path is None or class_name in seen:
        return set(), []
    seen.add(class_name)
    text = path.read_text(encoding="utf-8", errors="replace")
    fields = set(re.findall(r"\bm_[A-Za-z0-9_]+", _clean_csharp(text)))
    fields -= CSHARP_NOISE
    sources = [str(path.relative_to(ROOT))]
    base = re.search(r"\bclass\s+\w+\s*:\s*([A-Za-z0-9_]+)", text)
    if base:
        parent_fields, parent_sources = _csharp_reads(base.group(1), seen)
        fields |= parent_fields
        sources.extend(parent_sources)
    return fields, sources


def _nested_short_types(value: Any, out: set[str]) -> None:
    short = _short_type(value)
    if short:
        out.add(short)
    if isinstance(value, Mapping):
        for child in value.values():
            _nested_short_types(child, out)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _nested_short_types(child, out)


def _guid_of(value: Any) -> str:
    if isinstance(value, Mapping):
        guid = value.get("m_Guid")
        if isinstance(guid, str) and guid.lower() != ZERO_GUID:
            return guid.lower()
    return ""


def _used_effect_types(db: sqlite3.Connection) -> dict[str, int]:
    rows = db.execute(
        "SELECT effect_type, COUNT(*) FROM ability_effects GROUP BY 1 "
        "ORDER BY 2 DESC")
    return {str(name): int(count) for name, count in rows if name}


def _effect_data_fields(
        db: sqlite3.Connection) -> tuple[dict[str, set[str]], set[str]]:
    fields: dict[str, set[str]] = defaultdict(set)
    nested: set[str] = set()
    rows = db.execute(
        "SELECT DISTINCT effect_guid, effect_type FROM ability_effects")
    for guid, effect_type in rows:
        template = effect_template(guid)
        if not template:
            continue
        fields[str(effect_type)] |= {
            key for key in template
            if isinstance(key, str) and key.startswith("m_")}
        _nested_short_types(template, nested)
    return fields, nested


def _field_parity(used: dict[str, int], data_fields: dict[str, set[str]],
                  python_fields: set[str]) -> list[dict[str, Any]]:
    gaps = []
    for effect_type in used:
        csharp_fields, sources = _csharp_reads(effect_type)
        for field in sorted((csharp_fields & data_fields.get(effect_type, set()))
                            - python_fields):
            gaps.append({
                "effect_type": effect_type,
                "field": field,
                "csharp_sources": sources,
            })
    return gaps


def _nested_type_report(nested: set[str],
                        python_literals: set[str]) -> list[str]:
    return sorted(name for name in nested if name not in python_literals)


HANDLED_OPERATIONS = {"add", "remove", "set", "clear", "removeall",
                      "increment", "subtract"}

MATCHED_KEYWORDS = {"deathcry", "momentum", "deploy"}


def _value_issues(base: dict[str, Any], effect, template,
                  reference_lower: str,
                  csharp_intattrs: set[str]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if effect.concrete_type == "CardModifierAbilityEffectTemplate":
        modifier = template.get("m_Modifier")
        if isinstance(modifier, Mapping):
            kind = _short_type(modifier)
            operation = str(modifier.get("m_Operation") or "")
            if (operation and operation.lower() not in HANDLED_OPERATIONS
                    and not (operation.lower() == "unknown"
                             and modifier.get("m_Double"))):
                issues.append({**base, "kind": "unusual_operation",
                               "detail": f"{kind}.{operation}"})
            if kind == "IntAttrModifier":
                attribute = str(modifier.get("m_Attribute") or "")
                consumed = attribute and re.search(
                    r"\b" + re.escape(attribute.lower()) + r"\b", reference_lower)
                if ("insteadof" in attribute.lower() or
                        attribute.lower().endswith("creationbonus")):
                    # Creation-replacement markers are evaluated generically
                    # by creation_effects.replacement_* -- the literal name is
                    # never compared in Python code.
                    consumed = True
                if (attribute and not consumed
                        and attribute in csharp_intattrs):
                    issues.append({**base, "kind": "unconsumed_intattr",
                                   "detail": attribute})
            elif kind == "AttributeModifier":
                flags = str(modifier.get("m_AttributeFlags") or "")
                if flags and not flags.isdigit():
                    from rules_port.attribute_effects import _flags
                    if not _flags(flags):
                        issues.append({**base,
                                       "kind": "unmapped_attribute_flag",
                                       "detail": flags})
    elif effect.concrete_type == "ActivateTriggeredAbilityEffectTemplate":
        keyword = str(template.get("m_Keyword") or "")
        normalized = keyword.lower()
        if normalized.endswith("ies"):
            normalized = normalized[:-3] + "y"
        elif normalized.endswith("s"):
            normalized = normalized[:-1]
        if keyword and normalized not in MATCHED_KEYWORDS:
            issues.append({**base, "kind": "unmatched_keyword",
                           "detail": keyword})
    return issues


def _graph_issues(store, graphs, gaps_by_type, reference_lower: str,
                  csharp_intattrs: set[str]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for graph in graphs:
        for order, effect in enumerate(graph.effects):
            base = {"ability_guid": graph.guid, "ability_name": graph.name,
                    "effect_order": order, "effect_type": effect.concrete_type,
                    "effect_guid": effect.guid}
            if effect.template is None:
                issues.append({**base, "kind": "missing_effect_template",
                               "detail": effect.guid})
                continue
            template = effect.template
            condition = (effect.condition_guid or "").lower()
            if condition and condition != ZERO_GUID and store.get(
                    "AbilityEffectConditionTemplate", condition) is None:
                issues.append({**base, "kind": "missing_condition_template",
                               "detail": condition})
            reference = ABILITY_REF_FIELDS.get(effect.concrete_type)
            if reference:
                field, fallback_flags = reference
                target_guid = _guid_of(template.get(field))
                fallback = any(_field_is_active(template.get(flag))
                               for flag in fallback_flags)
                if fallback:
                    pass
                elif not target_guid or store.get(
                        "AbilityTemplate", target_guid) is None:
                    issues.append({**base, "kind": "dangling_ability_ref",
                                   "detail": f"{field}={target_guid or '<empty>'}"})
            reference = CARD_REF_FIELDS.get(effect.concrete_type)
            if reference:
                field, fallback_fields = reference
                target_guid = _guid_of(template.get(field))
                fallback = any(
                    template.get(name) not in (None, {}, [])
                    for name in fallback_fields)
                if fallback:
                    pass
                elif not target_guid or store.get(
                        "CardTemplate", target_guid) is None:
                    issues.append({**base, "kind": "dangling_card_ref",
                                   "detail": f"{field}={target_guid or '<empty>'}"})
            if effect.concrete_type == "CardModifierAbilityEffectTemplate":
                from rules_port.metadata import modifier_metadata
                metadata = modifier_metadata(effect.guid, template=template)
                if not str(metadata.get("property") or ""):
                    issues.append({
                        **base, "kind": "unmapped_card_modifier",
                        "detail": _short_type(template.get("m_Modifier"))
                        or "<none>"})
            for gap in gaps_by_type.get(effect.concrete_type, ()):
                if _field_is_active(template.get(gap)):
                    issues.append({**base, "kind": "field_gap",
                                   "detail": gap})
            issues.extend(_value_issues(
                base, effect, template, reference_lower, csharp_intattrs))
        target_ids = list(getattr(graph.source, "target_template_guids", ()))
        for index, target_guid in enumerate(target_ids):
            if store.get("AbilityTargetTemplate", target_guid) is None:
                issues.append({
                    "ability_guid": graph.guid, "ability_name": graph.name,
                    "effect_order": -1, "effect_type": "",
                    "effect_guid": "", "kind": "missing_target_template",
                    "detail": f"index={index} {target_guid}"})
    return issues


def build_report(card_filter: str | None = None) -> dict[str, Any]:
    db = sqlite3.connect(str(ROOT / "hconnect.db"))
    try:
        used = _used_effect_types(db)
        data_fields, nested_types = _effect_data_fields(db)
        cards = db.execute(
            "SELECT guid, name, is_pve, no_pvp FROM card_templates "
            "ORDER BY name").fetchall()
    finally:
        db.close()

    python_literals, python_fields = _string_literals()
    reference_lower = _reference_text().lower()
    csharp_intattrs = _csharp_consumed_intattrs()
    handled = set(NATIVE_EFFECTS) | set(STRUCTURAL_EFFECTS) | _decorated_registrations()
    unhandled = [name for name in used if name not in handled]
    missing_nested = _nested_type_report(nested_types, python_literals)
    gaps = _field_parity(used, data_fields, python_fields)
    gaps_by_type: dict[str, list[str]] = defaultdict(list)
    for gap in gaps:
        gaps_by_type[gap["effect_type"]].append(gap["field"])

    store = DEFAULT_RECORD_STORE
    card_reports = []
    issue_counts: Counter[str] = Counter()
    for guid, name, is_pve, no_pvp in cards:
        if card_filter and card_filter.lower() not in (name or "").lower():
            continue
        graphs = card_ability_graphs(store, guid)
        if not graphs:
            continue
        issues = _graph_issues(store, graphs, gaps_by_type, reference_lower,
                               csharp_intattrs)
        for issue in issues:
            issue_counts[issue["kind"]] += 1
        card_reports.append({
            "guid": guid,
            "name": name,
            "pve": bool(is_pve),
            "pvp_legal": not is_pve and not no_pvp,
            "ability_count": len(graphs),
            "issues": issues,
        })
    card_reports.sort(key=lambda entry: (-len(entry["issues"]), entry["name"]))

    value_gaps: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    field_gap_cards: dict[tuple[str, str], list[str]] = defaultdict(list)
    for entry in card_reports:
        for issue in entry["issues"]:
            if issue["kind"] == "field_gap":
                field_gap_cards[
                    (issue["effect_type"], issue["detail"])].append(
                        entry["name"])
                continue
            key = (issue["kind"], issue["effect_type"], issue["detail"])
            value_gaps[key].append(entry["name"])
    gap_list = [{
        "kind": kind,
        "effect_type": effect_type,
        "detail": detail,
        "card_count": len(set(names)),
        "sample_cards": sorted(set(names))[:10],
    } for (kind, effect_type, detail), names in value_gaps.items()]
    gap_list.sort(key=lambda item: (-item["card_count"], item["kind"]))
    for gap in gaps:
        names = field_gap_cards.get((gap["effect_type"], gap["field"]), ())
        gap["card_count"] = len(set(names))
        gap["sample_cards"] = sorted(set(names))[:10]

    return {
        "summary": {
            "cards_audited": len(card_reports),
            "effect_types_used": len(used),
            "unhandled_effect_types": len(unhandled),
            "missing_nested_types": len(missing_nested),
            "field_gaps": len(gaps),
            "value_gaps": len(gap_list),
            "cards_with_issues": sum(
                1 for entry in card_reports if entry["issues"]),
            "pvp_cards_with_issues": sum(
                1 for entry in card_reports
                if entry["issues"] and entry["pvp_legal"]),
            "issue_counts": dict(issue_counts.most_common()),
        },
        "unhandled_effect_types": unhandled,
        "missing_nested_types": missing_nested,
        "field_gaps": gaps,
        "value_gaps": gap_list,
        "cards": card_reports,
    }


def print_summary(report: dict[str, Any], top: int) -> None:
    summary = report["summary"]
    print(f"Cards audited:            {summary['cards_audited']}")
    print(f"Effect types used:        {summary['effect_types_used']} "
          f"(unhandled: {summary['unhandled_effect_types']})")
    print(f"Nested types w/o Python:  {summary['missing_nested_types']}")
    print(f"C# field gaps:            {summary['field_gaps']}")
    print(f"Cards with issues:        {summary['cards_with_issues']} "
          f"(PvP-legal: {summary['pvp_cards_with_issues']})")
    if summary["issue_counts"]:
        print("Issue counts:             " +
              ", ".join(f"{kind}={count}" for kind, count
                        in summary["issue_counts"].items()))
    if report["unhandled_effect_types"]:
        print("\nUnhandled effect types:")
        for name in report["unhandled_effect_types"]:
            print(f"  {name}")
    if report["missing_nested_types"]:
        print("\nNested types with no Python literal:")
        for name in report["missing_nested_types"]:
            print(f"  {name}")
    if report["field_gaps"]:
        print("\nC# field gaps (field present in card data):")
        for gap in report["field_gaps"]:
            print(f"  {gap['effect_type']}.{gap['field']} "
                  f"cards={gap.get('card_count', 0)}")
            for card in gap.get("sample_cards", ()):
                print(f"      {card}")
    if report["value_gaps"]:
        print("\nValue gaps (authored value has no Python consumer):")
        for gap in report["value_gaps"]:
            print(f"  [{gap['kind']}] {gap['effect_type']} = "
                  f"{gap['detail']}  cards={gap['card_count']}")
            if gap["sample_cards"]:
                print(f"      e.g. {', '.join(gap['sample_cards'][:5])}")
    print(f"\nTop {top} cards by issue count:")
    for entry in report["cards"][:top]:
        if not entry["issues"]:
            break
        kinds = Counter(issue["kind"] for issue in entry["issues"])
        detail = ", ".join(f"{kind}x{count}" for kind, count
                           in kinds.most_common())
        print(f"  {entry['name'][:48]:48s} {len(entry['issues']):3d}  {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="write full report JSON")
    parser.add_argument("--card", help="filter card names (substring)")
    parser.add_argument("--top", type=int, default=25,
                        help="cards to list in the console summary")
    args = parser.parse_args()

    report = build_report(args.card)
    print_summary(report, args.top)
    if args.json:
        args.json.write_text(
            json.dumps(report, indent=2, sort_keys=False), encoding="utf-8")
        print(f"\nFull report: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Card text vs implemented ability audit.

For every card template, read the printed game text of each authored ability
and compare it with the ability's typed Records graph (effects, typed amounts,
attribute/TAC keywords, target player filters).  The report is a triage aid,
not a rules engine: it flags the cases where the text makes an unambiguous
claim the implementation never has to honour, or where a typed number
contradicts the printed one.

Usage:
    python3 AssetExtraction/audit_card_text.py
    python3 AssetExtraction/audit_card_text.py --json /tmp/card_text.json
    python3 AssetExtraction/audit_card_text.py --only "Necrophage Sensei"
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TAG_RE = re.compile(r"<[^>]+>")
_PLACEHOLDER_RE = re.compile(r"#([A-Za-z_]+)#")
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
                 "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                 "ten": 10, "twelve": 12, "twenty": 20, "x": 0}

_DRAW_RE = re.compile(r"\bdraws?\s+(a|an|one|two|three|four|five|six|seven|"
                      r"eight|nine|ten|twelve|\d+|\bX\b)", re.IGNORECASE)
_DAMAGE_RE = re.compile(r"\bdeals?\s+(\d+)\s+damage", re.IGNORECASE)
_ATK_RE = re.compile(r"([+-])\s*(\d+)\s*\[ATK\]")
_DEF_RE = re.compile(r"([+-])\s*(\d+)\s*\[DEF\]")
_HEAL_RE = re.compile(r"\bgains?\s+(\d+)\s+health", re.IGNORECASE)
_LOSE_RE = re.compile(r"\bloses?\s+(\d+)\s+health", re.IGNORECASE)
_SUMMON_RE = re.compile(r"\b(?:summon|create)s?\s+(a|an|one|two|"
                        r"three|four|five|six|seven|eight|nine|ten|\d+)",
                        re.IGNORECASE)
_COST_RE = re.compile(r"cost\s*([+-])\s*\(?\s*(\d+)", re.IGNORECASE)
_MILL_RE = re.compile(r"\bburies?\s+(a|an|one|two|three|four|five|six|seven|"
                      r"eight|nine|ten|\d+)", re.IGNORECASE)

_KEYWORD_CLAIMS = ("Flight", "Speed", "Crush", "Lethal", "Rage", "Steadfast",
                   "Spellshield", "Skyguard", "Juggernaught", "Immortal",
                   "Defensive", "FirstStrike", "Swiftstrike", "Lifedrain",
                   "Unblockable", "Tunneling", "Mobilize", "Escalation",
                   "Inspire", "Deploy", "Deathcry")

# Effect class -> implemented claim kinds.
_EFFECT_CLAIMS = {
    "DrawNCardsAbilityEffectTemplate": {"draw"},
    "DrawCardAbilityEffectTemplate": {"draw"},
    "BuryCardAbilityEffectTemplate": {"mill"},
    "DestroyCardAbilityEffectTemplate": {"destroy"},
    "DestroyCardByDefenseAbilityEffectTemplate": {"destroy"},
    "VoidCardAbilityEffectTemplate": {"void"},
    "DiscardCardAbilityEffectTemplate": {"discard", "zone_move"},
    "DiscardOrSacrificeCardAbilityEffectTemplate": {"discard", "sacrifice"},
    "SacrificeCardAbilityEffectTemplate": {"sacrifice"},
    "SummonTokenTroopAbilityEffectTemplate": {"summon"},
    "SummonXTokenTroopsAbilityEffectTemplate": {"summon"},
    "CreateTokenCopyAbilityEffectTemplate": {"summon", "copy"},
    "CreateTokenMatchingTargetAbilityEffectTemplate": {"summon"},
    "ConscriptAbilityEffectTemplate": {"summon", "card_gain"},
    "TransformCardAbilityEffectTemplate": {"transform"},
    "TransformCardAtRandomAbilityEffectTemplate": {"transform"},
    "TransformSelfAbilityEffectTemplate": {"transform"},
    "TransformCardToTargetAbilityEffectTemplate": {"transform"},
    "TransformCardIntoReplicaAbilityEffectTemplate": {"transform"},
    "UntapCardAbilityEffectTemplate": {"ready"},
    "TapCardAbilityEffectTemplate": {"tap"},
    "RevealCardsAbilityEffectTemplate": {"reveal"},
    "MoveCardToZoneEffectTemplate": {"zone_move"},
    "PutTopOfDeckIntoHandAbilityEffectTemplate": {"draw", "card_gain"},
    "ActivateAbilityEffectTemplate": {"recursion"},
    "ActivatePowerAbilityEffectTemplate": {"recursion"},
    "PlayCardAbilityEffectTemplate": {"play"},
    "BuiltInPlayCardAbilityEffectTemplate": {"play"},
    "Battle2CardsAbilityEffectTemplate": {"battle"},
    "CounterSpellAbilityEffectTemplate": {"counter"},
    "InterruptSpellAbilityEffectTemplate": {"counter"},
    "GrantAbilityEffectTemplate": {"grant"},
    "ReplenishResourcesAbilityEffectTemplate": {"resource"},
}

_MODIFIER_CLAIMS = {
    "damage": {"damage"},
    "attack": {"attack_mod"},
    "defense": {"defense_mod"},
    "healhero": {"heal"},
    "loselife": {"lose_life"},
    "counter": {"counter"},
    "cardcost": {"cost_mod"},
    "attribute": {"keyword"},
    "intattr": {"keyword", "int_attr"},
    "subtype": {"subtype"},
    "currentresource": {"resource"},
    "totalresource": {"resource"},
    "chargepoints": {"charge"},
    "spellpoints": {"spellpoints"},
    "threshold": {"threshold"},
    "cardthreshold": {"threshold"},
    "damageshield": {"shield"},
    "damagemultiplier": {"modifier"},
    "damageimmunity": {"immunity"},
    "targetingimmunity": {"immunity"},
    "attackimmunity": {"immunity"},
    "blockimmunity": {"immunity"},
    "setherohealth": {"health_set"},
    "healhero": {"heal"},
}

# Cards whose printed claim is fulfilled outside battle resolution (PvE
# collection/reward bookkeeping), with the reason recorded here.
ACCEPTED_CARDS = {
    "Spectral Oak":
        "daily-login collection reward resolved outside battle state",
}

_CLAIM_RULES = [
    ("draw", _DRAW_RE, 1),
    ("damage", _DAMAGE_RE, 1),
    ("attack_mod", _ATK_RE, 2),
    ("defense_mod", _DEF_RE, 2),
    ("heal", _HEAL_RE, 1),
    ("lose_life", _LOSE_RE, 1),
    ("summon", _SUMMON_RE, 1),
    ("cost_mod", _COST_RE, 2),
    ("mill", _MILL_RE, 1),
]


def plain_text(value):
    text = _TAG_RE.sub("", str(value or ""))
    text = _PLACEHOLDER_RE.sub(lambda m: m.group(1), text)
    return text.replace("[ARROWR]", " ").replace("&nbsp;", " ").strip()


def _number(token):
    token = str(token).strip().lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _field(value, name, default=None):
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(name, default)
    if hasattr(value, "field"):
        try:
            return value.field(name, default)
        except (TypeError, ValueError):
            return default
    return default


def _spec_dict(spec):
    value = getattr(spec, "template", None)
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raw = getattr(value, "raw", None)
    if isinstance(raw, dict):
        return raw
    to_dict = getattr(value, "to_dict", None)
    return to_dict() if callable(to_dict) else {}


def _constant_number(field, variables=None):
    """Resolve a typed EffectField to a constant when it is one."""
    if field is None:
        return None
    if isinstance(field, (int, float)):
        return int(field)
    if isinstance(field, str):
        return _number(field)
    kind = str(_field(field, "_t", "")).rsplit(".", 1)[-1]
    if kind in ("EffectConstant", "AbilityConstant", "Constant"):
        return _number(_field(field, "m_Value",
                              _field(field, "m_DefaultValue")))
    if kind in ("EffectInputVariable", "EffectAbilityVariable"):
        name = str(_field(field, "m_InputVariableName", "") or "")
        if variables and name in variables and variables[name]:
            return variables[name]
        # Dynamic operands ("AForEach...", "EqualToTheNumber...") are not
        # constants; only a literal number-word name is comparable.
        token = _number(name)
        return token if name.strip().lower() in _NUMBER_WORDS else None
    return None


def _effect_ability_guids(raw):
    """Extract referenced ability GUIDs from a list/single ResourceId field."""
    found = []
    values = raw if isinstance(raw, (list, tuple)) else (raw,)
    for value in values:
        guid = str(_field(value, "m_Guid", "") or "").lower()
        if guid and guid != "0" * 36:
            found.append(guid)
    return found


def _claim_kinds_for_modifier(prop):
    for key, mapped in _MODIFIER_CLAIMS.items():
        if key in prop:
            return mapped, key
    return set(), ""


def effect_evidence(effect, graph, variables=None, depth=0):
    """Return ({claim kinds}, {numeric kinds: values}, {keywords}) for one effect."""
    claims = set()
    numbers = defaultdict(list)
    keywords = set()
    concrete = effect.concrete_type
    claims.update(_EFFECT_CLAIMS.get(concrete, ()))
    spec = _spec_dict(effect)
    if concrete == "CardModifierAbilityEffectTemplate":
        modifier = _field(spec, "m_Modifier", {}) or {}
        prop = str(_field(modifier, "_t", "")).rsplit(".", 1)[-1].lower()
        mapped, _key = _claim_kinds_for_modifier(prop)
        claims.update(mapped)
        amount = _constant_number(_field(modifier, "m_InputValue"), variables)
        if amount is None and _field(modifier, "m_Amount_DEPRECATED") is not None:
            amount = _number(_field(modifier, "m_Amount_DEPRECATED"))
        for kind in ("damage", "attack_mod", "defense_mod", "heal",
                     "lose_life", "cost_mod"):
            if kind in claims:
                numbers[kind].append(amount)
        if "keyword" in claims:
            flags = _field(modifier, "m_AttributeFlags", "") or ""
            for name in str(flags).replace(",", "|").split("|"):
                name = name.strip()
                if name and name.lower() != "unknown":
                    keywords.add(name.lower())
            attr = str(_field(modifier, "m_Attribute", "") or "")
            if attr:
                keywords.add(attr.lower())
            if "creat" in attr.lower():
                # Creation-replacement/bonus markers are engine-level
                # creation behavior, not an ordinary summon effect.
                claims.add("summon")
    elif concrete == "SummonTokenTroopAbilityEffectTemplate":
        # C# uses m_AmountField whenever it is present, even when the field is
        # a dynamic expression that has no constant value in the audit.
        amount_field = _field(spec, "m_AmountField", None)
        if amount_field is not None:
            numbers["summon"].append(
                _constant_number(amount_field, variables))
        else:
            numbers["summon"].append(
                _constant_number(_field(spec, "m_Amount"), variables))
        token_guid = str(_field(
            _field(spec, "m_CardTemplateId", {}) or {}, "m_Guid", "") or "")
        if (token_guid and token_guid != "0" * 36 and depth < 4):
            # A Choosing summon materializes authored choice cards; the
            # printed outcome (draw/resource) lives on those card templates.
            from gamedata import DEFAULT_RECORD_STORE, card_ability_graphs
            for token_graph in card_ability_graphs(
                    DEFAULT_RECORD_STORE, token_guid):
                for token_effect in token_graph.effects:
                    c, n, k = effect_evidence(
                        token_effect, token_graph,
                        child_variables(token_graph), depth + 1)
                    claims.update(c)
                    keywords.update(k)
                    # The card's own ability numbers are not the parent's
                    # printed amount; only its capability evidence counts.
    elif concrete == "SummonXTokenTroopsAbilityEffectTemplate":
        numbers["summon"].append(_number(_field(spec, "m_BaseAmount", None)))
    elif concrete in ("DrawNCardsAbilityEffectTemplate",
                      "DrawCardAbilityEffectTemplate"):
        numbers["draw"].append(
            _constant_number(_field(spec, "m_InputValue"), variables))
    elif concrete == "BuryCardAbilityEffectTemplate":
        numbers["mill"].append(
            _constant_number(_field(spec, "m_Amount"), variables))
    elif concrete == "RepeatingAbilityEffectTemplate":
        loop = _constant_number(_field(spec, "m_LoopCount"), variables)
        if loop:
            numbers["repeat"] = [loop]
        nested = _field(spec, "m_RepeatingEffect", None)
        if isinstance(nested, dict) and depth < 4:
            from types import SimpleNamespace
            nested_type = str(_field(nested, "_t", "")).rsplit(".", 1)[-1]
            if nested_type:
                shim = SimpleNamespace(
                    concrete_type=nested_type,
                    template=SimpleNamespace(raw=nested))
                c, n, k = effect_evidence(shim, graph, variables, depth + 1)
                claims.update(c)
                keywords.update(k)
                for key, values in n.items():
                    numbers[key].extend(values)
    if depth >= 4:
        return claims, dict(numbers), keywords
    # Recursion: invoked/granted/choice children contribute their claims too.
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    child_guids = []
    for field_name in ("m_AbilityToInvoke", "m_GrantedAbilityTemplateId"):
        child_guids.extend(_effect_ability_guids(_field(spec, field_name)))
    if concrete == "DoubleChoiceAbilityEffectTemplate":
        child_guids.extend(_effect_ability_guids(_field(spec, "m_Choices")))
    if (concrete == "GrantAbilityEffectTemplate" and
            _field(spec, "m_RandomInspirePower", False)):
        keywords.add("inspire")
    for child_guid in dict.fromkeys(child_guids):
        child = ability_graph(DEFAULT_RECORD_STORE, child_guid)
        if child is None:
            continue
        child_trigger = str(
            child.trigger_event_type or "").rsplit(".", 1)[-1]
        for keyword, event in _TRIGGER_EVENTS.items():
            if event == child_trigger:
                keywords.add(keyword)
        for child_effect in child.effects:
            c, n, k = effect_evidence(
                child_effect, child, child_variables(child), depth + 1)
            claims.update(c)
            keywords.update(k)
            for key, values in n.items():
                numbers[key].extend(values)
    return claims, dict(numbers), keywords


def child_variables(graph):
    values = {}
    for variable in graph.variables or ():
        name = variable.field("m_Name", "")
        if name:
            values[str(name)] = variable.field(
                "m_DefaultValue", variable.field("m_Value", 0))
    return values


def ability_evidence(graph):
    claims, numbers, keywords = set(), defaultdict(list), set()
    variables = child_variables(graph)
    for effect in graph.effects:
        c, n, k = effect_evidence(effect, graph, variables)
        claims.update(c)
        keywords.update(k)
        for key, values in n.items():
            numbers[key].extend(values)
    return claims, dict(numbers), keywords


def card_attribute_keywords(record):
    keywords = set()
    flags = str(record.field("m_AttributeFlags", "") or "")
    if flags and flags.lower() != "unknown":
        for name in flags.replace(",", "|").split("|"):
            name = name.strip()
            if name:
                keywords.add(name.lower())
    return keywords


def tac_keywords(record):
    from rules_port.tac import _tac_attr_hash, decode_tac_tree
    serialized = record.field("m_SerializedTAC", None)
    data = (serialized.field("data", "") if hasattr(serialized, "field")
            else serialized.get("data", "") if isinstance(serialized, dict)
            else serialized)
    if not data:
        return set()
    tree = decode_tac_tree(str(data))
    return {name.lower() for name in _KEYWORD_CLAIMS
            if _tac_attr_hash(name) in tree}


_EVENT_CLAUSE_KINDS = {"draw", "damage", "heal", "lose_life", "mill"}

_TRIGGER_EVENTS = {
    "deploy": "CardEnteredZoneEvent",
    "deathcry": "CardEnteredZoneEvent",
    "inspire": "AsEntersPlayEvent",
}

# Printed keyword -> the client attribute/IntAttr that implements it.
_KEYWORD_ALIASES = {
    "crush": {"juggernaught"},
    "lifedrain": {"spiritdrain"},
    "swiftstrike": {"firststrike"},
    "unblockable": {"cantbeblocked"},
    "spellshield": {"spellshield"},
    "skyguard": {"skyguard"},
    "flight": {"flight"},
}


def _in_event_clause(text, position):
    """Whether a match sits before the comma of a When/If event clause.

    "When a champion draws a card, ..." describes an event; "When you play
    this, draw a card" describes the effect.  The comma separates them.
    """
    for match in re.finditer(r"\b(when|whenever|if|until)\b", text,
                             re.IGNORECASE):
        start = match.start()
        if start > position:
            continue
        comma = text.find(",", start)
        boundary = comma if comma != -1 else len(text)
        if start < position < boundary:
            return True
    return False


def find_mismatches(graph):
    trigger_name = str(graph.trigger_event_type or "").rsplit(".", 1)[-1]
    text = plain_text(graph.game_text) + " " + plain_text(
        graph.activation_game_text)
    claims = set()
    claimed_numbers = defaultdict(list)
    for kind, regex, group in _CLAIM_RULES:
        signed = kind in ("attack_mod", "defense_mod", "cost_mod")
        for match in regex.finditer(text):
            if (kind in _EVENT_CLAUSE_KINDS
                    and _in_event_clause(text, match.start())):
                continue
            value = _number(match.group(group))
            if value and signed:
                sign = match.group(group - 1) if group > 1 else "+"
                if group == 1 or str(sign).lower() not in ("+", "-"):
                    sign = "+"
                value = -value if str(sign) == "-" else value
            if signed and value == 0:
                # "+0[DEF]" is a printed no-op, not a defense claim.
                continue
            claims.add(kind)
            if value:
                claimed_numbers[kind].append(value)
    keyword_claims = set()
    for keyword in _KEYWORD_CLAIMS:
        # Only a grant phrase claims the keyword: "gets/has/gains/and <Kw>".
        # A reference such as "troops with Flight have ..." does not, and a
        # conditional event clause ("If that troop has Flight, ...") does not
        # grant it either.
        pattern = re.compile(
            r"\b(?:gets?|has|gains?|and)\b[^.;]{0,45}?\b"
            + re.escape(keyword.lower()) + r"\b", re.IGNORECASE)
        for match in pattern.finditer(text):
            if _in_event_clause(text, match.start()):
                continue
            if keyword.lower() == "tunneling" and re.search(
                    r"tunneling counters?", text[max(0, match.start() - 30):],
                    re.IGNORECASE):
                continue
            keyword_claims.add(keyword)
            break
    implemented, numbers, keywords = ability_evidence(graph)
    if "inspire" in keyword_claims:
        for effect in graph.effects:
            if _field(_spec_dict(effect), "m_RandomInspirePower", False):
                keywords.add("inspire")
    findings = []
    for kind in sorted(claims):
        if kind not in implemented:
            findings.append({"field": kind, "issue": "text claim has no effect",
                             "text": [match.group(0) for match in
                                      _rule_for(kind).finditer(text)][:3]})
            continue
        typed = [value for value in numbers.get(kind, [])
                 if value is not None and value > 0]
        printed = claimed_numbers.get(kind, [])
        if typed and printed and not any(
                value in typed for value in printed):
            findings.append({"field": kind, "issue": "number mismatch",
                             "printed": printed, "typed": sorted(set(typed))})
    for keyword in sorted(keyword_claims):
        wanted = {keyword.lower()} | _KEYWORD_ALIASES.get(keyword.lower(), set())
        if wanted & keywords:
            continue
        if _TRIGGER_EVENTS.get(keyword.lower()) == trigger_name:
            continue
        findings.append({"field": "keyword", "issue": "keyword not granted",
                         "keyword": keyword})
    return findings, sorted(claims), sorted(implemented)


def _rule_for(kind):
    for name, regex, _group in _CLAIM_RULES:
        if name == kind:
            return regex
    return re.compile("$^")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default=os.path.join(
        ROOT, "docs", "generated", "card_text_audit.json"))
    parser.add_argument("--only", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    from gamedata import DEFAULT_RECORD_STORE, card_ability_graphs

    report = {}
    for card in DEFAULT_RECORD_STORE.load("CardTemplate"):
        name = str(card.field("m_Name", "") or "")
        if args.only and args.only.lower() not in name.lower():
            continue
        graphs = card_ability_graphs(DEFAULT_RECORD_STORE, card.guid)
        if not graphs:
            continue
        card_keywords = card_attribute_keywords(card) | tac_keywords(card)
        card_claims = set()
        for graph in graphs:
            _f, claims, implemented = find_mismatches(graph)
            card_claims.update(implemented)
            keywords = ability_evidence(graph)[2]
            card_keywords.update(keywords)
            trigger = str(graph.trigger_event_type or "").rsplit(".", 1)[-1]
            for keyword, event in _TRIGGER_EVENTS.items():
                if event == trigger:
                    card_keywords.add(keyword)
        for graph in graphs:
            findings, _claims, _impl = find_mismatches(graph)
            if not findings:
                continue
            if name in ACCEPTED_CARDS:
                continue
            kept = []
            for finding in findings:
                keyword = finding.get("keyword", "").lower()
                wanted = _KEYWORD_ALIASES.get(keyword, set()) | {keyword}
                if wanted & card_keywords:
                    continue
                # A claim implemented by another ability on the same card is
                # not missing; the printed text is duplicated on a variant.
                if (finding["issue"] == "text claim has no effect"
                        and finding["field"] in card_claims):
                    continue
                kept.append(finding)
            if kept:
                report.setdefault(name or card.guid, []).append({
                    "guid": card.guid, "ability": graph.guid,
                    "ability_name": graph.name,
                    "text": plain_text(graph.game_text),
                    "trigger": graph.trigger_event_type,
                    "findings": kept,
                })

    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, sort_keys=True)

    issue_counts = Counter()
    for entries in report.values():
        for entry in entries:
            for finding in entry["findings"]:
                issue_counts[(finding["field"], finding["issue"])] += 1
    print(f"cards with findings: {len(report)}")
    for (field, issue), count in issue_counts.most_common():
        print(f"  {count:5d}  {field}: {issue}")
    print(f"\nwrote {os.path.relpath(args.json, ROOT)}")


if __name__ == "__main__":
    main()

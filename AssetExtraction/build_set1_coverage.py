#!/usr/bin/env python3
"""Build a metadata-driven Set 1 ability and targeting coverage report.

This tool is deliberately separate from the live rules engine.  It reads the
canonical SQLite metadata in read-only mode, inventories every Set 1 card,
normalizes ability/effect/target signatures, and chooses a greedy
representative-card set that covers every discovered feature.

The exact signatures are retained in the JSON output for later test generation.
The representative set is selected from semantic feature atoms, so a card can
cover several related behaviors at once.  Every card remains in the inventory;
cards outside the representative set are "smoke-only", not ignored.

Usage:
    python3 AssetExtraction/build_set1_coverage.py
    python3 AssetExtraction/build_set1_coverage.py --out-dir /tmp/set1-coverage
"""

import argparse
import hashlib
import json
import os
import sqlite3
from collections import Counter, defaultdict


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "hconnect.db")
DEFAULT_OUT = os.path.join(ROOT, "docs", "generated")
SET1_GUID = "0382f729-7710-432b-b761-13677982dcd2"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"


def short_type(value):
    """Return the stable final C# type name from a serialized type name."""
    text = str(value or "")
    return text.rsplit(".", 1)[-1]


def load_json(value, default):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def canonical(value):
    """Make nested metadata deterministic without discarding semantics."""
    if isinstance(value, dict):
        return {str(k): canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    return value


def digest(value):
    payload = json.dumps(canonical(value), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def feature(kind, value):
    return "%s:%s" % (kind, value)


def split_flags(value):
    return sorted(x for x in str(value or "").split("|") if x)


def walk_types(node, out):
    """Collect serialized C# type names from an arbitrary metadata tree."""
    if isinstance(node, dict):
        if node.get("_t"):
            out.add(short_type(node["_t"]))
        for value in node.values():
            walk_types(value, out)
    elif isinstance(node, list):
        for value in node:
            walk_types(value, out)


def filter_types(node):
    out = set()
    walk_types(node, out)
    return sorted(out)


def target_record(row):
    (template_id, game_text, is_auto, is_random, optional, explicit,
     player_filter, collection_flags, min_count, max_count, filter_json,
     target_kind) = row
    filt = load_json(filter_json, {})
    normalized = {
        "player_filter": player_filter or "",
        "collection_flags": split_flags(collection_flags),
        "min_target_count": int(min_count or 0),
        "max_target_count": int(max_count or 0),
        "is_auto_target": bool(is_auto),
        "is_random_target": bool(is_random),
        "optional": bool(optional),
        "explicit": bool(explicit),
        "target_kind": target_kind or "",
        "filter": canonical(filt),
    }
    signature = digest(normalized)
    features = {
        feature("target:signature", signature),
        feature("target:player", player_filter or ""),
        feature("target:count", "%d-%d" % (int(min_count or 0),
                                              int(max_count or 0))),
        feature("target:kind", target_kind or ""),
    }
    if is_auto:
        features.add(feature("target:selection", "auto"))
    if explicit:
        features.add(feature("target:selection", "explicit"))
    if not is_auto and not explicit:
        features.add(feature("target:selection", "implicit"))
    if is_random:
        features.add(feature("target:selection", "random"))
    if optional:
        features.add(feature("target:selection", "optional"))
    for zone in split_flags(collection_flags):
        features.add(feature("target:zone", zone))
    for filter_type in filter_types(filt):
        features.add(feature("target:filter", filter_type))
    return {
        "template_id": template_id,
        "game_text": game_text or "",
        "signature": signature,
        "normalized": normalized,
        "features": sorted(features),
        "semantic_features": sorted(
            x for x in features if not x.startswith("target:signature:")),
    }


def casting_name(value):
    values = {64: "QuickAction", 8: "BasicAction"}
    return values.get(int(value or 0), "Value%d" % int(value or 0))


def _guid_from_target_value(value):
    if isinstance(value, dict):
        guid = value.get("m_Guid")
        if guid and isinstance(guid, str):
            return guid.lower()
    return ""


def extract_target_refs(raw_json, ability_target_ids):
    """Collect target-template GUIDs from both standard and effect targets.

    The client stores activation targets in ``m_AbilityTargetTemplateIds`` but
    stores several effect/cost targets separately, such as
    ``m_PutIntoDeckTarget``.  Time Bug is the important example: its generic
    activation player target and its two-card put-into-deck target are both
    meaningful, but only the former is present in card_abilities_meta.
    """
    refs = []
    seen = set()

    def add(role, guid):
        guid = str(guid or "").lower()
        if not guid or guid == ZERO_GUID:
            return
        key = (role, guid)
        if key not in seen:
            seen.add(key)
            refs.append(key)

    for target_id in ability_target_ids:
        add("ability_target", target_id)

    raw = load_json(raw_json, {})

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                lower = str(key).lower()
                if "target" in lower:
                    # The activation list is already supplied by
                    # card_abilities_meta; do not add a second copy from the
                    # raw record under a different role.
                    if lower == "m_abilitytargettemplateids":
                        walk(value)
                        continue
                    if isinstance(value, dict):
                        add(str(key), _guid_from_target_value(value))
                    elif isinstance(value, list):
                        for item in value:
                            add(str(key), _guid_from_target_value(item))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(raw)
    return refs


def ability_record(ability_guid, meta, effects, targets):
    (casting, is_manual, activation_cost, uses_game, uses_turn, cooldown,
     exhausts, is_triggered, target_ids, trigger_event, game_text, raw_json) = meta
    target_ids = load_json(target_ids, [])
    target_ids = [str(x).lower() for x in target_ids if x]
    target_refs = extract_target_refs(raw_json, target_ids)
    target_records = []
    missing_targets = []
    for role, target_id in target_refs:
        target = targets.get(target_id)
        if target is None:
            missing_targets.append({"role": role, "template_id": target_id})
        else:
            target = dict(target)
            target["role"] = role
            target_records.append(target)

    effect_records = []
    missing_effect_type = []
    for row in effects:
        (_, effect_guid, effect_order, effect_type, param, group_id,
         condition_id, target_index, effect_instance_id,
         contingent_effect_instance_id, secondary_target_index,
         recalculate_targets, is_optional, duration, output_variables) = row
        effect_type = effect_type or ""
        if not effect_type:
            missing_effect_type.append(effect_guid)
        effect_records.append({
            "order": int(effect_order or 0),
            "type": effect_type,
            "param": load_json(param, param or ""),
            "effect_group_id": int(group_id or 0),
            "condition_id": condition_id or "",
            "target_index": int(target_index if target_index is not None else -1),
            "effect_instance_id": int(effect_instance_id if effect_instance_id is not None else -1),
            "contingent_effect_instance_id": int(contingent_effect_instance_id if contingent_effect_instance_id is not None else -1),
            "secondary_target_index": int(secondary_target_index if secondary_target_index is not None else -1),
            "recalculate_targets": int(recalculate_targets if recalculate_targets is not None else -1),
            "optional": bool(is_optional),
            "duration": duration or "Instant",
            "output_variables": load_json(output_variables, {}),
        })

    target_signatures = [
        {"role": x["role"], "signature": x["signature"]}
        for x in target_records
    ]
    effect_signature = [
        {
            "type": x["type"],
            "param": canonical(x["param"]),
            "group": x["effect_group_id"],
            "condition": x["condition_id"],
            "target": x["target_index"],
            "secondary_target": x["secondary_target_index"],
            "duration": x["duration"],
            "optional": x["optional"],
        }
        for x in effect_records
    ]
    exact_signature = {
        "casting": int(casting or 0),
        "manual": bool(is_manual),
        "activation_cost": int(activation_cost or 0),
        "uses_per_game": int(uses_game or 0),
        "uses_per_turn": int(uses_turn or 0),
        "cooldown": int(cooldown or 0),
        "exhausts_on_use": bool(exhausts),
        "triggered": bool(is_triggered),
        "trigger_event": short_type(trigger_event),
        "targets": target_signatures,
        "effects": effect_signature,
    }

    features = {
        feature("ability:signature", digest(exact_signature)),
        feature("ability:casting", casting_name(casting)),
        feature("ability:mode", "manual" if is_manual else (
            "triggered" if is_triggered else "passive")),
    }
    if trigger_event:
        features.add(feature("ability:trigger", short_type(trigger_event)))
    if activation_cost:
        features.add(feature("ability:activation_cost", int(activation_cost)))
    if uses_game:
        features.add(feature("ability:uses_per_game", int(uses_game)))
    if uses_turn:
        features.add(feature("ability:uses_per_turn", int(uses_turn)))
    if cooldown:
        features.add(feature("ability:cooldown", int(cooldown)))
    if exhausts:
        features.add(feature("ability:exhausts", "true"))
    for target in target_records:
        features.update(target["features"])
    for missing in missing_targets:
        features.add(feature("target:missing", missing["template_id"]))
    for effect in effect_records:
        effect_type = short_type(effect["type"])
        if effect_type:
            leaf_signature = digest({
                "type": effect_type,
                "param": canonical(effect["param"]),
                "condition": effect["condition_id"],
                "target": effect["target_index"],
                "secondary_target": effect["secondary_target_index"],
                "duration": effect["duration"],
                "optional": effect["optional"],
            })
            effect["leaf_type"] = effect_type
            effect["leaf_signature"] = leaf_signature
            features.add(feature("bom:leaf_type", effect_type))
            features.add(feature("bom:leaf_signature", leaf_signature))
        if effect["duration"] and effect["duration"] != "Instant":
            features.add(feature("effect:duration", effect["duration"]))
        if effect["condition_id"]:
            features.add(feature("effect:condition", effect["condition_id"]))
        if effect["optional"]:
            features.add(feature("effect:optional", "true"))
    for effect_guid in missing_effect_type:
        features.add(feature("effect:missing_type", effect_guid))

    semantic_features = set(
        x for x in features
        if not x.startswith("ability:signature:")
        and not x.startswith("bom:leaf_signature:")
        and not x.startswith("target:signature:"))
    for target in target_records:
        semantic_features.update(target["semantic_features"])

    return {
        "ability_guid": ability_guid,
        "game_text": game_text or "",
        "raw_json_present": bool(raw_json),
        "casting_behavior": int(casting or 0),
        "casting_name": casting_name(casting),
        "is_manual": bool(is_manual),
        "is_triggered": bool(is_triggered),
        "trigger_event": trigger_event or "",
        "targets": target_records,
        "target_template_refs": [
            {"role": role, "template_id": template_id}
            for role, template_id in target_refs
        ],
        "missing_target_templates": missing_targets,
        "effects": effect_records,
        "missing_effect_types": missing_effect_type,
        "signature": digest(exact_signature),
        "features": sorted(features),
        "semantic_features": sorted(semantic_features),
    }


def open_readonly(path):
    absolute = os.path.abspath(path)
    return sqlite3.connect("file:%s?mode=ro" % absolute, uri=True)


def build_inventory(db_path, set_guid, selection_mode="semantic"):
    db = open_readonly(db_path)
    try:
        target_rows = db.execute(
            "SELECT template_id, game_text, is_auto_target, is_random_target, "
            "optional, explicit, player_filter, collection_flags, "
            "min_target_count, max_target_count, filter_json, target_kind "
            "FROM target_templates").fetchall()
        targets = {row[0].lower(): target_record(row) for row in target_rows}

        effect_rows = defaultdict(list)
        for row in db.execute(
                "SELECT ability_guid, effect_guid, effect_order, effect_type, "
                "param, effect_group_id, condition_id, target_index, "
                "effect_instance_id, contingent_effect_instance_id, "
                "secondary_target_index, recalculate_targets, is_optional, "
                "effect_duration, output_variables FROM ability_effects "
                "ORDER BY ability_guid, effect_order"):
            effect_rows[row[0].lower()].append(row)

        meta_rows = {}
        for row in db.execute(
                "SELECT ability_guid, casting_behavior, is_manual, "
                "activation_cost, uses_per_game, uses_per_turn, cooldown, "
                "exhausts_on_use, is_triggered, target_template_ids, "
                "trigger_event_type, game_text, raw_json FROM card_abilities_meta"):
            meta_rows[row[0].lower()] = row[1:]

        cards = []
        all_abilities = {}
        missing_meta = []
        missing_effect_rows = []
        for row in db.execute(
                "SELECT guid, name, rarity, card_type, abilities_json, "
                "no_pvp, is_pve FROM card_templates WHERE set_guid=? "
                "ORDER BY name, guid", (set_guid,)):
            guid, name, rarity, card_type, abilities_json, no_pvp, is_pve = row
            ability_ids = load_json(abilities_json, [])
            ability_ids = [str(x).lower() for x in ability_ids if x]
            abilities = []
            card_features = set()
            card_semantic_features = set()
            for ability_guid in ability_ids:
                meta = meta_rows.get(ability_guid)
                if meta is None:
                    missing_meta.append({"card": name, "ability_guid": ability_guid})
                    card_features.add(feature("ability:missing_meta", ability_guid))
                    continue
                effects = effect_rows.get(ability_guid, [])
                if not effects:
                    missing_effect_rows.append({"card": name, "ability_guid": ability_guid})
                record = ability_record(ability_guid, meta, effects, targets)
                abilities.append(record)
                all_abilities[ability_guid] = record
                card_features.update(record["features"])
                card_semantic_features.update(record["semantic_features"])
            if not ability_ids:
                card_features.add(feature("card:mode", "no_abilities"))
                card_semantic_features.add(feature("card:mode", "no_abilities"))
            card = {
                "guid": guid,
                "name": name,
                "rarity": rarity or "",
                "card_type": card_type or "",
                "no_pvp": bool(no_pvp),
                "is_pve": bool(is_pve),
                "ability_guids": ability_ids,
                "abilities": abilities,
                "features": sorted(card_features),
                "coverage_features": sorted(card_semantic_features),
            }
            cards.append(card)

        universe = set()
        candidates = {}
        for card in cards:
            candidate_features = (set(card["features"])
                                  if selection_mode == "exact" else
                                  set(card["coverage_features"]))
            if candidate_features and any(x.startswith("ability:")
                                          or x.startswith("bom:")
                                          or x.startswith("target:")
                                          for x in candidate_features):
                candidates[card["guid"]] = candidate_features
                universe.update(candidate_features)

        selected = []
        uncovered = set(universe)
        while uncovered:
            best_guid = None
            best_new = set()
            for guid, features in candidates.items():
                new = features & uncovered
                if len(new) > len(best_new):
                    best_guid, best_new = guid, new
                elif len(new) == len(best_new) and new and best_guid:
                    current = next(x for x in cards if x["guid"] == best_guid)
                    candidate = next(x for x in cards if x["guid"] == guid)
                    if (candidate["name"].lower(), guid) < (
                            current["name"].lower(), best_guid):
                        best_guid, best_new = guid, new
            if best_guid is None:
                break
            selected.append(best_guid)
            uncovered -= best_new
            del candidates[best_guid]

        selected_set = set(selected)
        for card in cards:
            card["coverage_role"] = (
                "representative" if card["guid"] in selected_set else
                ("smoke_only" if card["ability_guids"] else "no_abilities"))
            candidate_features = (set(card["features"])
                                  if selection_mode == "exact" else
                                  set(card["coverage_features"]))
            card["covered_feature_count"] = len(candidate_features & universe)

        feature_counts = Counter()
        for item in universe:
            feature_counts[item.split(":", 1)[0]] += 1
        selected_cards = [x for x in cards if x["guid"] in selected_set]
        smoke_cards = [x for x in cards if x["coverage_role"] == "smoke_only"]
        no_ability_cards = [x for x in cards if x["coverage_role"] == "no_abilities"]
        duplicate_ability_groups = defaultdict(list)
        for ability_guid, record in all_abilities.items():
            duplicate_ability_groups[record["signature"]].append(ability_guid)
        leaf_catalog = defaultdict(lambda: {
            "leaf_type": "",
            "occurrences": 0,
            "cards": set(),
            "abilities": set(),
            "variants": set(),
        })
        for card in cards:
            for record in card["abilities"]:
                for effect in record["effects"]:
                    leaf_type = effect.get("leaf_type") or short_type(effect["type"])
                    if not leaf_type:
                        continue
                    item = leaf_catalog[leaf_type]
                    item["leaf_type"] = leaf_type
                    item["occurrences"] += 1
                    item["cards"].add(card["guid"])
                    item["abilities"].add(record["ability_guid"])
                    if effect.get("leaf_signature"):
                        item["variants"].add(effect["leaf_signature"])
        leaf_catalog = [
            {
                "leaf_type": item["leaf_type"],
                "occurrences": item["occurrences"],
                "card_guids": sorted(item["cards"]),
                "ability_guids": sorted(item["abilities"]),
                "variant_signatures": sorted(item["variants"]),
            }
            for item in sorted(leaf_catalog.values(),
                               key=lambda x: x["leaf_type"])
        ]
        unique_leaf_variants = {
            effect["leaf_signature"]
            for record in all_abilities.values()
            for effect in record["effects"]
            if effect.get("leaf_signature")
        }
        used_target_ids = {
            target["template_id"]
            for record in all_abilities.values()
            for target in record["targets"]
        }

        return {
            "schema_version": 1,
            "set": {"guid": set_guid, "name": "Shards of Fate"},
            "database": os.path.abspath(db_path),
            "summary": {
                "cards": len(cards),
                "cards_with_abilities": len(cards) - len(no_ability_cards),
                "cards_without_abilities": len(no_ability_cards),
                "ability_references": sum(len(x["ability_guids"]) for x in cards),
                "unique_abilities": len(all_abilities),
                "unique_ability_signatures": len(duplicate_ability_groups),
                "unique_bom_leaf_types": len(leaf_catalog),
                "unique_bom_leaf_signatures": len(unique_leaf_variants),
                "unique_target_signatures": len({
                    target["signature"] for record in all_abilities.values()
                    for target in record["targets"]}),
                "feature_atoms": len(universe),
                "representative_cards": len(selected_cards),
                "smoke_only_cards": len(smoke_cards),
                "cards_without_ability_metadata": len(missing_meta),
                "abilities_without_effect_rows": len(missing_effect_rows),
                "uncovered_features": len(uncovered),
                "selection_mode": selection_mode,
            },
            "feature_counts_by_kind": dict(sorted(feature_counts.items())),
            "bom_leaf_catalog": leaf_catalog,
            "selection_features": sorted(universe),
            "cards": cards,
            "representative_card_guids": selected,
            "missing_metadata": missing_meta,
            "abilities_without_effect_rows": missing_effect_rows,
            "target_templates": sorted(
                (targets[target_id] for target_id in used_target_ids),
                key=lambda x: x["template_id"]),
        }
    finally:
        db.close()


def markdown_report(report):
    summary = report["summary"]
    lines = [
        "# Set 1 BOM Leaf and Target Coverage",
        "",
        "Generated from the read-only canonical SQLite metadata.",
        "Representative selection mode: `%s`. Exact signatures remain in the "
        "JSON inventory; semantic mode intentionally does not require one "
        "card for every exact GUID-level composition." %
        summary["selection_mode"],
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|---|---:|",
    ]
    labels = [
        ("Cards", "cards"),
        ("Cards with abilities", "cards_with_abilities"),
        ("Cards without abilities", "cards_without_abilities"),
        ("Card ability references", "ability_references"),
        ("Unique card abilities (traceability)", "unique_abilities"),
        ("Unique card-ability signatures (traceability)",
         "unique_ability_signatures"),
        ("Unique BOM leaf types", "unique_bom_leaf_types"),
        ("Unique BOM leaf variants", "unique_bom_leaf_signatures"),
        ("Unique target signatures", "unique_target_signatures"),
        ("Feature atoms", "feature_atoms"),
        ("Representative cards", "representative_cards"),
        ("Smoke-only cards", "smoke_only_cards"),
        ("Cards missing ability metadata", "cards_without_ability_metadata"),
        ("Abilities missing BOM rows", "abilities_without_effect_rows"),
        ("Uncovered features", "uncovered_features"),
    ]
    for label, key in labels:
        lines.append("| %s | %s |" % (label, summary[key]))

    lines.extend(["", "## Feature counts", "", "| Feature kind | Count |",
                  "|---|---:|"])
    for kind, count in report["feature_counts_by_kind"].items():
        lines.append("| `%s` | %s |" % (kind, count))

    lines.extend(["", "## BOM leaf catalog", "",
                  "Leaf types are the primary implementation coverage unit; "
                  "variant signatures retain parameterized forms for audit.",
                  "", "| BOM leaf type | Occurrences | Cards | Variants |",
                  "|---|---:|---:|---:|"])
    for leaf in report["bom_leaf_catalog"]:
        lines.append("| `%s` | %s | %s | %s |" % (
            leaf["leaf_type"], leaf["occurrences"],
            len(leaf["card_guids"]), len(leaf["variant_signatures"])))

    lines.extend(["", "## Representative cards", "",
                  "These cards form the greedy set-cover result. Every other "
                  "card remains in the inventory and receives smoke coverage.",
                  "", "| Card | Type | Abilities | Features |", "|---|---|---:|---:|"])
    for card in report["cards"]:
        if card["coverage_role"] == "representative":
            lines.append("| %s | %s | %s | %s |" % (
                card["name"], card["card_type"], len(card["abilities"]),
                card["covered_feature_count"]))

    if report["missing_metadata"]:
        lines.extend(["", "## Missing ability metadata", ""])
        for item in report["missing_metadata"]:
            lines.append("- `%s` — `%s`" % (item["card"], item["ability_guid"]))
    if report["abilities_without_effect_rows"]:
        lines.extend(["", "## Abilities without BOM rows", ""])
        for item in report["abilities_without_effect_rows"]:
            lines.append("- `%s` — `%s`" % (item["card"], item["ability_guid"]))
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="SQLite database (read-only; default: hconnect.db)")
    parser.add_argument("--set-guid", default=SET1_GUID)
    parser.add_argument("--out-dir", default=DEFAULT_OUT)
    parser.add_argument("--selection-mode", choices=("semantic", "exact"),
                        default="semantic",
                        help="Representative selection granularity (default: semantic)")
    args = parser.parse_args(argv)

    report = build_inventory(args.db, args.set_guid, args.selection_mode)
    os.makedirs(args.out_dir, exist_ok=True)
    json_path = os.path.join(args.out_dir, "set1_coverage.json")
    markdown_path = os.path.join(args.out_dir, "set1_coverage.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")
    with open(markdown_path, "w", encoding="utf-8") as fh:
        fh.write(markdown_report(report))

    for key, value in report["summary"].items():
        print("%-36s %s" % (key.replace("_", " "), value))
    print("\nRepresentative cards: %s" % ", ".join(
        x["name"] for x in report["cards"]
        if x["coverage_role"] == "representative"))
    print("\nWrote %s" % json_path)
    print("Wrote %s" % markdown_path)
    return 0 if report["summary"]["uncovered_features"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

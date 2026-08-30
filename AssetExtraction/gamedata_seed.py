"""Extract client reference data from the Hex ``Data/gamedata`` blob.

The client file is a gzip-compressed stream of JSON-like records grouped into
named sections.  This module is deliberately independent of ``static.py`` so
it can be used in three places:

* ``extract_gamedata.py`` can inspect or compare a client installation;
* fresh database creation can seed the normal server tables directly; and
* tests can build a seed manifest without changing the live database.

``HEX_GAMEDATA`` is the preferred environment variable.  ``GAMEDATA`` is
accepted as a compatibility alias because the older one-off extractors use
that name.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - Records parsing still works for JSON
    yaml = None


DEFAULT_GAMEDATA = "/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Data/gamedata"
DEFAULT_RECORDS = Path(__file__).resolve().parents[1] / "Records"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"

# These are the client-derived sections needed by the server schema.  Records
# is a JSONL representation of the same gamedata sections. A local checkout
# may provide it as a fallback; Docker creates it at first startup when a
# mounted gamedata file is available.
RECORD_SECTIONS = (
    "AbilityEffectConditionTemplate",
    "AbilityEffectTemplate",
    "AbilityTargetTemplate",
    "AbilityTemplate",
    "CardCounterTemplate",
    "CardTemplate",
    "ChampionClassData",
    "ChampionTalentData",
    "ChampionTemplate",
    "DeckTemplate",
    "EncounterDeck",
    "InventoryItemData",
    "QuestTemplate",
    "SceneData",
    "ConversationTemplate",
)


def configured_path() -> str | None:
    return os.environ.get("HEX_GAMEDATA") or os.environ.get("GAMEDATA")


def configured_records_path() -> Path:
    value = os.environ.get("HEX_RECORDS")
    return Path(os.path.abspath(os.path.expanduser(value))) if value else DEFAULT_RECORDS


def records_available(path: str | Path | None = None) -> bool:
    root = Path(path) if path else configured_records_path()
    return root.is_dir() and all((root / f"{section}.jsonl").is_file()
                                  for section in RECORD_SECTIONS)


def resolve_path(path: str | None = None) -> str:
    value = path or configured_path() or DEFAULT_GAMEDATA
    expanded = os.path.abspath(os.path.expanduser(value))
    if not os.path.isfile(expanded):
        raise FileNotFoundError(
            f"gamedata file not found: {expanded}. Set HEX_GAMEDATA to the client file."
        )
    return expanded


def load_text(path: str | None = None) -> str:
    with gzip.open(resolve_path(path), "rb") as handle:
        return handle.read().decode("utf-8", "replace")


def load_records_text(path: str | Path | None = None) -> str:
    """Build the normal section stream from the checked-in JSONL records."""
    root = Path(path) if path else configured_records_path()
    if not records_available(root):
        missing = [section for section in RECORD_SECTIONS
                   if not (root / f"{section}.jsonl").is_file()]
        raise FileNotFoundError(
            f"Records source is incomplete at {root}; missing "
            + ", ".join(f"{section}.jsonl" for section in missing)
        )

    sections = []
    for section in RECORD_SECTIONS:
        lines = (root / f"{section}.jsonl").read_text(
            encoding="utf-8", errors="replace").splitlines()
        records = []
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(value if isinstance(value, str) else line)
        sections.append(
            f"\n{section}\n$$--$$\n"
            + "\n$$--$$\n".join(records)
            + "\n$$$---$$$\n"
        )
    return "".join(sections)


def source_available() -> bool:
    """Whether the configured gamedata or local Records source exists."""
    if configured_path():
        return os.path.isfile(os.path.abspath(os.path.expanduser(configured_path())))
    return records_available()


def section_text(data: str, name: str) -> list[str]:
    marker = f"\n{name}\n$$--$$\n"
    start = data.find(marker)
    if start < 0:
        return []
    start += len(marker)
    end = data.find("$$$---$$$", start)
    body = data[start:end] if end >= 0 else data[start:]
    return [record for record in body.split("\n$$--$$\n") if record.strip()]


def parse_record(raw: str) -> dict[str, Any] | None:
    """Parse a record, tolerating the export's trailing commas."""
    cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
    try:
        value = json.loads(cleaned)
    except (TypeError, json.JSONDecodeError):
        # ConversationTemplate records use relaxed JSON with integer object
        # keys (``0 : {...}``).  PyYAML is also what campaign.py uses for the
        # checked-in Records snapshot, and provides a safe parser for this
        # otherwise valid client serialization.
        if yaml is None:
            return None
        try:
            value = yaml.safe_load(cleaned)
        except Exception:
            return None
    if isinstance(value, str):
        try:
            value = json.loads(re.sub(r",\s*([}\]])", r"\1", value))
        except (TypeError, json.JSONDecodeError):
            if yaml is None:
                return None
            try:
                value = yaml.safe_load(value)
            except Exception:
                return None
    return value if isinstance(value, dict) else None


def records(data: str, name: str) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    parsed: list[tuple[str, dict[str, Any]]] = []
    errors = 0
    for raw in section_text(data, name):
        value = parse_record(raw)
        if value is None:
            errors += 1
        else:
            parsed.append((raw, value))
    return parsed, errors


def guid(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("m_Guid") or "").lower()
    return ""


def nested_guid(record: dict[str, Any], field: str) -> str:
    return guid(record.get(field))


def int_value(value: Any, default: int = 0) -> int:
    if isinstance(value, dict):
        value = value.get("m_Value", default)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def json_text(value: Any, default: Any = None) -> str:
    return json.dumps(default if value is None else value, sort_keys=True)


def _card_helpers():
    # Import lazily: static.ensure_schema imports this module during startup,
    # and the extractor must not load the database or rewrite static.py.
    try:
        from AssetExtraction.extract_cards import (  # type: ignore
            _abilities_to_json,
            _attributes_to_int,
            _card_ability_bom,
            _guid,
            _int_field,
            _str_field,
            _threshold_to_json,
            _ability_meta,
            _target_templates,
        )
    except ImportError:  # direct execution from AssetExtraction/
        from extract_cards import (  # type: ignore
            _abilities_to_json,
            _attributes_to_int,
            _card_ability_bom,
            _guid,
            _int_field,
            _str_field,
            _threshold_to_json,
            _ability_meta,
            _target_templates,
        )
    return {
        "abilities_to_json": _abilities_to_json,
        "attributes_to_int": _attributes_to_int,
        "card_ability_bom": _card_ability_bom,
        "guid": _guid,
        "int_field": _int_field,
        "str_field": _str_field,
        "threshold_to_json": _threshold_to_json,
        "ability_meta": _ability_meta,
        "target_templates": _target_templates,
    }


def _target_count(value: Any) -> int:
    if isinstance(value, dict):
        return int_value(value, 1) or 1
    return 1


def _talent_condition(raw: str) -> str:
    if '"m_TriggerCondition"' not in raw:
        return ""
    end = raw.find('", "m_ActivationCost"')
    condition = raw if end < 0 else raw[:end]
    if "RequiresCardsControlled" in condition:
        color = re.search(r'"m_ColorFlags"\s*:\s*"([^"]+)"', condition)
        quantity = re.search(r'"m_RequiredQuantity"\s*:\s*(\d+)', condition)
        if quantity:
            if color:
                return f"pregame_shards_in_deck:{color.group(1)},{quantity.group(1)}"
            return f"pregame_cards_in_deck:{quantity.group(1)}"
    if "IntAttrFilter" in condition and "IsDungeonBoss" in condition:
        return "pregame_is_dungeon"
    return ""


def _phase_and_casting(record: dict[str, Any]) -> tuple[int, int]:
    trigger = record.get("m_TriggerEventType") or {}
    trigger_name = trigger.get("m_InternalType", "") if isinstance(trigger, dict) else str(trigger)
    phases = 4 if "PreGameEvent" in trigger_name else (1 << 10) | (1 << 19)
    casting = {"QuickAction": 64, "BasicAction": 8}.get(
        str(record.get("m_CastingBehavior") or ""), 0
    )
    return phases, casting


def _extract_cards(data: str) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    helpers = _card_helpers()
    cards: list[tuple[Any, ...]] = []
    ability_guids: set[str] = set()
    for raw in section_text(data, "CardTemplate"):
        card_guid = helpers["guid"](raw, "m_Id")
        if not card_guid or len(card_guid) != 36:
            continue
        game_text = helpers["str_field"](raw, "m_GameText")
        rage = helpers["int_field"](raw, "m_RageValue")
        # Older card templates (including Mazat Spearman) store the printed
        # keyword in the template TAC/game text while leaving m_RageValue at
        # zero. Recover the numeric keyword for the normalized card row so
        # combat, AI and CardUpdated see the same metadata as the client.
        if not rage:
            rage_match = re.search(r"\brage\s+(\d+)\b", game_text,
                                   re.IGNORECASE)
            rage = int(rage_match.group(1)) if rage_match else 0
        if rage and not re.search(r"\brage\b", game_text, re.IGNORECASE):
            rage = 0
        sacrifice = ""
        sacrifice_match = re.search(
            r'"m_SacrificeTarget"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', raw
        )
        if sacrifice_match and sacrifice_match.group(1).lower() != ZERO_GUID:
            sacrifice = sacrifice_match.group(1).lower()
        subtype = helpers["str_field"](raw, "m_CardSubtype")
        row = (
            card_guid,
            helpers["guid"](raw, "m_SetId") or "",
            helpers["str_field"](raw, "m_Name"),
            helpers["str_field"](raw, "m_CardRarity") or "Common",
            helpers["int_field"](raw, "m_ResourceCost"),
            helpers["int_field"](raw, "m_BaseAttackValue"),
            helpers["int_field"](raw, "m_BaseDefenseValue"),
            helpers["str_field"](raw, "m_CardType"),
            helpers["int_field"](raw, "m_SocketCount"),
            1 if helpers["int_field"](raw, "m_IneligibleForPvPRandomTemplates") else 0,
            1 if helpers["int_field"](raw, "m_IsPvE") else 0,
            helpers["threshold_to_json"](raw),
            helpers["abilities_to_json"](raw),
            helpers["attributes_to_int"](helpers["str_field"](raw, "m_AttributeFlags")),
            sacrifice,
            helpers["int_field"](raw, "m_VariableCost"),
            helpers["int_field"](raw, "m_VariableCostMinimum"),
            rage,
            subtype,
        )
        cards.append(row)
        ability_guids.update(
            match.lower()
            for match in re.findall(
                r'"m_CardAbilityId"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', raw
            )
        )
    by_guid = {row[0]: row for row in cards}
    bom, discovered = helpers["card_ability_bom"](data, ability_guids)
    ability_guids.update(discovered)
    meta = helpers["ability_meta"](data, ability_guids)
    return sorted(by_guid.values()), bom, meta


def _extract_targets(data: str) -> list[tuple[Any, ...]]:
    helpers = _card_helpers()
    parsed = {}
    for _, record in records(data, "AbilityTargetTemplate")[0]:
        template_id = nested_guid(record, "m_TemplateId")
        if not template_id:
            continue
        parsed[template_id] = record
    rows = {}
    # Keep the same complete target-text universe as extract_cards.py.  Its
    # historical parser intentionally walks from the AbilityTargetTemplate
    # section onward, because the client also stores generated target text in
    # later sections and the server uses those IDs in card metadata.
    for template_id, game_text in helpers["target_templates"](data):
        record = parsed.get(template_id, {})
        rows[template_id] = (
            template_id,
            record.get("m_GameText") or game_text,
            int_value(record.get("m_IsAutoTarget")),
            int_value(record.get("m_IsRandomTarget")),
            int_value(record.get("m_Optional")),
            int_value(record.get("m_Explicit")),
            record.get("m_PlayerFilter") or "",
            record.get("m_CollectionFlags") or "",
            _target_count(record.get("m_MinTargetCount")),
            _target_count(record.get("m_MaxTargetCount")),
            json_text(record.get("m_CardFilter"), {}),
            str(record.get("_t") or "").split(".")[-1],
        )
    return sorted(rows.values())


def _extract_talents(data: str) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], list[str]]:
    ability_records = {}
    for raw in section_text(data, "AbilityTemplate"):
        match = re.search(
            r'"m_AbilityTemplateId"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', raw
        )
        if match:
            ability_records[match.group(1).lower()] = (raw, parse_record(raw) or {})

    talent_rows = []
    ability_rows = []
    ability_guids: set[str] = set()
    for raw, record in records(data, "ChampionTalentData")[0]:
        talent_guid = nested_guid(record, "m_Id")
        if not talent_guid:
            continue
        granted = [
            nested_guid(item.get("m_CardAbilityId") or {}, "")
            for item in (record.get("m_Abilities") or [])
            if isinstance(item, dict) and guid(item.get("m_CardAbilityId"))
        ]
        # The compact helper above intentionally accepts the nested object;
        # normalize it explicitly for readability and malformed records.
        granted = [guid(item.get("m_CardAbilityId")) for item in (record.get("m_Abilities") or []) if isinstance(item, dict)]
        granted = [value for value in granted if value]
        first = granted[0] if granted else None
        first_record = ability_records.get(first, ("", {}))[1] if first else {}
        talent_rows.append(
            (
                talent_guid,
                record.get("m_Name") or "",
                first,
                1 if granted else 0,
                record.get("m_Description") or "",
                int_value(first_record.get("m_ChargePointCost")),
                int_value(first_record.get("m_SpellPointCost")),
            )
        )
        for ability_guid in granted:
            raw_ability, ability = ability_records.get(ability_guid, ("", {}))
            phases, casting = _phase_and_casting(ability)
            ability_rows.append(
                (
                    talent_guid,
                    ability_guid,
                    int_value(ability.get("m_ChargePointCost")),
                    int_value(ability.get("m_SpellPointCost")),
                    phases,
                    casting,
                    _talent_condition(raw_ability),
                    json_text([
                        value for value in (
                            guid(item) for item in
                            (ability.get("m_AbilityTargetTemplateIds") or [])
                        ) if value
                    ], []),
                )
            )
            ability_guids.add(ability_guid)
    helpers = _card_helpers()
    bom, _ = helpers["card_ability_bom"](data, ability_guids)
    return sorted(set(talent_rows)), sorted(set(ability_rows)), bom


def _extract_mandatory_talents(data: str) -> list[tuple[Any, ...]]:
    """Extract class/race talents automatically selected by the client.

    ``TalentManager.AddMandatoryTalents`` selects every non-Normal talent
    whose class-data group and talent are unlocked at the champion's level.
    Keep that authored relationship in the champion-template seed so
    newly-created champions can receive the same IDs before the first profile
    refresh.
    """
    talent_records = {}
    for _, record in records(data, "ChampionTalentData")[0]:
        talent_guid = nested_guid(record, "m_Id")
        if talent_guid:
            talent_records[talent_guid] = record

    rows = []
    for _, record in records(data, "ChampionClassData")[0]:
        race = record.get("m_Race") or ""
        champion_class = record.get("m_Class") or ""
        if race in ("", "None") or champion_class in ("", "None"):
            continue
        for group_order, group in enumerate(record.get("m_TalentGroups") or []):
            group_min_level = int_value(group.get("m_MinLevel"), 1)
            for talent_order, ref in enumerate(group.get("m_Talents") or []):
                talent_guid = guid(ref)
                talent = talent_records.get(talent_guid)
                if not talent or talent.get("m_SelectionType") == "Normal":
                    continue
                min_level = max(
                    group_min_level,
                    int_value(talent.get("m_MinLevel"), 1),
                )
                rows.append((
                    race,
                    champion_class,
                    min_level,
                    talent_guid,
                    group_order * 1000 + talent_order,
                ))
    return sorted(set(rows))


def _extract_gems(data: str) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    try:
        from AssetExtraction.extract_gems import GEM_TYPE_ID  # type: ignore
    except ImportError:
        from extract_gems import GEM_TYPE_ID  # type: ignore
    ability_by_guid = {}
    effect_by_guid = {}
    for raw, record in records(data, "AbilityTemplate")[0]:
        ability_by_guid[nested_guid(record, "m_AbilityTemplateId")] = (raw, record)
    for _, record in records(data, "AbilityEffectTemplate")[0]:
        effect_by_guid[nested_guid(record, "m_TemplateId")] = record

    gem_rows = []
    wanted = set()
    seen = set()
    for _, record in records(data, "InventoryItemData")[0]:
        if not str(record.get("_t") or "").endswith("InventoryGemData"):
            continue
        type_name = record.get("m_GemTypeNew") or ""
        type_id = GEM_TYPE_ID.get(type_name)
        if type_id is None or type_id in seen:
            continue
        abilities = [
            guid(item.get("m_CardAbilityId"))
            for item in (record.get("m_Abilities") or [])
            if isinstance(item, dict) and guid(item.get("m_CardAbilityId"))
        ]
        seen.add(type_id)
        wanted.update(abilities)
        gem_rows.append((type_id, type_name, record.get("m_Name") or "", json.dumps(abilities)))

    meta_rows = []
    effect_rows = []
    for ability_guid in sorted(wanted):
        raw, record = ability_by_guid.get(ability_guid, ("", {}))
        if not record:
            continue
        trigger = record.get("m_TriggerEventType") or {}
        trigger_name = trigger.get("m_InternalType", "") if isinstance(trigger, dict) else str(trigger)
        target_ids = [
            guid(item) for item in (record.get("m_AbilityTargetTemplateIds") or []) if guid(item)
        ]
        meta_rows.append(
            (
                ability_guid,
                1,
                trigger_name,
                record.get("m_GameText") or "",
                json.dumps(record, separators=(",", ":")),
                64,
                0,
                0,
                0,
                0,
                json.dumps(target_ids),
                0,
            )
        )
        for order, entry in enumerate(record.get("m_AbilityEffectList") or []):
            effect_guid = guid(entry.get("m_EffectTemplateId")) if isinstance(entry, dict) else ""
            effect = effect_by_guid.get(effect_guid) or {}
            effect_type = str(effect.get("_t") or "").split(".")[-1]
            modifier = effect.get("m_Modifier") or {}
            param = ""
            if isinstance(modifier, dict) and modifier:
                param = json.dumps(
                    {
                        "property": "intattr",
                        "attribute": modifier.get("m_Attribute", ""),
                        "operation": modifier.get("m_Operation", "Add"),
                        "amount": int_value(modifier.get("m_Value")),
                        "duration": entry.get("m_EffectDuration", "Permanent"),
                        "text": effect.get("m_GameText") or "",
                    }
                )
            elif effect_type == "ActivateAbilityEffectTemplate":
                param = guid(effect.get("m_AbilityToInvoke"))
            effect_rows.append((ability_guid, effect_guid, order, effect_type, param))
    # Use the shared BOM extractor so gem abilities receive the same complete
    # parent-level effect wiring as cards and talents.
    gem_bom, _ = _card_helpers()["card_ability_bom"](data, wanted)
    return sorted(gem_rows), sorted(meta_rows), gem_bom


def _extract_champions(data: str) -> dict[str, list[tuple[Any, ...]]]:
    helpers = _card_helpers()
    ability_records = {}
    for _, record in records(data, "AbilityTemplate")[0]:
        ability_records[nested_guid(record, "m_AbilityTemplateId")] = record

    base_rows = []
    extended_rows = []
    ability_rows = []
    ability_guids: set[str] = set()
    for _, record in records(data, "ChampionTemplate")[0]:
        champion_guid = nested_guid(record, "m_Id")
        if not champion_guid:
            continue
        is_selectable = bool(int_value(record.get("m_IsPlayerSelectable")))
        champion_type = record.get("m_ChampionType") or ""
        if is_selectable and champion_type == "Hero" and record.get("m_Race") not in (None, "None") and record.get("m_Class") not in (None, "None"):
            base_rows.append(
                (
                    champion_guid,
                    record.get("m_Race") or "",
                    record.get("m_Class") or "",
                    record.get("m_Gender") or "",
                    1,
                )
            )
        if champion_type != "PvPChampion":
            continue
        extended_rows.append(
            (
                champion_guid,
                record.get("m_Name") or "",
                record.get("m_Race") or "",
                record.get("m_Class") or "",
                record.get("m_Gender") or "",
                1,
                int_value(record.get("m_StartingHealth"), 20),
                record.get("m_Faction") or "",
            )
        )
        for entry in record.get("m_ChampionAbilities") or []:
            ability_guid = guid(entry.get("m_CardAbilityId")) if isinstance(entry, dict) else ""
            if not ability_guid:
                continue
            ability_guids.add(ability_guid)
            ability = ability_records.get(ability_guid) or {}
            condition = ability.get("m_AbilityCondition") or {}
            threshold = condition.get("m_ColorFlags", "") if isinstance(condition, dict) else ""
            thresholds = []
            if isinstance(condition, dict):
                if str(condition.get("_t") or "").endswith("HasThresholdAbilityCondition"):
                    thresholds.append({"color": condition.get("m_ColorFlags", ""), "quantity": int_value(condition.get("m_RequiredQuantity"))})
                elif str(condition.get("_t") or "").endswith("AndAbilityCondition"):
                    for child in condition.get("m_Conditions") or []:
                        if isinstance(child, dict) and str(child.get("_t") or "").endswith("HasThresholdAbilityCondition"):
                            thresholds.append({"color": child.get("m_ColorFlags", ""), "quantity": int_value(child.get("m_RequiredQuantity"))})
            target_ids = [guid(item) for item in (ability.get("m_AbilityTargetTemplateIds") or []) if guid(item)]
            casting = {"QuickAction": 64, "BasicAction": 8}.get(str(ability.get("m_CastingBehavior") or ""), 0)
            ability_rows.append(
                (
                    champion_guid,
                    record.get("m_Name") or "",
                    ability_guid,
                    ability.get("m_Name") or "",
                    int_value(ability.get("m_ChargePointCost")),
                    int_value(ability.get("m_ActivationCost")),
                    threshold,
                    record.get("m_GameText") or "",
                    casting,
                    json.dumps(thresholds),
                    json.dumps(target_ids),
                )
            )

    class_rows = []
    for _, record in records(data, "ChampionClassData")[0]:
        race = record.get("m_Race") or ""
        champion_class = record.get("m_Class") or ""
        if race and champion_class and race != "None" and champion_class != "None":
            class_rows.append((race, champion_class, int_value(record.get("m_StartingHealth")), int_value(record.get("m_StartingHandSize"))))

    mandatory_by_pair = {}
    for race, champion_class, min_level, talent_guid, talent_order in _extract_mandatory_talents(data):
        if min_level <= 1:
            mandatory_by_pair.setdefault((race, champion_class), []).append(
                (talent_order, talent_guid))
    player_template_rows = []
    for guid_value, race, champion_class, gender, is_player in base_rows:
        default_talents = [talent_guid for _, talent_guid in sorted(
            mandatory_by_pair.get((race, champion_class), []))]
        player_template_rows.append(
            (guid_value, race, champion_class, gender, is_player,
             json.dumps(default_talents)))

    template_rows = [(row[0], row[1], row[3], row[2], row[6], 7) for row in extended_rows]
    champion_effects, discovered = helpers["card_ability_bom"](data, ability_guids)
    champion_meta = helpers["ability_meta"](data, ability_guids | discovered)
    return {
        "champion_templates": sorted(set(player_template_rows)),
        "champion_templates_extended": sorted(set(extended_rows)),
        "champion_template_data": sorted(set(template_rows)),
        "champion_abilities": sorted(set(ability_rows)),
        "champion_class_data": sorted(set(class_rows)),
        "champion_ability_effects": champion_effects,
        "champion_ability_meta": champion_meta,
    }


def _extract_encounters(data: str) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    deck_templates = {}
    for _, record in records(data, "DeckTemplate")[0]:
        deck_guid = nested_guid(record, "m_Id")
        if deck_guid:
            deck_templates[deck_guid] = {
                "cards": record.get("m_DeckResources") or [],
                "champion": nested_guid(record, "m_ChampionId"),
            }
    encounter_decks = {}
    for _, record in records(data, "EncounterDeck")[0]:
        deck_guid = nested_guid(record, "m_Id")
        if deck_guid:
            template_guid = nested_guid(record, "m_DeckTemplateId")
            encounter_decks[deck_guid] = deck_templates.get(template_guid) or {
                "cards": record.get("m_DeckOrder") or [], "champion": ""
            }
    scenes = []
    deck_cards: dict[tuple[str, str], list[Any]] = {}
    for _, record in records(data, "SceneData")[0]:
        if not str(record.get("_t") or "").endswith("EncounterScene"):
            continue
        name = record.get("m_Name") or ""
        # Keep the AZ0 training scenes and the race-specific Crayburn tutorial
        # scenes used by campaign/FRA.  The latter carry the real encounter
        # deck-template mapping (for example Orc -> Vilefang), so omitting
        # them silently causes the battle setup to use placeholder cards.
        if not (name.startswith("AZ0_") or
                name.startswith("AZ 1") or name.startswith("AZ 2") or
                " Tutorial Castle Gatehouse" in name or
                " Tutorial Tower Gatehouse" in name or
                " Tutorial Tower of Penworth" in name):
            continue
        ai_deck = ""
        for player in record.get("m_EncounterPlayers") or []:
            if isinstance(player, dict) and int_value(player.get("m_IsAI")):
                ai_deck = nested_guid(player, "m_EncounterDeck")
                if ai_deck:
                    break
        ai_champion = (encounter_decks.get(ai_deck) or {}).get("champion", "")
        scene_mods = []
        for mod in record.get("m_Mods") or []:
            scene_mods.append({"title": mod.get("m_Title", ""),
                               "description": mod.get("m_Description", ""),
                               "mods": [{"guid": str(x.get("m_Guid") or "").lower()}
                                        for x in (mod.get("m_Mods") or [])
                                        if x.get("m_Guid")]})
        for player in record.get("m_EncounterPlayers") or []:
            if not isinstance(player, dict):
                continue
            target = "AIPlayer" if any(
                str(e.get("m_ModTargetPlayer") or "") == "AIPlayer"
                for group in (player.get("m_EncounterMods") or [])
                for e in (group.get("m_Effects") or []) if isinstance(group, dict)
            ) else ("AIPlayer" if int_value(player.get("m_IsAI")) else "Player")
            for group in player.get("m_EncounterMods") or []:
                for effect in group.get("m_Effects") or []:
                    if not isinstance(effect, dict):
                        continue
                    card = str((effect.get("m_CardId") or {}).get("m_Guid") or "").lower()
                    if card:
                        scene_mods.append({"target": target, "round": int_value(effect.get("m_RoundToApply")),
                                           "mods": [{"guid": card}]})
        rewards = {}
        if name.upper().startswith("AZ 1"):
            rewards.update({"gold": 100, "xp": 100, "one_time": False})
        elif name.upper().startswith("AZ0_"):
            rewards.update({"gold": 100, "xp": 100, "one_time": False})
        elif " TUTORIAL " in f" {name.upper()} ":
            rewards.update({"gold": 200, "xp": 200, "one_time": False})
        if "SHROOM HAUS" in name.upper():
            rewards["card_choice"] = [
                {"name": "Builder Bot", "guid": "85112245-7aed-4a5b-9de9-3c262f11168a"},
                {"name": "Deployment Orders", "guid": "bd25218f-0ed6-4b58-a282-fe9e01eec99e"},
                {"name": "Tricerobot", "guid": "5793dc95-b2e3-40d7-8010-5507d07f5328"},
            ]
        # The captured creature is determined by the battle state, not by a
        # fixed card name.  Keep the reward declaration on the authored scene
        # so the campaign engine can evaluate it generically at game end.
        if name.upper() == "AZ 1 - NODE 03 - WILD CUB":
            rewards.pop("gold", None)
            rewards.pop("xp", None)
            rewards.pop("one_time", None)
            rewards["end_of_game_rewards"] = [
                {"gold": 100, "xp": 100, "one_time": False},
                {"end_of_game_condition": {
                    "type": "void_tamed_troop", "owner": "opponent"},
                 "card_guid": "$condition.template_guid",
                 "quantity": 1, "one_time": True},
            ]
        scenes.append((nested_guid(record, "m_Id"), name, record.get("m_Title") or "", record.get("m_Gameboard") or "", ai_deck, ai_champion, json_text(scene_mods, []), json_text(rewards, {})))
        for card in (encounter_decks.get(ai_deck) or {}).get("cards", []):
            if isinstance(card, dict) and card.get("m_idTemplate"):
                card_guid = guid(card.get("m_idTemplate"))
                quantity = int_value(card.get("m_Count"), 1)
            else:
                card_guid = guid(card)
                quantity = 1
            if card_guid:
                key = (ai_deck, card_guid)
                entry = deck_cards.setdefault(key, [0, [], False])
                gem_types = card.get("m_GemTypesNewList") or [] if isinstance(card, dict) else []
                if gem_types:
                    if not entry[2]:
                        entry[1] = [[] for _ in range(entry[0])]
                    entry[1].extend(gem_types)
                    entry[2] = True
                elif entry[2]:
                    entry[1].extend([[] for _ in range(quantity)])
                entry[0] += quantity
    return sorted(set(scenes)), sorted(
        (deck, card, values[0], json_text(values[1], []))
        for (deck, card), values in deck_cards.items()
    )


def _campaign_node_id(token: str) -> str:
    """Normalize the abbreviated node token used in conversation names.

    The authored names use both ``Node 3`` and ``Node 3A`` while the map
    state uses three-digit IDs (``Node003``/``Node003A``).  Lettered branches
    such as ``Node B4`` are already stable and are left unchanged.
    """
    token = str(token or "").strip().upper()
    match = re.fullmatch(r"(\d+)([A-Z0-9]*)", token)
    if match:
        digits, suffix = match.groups()
        # A handful of authored branch IDs are intentionally two digits
        # (Node00B); ordinary numeric/lettered nodes use three digits.
        width = 2 if digits == "00" and suffix else 3
        return f"Node{digits.zfill(width)}{suffix}"
    return f"Node{token}"


def _extract_campaign_node_conversations(data: str) -> list[tuple[Any, ...]]:
    """Extract AZ1/AZ2 node conversation references from authored names.

    SceneData contains the node presentation data but the extracted area
    records do not retain the Unity NodesPrefab path graph.  Conversation
    templates do retain a stable, explicit ``AZ[1|2] - Node ...`` naming
    convention, so keep those references in a server table rather than
    scattering node-specific GUIDs through campaign.py.
    """
    pattern = re.compile(
        r"^AZ([12])\s*-?\s*Node\s*([0-9A-Za-z]+)"
        r"(?:\s+[A-Z])?\s*-\s*(.+?)\s*$",
        re.IGNORECASE,
    )
    rows = []
    for _, record in records(data, "ConversationTemplate")[0]:
        name = str(record.get("m_Name") or "").strip()
        conversation_guid = nested_guid(record, "m_Id")
        row = _campaign_conversation_row(name, conversation_guid, pattern)
        if row:
            rows.append(row)
    return sorted(set(rows))


def _campaign_conversation_row(name: str, conversation_guid: str,
                                pattern: re.Pattern[str]) -> tuple[Any, ...] | None:
    match = pattern.match(str(name or "").strip())
    if not match or not conversation_guid:
        return None
    campaign_template = f"AZ{match.group(1)}"
    node_id = _campaign_node_id(match.group(2))
    suffix = match.group(3)
    lower = suffix.lower()
    trigger = {"source": "conversation_name", "label": suffix}
    if "player already has fortune" in lower:
        trigger["state"] = "fortune"
    elif "first encounter" in lower:
        trigger["visit"] = "first"
    elif "repeat" in lower:
        trigger["visit"] = "repeat"
    if "success" in lower:
        trigger["outcome"] = "success"
    elif "fail" in lower:
        trigger["outcome"] = "fail"
    return (campaign_template, node_id, conversation_guid,
            name, json_text(trigger, {}), 0, 1)


def extract_campaign_node_conversations(path: str | None = None) -> list[tuple[Any, ...]]:
    """Load only the conversation/scene records needed for campaign links."""
    if path or configured_path():
        return _extract_campaign_node_conversations(load_text(path))

    # Avoid rebuilding the complete multi-section Records stream during each
    # server process startup.  The campaign catalog only needs this one JSONL
    # section, and ConversationTemplate uses relaxed JSON that parse_record()
    # already handles via PyYAML.
    pattern = re.compile(
        r"^AZ([12])\s*-?\s*Node\s*([0-9A-Za-z]+)"
        r"(?:\s+[A-Z])?\s*-\s*(.+?)\s*$",
        re.IGNORECASE,
    )
    rows = []
    conversation_path = configured_records_path() / "ConversationTemplate.jsonl"
    for line in conversation_path.read_text(
            encoding="utf-8", errors="replace").splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw = value if isinstance(value, str) else line
        name_match = re.search(r'"m_Name"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', raw)
        guid_match = re.search(
            r'"m_Id"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"',
            raw,
        )
        row = _campaign_conversation_row(
            name_match.group(1) if name_match else "",
            guid_match.group(1).lower() if guid_match else "",
            pattern,
        )
        if row:
            rows.append(row)
    return sorted(set(rows))


def _quest_text(value: Any) -> str:
    """Return the first useful localized-text value from a quest field."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict):
                text = entry.get("m_Text") or entry.get("m_Description")
                if text:
                    return str(text).strip()
    return ""


def _quest_objective_type(record: dict[str, Any]) -> str:
    kind = str(record.get("_t") or "")
    if "QuestObjectiveDungeon" in kind:
        return "Dungeon"
    if "QuestObjectiveEncounter" in kind:
        return "Encounter"
    if "QuestObjectiveCollect" in kind:
        return "Collect"
    return "Convo" if "QuestObjectiveConversation" in kind else ""


def _extract_quest_templates(data: str) -> list[tuple[Any, ...]]:
    """Extract QuestTemplate rows and their ordered objective metadata."""
    rows = []
    for _, record in records(data, "QuestTemplate")[0]:
        script = str(record.get("m_ScriptName") or "").strip()
        if not script:
            continue
        objectives = []
        for objective in record.get("m_Objectives") or []:
            if not isinstance(objective, dict):
                continue
            item = {
                "id": str(objective.get("m_QuestLocationId") or ""),
                "title": _quest_text(objective.get("m_Title"))
                         or str(objective.get("m_TitleOld") or ""),
                "type": _quest_objective_type(objective),
            }
            encounter = nested_guid(objective, "m_EncounterId")
            dungeon = nested_guid(objective, "m_DungeonId")
            if encounter and encounter != ZERO_GUID:
                item["encounter"] = encounter
            if dungeon and dungeon != ZERO_GUID:
                item["dungeon"] = dungeon
            conversation_ids = []
            old = nested_guid(objective, "m_ConversationIdOld")
            if old and old != ZERO_GUID:
                conversation_ids.append(old)
            for entry in objective.get("m_ConversationIdList") or []:
                guid = nested_guid(entry, "m_Guid") if isinstance(entry, dict) else ""
                if guid and guid != ZERO_GUID and guid not in conversation_ids:
                    conversation_ids.append(guid)
            if conversation_ids:
                item["conversation"] = conversation_ids[0]
                item["conversation_ids"] = conversation_ids
            objectives.append(item)
        title = _quest_text(record.get("m_Title"))
        name = str(record.get("m_Name") or "").strip()
        # Crayburn's quest is hosted by the dungeon campaign.  The remaining
        # authored AZ1/AZ2 quest templates are overworld journal campaigns.
        group = "DUNGEON" if "crayburn" in (script + " " + name).lower() else "AREA"
        rows.append((script, name, title or name, json_text(objectives, []), group,
                     _QUEST_START_HOOKS.get(script, ""), 1))
    return sorted(set(rows))


_QUEST_FACTIONS = {
    "ardent": "Ardent", "aria": "Ardent", "underworld": "Underworld",
    "human": "Ardent", "elf": "Ardent", "coyotle": "Ardent", "orc": "Ardent",
    "dwarf": "Underworld", "necrotic": "Underworld",
    "shin'hare": "Underworld", "vennen": "Underworld",
}
_QUEST_OWNER_FACTIONS = {
    "belarius": "Ardent", "takumi": "Underworld",
}
_QUEST_START_HOOKS = {
    "az01_tamed": "az1_tamed_start",
    "q_seawitch": "az1_find_horwich_sea_start",
}


def _normalise_quest_label(value: str) -> str:
    value = str(value or "").lower().replace("mithral", "mithril")
    return re.sub(r"[^a-z0-9]+", "", value)


def _quest_title_variants(record: dict[str, Any]) -> list[str]:
    values = [record.get("m_Name"), record.get("m_TitleOld"),
              _quest_text(record.get("m_Title"))]
    variants = []
    for value in values:
        value = str(value or "").strip()
        if not value:
            continue
        value = re.sub(r"^AZ\d+\s*-\s*(?:AR|UW)\s*-\s*", "", value,
                       flags=re.IGNORECASE)
        value = re.sub(r"^AZ\d+\s*-\s*", "", value,
                       flags=re.IGNORECASE)
        if value and value not in variants:
            variants.append(value)
        # Authored conversation names commonly omit the leading article.
        if value.lower().startswith("the "):
            variants.append(value[4:])
    return variants


_QUEST_CONVERSATION_ALIASES = {
    # The authored AZ2 map calls this location Brutecrown Delta while the
    # quest template is named after the Bluff encounter.
    "q_brutecrown_bluff": ("Brutecrown Delta",),
}


def _conversation_role(label: str) -> str:
    lower = str(label or "").lower()
    if "quest completed repeating" in lower:
        return "repeat"
    if "quest not complete" in lower:
        return "not_complete"
    if "quest end" in lower:
        return "complete"
    if "quest start" in lower:
        return "start"
    # A number of authored records use ``<Quest> Quest`` without the words
    # ``Quest Start``.  Treat that generic conversation as another start
    # variant; the per-champion quest row makes the operation idempotent.
    if re.search(r"\bquest\b", lower):
        return "start"
    return ""


def _conversation_faction(label: str, owner: str = "") -> str:
    values = re.findall(
        r"(?:Ardent|Aria|Underworld|Human|Elf|Coyotle|Orc|Dwarf|Necrotic|Shin['’]hare|Vennen)",
        f"{label} {owner}", flags=re.IGNORECASE)
    for value in reversed(values):
        faction = _QUEST_FACTIONS.get(value.lower().replace("’", "'"))
        if faction:
            return faction
    owner_key = str(owner or "").strip().lower()
    if owner_key in _QUEST_OWNER_FACTIONS:
        return _QUEST_OWNER_FACTIONS[owner_key]
    return ""


def _extract_quest_conversations(data: str) -> list[tuple[Any, ...]]:
    """Link authored AZ1/AZ2 quest conversations to QuestTemplate scripts.

    The client data has no quest-start foreign key.  Most links are recoverable
    from the authored quest title in the conversation name; the two AZ1
    Node002 opening conversations are the one intentional exception because
    they start both the faction and universal taming quests without naming
    either quest.
    """
    quest_records = records(data, "QuestTemplate")[0]
    quest_rows = _extract_quest_templates(data)
    by_script = {row[0]: row for row in quest_rows}
    title_candidates = []
    for _, record in quest_records:
        script = str(record.get("m_ScriptName") or "").strip()
        if not script or script not in by_script:
            continue
        for title in _quest_title_variants(record):
            norm = _normalise_quest_label(title)
            if len(norm) >= 5:
                title_candidates.append((len(norm), norm, script))
    title_candidates.sort(reverse=True)
    pattern = re.compile(
        r"^AZ([12])\s*-?\s*Node\s*([0-9A-Za-z]+)"
        r"(?:\s+[A-Z])?\s*-\s*(.+?)\s*$", re.IGNORECASE)
    rows = []
    for _, record in records(data, "ConversationTemplate")[0]:
        name = str(record.get("m_Name") or "").strip()
        guid = nested_guid(record, "m_Id")
        match = pattern.match(name)
        if not match or not guid:
            continue
        campaign_template = f"AZ{match.group(1)}"
        node_id = _campaign_node_id(match.group(2))
        label = match.group(3)
        role = _conversation_role(label)
        owner = str(record.get("m_OwnerName") or "").strip()
        faction = _conversation_faction(label, owner)
        # The AZ1 Node002 Belarius/Takumi records are quest hand-offs even
        # though their authored names contain neither ``Quest`` nor a quest
        # title.  They are handled below by the same metadata override that
        # associates both Tamed and the faction Find quest.
        if campaign_template == "AZ1" and node_id == "Node002" and faction:
            role = "start"
        if not role:
            continue
        full_norm = _normalise_quest_label(name)
        matches = [script for _length, title, script in title_candidates
                   if title in full_norm]
        for script, aliases in _QUEST_CONVERSATION_ALIASES.items():
            if any(_normalise_quest_label(alias) in full_norm
                   for alias in aliases):
                matches.append(script)
        # Node002 is the authored hand-off conversation for Tamed plus the
        # faction-specific Find quest, despite having no quest title in its
        # name.  Keep this source-data exception in the extracted catalog,
        # rather than in the runtime campaign handler.
        if campaign_template == "AZ1" and node_id == "Node002":
            if faction == "Ardent":
                matches = ["az01_tamed", "az01_ar_find_ambling_mesa"]
            elif faction == "Underworld":
                matches = ["az01_tamed", "az01_uw_find_cave_in"]
        # Keep the longest/title-specific match only, while allowing a single
        # conversation to have multiple quest rows when it is a hand-off.
        scripts = list(dict.fromkeys(matches))
        for script in scripts:
            if script not in by_script:
                continue
            conditions = {"faction": faction} if faction else {}
            rows.append((script, guid, campaign_template, node_id, owner,
                         role, faction, name, "", json_text(conditions, {}),
                         0, 1))
    return sorted(set(rows))


def extract_quest_templates(path: str | None = None) -> list[tuple[Any, ...]]:
    """Extract quest templates from gamedata or the checked-in Records."""
    if path or configured_path():
        return _extract_quest_templates(load_text(path))
    return _extract_quest_templates(load_records_text(configured_records_path()))


def extract_quest_conversations(path: str | None = None) -> list[tuple[Any, ...]]:
    """Extract quest/conversation links from gamedata or local Records."""
    if path or configured_path():
        return _extract_quest_conversations(load_text(path))
    return _extract_quest_conversations(load_records_text(configured_records_path()))


def _extract_chests(data: str) -> list[tuple[Any, ...]]:
    rows = []
    for _, record in records(data, "InventoryItemData")[0]:
        if not str(record.get("_t") or "").endswith("InventoryTreasureChest"):
            continue
        rows.append(
            (
                nested_guid(record, "m_Id"),
                record.get("m_Name") or "",
                nested_guid(record, "m_SetId"),
                record.get("m_TreasureChestType") or "Common",
                record.get("m_TreasureSpinType") or "NoSpin",
                int_value(record.get("m_PromotionalID")),
            )
        )
    return sorted(set(row for row in rows if row[0]))


def _extract_pack_map(data: str) -> list[tuple[Any, ...]]:
    rows = []
    for _, record in records(data, "InventoryItemData")[0]:
        if not str(record.get("_t") or "").endswith("InventoryCardPack"):
            continue
        pack_guid = nested_guid(record, "m_Id")
        set_guid = nested_guid(record, "m_SetId")
        if not pack_guid or not set_guid or set_guid == ZERO_GUID:
            continue
        pack_type = str(record.get("m_CardPackType") or "")
        rows.append((pack_guid, set_guid, 1 if pack_type == "FullSet" else 0, 1 if pack_type == "PrimalPack" else 0))
    return sorted(set(rows))


def extract(path: str | None = None) -> dict[str, Any]:
    if path or configured_path():
        resolved = resolve_path(path)
        data = load_text(resolved)
    else:
        resolved = str(configured_records_path())
        data = load_records_text(configured_records_path())
    cards, card_bom, card_meta = _extract_cards(data)
    talents, talent_abilities, talent_bom = _extract_talents(data)
    gems, gem_meta_raw, gem_bom = _extract_gems(data)
    # extract_gems.py historically emits its compact metadata shape; normalize
    # it to card_abilities_meta's full 13-column table here.
    gem_meta = [
        (row[0], row[5], row[6], row[7], row[8], row[9], 0, row[11], row[1], row[10], row[2], row[3], row[4])
        for row in gem_meta_raw
    ]
    champions = _extract_champions(data)
    champion_effects = champions.pop("champion_ability_effects")
    champion_meta = champions.pop("champion_ability_meta")
    scenes, encounter_cards = _extract_encounters(data)
    campaign_node_conversations = _extract_campaign_node_conversations(data)
    quest_templates = _extract_quest_templates(data)
    quest_conversations = _extract_quest_conversations(data)

    # The table contains one BOM namespace for cards, talents, and gems.  Keep
    # the first row for each primary-key slot, matching INSERT OR IGNORE in
    # seed_database().
    effects = {}
    for row in list(talent_bom) + list(card_bom) + list(gem_bom) + list(champion_effects):
        effects.setdefault((row[0], row[2]), row)
    target_rows = _extract_targets(data)
    counter_records, _ = records(data, "CardCounterTemplate")
    counters = sorted(
        set(
            (nested_guid(record, "m_CardCounterId"), record.get("m_Name") or "", record.get("m_Description") or "")
            for _, record in counter_records
            if nested_guid(record, "m_CardCounterId")
        )
    )
    condition_records, _ = records(data, "AbilityEffectConditionTemplate")
    conditions = sorted(
        set(
            (nested_guid(record, "m_TemplateId"), record.get("m_Name") or "", json_text(record.get("m_Condition"), {}))
            for _, record in condition_records
            if nested_guid(record, "m_TemplateId")
        )
    )

    return {
        "source": resolved,
        "tables": {
            "card_templates": cards,
            "card_abilities_meta": card_meta + gem_meta + champion_meta,
            "ability_effects": sorted(effects.values()),
            "target_templates": target_rows,
            "talent_data": talents,
            "talent_abilities": talent_abilities,
            "ability_effect_conditions": conditions,
            "card_counter_templates": counters,
            "gem_templates": gems,
            **champions,
            "encounter_scenes": scenes,
            "encounter_deck_cards": encounter_cards,
            "campaign_node_conversations": campaign_node_conversations,
            "quest_templates": quest_templates,
            "quest_conversations": quest_conversations,
            "chest_templates": _extract_chests(data),
            "pack_set_map": _extract_pack_map(data),
        },
    }


TABLE_COLUMNS = {
    "card_templates": ("guid", "set_guid", "name", "rarity", "cost", "attack", "defense", "card_type", "socket_count", "no_pvp", "is_pve", "threshold_json", "abilities_json", "attributes", "sacrifice_target", "variable_cost", "variable_cost_minimum", "rage_value", "subtype"),
    "card_abilities_meta": ("ability_guid", "casting_behavior", "is_manual", "activation_cost", "uses_per_game", "uses_per_turn", "cooldown", "exhausts_on_use", "is_triggered", "target_template_ids", "trigger_event_type", "game_text", "raw_json"),
    "ability_effects": (
        "ability_guid", "effect_guid", "effect_order", "effect_type", "param",
        "effect_group_id", "condition_id", "target_index", "effect_instance_id",
        "contingent_effect_instance_id", "secondary_target_index",
        "recalculate_targets", "is_optional", "effect_duration",
        "output_variables",
    ),
    "target_templates": ("template_id", "game_text", "is_auto_target", "is_random_target", "optional", "explicit", "player_filter", "collection_flags", "min_target_count", "max_target_count", "filter_json", "target_kind"),
    "talent_data": ("talent_guid", "name", "ability_guid", "has_ability", "description", "charge_cost", "spell_cost"),
    "talent_abilities": ("talent_guid", "ability_guid", "charge_cost", "spell_cost", "activatable_phases", "casting_behavior", "condition", "target_template_ids"),
    "ability_effect_conditions": ("condition_id", "name", "condition_json"),
    "card_counter_templates": ("template_id", "name", "description"),
    "gem_templates": ("gem_type", "gem_type_name", "name", "abilities_json"),
    "champion_abilities": ("champion_guid", "champion_name", "ability_guid", "ability_name", "charge_cost", "spell_cost", "threshold_colors", "game_text", "casting_behavior", "thresholds_json", "target_template_ids"),
    "champion_templates_extended": ("guid", "name", "race", "champion_class", "gender", "is_selectable", "starting_health", "faction"),
    "champion_template_data": ("guid", "name", "champion_class", "race", "starting_health", "starting_hand_size"),
    "champion_templates": ("guid", "race", "champion_class", "gender", "is_player", "default_talents"),
    "champion_class_data": ("race", "champion_class", "starting_health", "starting_hand_size"),
    "encounter_scenes": ("guid", "name", "title", "gameboard", "ai_deck_guid", "ai_champion_guid", "mods_json", "rewards_json"),
    "encounter_deck_cards": ("deck_guid", "card_guid", "quantity", "gem_types_new_list_json"),
    "campaign_node_conversations": ("campaign_template", "node_id", "conversation_guid", "conversation_name", "trigger_json", "priority", "enabled"),
    "quest_templates": ("script_name", "name", "title", "objectives_json", "campaign_group", "start_hook", "enabled"),
    "quest_conversations": ("quest_script", "conversation_guid", "campaign_template", "node_id", "npc", "role", "faction", "conversation_name", "start_hook", "conditions_json", "priority", "enabled"),
    "chest_templates": ("guid", "name", "set_guid", "chest_type", "spin_type", "promotional_id"),
    "pack_set_map": ("pack_guid", "set_guid", "is_full_set", "is_primal"),
}


def seed_database(db: sqlite3.Connection, seed: dict[str, Any], *, include_pack_map: bool = False) -> dict[str, int]:
    """Insert client-derived rows into an already-created server schema.

    This function is intentionally insert-if-missing.  ``static.ensure_schema``
    calls it only when creating a fresh reference database, while the CLI can
    use it against a comparison database without replacing runtime data.
    """
    tables = seed["tables"]
    if not include_pack_map:
        tables = {name: rows for name, rows in tables.items() if name != "pack_set_map"}
    inserted: dict[str, int] = {}
    for table, rows in tables.items():
        columns = TABLE_COLUMNS[table]
        placeholders = ",".join("?" for _ in columns)
        before = db.total_changes
        db.executemany(
            f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
            rows,
        )
        inserted[table] = db.total_changes - before
    db.commit()
    return inserted


def _primary_key(table: str, row: tuple[Any, ...]) -> tuple[Any, ...]:
    indexes = {
        "ability_effects": (0, 2),
        "champion_abilities": (0, 2),
        "talent_abilities": (0, 1),
        "encounter_deck_cards": (0, 1),
        "champion_class_data": (0, 1),
    }
    return tuple(row[index] for index in indexes.get(table, (0,)))


def compare_database(db_path: str, seed: dict[str, Any]) -> dict[str, Any]:
    json_columns = {
        "threshold_json", "abilities_json", "target_template_ids", "raw_json",
        "filter_json", "condition_json", "thresholds_json", "gem_types_new_list_json",
    }

    def comparable(table: str, row: tuple[Any, ...]) -> tuple[Any, ...]:
        columns = TABLE_COLUMNS[table]
        result = []
        for column, value in zip(columns, row):
            if column in json_columns and isinstance(value, str):
                try:
                    result.append(json.loads(value))
                    continue
                except (TypeError, json.JSONDecodeError):
                    pass
            result.append(value)
        return tuple(result)

    db = sqlite3.connect(db_path)
    result = {}
    for table, rows in seed["tables"].items():
        if table == "pack_set_map":
            continue
        try:
            columns = TABLE_COLUMNS[table]
            existing = [tuple(row) for row in db.execute(f"SELECT {','.join(columns)} FROM {table}")]
        except sqlite3.Error as exc:
            result[table] = {"error": str(exc)}
            continue
        wanted = {_primary_key(table, tuple(row)): tuple(row) for row in rows}
        actual = {_primary_key(table, row): row for row in existing}
        missing = sorted(set(wanted) - set(actual), key=str)
        extra = sorted(set(actual) - set(wanted), key=str)
        changed = sorted(
            [key for key in set(wanted) & set(actual) if comparable(table, wanted[key]) != comparable(table, actual[key])], key=str
        )
        result[table] = {
            "extracted": len(rows),
            "database": len(existing),
            "missing": len(missing),
            "extra": len(extra),
            "changed": len(changed),
            "missing_keys": [list(key) for key in missing[:10]],
            "extra_keys": [list(key) for key in extra[:10]],
            "changed_keys": [list(key) for key in changed[:10]],
        }
    db.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gamedata", help="path to the client's gzip gamedata file")
    parser.add_argument("--manifest", help="write extracted rows as JSON")
    parser.add_argument("--compare-db", help="compare extracted rows with an existing SQLite database")
    parser.add_argument("--populate-db", help="insert extracted rows into an existing SQLite database")
    parser.add_argument("--include-pack-map", action="store_true", help="also seed pack_set_map from InventoryCardPack")
    args = parser.parse_args()
    seed = extract(args.gamedata)
    print(f"Source: {seed['source']}")
    for table, rows in seed["tables"].items():
        print(f"  {table}: {len(rows)} rows")
    if args.manifest:
        Path(args.manifest).write_text(json.dumps(seed, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Wrote manifest: {args.manifest}")
    if args.compare_db:
        print(json.dumps(compare_database(args.compare_db, seed), indent=2, sort_keys=True))
    if args.populate_db:
        db = sqlite3.connect(args.populate_db)
        print(json.dumps(seed_database(db, seed, include_pack_map=args.include_pack_map), indent=2, sort_keys=True))
        db.close()
if __name__ == "__main__":
    main()

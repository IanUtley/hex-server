#!/usr/bin/env python3
"""Read-only MCP server for Hex champion, encounter, and ability details.

Every answer is derived from the same Records snapshot the server seeds from,
split along the seam that already owns each fact:

* ``AssetExtraction.gamedata_seed`` owns the authored joins — champion charge
  powers, encounter scene -> AI deck -> cards, card -> abilities, talents.
* ``gamedata.ability_graph`` owns what one ability does — costs, targets, the
  effect chain, and effect parameters.

Nothing here opens ``hconnect.db`` or mutates state, so the tool can run next
to a live server and cannot disagree with the engine about static definitions.

Tools
-----
``hex_search``    name/GUID -> candidate champions, encounters, cards, abilities, talents
``hex_champion``  champion identity, signature charge powers, level-1 talents
``hex_encounter`` encounter scene, AI champion, AI deck cards, mods, rewards
``hex_card``      one card template with its full ability chain
``hex_ability``   one ability with costs, targets, effects, and users

Run over stdio::

    python3 hex_mcp.py

Register with a Codex client (``~/.codex/config.toml``)::

    [mcp_servers.hex]
    command = "python3"
    args = ["location of hex_mcp.py"]
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from threading import RLock
from typing import Any, Callable, Iterable, Mapping

from AssetExtraction.gamedata_seed import TABLE_COLUMNS, extract
from domain.enums import ECardShard
from gamedata import DEFAULT_RECORD_STORE, RecordObject, ability_graph
from gamedata.records import reference_guid

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "hex-data"
SERVER_VERSION = "1.0.0"
SEARCH_LIMIT = 50
SEARCH_KINDS = ("champion", "encounter", "card", "ability", "talent")
GUID_PATTERN = re.compile(r"\A[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
# Card thresholds are indexed by ECardShard (0=Colorless .. 5=Diamond).
SHARD_NAMES = tuple(
    name for name, _ in sorted(
        ((name, value) for name, value in vars(ECardShard).items()
         if isinstance(value, int)),
        key=lambda pair: pair[1],
    )
)

INSTRUCTIONS = (
    "Static Hex TCG data from the client Records snapshot: champions, "
    "encounter scenes, card templates, and abilities. Read-only; never "
    "reports live match or profile state. Start with hex_search when a name "
    "is uncertain, then call hex_champion / hex_encounter / hex_card / "
    "hex_ability with the returned GUID for full detail."
)


def _norm(value: Any) -> str:
    """Case-folded, whitespace-collapsed text used for name matching."""
    return " ".join(str(value or "").casefold().split())


def _looks_like_guid(value: Any) -> bool:
    return bool(GUID_PATTERN.match(str(value or "").strip().lower()))


def _decode_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _short(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _jsonable(value: Any) -> Any:
    """Convert Records objects (and their containers) into plain JSON data."""
    if isinstance(value, RecordObject):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _rows(tables: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    columns = TABLE_COLUMNS[name]
    return [dict(zip(columns, row)) for row in tables.get(name, ())]


def _record_champion_abilities(record: RecordObject) -> list[str]:
    """Ability slots plus ``m_ChampionAbilities`` of one ChampionTemplate."""
    guids: list[str] = []
    for slot in ("m_AbilitySlot1", "m_AbilitySlot2", "m_AbilitySlot3"):
        guid = reference_guid(record.field(slot)).lower()
        if guid and guid != ZERO_GUID and guid not in guids:
            guids.append(guid)
    for entry in record.field("m_ChampionAbilities") or ():
        guid = reference_guid((entry or {}).get("m_CardAbilityId")).lower()
        if guid and guid != ZERO_GUID and guid not in guids:
            guids.append(guid)
    return guids


def _match(records: Iterable[dict[str, Any]],
           keys: Callable[[dict[str, Any]], Iterable[Any]],
           query: str) -> list[dict[str, Any]]:
    """Exact (case/space-insensitive) name matches, else substring matches."""
    wanted = _norm(query)
    if not wanted:
        return []
    exact: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    for record in records:
        texts = [_norm(text) for text in keys(record)]
        if wanted in texts:
            exact.append(record)
        elif any(wanted in text for text in texts):
            partial.append(record)
    return exact or partial


class _Index:
    """Immutable lookups over the Records-derived seed snapshot."""

    def __init__(self, tables: Mapping[str, Any]):
        self.cards: dict[str, dict[str, Any]] = {}
        self.champions: dict[str, dict[str, Any]] = {}
        self.encounters: dict[str, dict[str, Any]] = {}
        self.abilities: dict[str, dict[str, Any]] = {}
        self.talents: dict[str, dict[str, Any]] = {}
        self.conditions: dict[str, dict[str, Any]] = {}
        self.used_by_cards: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.used_by_champions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.used_by_talents: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.champion_abilities: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.talent_abilities: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.default_talents: dict[str, list[str]] = {}
        self.deck_cards: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.card_encounters: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._build(tables)

    def _build(self, tables: Mapping[str, Any]) -> None:
        for row in _rows(tables, "card_templates"):
            self.cards[row["guid"]] = row
            for ability in _decode_json(row["abilities_json"], ()):
                self.used_by_cards[str(ability).lower()].append(row)

        for row in _rows(tables, "card_abilities_meta"):
            guid = str(row["ability_guid"]).lower()
            raw = _decode_json(row["raw_json"], {})
            # INSERT OR IGNORE semantics: the first namespace wins.
            self.abilities.setdefault(guid, {
                "guid": guid,
                "name": str(raw.get("m_Name") or ""),
                "game_text": str(row["game_text"] or ""),
            })
        # The typed Records snapshot fills in abilities the seed index missed
        # (hero champions such as "Boat" are not in the seed ability tables).
        for record in DEFAULT_RECORD_STORE.load("AbilityTemplate"):
            guid = str(record.ability_guid or record.guid).lower()
            if not guid:
                continue
            entry = self.abilities.get(guid)
            if entry is None:
                self.abilities[guid] = {
                    "guid": guid,
                    "name": record.name,
                    "game_text": record.game_text,
                }
            else:
                entry["name"] = entry["name"] or record.name
                entry["game_text"] = entry["game_text"] or record.game_text

        for row in _rows(tables, "ability_effect_conditions"):
            self.conditions[row["condition_id"]] = row

        for row in _rows(tables, "champion_template_data"):
            self.champions[row["guid"]] = {
                "guid": row["guid"],
                "name": row["name"],
                "race": row["race"],
                "champion_class": row["champion_class"],
                "gender": "",
                "faction": "",
                "selectable": 0,
                "starting_health": row["starting_health"],
                "starting_hand_size": row["starting_hand_size"],
                "charge_powers": 0,
                "record_abilities": [],
            }
        for row in _rows(tables, "champion_templates_extended"):
            champion = self.champions.setdefault(row["guid"], {
                "guid": row["guid"],
                "name": row["name"],
                "race": row["race"],
                "champion_class": row["champion_class"],
                "gender": "",
                "faction": "",
                "selectable": 0,
                "starting_health": row["starting_health"],
                "starting_hand_size": None,
                "charge_powers": 0,
                "record_abilities": [],
            })
            champion.update({
                "name": champion["name"] or row["name"],
                "race": row["race"] or champion["race"],
                "champion_class": row["champion_class"] or champion["champion_class"],
                "gender": row["gender"],
                "faction": row["faction"],
                "selectable": row["is_selectable"],
                "starting_health": champion["starting_health"] or row["starting_health"],
            })

        # The seed tables carry the PvP and encounter champions, whose relaxed
        # parser reads every authored record.  Hero templates (the playable
        # campaign champions such as "Boat") exist only in Records, so add the
        # ones that author abilities.
        for record in DEFAULT_RECORD_STORE.load("ChampionTemplate"):
            ability_guids = _record_champion_abilities(record)
            guid = (reference_guid(record.field("m_Id")) or record.guid).lower()
            if not ability_guids or not guid or guid in self.champions:
                continue
            self.champions[guid] = {
                "guid": guid,
                "name": str(record.field("m_Name") or ""),
                "race": str(record.field("m_Race") or ""),
                "champion_class": str(record.field("m_Class") or ""),
                "gender": str(record.field("m_Gender") or ""),
                "faction": str(record.field("m_Faction") or ""),
                "selectable": 1 if record.field("m_IsPlayerSelectable") else 0,
                "starting_health": record.field("m_StartingHealth"),
                "starting_hand_size": record.field("m_StartingHandSize"),
                "charge_powers": 0,
                "record_abilities": ability_guids,
            }

        for row in _rows(tables, "champion_abilities"):
            self.champion_abilities[row["champion_guid"]].append(row)
            self.used_by_champions[str(row["ability_guid"]).lower()].append(row)
        for rows in self.champion_abilities.values():
            rows.sort(key=lambda row: (row["charge_cost"], row["ability_name"]))
        for champion in self.champions.values():
            authored = {str(row["ability_guid"]).lower()
                        for row in self.champion_abilities.get(
                            champion["guid"], ())}
            champion["charge_powers"] = len(
                authored | set(champion["record_abilities"]))

        for row in _rows(tables, "talent_data"):
            self.talents[row["talent_guid"]] = row
        for row in _rows(tables, "talent_abilities"):
            self.talent_abilities[row["talent_guid"]].append(row)
            self.used_by_talents[str(row["ability_guid"]).lower()].append(
                self.talents.get(row["talent_guid"],
                                 {"talent_guid": row["talent_guid"], "name": ""}))
        for row in _rows(tables, "champion_templates"):
            talents = [str(guid).lower()
                       for guid in _decode_json(row["default_talents"], ())]
            if talents:
                self.default_talents[row["guid"]] = talents

        for row in _rows(tables, "encounter_scenes"):
            self.encounters[row["guid"]] = row
        for row in _rows(tables, "encounter_deck_cards"):
            self.deck_cards[row["deck_guid"]].append(row)
        for scene in self.encounters.values():
            if not scene["ai_deck_guid"]:
                continue
            for deck_row in self.deck_cards.get(scene["ai_deck_guid"], ()):
                encounters = self.card_encounters[deck_row["card_guid"]]
                if scene not in encounters:
                    encounters.append(scene)

    def champion_name(self, guid: Any) -> str:
        champion = self.champions.get(str(guid or "").lower())
        return champion["name"] if champion else ""


_INDEX: _Index | None = None
_INDEX_LOCK = RLock()


def index() -> _Index:
    """Build (once per process) the Records-derived lookup index."""
    global _INDEX
    if _INDEX is None:
        with _INDEX_LOCK:
            if _INDEX is None:
                _INDEX = _Index(extract()["tables"])
    return _INDEX


# --------------------------------------------------------------------------
# Shared renderers
# --------------------------------------------------------------------------

def _thresholds(card: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Decode a card's ``{"values": [c,b,r,s,w,d], "list": [...]}`` blob."""
    data = _decode_json(card.get("threshold_json"), {})
    if isinstance(data, list):
        values, indexes = data, range(len(data))
    else:
        values = data.get("values") or []
        indexes = data.get("list") or []
    result = []
    for shard_index in dict.fromkeys(int(value) for value in indexes):
        count = values[shard_index] if 0 <= shard_index < len(values) else 0
        if count:
            result.append({"shard": SHARD_NAMES[shard_index], "count": count})
    return result


def _card_ability_guids(card: Mapping[str, Any] | None) -> list[str]:
    if not card:
        return []
    return [str(guid).lower()
            for guid in _decode_json(card.get("abilities_json"), ())]


def _cost_brief(costs: Any) -> dict[str, Any]:
    return {key: value for key, value in costs.as_dict().items()
            if value not in (0, False, None, "", [], {})}


def _ability_brief(ability_guid: Any) -> dict[str, Any]:
    """Compact ability view for lists; use :func:`_ability_detail` to drill in."""
    guid = str(ability_guid).lower()
    graph = ability_graph(DEFAULT_RECORD_STORE, guid)
    if graph is None:
        entry = index().abilities.get(guid, {})
        return {"guid": guid,
                "name": entry.get("name", ""),
                "game_text": entry.get("game_text", "")}
    return {
        "guid": guid,
        "name": graph.name,
        "game_text": graph.game_text,
        "casting_behavior": graph.casting_behavior,
        "costs": _cost_brief(graph.costs),
        "targets": [target.name for target in graph.targets],
        "effect_types": [effect.concrete_type for effect in graph.effects],
    }


def _effect_detail(effect: Any, order: int) -> dict[str, Any]:
    data = effect.as_dict()
    # ``graph.effects`` is already in authored resolution order.
    data["order"] = order
    if effect.template is not None:
        data["template"] = effect.template.to_dict(include_metadata=False)
    condition = index().conditions.get(str(data.get("condition_guid") or ""))
    if condition:
        data["condition"] = {
            "id": condition["condition_id"],
            "name": condition["name"],
            "definition": _decode_json(condition["condition_json"], {}),
        }
    return data


def _ability_users(ability_guid: str) -> dict[str, list[dict[str, Any]]]:
    def unique(entries: Iterable[Mapping[str, Any]], guid_key: str,
               name_key: str) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for entry in entries:
            guid = str(entry.get(guid_key) or "").lower()
            if not guid:
                continue
            seen.setdefault(guid,
                            {"guid": guid, "name": entry.get(name_key) or ""})
        return list(seen.values())

    return {
        "cards": unique(index().used_by_cards.get(ability_guid, ()),
                        "guid", "name"),
        "champions": unique(index().used_by_champions.get(ability_guid, ()),
                            "champion_guid", "champion_name"),
        "talents": unique(index().used_by_talents.get(ability_guid, ()),
                          "talent_guid", "name"),
    }


def _ability_detail(ability_guid: Any, *, thresholds: Any = None,
                    include_users: bool = True) -> dict[str, Any]:
    """Full authored detail for one ability, from the typed ability graph."""
    guid = str(ability_guid).lower()
    graph = ability_graph(DEFAULT_RECORD_STORE, guid)
    entry = index().abilities.get(guid, {})
    if graph is None:
        detail: dict[str, Any] = {
            "kind": "ability",
            "guid": guid,
            "name": entry.get("name", ""),
            "game_text": entry.get("game_text", ""),
            "note": "ability is absent from the Records AbilityTemplate snapshot",
        }
    else:
        detail = {"kind": "ability", **graph.as_dict()}
        detail["effects"] = [_effect_detail(effect, order)
                             for order, effect in enumerate(graph.effects)]
    if thresholds:
        detail["thresholds"] = thresholds
    if include_users:
        detail["used_by"] = _ability_users(guid)
    return detail


def _talent_detail(talent_guid: str) -> dict[str, Any]:
    talent = index().talents.get(talent_guid, {})
    return {
        "guid": talent_guid,
        "name": talent.get("name", ""),
        "description": talent.get("description", ""),
        "abilities": [
            _ability_brief(row["ability_guid"])
            for row in index().talent_abilities.get(talent_guid, ())
        ],
    }


def _champion_summary(champion: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "champion",
        "guid": champion["guid"],
        "name": champion["name"],
        "race": champion["race"],
        "champion_class": champion["champion_class"],
        "starting_health": champion["starting_health"],
        "charge_powers": champion["charge_powers"],
    }


def _champion_detail(champion: Mapping[str, Any]) -> dict[str, Any]:
    guid = champion["guid"]
    rows = index().champion_abilities.get(guid, ())
    abilities = [
        _ability_detail(
            row["ability_guid"],
            thresholds=_decode_json(row["thresholds_json"], []),
            include_users=False,
        )
        for row in rows
    ]
    authored = {str(row["ability_guid"]).lower() for row in rows}
    for ability_guid in champion["record_abilities"]:
        if ability_guid not in authored:
            abilities.append(_ability_detail(ability_guid, include_users=False))
    return {
        **_champion_summary(champion),
        "gender": champion["gender"],
        "faction": champion["faction"],
        "selectable": bool(champion["selectable"]),
        "starting_hand_size": champion["starting_hand_size"],
        "abilities": abilities,
        "talents": [
            _talent_detail(talent_guid)
            for talent_guid in index().default_talents.get(guid, ())
        ],
    }


def _card_summary(card: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "card",
        "guid": card["guid"],
        "name": card["name"],
        "cost": card["cost"],
        "card_type": card["card_type"],
        "attack": card["attack"],
        "defense": card["defense"],
    }


def _card_detail(card: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_card_summary(card),
        "rarity": card["rarity"],
        "subtype": card["subtype"],
        "thresholds": _thresholds(card),
        "rage": card["rage_value"],
        "lethal": bool(card["lethal"]),
        "is_pve": bool(card["is_pve"]),
        "no_pvp": bool(card["no_pvp"]),
        "equipment_modified": bool(card["equipment_modified"]),
        "variable_cost": bool(card["variable_cost"]),
        "variable_cost_minimum": card["variable_cost_minimum"],
        "sacrifice_target": card["sacrifice_target"] or None,
        "abilities": [
            _ability_detail(guid, include_users=False)
            for guid in _card_ability_guids(card)
        ],
        "encounters": [
            {"guid": scene["guid"], "name": scene["name"]}
            for scene in index().card_encounters.get(card["guid"], ())
        ],
    }


def _guid_label(guid: str) -> dict[str, Any]:
    card = index().cards.get(guid)
    if card:
        return {"kind": "card", "guid": guid, "name": card["name"]}
    champion = index().champions.get(guid)
    if champion:
        return {"kind": "champion", "guid": guid, "name": champion["name"]}
    talent = index().talents.get(guid)
    if talent:
        return {"kind": "talent", "guid": guid, "name": talent["name"]}
    scene = index().encounters.get(guid)
    if scene:
        return {"kind": "encounter", "guid": guid, "name": scene["name"]}
    return {"kind": "unknown", "guid": guid}


def _encounter_mods(scene: Mapping[str, Any]) -> list[dict[str, Any]]:
    mods = []
    for entry in _decode_json(scene["mods_json"], ()):
        if not isinstance(entry, Mapping):
            continue
        resolved = {
            key: value for key, value in entry.items() if key != "mods"
        }
        resolved["mods"] = [
            _guid_label(str((mod or {}).get("guid") or "").lower())
            for mod in entry.get("mods") or ()
            if (mod or {}).get("guid")
        ]
        mods.append(resolved)
    return mods


def _deck_detail(deck_guid: str) -> dict[str, Any]:
    cards = []
    total = 0
    for row in index().deck_cards.get(deck_guid, ()):
        card = index().cards.get(row["card_guid"])
        total += row["quantity"]
        entry: dict[str, Any] = {
            "guid": row["card_guid"],
            "quantity": row["quantity"],
            "name": card["name"] if card else "",
            "cost": card["cost"] if card else None,
            "card_type": card["card_type"] if card else "",
            "attack": card["attack"] if card else None,
            "defense": card["defense"] if card else None,
            "thresholds": _thresholds(card) if card else [],
            "abilities": [_ability_brief(guid)
                          for guid in _card_ability_guids(card)],
        }
        if card is None:
            entry["note"] = "card template is not in the seed snapshot"
        gems = sorted({str(gem) for slot in _decode_json(
            row["gem_types_new_list_json"], ()) for gem in (slot or ())})
        if gems:
            entry["gems"] = gems
        cards.append(entry)
    return {
        "guid": deck_guid,
        "card_count": total,
        "distinct_cards": len(cards),
        "cards": cards,
    }


def _encounter_summary(scene: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "encounter",
        "guid": scene["guid"],
        "name": scene["name"],
        "title": scene["title"],
        "ai_champion": index().champion_name(scene["ai_champion_guid"]),
    }


def _encounter_detail(scene: Mapping[str, Any]) -> dict[str, Any]:
    champion_guid = str(scene["ai_champion_guid"] or "").lower()
    champion = index().champions.get(champion_guid)
    if champion:
        ai_champion: dict[str, Any] | None = {
            **_champion_summary(champion),
            "abilities": [
                _ability_brief(row["ability_guid"])
                for row in index().champion_abilities.get(champion_guid, ())
            ],
        }
    elif champion_guid:
        ai_champion = {"kind": "champion", "guid": champion_guid, "name": "",
                       "note": "champion template is not in the seed snapshot"}
    else:
        ai_champion = None
    return {
        **_encounter_summary(scene),
        "gameboard": scene["gameboard"],
        "rewards": _decode_json(scene["rewards_json"], {}),
        "mods": _encounter_mods(scene),
        "ai_champion": ai_champion,
        "ai_deck": _deck_detail(scene["ai_deck_guid"])
                    if scene["ai_deck_guid"] else None,
    }


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def _not_found(kind: str, query: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "query": query,
        "found": False,
        "error": f"no {kind} matched {str(query)!r}",
        "hint": f"use hex_search to list candidate {kind} names",
    }


def _resolve(kind: str, records: Mapping[str, dict[str, Any]], query: Any,
             keys: Callable[[dict[str, Any]], Iterable[Any]],
             summary: Callable[[dict[str, Any]], dict[str, Any]],
             detail: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    text = str(query or "").strip()
    if not text:
        return _not_found(kind, query)
    if _looks_like_guid(text):
        record = records.get(text.lower())
        matches = [record] if record else []
    else:
        matches = _match(records.values(), keys, text)
    if not matches:
        return _not_found(kind, query)
    if len(matches) > 1:
        return {
            "kind": kind,
            "query": query,
            "found": True,
            "ambiguous": True,
            "candidates": [summary(record) for record in matches],
            "hint": "several records share this name; call again with a guid",
        }
    return {"found": True, **detail(matches[0])}


def hex_search(query: str, kind: str | None = None) -> dict[str, Any]:
    """Find champions, encounters, cards, abilities, or talents by name."""
    wanted = (kind or "all").strip().lower()
    if wanted != "all" and wanted not in SEARCH_KINDS:
        raise ValueError(f"kind must be one of: all, {', '.join(SEARCH_KINDS)}")
    index_ = index()
    groups: dict[str, list[dict[str, Any]]] = {}
    if wanted in ("all", "champion"):
        groups["champion"] = [
            _champion_summary(champion) for champion in
            _match(index_.champions.values(), lambda row: (row["name"],), query)
        ]
    if wanted in ("all", "encounter"):
        groups["encounter"] = [
            _encounter_summary(scene) for scene in _match(
                index_.encounters.values(),
                lambda row: (row["name"], row["title"],
                             index_.champion_name(row["ai_champion_guid"])),
                query,
            )
        ]
    if wanted in ("all", "card"):
        groups["card"] = [
            _card_summary(card) for card in
            _match(index_.cards.values(), lambda row: (row["name"],), query)
        ]
    if wanted in ("all", "ability"):
        groups["ability"] = [
            {"kind": "ability",
             "guid": row["guid"],
             "name": row["name"],
             "game_text": _short(row["game_text"])}
            for row in _match(
                index_.abilities.values(),
                lambda row: (row["name"], row["game_text"]),
                query,
            )
        ]
    if wanted in ("all", "talent"):
        groups["talent"] = [
            {"kind": "talent", "guid": row["talent_guid"],
             "name": row["name"], "description": _short(row["description"])}
            for row in _match(index_.talents.values(),
                              lambda row: (row["name"],), query)
        ]
    results = [entry for name in SEARCH_KINDS for entry in groups.get(name, ())]
    return {
        "query": query,
        "kind": wanted,
        "count": len(results),
        "truncated": len(results) > SEARCH_LIMIT,
        "results": results[:SEARCH_LIMIT],
    }


def hex_champion(query: str) -> dict[str, Any]:
    """One champion: identity, signature charge powers, level-1 talents."""
    index_ = index()
    return _resolve(
        "champion", index_.champions, query,
        keys=lambda row: (row["name"],),
        summary=_champion_summary,
        detail=_champion_detail,
    )


def hex_encounter(query: str) -> dict[str, Any]:
    """One encounter scene: AI champion, AI deck, mods, and rewards."""
    index_ = index()
    return _resolve(
        "encounter", index_.encounters, query,
        keys=lambda row: (row["name"], row["title"],
                          index_.champion_name(row["ai_champion_guid"])),
        summary=_encounter_summary,
        detail=_encounter_detail,
    )


def hex_card(query: str) -> dict[str, Any]:
    """One card template with its full ability chain."""
    index_ = index()
    return _resolve(
        "card", index_.cards, query,
        keys=lambda row: (row["name"],),
        summary=_card_summary,
        detail=_card_detail,
    )


def hex_ability(query: str) -> dict[str, Any]:
    """One ability: costs, targets, effect chain, and the records using it."""
    index_ = index()
    result = _resolve(
        "ability", index_.abilities, query,
        keys=lambda row: (row["name"], row["game_text"]),
        summary=lambda row: {
            "kind": "ability",
            "guid": row["guid"],
            "name": row["name"],
            "game_text": _short(row["game_text"]),
        },
        detail=lambda row: _ability_detail(row["guid"], include_users=True),
    )
    if not result.get("found"):
        cards = _match(index_.cards.values(), lambda row: (row["name"],), query)
        if cards:
            result["cards"] = [_card_summary(card) for card in cards[:10]]
            result["hint"] = (
                "no ability name matched; these cards did - use hex_card for "
                "their abilities"
            )
    return result


def _query(args: Any) -> str:
    if not isinstance(args, Mapping):
        raise ValueError("arguments must be an object")
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    return query


TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "hex_search",
        "description": (
            "Search Hex static data (champions, encounters, cards, abilities, "
            "talents) by case-insensitive name fragment or GUID. Returns "
            "compact candidates with GUIDs. Use before the specific tools "
            "when the exact name is uncertain."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Name fragment or GUID to search for."},
                "kind": {"type": "string",
                         "enum": ["all", *SEARCH_KINDS],
                         "description": "Restrict the search to one kind."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": lambda args: hex_search(_query(args), args.get("kind")),
    },
    {
        "name": "hex_champion",
        "description": (
            "Full authored detail for one Hex champion: identity, signature "
            "charge powers (costs, thresholds, targets, effect chain), and "
            "level-1 talents. Accepts a champion name (e.g. 'Princess "
            "Victoria') or GUID. Ambiguous names return candidate GUIDs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Champion name or GUID."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": lambda args: hex_champion(_query(args)),
    },
    {
        "name": "hex_encounter",
        "description": (
            "Full detail for one authored encounter scene: AI champion with "
            "its abilities, the AI deck with every card and printed ability, "
            "authored encounter mods, and rewards. Accepts the scene name "
            "(e.g. 'AZ 1 - NODE 14 - SAVAGE LORD' or the 'SAVAGE LORD' "
            "fragment), the AI champion's name, or a GUID."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Encounter scene name, AI champion name, or GUID."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": lambda args: hex_encounter(_query(args)),
    },
    {
        "name": "hex_card",
        "description": (
            "Full detail for one card template: cost, thresholds, stats, "
            "subtype, every ability with targets and the complete effect "
            "chain (including effect parameters), and the encounters whose "
            "AI deck contains it. Accepts a card name or GUID."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Card name or GUID."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": lambda args: hex_card(_query(args)),
    },
    {
        "name": "hex_ability",
        "description": (
            "Full detail for one ability template: costs, casting behavior, "
            "trigger, targets, the complete effect chain with parameters, and "
            "every card, champion, and talent that uses it. Accepts an "
            "ability GUID, ability name, or printed-text fragment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Ability GUID, name, or printed-text fragment."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": lambda args: hex_ability(_query(args)),
    },
)

_TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


# --------------------------------------------------------------------------
# MCP stdio transport (JSON-RPC 2.0, newline-delimited)
# --------------------------------------------------------------------------

def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = (payload if isinstance(payload, str)
            else json.dumps(_jsonable(payload), indent=2, ensure_ascii=False))
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _initialize(params: Mapping[str, Any]) -> dict[str, Any]:
    requested = str(params.get("protocolVersion") or "")
    return {
        "protocolVersion": (requested if requested in PROTOCOL_VERSIONS
                            else PROTOCOL_VERSIONS[0]),
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": INSTRUCTIONS,
    }


def _call_tool(params: Mapping[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "")
    tool = _TOOLS_BY_NAME.get(name)
    if tool is None:
        return _tool_result(f"unknown tool: {name}", is_error=True)
    try:
        payload = tool["handler"](params.get("arguments") or {})
    except Exception as exc:  # surfaced to the client, never raised
        return _tool_result(f"{type(exc).__name__}: {exc}", is_error=True)
    return _tool_result(payload)


def handle_request(request: Any) -> dict[str, Any] | None:
    """Answer one JSON-RPC message; notifications return ``None``."""
    if not isinstance(request, Mapping) or "method" not in request:
        return _error(None, -32600, "invalid request")
    request_id = request.get("id")
    if request_id is None:
        # Notifications (initialize/initialized/cancelled) take no reply.
        return None
    method = str(request["method"])
    params = request.get("params") or {}
    if not isinstance(params, Mapping):
        return _error(request_id, -32602, "params must be an object")
    if method == "initialize":
        return _result(request_id, _initialize(params))
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {
            "tools": [{key: value for key, value in tool.items()
                       if key != "handler"} for tool in TOOLS],
        })
    if method == "tools/call":
        return _result(request_id, _call_tool(params))
    return _error(request_id, -32601, f"method not found: {method}")


def main(argv: list[str] | None = None) -> int:
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            request = json.loads(text)
        except ValueError as exc:
            response: dict[str, Any] | None = _error(None, -32700,
                                                     f"parse error: {exc}")
        else:
            response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass

"""A zone's rules as the campaign runtime and the editor's simulator see them.

``campaign/campaign_rules/<zone>.json`` holds node states, hooks and the map; what each
node *offers* is table data merged in here (``offers.build_offers``), with the
encounter scenes' flag actions rebuilt as hooks, their card-selector options
(``mod_choices``) and the campaign-wide quests
(``campaign/campaign_rules/quests.json``).

Built once per change of the rules file, the quests file or the editor's data
files; the cache is keyed and guarded by a lock, so callers on any thread
share one immutable result (treat it as read-only).
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

import campaign_rules as engine
from campaign_runtime import catalog_data, offers
from campaign_runtime.records import conversations

RULES_DIR = catalog_data.RULES_DIR
QUESTS_PATH = RULES_DIR / "quests.json"
_LOCK = threading.Lock()
_CACHE: dict[tuple, dict[str, Any]] = {}
_CACHE_SIZE = 128


def rules_path(zone_id: str) -> Path:
    return RULES_DIR / f"{zone_id}.json"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def campaign_quests() -> dict[str, Any]:
    """Every quest's steps (``quests.json``): campaign-wide, so a quest
    started in one zone can finish in another."""
    data = (json.loads(QUESTS_PATH.read_text(encoding="utf-8"))
            if QUESTS_PATH.exists() else None)
    return (data or {}).get("quests") or {}


def runtime_enabled(zone_id: str) -> bool:
    """Whether the server runs this zone from its rules (``"runtime": "rules"``)."""
    path = rules_path(zone_id)
    if not path.exists():
        return False
    built = rules(None, zone_id)[0]
    return bool(built) and built.get("runtime") == "rules"


def rules(db, zone_id: str) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """``(merged rules, validation problems)``; ``(None, [])`` without a
    rules file. With ``db`` None only the file and quests are merged."""
    path = rules_path(zone_id)
    if not path.exists():
        return None, []
    # One build per zone and database connection.
    key = (zone_id, id(db) if db is not None else 0)
    stamp = (_mtime(path), _mtime(QUESTS_PATH), catalog_data.data_mtime(),
             _mtime(catalog_data.ZONES_PATH))
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached["stamp"] == stamp:
            return cached["rules"], cached["problems"]
    built, problems = _build(db, zone_id, path)
    with _LOCK:
        _CACHE.pop(key, None)
        _CACHE[key] = {"stamp": stamp, "rules": built, "problems": problems}
        # Bounded: the oldest zone/connection entries go first (dicts keep
        # insertion order).
        while len(_CACHE) > _CACHE_SIZE:
            del _CACHE[next(iter(_CACHE))]
    return built, problems


def _build(db, zone_id: str, path: Path):
    loaded = json.loads(path.read_text(encoding="utf-8"))
    merged = copy.deepcopy(loaded)
    # The zone's own nodes (merged offers of a shared template can name
    # nodes of other zones, e.g. every race's AZ0 NPCs).
    merged["file_nodes"] = sorted(loaded.get("nodes") or ())
    quests = campaign_quests()
    if quests:
        merged["quests"] = dict(merged.get("quests") or {}, **quests)
    entry = catalog_data.zone(zone_id)
    # An overworld node that enters a dungeon stays retryable: the dungeon can be
    # played again from its node whether or not the node is completed.
    merged["retryable_nodes"] = sorted({
        str(zone["parent_node"]) for zone in catalog_data.zones()
        if zone.get("parent") == zone_id and zone.get("parent_node")})
    # Overworld battles stay replayable after a win (the client shows them as
    # Repeatable); a dungeon's battles complete when won.
    merged["battles_repeat"] = entry.get("kind") == "overworld"
    # Starter panoramas (own AZ0 rows) have rules: NPCs are nodes.
    if db is not None and entry.get("server_template") and (
            entry["kind"] != "panorama" or entry.get("race") or entry.get("npcs")):
        data = catalog_data.zone_catalog(db, entry)
        entries, _starts = offers.build_offers(
            entry["server_template"], data, merged.get("quests") or {},
            known_conversations=set(conversations()))
        # A template's rows can name nodes of other zones (every race's AZ0
        # NPCs; an overworld's panorama NPCs): keep this zone's own.
        own = set(loaded.get("nodes") or ()) | set(entry.get("npcs") or ())
        for path_ in (loaded.get("map") or {}).get("paths") or ():
            own.update((path_["from"], path_["to"]))
        for node_id, node_entries in entries.items():
            if own and node_id not in own:
                continue
            merged.setdefault("nodes", {}).setdefault(node_id, {})["entries"] = node_entries
        # Scene actions (set_flag / clear_flag / check_tamed()) are live
        # editor data: rebuild their hooks instead of trusting the copies an
        # importer baked into the rules file.
        canon = (offers.canonical_node if entry["kind"] == "overworld"
                 else (lambda n: n))
        merged["hooks"] = [
            hook for hook in merged.get("hooks") or ()
            if not str(hook.get("id") or "").startswith(("scene:", "tamed:"))]
        # Gaal camps (Milosh's paid readings) are told apart by their scene.
        merged["gaal_nodes"] = sorted({
            canon(raw) for raw, listed in data["encounters"].items() for e in listed
            if "GAAL CAMP" in str(e.get("name") or "").upper()})
        merged["hooks"] += offers.scene_action_hooks(
            e for listed in data["encounters"].values() for e in listed)
        # Card-selector options ("choice" scene mods), by scene.
        merged["mod_choices"] = {
            str(e["guid"]).lower(): options
            for listed in data["encounters"].values() for e in listed
            if (options := engine.scene_mod_choices(e.get("mods")))}
        for raw_node, listed in data["encounters"].items():
            for encounter in listed:
                if encounter.get("battle") and offers.tamed_scene(encounter):
                    # A taming win completes the node only once tamed.
                    merged.setdefault("nodes", {}).setdefault(
                        canon(raw_node), {}).setdefault(
                        "completed", {"flag": offers.tamed_flag(encounter["guid"])})
    if entry.get("npcs"):
        merged["npc_zone"] = True
        if entry.get("parent_node"):
            merged["parent_node"] = entry["parent_node"]
        # Each NPC's portrait is the champion of its conversation.
        champions = {}
        known = conversations()
        for npc in entry["npcs"]:
            for each in (merged.get("nodes", {}).get(npc) or {}).get("entries") or ():
                guid = str(each.get("conversation") or each.get("pre") or "").lower()
                champion = (known.get(guid) or {}).get("champion") or ""
                if champion.strip("0-"):
                    champions[npc] = champion
                    break
        merged["npc_champions"] = champions
    # Authored offer order (the editor's drag-and-drop): offers named in the
    # saved order come first in that order; the others keep their generated
    # order after them. Conditions still decide which offer applies.
    for node_id, order in (merged.get("offer_order") or {}).items():
        spec = (merged.get("nodes") or {}).get(node_id)
        if not spec or not spec.get("entries"):
            continue
        rank = {offer: index for index, offer in enumerate(order or ())}
        spec["entries"] = sorted(
            spec["entries"],
            key=lambda entry: rank.get(entry.get("id"), len(rank)))
    # Checked after merging: live scene-action hooks set flags too.
    return merged, engine.validate(merged)

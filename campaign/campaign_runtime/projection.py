"""Project a zone's engine state onto the client's ``GameplayState``.

The engine's ``layout`` says which nodes are visible, completed, retryable,
impassable, quest-marked and what each offers now; this module turns that into
the ``VisLocs`` / ``LocNodes`` / ``ALoc`` / ``PublicState`` the client's zone
view models read (``UIDungeonZoneViewModel`` for AREA/DUNGEON,
``UIPanoramaZoneViewModel`` for PANORAMA). Pure: no database, no globals.

Runtime-only node progress (kept in the zone's ``nodes`` beside the engine's
fields, which the engine ignores):

* ``pre_done``      - an encounter entry's pre-battle conversation was played
* ``result``        - the result conversation still to show after a battle
* ``choices``       - the card-selector options offered for this battle

A node id may carry a ``@zone`` suffix (an NPC standing in two panoramas);
the client only ever sees the plain name (``display``).
"""

from __future__ import annotations

import copy
from typing import Any

import campaign_rules as engine

# Client location type per zone kind for a node that leads into another zone.
CHILD_TYPES = {"dungeon": "Dungeon", "panorama": "Panorama"}
TEMP_TYPES = {"overworld": "AREA", "dungeon": "DUNGEON", "panorama": "PANORAMA"}
MOD_CHOICE_LIST = "mod_choice_list"


def count_picker_vars(source: str) -> tuple[str, str, str]:
    """The ``PublicState.Data`` keys a count picker over counter ``source``
    names as its ``min_var`` / ``max_var`` / ``count_var`` (the client reads
    public variables by top-level key)."""
    return f"{source}_pick_min", f"{source}_pick_max", f"{source}_pick_count"


def zone_nodes(rules: dict[str, Any], zone_entry: dict[str, Any],
               children: dict[str, dict[str, Any]]) -> list[str]:
    """The nodes this zone shows: its rules file's nodes, its map's nodes and
    its links into child zones (merged offers can also name other races'
    NPCs of a shared template; those are not in this panorama)."""
    nodes = set(rules.get("file_nodes") or ())
    for path in (rules.get("map") or {}).get("paths") or ():
        nodes.update((path["from"], path["to"]))
    nodes.update(children)
    if (rules.get("start") or {}).get("node"):
        nodes.add(rules["start"]["node"])
    return sorted(nodes)


def n_not_fork(node: str) -> bool:
    return not str(node).startswith("Fork")


def display(node: str) -> str:
    """The name the client knows a node by (``Myaa@cavein`` -> ``Myaa``)."""
    return str(node).split("@", 1)[0]


def _base_location(node: str) -> dict[str, Any]:
    return {"name": display(node), "node": display(node), "rules_node": node,
            "type": "Empty",
            "autostart": False, "autopan": False, "autotrigger": False,
            "battle": None, "completed": False, "enabled": True,
            "visible": True, "repeatable": False, "givequest": False,
            "turninquest": False, "impassable": False, "unknown": False,
            "encounter": None, "encounter_desc": None, "allow_cancel": False,
            "conversationId": None}


def location_content(rules, state, node, entry, children, pending_visit=None):
    """``(type, conversation, scene, allow_cancel)`` the node offers now.

    ``pending_visit`` is a dungeon entrance's visit conversation not yet played:
    the client starts a conversation location as soon as it is offered."""
    progress = (state.get("nodes") or {}).get(node) or {}
    started = state.get("encounter") or {}
    if started.get("node") == node:
        return "Encounter", None, started.get("scene"), False
    if progress.get("result"):
        return "Convo", progress["result"], None, False
    if node in children:
        return CHILD_TYPES.get(children[node]["kind"], "Empty"), None, None, True
    if pending_visit and (entry is None or entry.get("kind") == "empty"):
        return "Convo", pending_visit, None, False
    if entry is None:
        return "Empty", None, None, False
    if entry.get("kind") == "conversation":
        return "Convo", entry.get("conversation"), None, False
    if entry.get("kind") == "shroomhaus":
        # Kooo's introduction first, then the card-choice window.
        if entry.get("pre") and not progress.get("pre_done"):
            return "Convo", entry["pre"], None, False
        return "ShroomHaus", None, None, False
    if entry.get("kind") == "encounter":
        if entry.get("pre") and not progress.get("pre_done"):
            return "Convo", entry["pre"], None, False
        return "Encounter", None, entry.get("scene"), True
    return "Empty", None, None, False


def project(rules: dict[str, Any], state: dict[str, Any],
            zone_entry: dict[str, Any], previous: dict[str, Any] | None,
            *, children: dict[str, dict[str, Any]], active: str | None,
            parked: bool = False, spent: dict[str, str] | None = None,
            entrance_visit: str | None = None,
            scene_champions: dict[str, str] | None = None,
            gaal_nodes: frozenset = frozenset(), gaal_display: str | None = None,
            force_move: str | None = None,
            count_picker: str | None = None) -> dict[str, Any]:
    """The client ``GameplayState`` for ``state`` (one zone's engine state).

    ``previous`` is the campaign row's last state: keys this module does not
    own (``TutorialDone``, ``ActiveEncounterGuid`` ...) are kept. ``active``
    is the location the client started (``StartLoc``) and has not finished;
    ``parked``: the player cancelled the battle panel, so a pending battle
    waits for them to select its location again. ``spent`` maps a node to
    the auto-started conversation already played there: it does not start
    by itself again (the player can still select it).
    ``scene_champions`` maps encounter scenes to their AI champion (panorama
    NPC portraits once a conversation has become a battle). ``gaal_nodes``
    are the Gaal camps, ``gaal_display`` the one showing its Fortune card,
    ``force_move`` a node the client's token must walk to (a walk-back).
    ``count_picker`` is the node whose count picker (``engine.count_picker``)
    shows now, after the conversation played there.
    """
    out = copy.deepcopy(previous or {})
    layout = engine.layout(rules, state)
    kind = zone_entry.get("kind")
    nodes = zone_nodes(rules, zone_entry, children)
    entries = {n: next((e for e in (rules.get("nodes", {}).get(n) or {})
                        .get("entries") or () if e.get("id") == info.get("entry")), None)
               for n, info in layout["nodes"].items()}
    vislocs, locnodes, scenes = [], [], []
    autostart = None
    started = state.get("encounter") or {}
    picker = (engine.count_picker(rules, state, count_picker)
              if count_picker in nodes else None)
    for node in nodes:
        info = layout["nodes"].get(node) or {}
        entry = entries.get(node)
        if (kind == "panorama" and not info.get("visible") and
                started.get("node") != node):
            # The panorama client shows every listed NPC that is not
            # completed (UIPanoramaZoneViewModel.ProcessStateChange ignores
            # Location.Visible), and only a visible one gets its portrait on
            # the quick-navigation bar: list an NPC once it is revealed.
            locnodes.append({"Name": display(node), "Data": {"id": display(node), "type": "DEFAULT"}})
            continue
        pending_visit = None
        if kind == "dungeon" and node == (rules.get("start") or {}).get("node") and \
                not ((state.get("nodes") or {}).get(node) or {}).get("visits_done"):
            pending_visit = entrance_visit
        ltype, conversation, scene, allow_cancel = location_content(
            rules, state, node, entry, children, pending_visit)
        data = _base_location(node)
        progress = (state.get("nodes") or {}).get(node) or {}
        data.update({
            "type": ltype, "conversationId": conversation, "encounter": scene,
            "allow_cancel": allow_cancel,
            "visible": bool(info.get("visible")),
            "enabled": bool(info.get("enabled", info.get("visible"))),
            # The client refuses to start a completed location, so a dungeon's
            # entrance (always retryable) is reported as not completed.
            "completed": (bool(info.get("completed")) and not progress.get("result")
                          and node not in (rules.get("retryable_nodes") or ())),
            "repeatable": bool(info.get("retryable")),
            "givequest": bool(info.get("givequest")),
            "turninquest": bool(info.get("turninquest")),
            "impassable": bool(info.get("impassable")),
        })
        if ltype == "Encounter" and progress.get("choices"):
            data["type"] = "ModChoiceEncounter"
            data["modlistId"] = f"/{MOD_CHOICE_LIST}"
        if ltype == "ShroomHaus":
            data["choices"] = list((entry or {}).get("choices") or ())
            data["autostart"] = not info.get("completed")
        if gaal_display == node:
            # The Fortune card the paid reading drew, shown by the client's
            # Gaal camp panel (a ModDisplay location).
            data.update({"type": "ModDisplay", "modspath": "/gaal_fortune_guid",
                         "window-title": "Gaal_Window_Title",
                         "window-desc": "Gaal_Window_Text", "conversationId": None,
                         "completed": False, "enabled": True, "visible": True,
                         "autostart": False, "repeatable": False})
        picking = bool(picker) and count_picker == node
        if picking:
            # The client's count picker (UICampaignGnomeSelect): its answer
            # is a count_choice event.
            low, high, count = count_picker_vars(picker["from"])
            data.update({"type": "CountPicker", "min_var": low, "max_var": high,
                         "count_var": count, "conversationId": None,
                         "encounter": None, "completed": False, "enabled": True,
                         "visible": True, "repeatable": False})
        played = ltype == "Convo" and (spent or {}).get(node) == conversation
        wants_start = ((started.get("node") == node and not parked)
                       or bool(progress.get("result"))
                       or bool(pending_visit and info.get("visible") and not info.get("completed"))
                       or (gaal_display == node) or picking
                       or (info.get("autostart") and info.get("visible") and not played
                           and (not info.get("completed") or info.get("retryable"))))
        if kind == "overworld" and gaal_display != node and not picking:
            # The token stops at (and starts) an unfinished node that is
            # impassable, or whose conversation has not been heard yet.
            fresh = not progress.get("talks") and not progress.get("visits_done")
            first_talk = (ltype == "Convo" and fresh and not (entry or {}).get("repeatable")
                          and not (entry or {}).get("kind") == "encounter")
            pre_talk = ltype == "Convo" and (entry or {}).get("kind") in ("encounter", "shroomhaus")
            data["autostart"] = bool(
                info.get("visible") and not info.get("completed") and
                n_not_fork(node) and
                (info.get("impassable") or info.get("autostart") or first_talk or pre_talk
                 or ltype == "ShroomHaus") and not played)
        if pending_visit and info.get("visible") and not info.get("completed"):
            # Offered now: the client starts it, so it plays on entry.
            data["autostart"] = True
        if wants_start and autostart is None:
            autostart = node
        if scene:
            scenes.append(scene)
        vislocs.append({"Data": data})
        node_data = {"id": display(node), "type": "FORK" if not n_not_fork(node) else "DEFAULT"}
        champion = (scene_champions or {}).get(str(scene or "").lower())
        champion = champion or (rules.get("npc_champions") or {}).get(node)
        if kind == "panorama" and champion:
            node_data["championId"] = champion
        locnodes.append({"Name": display(node), "Data": node_data})
    aloc = (started.get("node") if started.get("node") in nodes and not parked
            else None) or \
        (active if active in nodes else None) or autostart
    for loc in vislocs:
        if loc["Data"]["rules_node"] == aloc:
            loc["Data"]["autostart"] = True
    pdata = out.setdefault("PublicState", {}).setdefault("Data", {})
    # The engine's flags, mirrored where the server's shared helpers (scene
    # reward ``when`` tags, conditional ending actions) read them.
    pdata["conversation_flags"] = list(state.get("flags") or ())
    pdata["counters"] = dict(state.get("counters") or {})
    # Gnomes Come Home's quest objectives read their numbers from these
    # top-level variables (Records QuestObjectiveCollect.PublicScriptVar):
    # "carry" is the Current gnomes line, "count" the delivered gnomes out of 20.
    pdata["carry"] = int(pdata["counters"].get("gnomes_carried", 0) or 0)
    pdata["count"] = int(pdata["counters"].get("gnomes_safe", 0) or 0)
    pdata["keywords"] = list(state.get("keywords") or ())
    for spec in (rules.get("count_pickers") or {}).values():
        for key in count_picker_vars(str((spec or {}).get("from") or "")):
            pdata.pop(key, None)
    if picker:
        # 0..the counter still to move; the slider starts at all of them.
        low, high, count = count_picker_vars(picker["from"])
        pdata.update({low: 0, high: picker["available"], count: picker["available"]})
    pdata["CampaignGroup"] = zone_entry.get("campaign_group") or TEMP_TYPES.get(kind, "AREA")
    if kind == "panorama":
        # Panorama NPC portraits stay actionable on the quick-navigation bar.
        pdata["HideQuickNavigation"] = False
        if zone_entry.get("race"):
            pdata["IsStarterPano"] = True
        if zone_entry.get("scene_guid"):
            pdata["PanoramaSceneGuid"] = zone_entry["scene_guid"]
            pdata["PanoramaNode"] = zone_entry.get("parent_node")
            out["PanoramaSceneGuid"] = zone_entry["scene_guid"]
            out["PanoramaNode"] = zone_entry.get("parent_node")
    if kind == "overworld":
        # What the map client draws: visited and quest-objective nodes,
        # visited and still-locked paths, and a walk-back to perform.
        pdata["visited_nodes"] = sorted(
            display(n) for n, v in layout["nodes"].items() if v.get("visited"))
        pdata["quest_nodes"] = sorted(
            display(n) for n, v in layout["nodes"].items() if v.get("marker"))
        pdata["locked_paths"] = [p["id"] for p in layout["paths"] if p.get("locked")]
        pdata["visited_paths"] = list(state.get("visited_paths") or ())
        if force_move:
            pdata["force_move_dest"] = display(force_move)
        else:
            pdata.pop("force_move_dest", None)
    # The card selector lists the active battle's options (modlistId path).
    choices = ((state.get("nodes") or {}).get(aloc) or {}).get("choices") if aloc else None
    if choices:
        pdata[MOD_CHOICE_LIST] = list(choices)
    else:
        pdata.pop(MOD_CHOICE_LIST, None)
    out.update({
        "TempType": TEMP_TYPES.get(kind, "AREA"),
        "PayGroups": out.get("PayGroups") or [],
        "CSlide": None,
        "ALoc": display(aloc) if aloc else aloc,
        "VisLocs": vislocs,
        "LocNodes": locnodes,
        "Encounters": [{"Name": s, "Data": {"encscene": s}}
                       for s in dict.fromkeys(scenes)],
        "Champions": out.get("Champions") or [],
        "CurState": "LOSE" if state.get("run_lost") else "EXPLORE",
        "LastNode": display(state.get("current") or aloc or ""),
        "RulesZone": zone_entry["id"],
    })
    for key, default in (("Started", None), ("Finished", None),
                         ("FinishReason", None), ("Wins", 0), ("Losses", 0),
                         ("Score", 0), ("HealthAdj", 0), ("DungeonLifeAdj", 0),
                         ("Flags", {})):
        out.setdefault(key, default)
    return out

"""Auto-play an overworld zone (AZ1 / AZ2 and the dungeons and panoramas it
leads to) in the rules engine, completing every quest it can, and tally the
rewards the server would grant on the way.

The editor's Simulate view drives this one step at a time: ``next_step``
picks the next player action for the state the page holds; the page applies
it through the simulator's own ``/api/sim/apply`` (or, for a zone change,
loads the zone and the state ``next_step`` built with ``/api/sim/load``), so
the map on the left follows the run. The step after reports the rewards the
previous action earned (``rewards_for``). ``run`` plays the same loop here,
for tests and offline checks.

Pure with respect to the campaign: rules and reward rows are only read, and
the state and the run's memo are plain JSON the caller passes in and gets
back. Nothing is written to the database or the campaign files.

Choice rules (in order, per step):

1. A teleport the last action produced: into a zone of this run, enter it
   (the zone's progress so far is kept, as the server keeps it); elsewhere
   (another overworld, Crayburn) it is noted and ignored.
2. A battle a conversation started: win it.
3. A count picker at the current node: carry everything.
4. A dungeon's entrance conversation (first arrival): play it.
5. The current node's offer, when it has not been played with the same
   quest/flag state before: a conversation takes the ending that finishes
   the most quests (then advances or starts the most, then has its own
   ending actions, then comes first); a battle is won, with the battle
   events / card-selector card that do most for the quests; a Shroomkin Haus
   takes its first card.
6. The current node enters a child dungeon/panorama not entered yet (or a
   quest-marked one whose quests moved since it was last left).
7. Otherwise travel to the nearest worthwhile node over the open paths
   (stopping where the client's token stops: an unfinished impassable or
   auto-starting node): quest-marked / quest-advancing offers first, then any
   playable offer, then dungeon/panorama entrances, then unvisited nodes.
8. Nothing left: leave a child zone for where it was entered from; in the
   overworld, stop.

The run stops after ``MAX_STEPS`` steps.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections import deque
from typing import Any

import campaign_rules as engine
import catalog
from campaign_runtime import catalog_data, locate

MAX_STEPS = 2000
MAX_CHILD_ENTRIES = 4
RACE_ORDER = list(catalog.FACTION_OF_RACE)


# ---------------------------------------------------------------- zones

def zone_scope(home: str) -> list[str]:
    """``home`` and every dungeon/panorama reached from it (recursively).

    A race's starter panorama (``az0-<race>``) also reaches the overworld
    AZ1: its teleport leaves for AZ1, which the run then follows."""
    found, todo = [home], [home]
    while todo:
        current = todo.pop()
        for child in locate.child_zones(current).values():
            if child["id"] not in found:
                found.append(child["id"])
                todo.append(child["id"])
    if home.lower().startswith("az0-"):
        # The starter's quests lead to Crayburn Castle, and from there to AZ1.
        for extra in ("crayburn", "AZ1"):
            found += [zone for zone in zone_scope(extra) if zone not in found]
    return found


def _rules(db, zone_id: str) -> dict[str, Any]:
    loaded = catalog.rules(db, zone_id)[0]
    if loaded is None:
        raise LookupError(f"no rules file for zone {zone_id}")
    return loaded


def _children(zone_id: str) -> dict[str, dict[str, Any]]:
    """``{entrance node: child zone}`` for the children that have rules."""
    return {node: child for node, child in locate.child_zones(zone_id).items()
            if catalog.rules_path(child["id"]).exists()}


def _has_lives(rules) -> bool:
    return "lives" in (((rules or {}).get("start") or {}).get("counters") or {})


# ---------------------------------------------------------------- memo

def new_run(db, home: str, race: str, limit: int = MAX_STEPS, lose: int = 0,
            lose_scope: str = "any") -> tuple[dict[str, Any], dict[str, Any]]:
    """``(state, memo)`` for a fresh run of ``home`` with ``race`` (the
    state is the simulator's New game: ``engine.initial_state``).  ``lose``
    is how many first battles, at distinct nodes, the run loses."""
    memo = start_memo(db, home, race, limit, lose, lose_scope)
    state = engine.initial_state(_rules(db, home), race=race,
                                 faction=catalog.FACTION_OF_RACE[race])
    return state, memo


def start_memo(db, home: str, race: str, limit: int = MAX_STEPS,
               lose: int = 0, lose_scope: str = "any") -> dict[str, Any]:
    """The run's bookkeeping for ``home`` (an overworld) and ``race``."""
    if race not in catalog.FACTION_OF_RACE:
        raise ValueError(f"unknown race {race!r}")
    home_zone = catalog_data.zone(home)
    starter = home_zone.get("kind") == "panorama" and bool(home_zone.get("race"))
    if home_zone.get("kind") != "overworld" and not starter:
        raise ValueError(f"{home} is not an overworld or a race starter zone")
    _rules(db, home)
    return {"home": home, "zone": home, "race": race,
            "expected": zone_quests(db, home, race),
            "faction": catalog.FACTION_OF_RACE[race],
            "limit": max(1, min(int(limit or MAX_STEPS), 20000)),
            "lose": max(0, int(lose or 0)), "lost": [],
            # "any": the first battles; "dungeon": only battles inside a child zone.
            "lose_scope": lose_scope,
            "steps": 0, "battles": 0, "conversations": 0, "visits": 0,
            "zones": {}, "played": [], "entered": {}, "left": {},
            "entrance_done": [], "pre_done": [], "pending": None,
            "claims": [], "tally": {},
            "quests_done": [], "notes": [], "log": [], "done": None}


def _note(memo, text):
    if text not in memo["notes"]:
        memo["notes"].append(text)


def _log(memo, text):
    memo["log"].append(f"{memo['steps']}. [{memo['zone']}] {text}")
    del memo["log"][:-400]


def _play_stamp(state, node_id) -> str:
    """The state a played offer is keyed on: the run's signature plus the
    node's own result, which the signature leaves out (a lost fight changes
    only the node)."""
    progress = (state.get("nodes") or {}).get(node_id) or {}
    return (f"{_signature(state)}|{progress.get('outcome', '')}|"
            f"{int(bool(progress.get('won')))}|{int(bool(progress.get('failed')))}")


def _signature(state) -> str:
    """What decides whether replaying an offer can matter: quests, counters,
    keywords and the flags conversations did not set as a by-product."""
    flags = sorted(f for f in state.get("flags") or ()
                   if not str(f).startswith("conversation("))
    blob = json.dumps([state.get("quests") or {}, flags,
                       state.get("counters") or {}, state.get("keywords") or []],
                      sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- scoring

_RANK = {"not_started": 0, "active": 1, "finished": 2}


def _quest_score(state) -> tuple[int, int]:
    """``(finished quests, progress)``; progress counts starts and steps."""
    finished, progress = 0, 0
    for quest in (state.get("quests") or {}).values():
        status = quest.get("status", "not_started")
        finished += status == "finished"
        progress += _RANK.get(status, 0) * 1000 + int(quest.get("step", 0) or 0)
    return finished, progress


def _best(rules, state, candidates, node_id=""):
    """The candidate action whose result does most for the quests.

    Candidates are ``(action, bonus)``; ties go to the larger bonus, then
    the first listed."""
    base = _quest_score(state)
    best, best_key = None, None
    for index, (action, bonus) in enumerate(candidates):
        try:
            after, _log = engine.apply(rules, state, action)
        except engine.RulesError:
            continue
        finished, progress = _quest_score(after)
        completed = bool(node_id) and engine.node_status(rules, after, node_id)[0]
        # Counters next: answer scores (the Tranquil Dream's) gate later offers.
        counters = sum(int(v or 0) for v in (after.get("counters") or {}).values()) - \
            sum(int(v or 0) for v in (state.get("counters") or {}).values())
        key = (finished - base[0], progress - base[1], counters, completed, bonus, -index)
        if best_key is None or key > best_key:
            best, best_key = action, key
    return best


def _endings(guid: str) -> list[dict[str, Any]]:
    found = catalog.conversation(guid) or {}
    seen, result = set(), []
    for ending in found.get("endings") or ():
        if ending["ending"] not in seen:
            seen.add(ending["ending"])
            result.append(ending)
    return result


def _conversation_name(guid: str, fallback: str = "") -> str:
    return (catalog.conversation(guid) or {}).get("name") or fallback


def conversation_action(rules, state, node_id, guid, entry=None,
                        result_of=None, home=None) -> dict[str, Any]:
    """The ``conversation_end`` that does most for the quests (see module doc)."""
    base = {"type": "conversation_end", "node": node_id, "conversation": guid,
            "name": _conversation_name(guid, (entry or {}).get("label", ""))}
    if result_of:
        base["result_of"] = result_of
    endings = _endings(guid)
    if not endings:
        # A conversation whose answers are only its authored action keys (a
        # dungeon's "one/two/three_point_answer" starts a battle) ends with
        # the answer that does most, as the player would choose it.
        answers = [key for key in ((entry or {}).get("actions") or {}) if key != "*"]
        if answers:
            candidates = [(dict(base, ending=key), False) for key in answers]
            return _best(rules, state, candidates, node_id) or candidates[0][0]
    if len(endings) <= 1:
        return dict(base, ending=endings[0]["ending"] if endings else "")
    own = set((entry or {}).get("actions") or {}) - {"*"}
    candidates = [(dict(base, ending=e["ending"],
                        events=[e["ending"]] if e.get("event") else []),
                   e["ending"] in own) for e in endings]
    if home:
        # Inside the home overworld: an ending that teleports elsewhere is not
        # taken while another ending stays put.
        actions = (entry or {}).get("actions") or {}
        staying = [c for c in candidates if not _leaves_area(
            {"actions": {c[0]["ending"]: actions.get(c[0]["ending"], [])}}, home)]
        candidates = staying or candidates
    return _best(rules, state, candidates, node_id) or candidates[0][0]


def battle_action(rules, state, layout, node_id, scene, entry=None,
                  result_conversation=False, victory=True) -> dict[str, Any]:
    """A won ``encounter_result`` with the events / selector card that do most
    (a lost one carries no events: a defeat grants no tamed troop)."""
    base = {"type": "encounter_result", "node": node_id, "scene": scene,
            "victory": bool(victory)}
    if not victory:
        return base
    if result_conversation:
        # The battle's victory conversation plays next (and runs its actions).
        base["result_conversation"] = True
    events = [[], ["tamed_troop"]] + [[e] for e in (entry or {}).get(
        "complete_requires_events") or ()]
    mods = ((layout["nodes"].get(node_id) or {}).get("mod_choices") or [None])
    candidates = []
    for mod in mods:
        for each in events:
            action = dict(base, events=each)
            if mod:
                action["mod"] = mod
            candidates.append((action, 0))
    return _best(rules, state, candidates, node_id) or candidates[0][0]


# ---------------------------------------------------------------- planning

def _quest_related(rules, state, info, entry) -> bool:
    if info.get("marker") or info.get("givequest") or info.get("turninquest"):
        return True
    if not entry:
        return False
    wanted = {str(entry.get(k) or "").lower() for k in ("scene", "conversation")} - {""}
    for script, quest in (state.get("quests") or {}).items():
        if quest.get("status") != "active":
            continue
        steps = ((rules.get("quests") or {}).get(script) or {}).get("steps") or ()
        step = int(quest.get("step", 0) or 0)
        done_when = (steps[step].get("done_when") or {}) if step < len(steps) else {}
        for key in ("scene", "conversation"):
            values = done_when.get(key)
            values = values if isinstance(values, list) else [values]
            if wanted & {str(v or "").lower() for v in values}:
                return True
        for actions in ((entry.get("actions") or {}).values()):
            if any(script in str(a) for a in actions or ()):
                return True
    return False


def _playable(rules, state, layout, memo, zone_id, node_id):
    """The node's offer when the run should play it now, else None."""
    info = layout["nodes"].get(node_id) or {}
    entry = engine.active_entry(rules, state, node_id)
    if not entry or entry.get("kind") not in ("conversation", "encounter", "shroomhaus"):
        return None
    # A completed node still offers its current entry (projection), so a new
    # entry there (the next step of a quest giver) is played like any other.
    if entry.get("kind") == "shroomhaus" and not entry.get("choices"):
        return None
    if _leaves_area(entry, memo["home"]) and entry.get("kind") != "conversation":
        # Runs stay inside their home overworld (and its dungeons): a battle
        # whose offer teleports to another overworld is never played.  A
        # conversation is judged per ending (conversation_action).
        return None
    if entry.get("kind") == "conversation" and entry.get("conversation"):
        endings = _endings(entry["conversation"])
        actions = entry.get("actions") or {}
        if endings and all(_leaves_area({"actions": {e["ending"]: actions.get(e["ending"], [])}},
                                        memo["home"]) for e in endings):
            return None
    key = f"{zone_id}|{node_id}|{entry.get('id')}"
    if key + f"|{_play_stamp(state, node_id)}" in memo["played"]:
        return None
    if any(p.startswith(key + "|") for p in memo["played"]) and not (
            _quest_related(rules, state, info, entry)
            or (entry.get("kind") == "encounter" and not info.get("completed"))):
        # Played already: again only when a quest moved on, or while an
        # encounter is unfinished (a lost one stays playable until it is won).
        return None
    return entry


def _worth_entering(db, state, memo, child) -> bool:
    """Enter a child zone the first time; again only when the quests moved
    since it was left and it now offers something not played there yet."""
    zone_id = child["id"]
    entered = memo["entered"].get(zone_id)
    if entered is None:
        return True
    if memo["left"].get(zone_id) == _signature(state):
        return False
    rules = _rules(db, zone_id)
    campaign, _part = engine.split_progress(state)
    view = engine.zone_view(rules, campaign, memo["zones"].get(zone_id))
    layout = engine.layout(rules, view)
    capped = entered >= MAX_CHILD_ENTRIES
    for node_id, info in layout["nodes"].items():
        if not info.get("visible"):
            continue
        entry = _playable(rules, view, layout, memo, zone_id, node_id)
        if entry is None or any(
                p.startswith(f"{zone_id}|{node_id}|{entry.get('id')}|")
                for p in memo["played"]):
            continue
        # Past the entry cap only a quest-related offer is worth another visit
        # (a quest can need a conversation the cap would otherwise skip).
        if not capped or _quest_related(rules, state, info, entry):
            return True
    return False


def _route(rules, layout, start, goal):
    """Nodes from ``start`` to ``goal`` over the shown paths, or None."""
    # The token walks up to an impassable node and stops there, so the last hop
    # into an impassable goal may use its locked path; no other locked path opens.
    goal_impassable = bool((layout["nodes"].get(goal) or {}).get("impassable"))
    open_paths = {p["id"] for p in layout["paths"] if p["shown"] and (
        not p["locked"] or (goal_impassable and goal in (p["from"], p["to"])))}
    links: dict[str, list[str]] = {}
    for path in (rules.get("map") or {}).get("paths") or ():
        if path["id"] in open_paths:
            links.setdefault(path["from"], []).append(path["to"])
            links.setdefault(path["to"], []).append(path["from"])
    previous = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node == goal:
            route = []
            while node is not None:
                route.append(node)
                node = previous[node]
            return route[::-1]
        for nxt in sorted(links.get(node, ())):
            if nxt not in previous:
                previous[nxt] = node
                queue.append(nxt)
    return None


def _stop(layout, route):
    """Where the client's token stops on ``route`` (runtime._route_stop)."""
    for node in route[1:]:
        info = layout["nodes"].get(node) or {}
        if (not node.startswith("Fork") and not info.get("completed") and
                (info.get("impassable") or info.get("autostart"))):
            return node
    return route[-1]


def _travel_target(db, rules, state, layout, memo, zone_id):
    """``(node to visit, why)`` or None (rule 7)."""
    current = state.get("current") or ""
    children = _children(zone_id)
    # A zone with no paths (a starter panorama) lists its NPCs directly.
    npc_zone = bool(rules.get("npc_zone")) or not (rules.get("map") or {}).get("paths")
    options = []
    for node_id, info in layout["nodes"].items():
        # A fork is a pass-through junction (no offer, never enabled): it is
        # only worth walking to when it is shown and unvisited, to open the
        # nodes behind it.
        fork = node_id.startswith("Fork")
        if node_id == current or not info.get("visible") or not (
                info.get("enabled") or (fork and not info.get("visited"))):
            continue
        entry = None if fork else _playable(rules, state, layout, memo, zone_id, node_id)
        if fork:
            rank, why = 3, "through a fork"
        elif entry is not None:
            rank = 0 if _quest_related(rules, state, info, entry) else 1
            if _leaves_area(entry, memo["home"]):
                rank = 9   # leaving the area is the last thing to do
            why = f"for {entry.get('label') or entry.get('id')}"
        elif node_id in children and _worth_entering(db, state, memo, children[node_id]):
            rank, why = 2, f"to enter {children[node_id]['id']}"
        elif not info.get("visited") and not npc_zone:
            rank, why = 3, "to explore"
        elif (not info.get("completed") and
              f"{zone_id}|{node_id}|{_signature(state)}" not in memo.get("looked", [])):
            # Nothing new elsewhere: revisit an incomplete node, in case a
            # conversation there is offered now that the quests have moved.
            rank, why = 4, "to look for a new conversation"
        else:
            continue
        route = None if npc_zone else _route(rules, layout, current, node_id)
        # The overworld simulator must follow open paths. Panorama NPC zones
        # have no path graph, so their visible NPCs remain directly selectable.
        if not npc_zone and route is None:
            continue
        options.append((rank, len(route) if route else 999, node_id,
                        route or [current, node_id], why))
    if not options:
        return None
    rank, _length, node_id, route, why = min(options)
    if rank == 4:
        # Each incomplete node is revisited once per quest/flag state.
        memo.setdefault("looked", []).append(f"{zone_id}|{node_id}|{_signature(state)}")
    stop = _stop(layout, route) if len(route) > 2 else route[-1]
    if stop != node_id:
        why = f"on the way to {node_id} ({why})"
    return stop, why


def _enter(db, state, memo, dest, node, *, child_of=None, done_child=False):
    """The state on arriving in zone ``dest`` (at ``node``, or its start)."""
    here = memo["zone"]
    campaign, part = engine.split_progress(state)
    memo["zones"][here] = part
    if done_child or child_of is None:
        # Leaving a child zone (or a teleport out of it).
        memo["left"][here] = _signature(state)
    rules = _rules(db, dest)
    if child_of is not None:
        memo["entered"][dest] = memo["entered"].get(dest, 0) + 1
        campaign["return_to"] = {"zone": here, "node": child_of}
        if _has_lives(rules):
            # Each entry starts a dungeon with lives afresh (the server's rule).
            memo["zones"].pop(dest, None)
            memo["entrance_done"] = [z for z in memo["entrance_done"] if z != dest]
            counters = campaign.setdefault("counters", {})
            start_counters = (rules.get("start") or {}).get("counters") or {}
            for name, value in start_counters.items():
                counters[name] = int(value or 0)
    new = engine.zone_view(rules, campaign, memo["zones"].get(dest))
    memo["zone"] = dest
    log = []
    if node and new.get("current") != node:
        new, log = engine.apply(rules, new, {"type": "visit", "node": node})
        memo["visits"] += 1
    return new, log


def _leaves_area(entry, home) -> bool:
    """Whether an offer's actions teleport to another overworld than ``home``."""
    for actions in (entry.get("actions") or {}).values():
        for action in actions or ():
            text = str(action)
            if text.startswith("teleport("):
                dest = text[len("teleport("):].split(",")[0].strip(") ")
                if catalog_data.zone(dest).get("kind") == "overworld" and dest.lower() != str(home).lower():
                    return True
    return False


def _step(db, zone_id, state, memo) -> dict[str, Any]:
    """The next step (without counting it): ``{"apply": action, "why"}``,
    ``{"enter": zone, "node", "state", "why"}`` or ``{"done": reason}``."""
    rules = _rules(db, zone_id)
    scope = zone_scope(memo["home"])
    layout = engine.layout(rules, state)
    current = state.get("current") or ""

    target = state.get("teleport")
    if target:
        dest, node = str(target.get("zone") or ""), str(target.get("node") or "")
        if dest.lower() == zone_id.lower():
            if node and node != current:
                return {"apply": {"type": "visit", "node": node},
                        "why": f"teleported to {node}"}
        else:
            if (catalog_data.zone(dest).get("kind") == "overworld"
                    and dest.lower() != str(memo["home"]).lower()):
                # Another overworld is on offer: stop here, without entering it.
                return {"done": f"stopped at the teleport to {dest}"
                                f"{' ' + node if node else ''} (the next area)"}
            match = next((z for z in scope if z.lower() == dest.lower()), None)
            if match:
                return {"enter": match, "node": node, "why": f"teleport to {match}",
                        "done_child": True}
            if catalog_data.zone(dest).get("kind") == "overworld":
                # The next overworld: the run plays everything up to here and stops.
                return {"done": f"stopped at the teleport to {dest}"
                                f"{' ' + node if node else ''} (the next area)"}
            _note(memo, f"teleport to {dest}{' ' + node if node else ''} "
                        f"(outside {memo['home']}) not followed")

    started = state.get("encounter") or {}
    if started.get("node"):
        return {"apply": battle_action(rules, state, layout, started["node"],
                                       started.get("scene", "")),
                "why": "win the battle the conversation started"}

    picker = engine.count_picker(rules, state, current) if current else None
    if picker:
        return {"apply": {"type": "count_choice", "node": current,
                          "count": picker["available"]},
                "why": f"carry all {picker['available']} ({picker['from']})"}

    zone_entry = catalog_data.zone(zone_id)
    start_node = (rules.get("start") or {}).get("node")
    if current and current == start_node and zone_id not in memo["entrance_done"]:
        guid = catalog_data.entrance_visit_conversation(zone_entry, current)
        memo["entrance_done"].append(zone_id)
        if guid:
            return {"apply": conversation_action(rules, state, current, guid),
                    "why": "dungeon entrance conversation"}

    pending = memo.get("pending") or {}
    if pending:
        memo["pending"] = None
        if pending.get("zone") == zone_id and pending.get("node") == current:
            return {"apply": conversation_action(
                rules, state, current, pending["conversation"],
                result_of=pending.get("result_of")),
                "why": "battle victory conversation"}

    if current:
        entry = _playable(rules, state, layout, memo, zone_id, current)
        if entry is not None and _leaves_area(entry, memo["home"]) and _travel_target(
                db, rules, state, layout, memo, zone_id) is not None:
            # Everything else in this area comes first: leaving is the last step.
            entry = None
        pre_key = f"{zone_id}|{current}"
        if (entry is not None and entry["kind"] == "encounter" and entry.get("pre")
                and pre_key not in memo["pre_done"]):
            # The client plays an offered battle's pre-battle conversation
            # first, once per node (projection: pre_done).
            memo["pre_done"].append(pre_key)
            return {"apply": conversation_action(rules, state, current, entry["pre"],
                                                 home=memo["home"]),
                    "why": f"pre-battle talk: {entry.get('label') or entry.get('id')}"}
        if entry is not None:
            memo["played"].append(
                f"{zone_id}|{current}|{entry.get('id')}|{_play_stamp(state, current)}")
            label = entry.get("label") or entry.get("id")
            if entry["kind"] == "conversation":
                return {"apply": conversation_action(
                    rules, state, current, entry["conversation"], entry,
                    home=memo["home"]), "why": f"talk: {label}"}
            if entry["kind"] == "encounter":
                key = f"{zone_id}|{current}"
                in_scope = (memo.get("lose_scope") == "any" or
                            zone_id != memo["home"])
                if memo.get("lose", 0) > 0 and key not in memo["lost"] and in_scope:
                    memo["lose"] -= 1
                    memo["lost"].append(key)
                    return {"apply": battle_action(
                        rules, state, layout, current, entry.get("scene", ""),
                        entry, False, victory=False),
                        "why": f"battle lost: {label}"}
                if entry.get("victory"):
                    memo["pending"] = {"zone": zone_id, "node": current,
                                       "conversation": entry["victory"],
                                       "result_of": entry.get("id")}
                return {"apply": battle_action(rules, state, layout, current,
                                               entry.get("scene", ""), entry,
                                               bool(entry.get("victory"))),
                        "why": f"battle: {label}"}
            return {"apply": {"type": "shroom_choice", "node": current,
                              "card": entry["choices"][0]},
                    "why": f"Shroomkin Haus: {label}"}
        child = _children(zone_id).get(current)
        if child and child["id"] in scope and _worth_entering(db, state, memo, child):
            return {"enter": child["id"], "node": "", "child_of": current,
                    "why": f"enter {child['id']}"}

    found = _travel_target(db, rules, state, layout, memo, zone_id)
    if found:
        node, why = found
        return {"apply": {"type": "visit", "node": node}, "why": f"go to {node} {why}"}

    if zone_entry.get("kind") == "overworld" and zone_id != memo["home"]:
        # Reached an overworld by teleport and nothing is left there: the run ends
        # in that area (it does not return to a starter zone).
        return {"done": f"finished in {zone_id}: nothing reachable left"}
    if zone_id != memo["home"]:
        back = state.get("return_to") or {}
        dest = back.get("zone") or zone_entry.get("parent") or memo["home"]
        node = back.get("node") or zone_entry.get("parent_node") or ""
        return {"enter": dest, "node": node, "why": f"nothing left: back to {dest}",
                "done_child": True}
    active = sorted(s for s, q in (state.get("quests") or {}).items()
                    if q.get("status") == "active")
    if active:
        return {"done": "stuck: no reachable action, quests still active: "
                        + ", ".join(active)}
    return {"done": "finished: nothing reachable left to play"}


def next_step(db, zone_id, state, memo, last=None) -> dict[str, Any]:
    """One step of the run, for the editor.

    ``last`` is ``{"action", "log", "state"}`` of the action the page just
    applied (its rewards are tallied first; ``state`` is the state after it,
    i.e. ``state`` here). Returns ``{"memo", "step"}`` where ``step`` is
    ``{"apply": action, "why"}``, ``{"enter": zone, "state", "why"}`` or
    ``{"done": reason}``; ``memo["summary"]`` is the report so far.
    """
    memo = copy.deepcopy(memo)
    memo.pop("summary", None)
    state = copy.deepcopy(state)
    if memo.get("done"):
        return {"memo": memo, "step": {"done": memo["done"]}}
    if last and last.get("action"):
        rewards_for(db, memo, zone_id, last["action"], last.get("log") or [], state)
    if memo["steps"] >= memo["limit"]:
        memo["done"] = f"step limit ({memo['limit']}) reached"
        memo["summary"] = summary(db, memo, state)
        return {"memo": memo, "step": {"done": memo["done"]}}
    step = _step(db, zone_id, state, memo)
    memo["steps"] += 1
    if "done" in step:
        memo["done"] = step["done"]
        _log(memo, step["done"])
    elif "apply" in step:
        action = step["apply"]
        kind = action["type"]
        memo["battles"] += kind == "encounter_result"
        memo["conversations"] += kind == "conversation_end"
        memo["visits"] += kind == "visit"
        _log(memo, step["why"])
    else:
        new, log = _enter(db, state, memo, step["enter"], step.get("node") or "",
                          child_of=step.get("child_of"),
                          done_child=bool(step.get("done_child")))
        _log(memo, step["why"])
        rewards_for(db, memo, step["enter"], {"type": "visit"}, log, new)
        step = {"enter": step["enter"], "state": new, "why": step["why"]}
    memo["summary"] = summary(db, memo, state if "enter" not in step else step["state"])
    return {"memo": memo, "step": step}


# ---------------------------------------------------------------- rewards

def _json(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value or "null") or default
        except ValueError:
            return default
    return value if value is not None else default


_OVERRIDES: dict[str, tuple[float, dict[str, dict]]] = {}
_OVERRIDES_LOCK = threading.Lock()


def _override(table, key_field, key):
    """The campaign files' row for ``key`` (the last one wins), cached per
    change of the data files."""
    stamp = catalog_data.data_mtime()
    with _OVERRIDES_LOCK:
        cached = _OVERRIDES.get(table)
    if cached is None or cached[0] != stamp:
        rows = {}
        for row in catalog_data.data_overrides(table):
            rows[str(row.get(key_field) or "").lower()] = row
        cached = (stamp, rows)
        with _OVERRIDES_LOCK:
            _OVERRIDES[table] = cached
    return cached[1].get(str(key).lower())


def quest_reward(db, script):
    """``rewards_json`` of a quest (campaign file first, then the database),
    or None when the quest is disabled."""
    row = _override("quest_templates", "script_name", script)
    enabled = True
    db_row = db.execute("SELECT rewards_json, enabled FROM quest_templates "
                        "WHERE script_name=?", (script,)).fetchone() if db else None
    if db_row is not None:
        enabled = bool(db_row[1])
    if row is not None and "enabled" in row:
        enabled = bool(row["enabled"])
    if not enabled:
        return None
    if row is not None and "rewards_json" in row:
        return _json(row["rewards_json"], {})
    return _json(db_row[0], {}) if db_row is not None else {}


def scene_rewards(db, scene):
    """The scene's end-of-game reward records (campaign._scene_reward_records)."""
    row = _override("encounter_scenes", "guid", scene)
    if row is not None and "rewards_json" in row:
        data = _json(row["rewards_json"], {})
    else:
        db_row = db.execute("SELECT rewards_json FROM encounter_scenes WHERE guid=?",
                            (str(scene),)).fetchone() if db else None
        data = _json(db_row[0] if db_row else None, {})
    if not isinstance(data, dict):
        return []
    found = data.get("end_of_game_rewards")
    if found is None:
        found = data.get("end_of_game")
    if found is None and (data.get("end_of_game_condition") or data.get("card_guid")
                          or data.get("gold") is not None or data.get("xp") is not None
                          or data.get("chest_guid") or data.get("items")
                          or data.get("item_guid") or data.get("item_guid_by_race")
                          or data.get("stardust")):
        found = [data]
    if isinstance(found, dict):
        found = [found]
    return [r for r in found or () if isinstance(r, dict)]


def conversation_reward(db, guid):
    """``(reward, one_time)`` of a conversation, or None."""
    row = _override("conversation_rewards", "conversation_guid", guid)
    db_row = db.execute("SELECT reward_json, one_time, enabled FROM conversation_rewards "
                        "WHERE conversation_guid=?", (str(guid),)).fetchone() if db else None
    if row is not None:
        reward = _json(row.get("reward_json"), {})
        one_time = bool(row.get("one_time", True))
        enabled = bool(row.get("enabled", True))
    elif db_row is not None:
        reward, one_time, enabled = _json(db_row[0], {}), bool(db_row[1]), bool(db_row[2])
    else:
        return None
    return (reward, one_time) if enabled and reward else None


def _add(memo, kind, guid, amount, source):
    if not amount:
        return
    key = f"{kind}:{str(guid or '').lower()}"
    row = memo["tally"].setdefault(key, {"kind": kind, "guid": str(guid or ""),
                                         "count": 0, "sources": []})
    row["count"] += amount
    if source not in row["sources"]:
        row["sources"].append(source)


def _int(value, default=0):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return default


def _specs(value, extra):
    specs = value or []
    if isinstance(specs, dict):
        specs = [specs]
    return list(specs) + extra


def grant_reward_dict(memo, reward, race, source):
    """``campaign._apply_reward_dict``: gold, xp, cards, items, stardust, chest."""
    _add(memo, "gold", "", _int(reward.get("gold")), source)
    _add(memo, "xp", "", _int(reward.get("xp")), source)
    quantity = max(1, _int(reward.get("quantity"), 1) or 1)
    extra = []
    if (reward.get("card_guid_by_race") or {}).get(race):
        extra.append({"guid": reward["card_guid_by_race"][race], "quantity": quantity})
    if reward.get("card_guid"):
        extra.append({"guid": reward["card_guid"], "quantity": quantity})
    for spec in _specs(reward.get("cards"), extra):
        spec = {"guid": spec} if isinstance(spec, str) else spec
        if isinstance(spec, dict):
            guid = spec.get("guid") or spec.get("template") or spec.get("card_guid")
            _add(memo, "card", guid, max(1, _int(spec.get("quantity"), 1) or 1) if guid else 0,
                 source)
    extra = []
    if (reward.get("item_guid_by_race") or {}).get(race):
        extra.append({"guid": reward["item_guid_by_race"][race], "quantity": quantity})
    if reward.get("item_guid"):
        extra.append({"guid": reward["item_guid"], "quantity": quantity})
    for spec in _specs(reward.get("items"), extra):
        spec = {"guid": spec} if isinstance(spec, str) else spec
        if isinstance(spec, dict):
            guid = spec.get("guid") or spec.get("template") or spec.get("item_guid")
            _add(memo, "item", guid, max(1, _int(spec.get("quantity"), 1) or 1) if guid else 0,
                 source)
    _stardust(memo, reward, source)
    chest = (reward.get("chest_guid") or reward.get("chest_template")
             or reward.get("pack_guid") or (reward.get("chest_guid_by_race") or {}).get(race))
    if chest:
        _add(memo, "chest", chest, 1, source)
    _mercenaries(memo, reward, source)


def _stardust(memo, reward, source):
    for entry in reward.get("stardust") or ():
        if isinstance(entry, dict) and _int(entry.get("amount")):
            try:
                chance = float(entry.get("probability", 100))
            except (TypeError, ValueError):
                chance = 100.0
            rarity = str(entry.get("rarity") or "").lower()
            label = rarity if chance >= 100 else f"{rarity} @{chance:g}%"
            _add(memo, "stardust", label, _int(entry["amount"]), source)


def _mercenaries(memo, reward, source):
    """Mercenary and sleeve rewards: tallied the same way the server grants them."""
    for key, kind in (("mercenary", "mercenary"), ("mercenaries", "mercenary"),
                      ("mercenary_guid", "mercenary"), ("sleeves", "sleeve")):
        value = reward.get(key)
        for entry in (value if isinstance(value, list) else [value] if value else []):
            guid = entry.get("guid") if isinstance(entry, dict) else entry
            quantity = _int(entry.get("quantity"), 1) if isinstance(entry, dict) else 1
            _add(memo, kind, guid, quantity, source)


def grant_scene(memo, state, scene, events, node, source):
    """``campaign._apply_encounter_end_rewards`` for a won battle."""
    race = memo["race"]
    for index, record in enumerate(scene_rewards(memo["_db"], scene)):
        reward = record.get("reward") if isinstance(record.get("reward"), dict) else record
        one_time = record.get("one_time")
        if one_time is None:
            one_time = reward.get("one_time", True)
        claim = f"scene:{str(scene).lower()}:{index}"
        if one_time and claim in memo["claims"]:
            continue
        when = record.get("when")
        if when and not engine.evaluate(when, state, node):
            continue
        condition = record.get("end_of_game_condition")
        met = not condition or (
            str((condition or {}).get("type") or "").lower() == "void_tamed_troop"
            and "tamed_troop" in (events or ()))
        _add(memo, "gold", "", _int(reward.get("gold")), source)
        _add(memo, "xp", "", _int(reward.get("xp")), source)
        if met:
            template = reward.get("card_guid") or reward.get("template")
            if isinstance(template, str) and template.startswith("$condition."):
                template = "(the tamed creature)"
            if template:
                _add(memo, "card", template, max(1, _int(reward.get("quantity"), 1) or 1),
                     source)
            chest = (reward.get("chest_guid") or reward.get("chest_template")
                     or reward.get("pack_guid"))
            if chest:
                _add(memo, "chest", chest, 1, source)
            extra = []
            if (reward.get("item_guid_by_race") or {}).get(race):
                extra.append({"guid": reward["item_guid_by_race"][race],
                              "quantity": reward.get("quantity", 1)})
            if reward.get("item_guid"):
                extra.append({"guid": reward["item_guid"],
                              "quantity": reward.get("quantity", 1)})
            for spec in _specs(reward.get("items"), extra):
                spec = {"guid": spec} if isinstance(spec, str) else spec
                if isinstance(spec, dict):
                    guid = spec.get("guid") or spec.get("template") or spec.get("item_guid")
                    _add(memo, "item", guid,
                         max(1, _int(spec.get("quantity"), 1) or 1) if guid else 0, source)
            _stardust(memo, reward, source)
            _mercenaries(memo, reward, source)
        if one_time:
            memo["claims"].append(claim)


def rewards_for(db, memo, zone_id, action, log, state):
    """Tally what ``action`` (already applied; ``log`` its engine log,
    ``state`` the state after it) earned, as the server grants it."""
    memo["_db"] = db
    try:
        kind = action.get("type")
        node = action.get("node", "")
        if kind == "conversation_end" and action.get("conversation"):
            guid = action["conversation"]
            found = conversation_reward(db, guid)
            claim = f"conversation:{str(guid).lower()}"
            if found and not (found[1] and claim in memo["claims"]):
                grant_reward_dict(memo, found[0], memo["race"],
                                  f"conversation {action.get('name') or guid}")
                if found[1]:
                    memo["claims"].append(claim)
        elif kind == "encounter_result" and action.get("victory"):
            scene = action.get("scene") or ""
            grant_scene(memo, state, scene, action.get("events") or [], node,
                        f"battle {_scene_name(scene)}")
        elif kind == "shroom_choice" and action.get("card"):
            _add(memo, "card", action["card"], 1, f"Shroomkin Haus {node}")
        for item in log or ():
            event = item.get("event") if isinstance(item, dict) else None
            if not event or event.get("type") != "quest_complete":
                continue
            script = event.get("quest", "")
            if script not in memo["quests_done"]:
                memo["quests_done"].append(script)
            claim = f"quest:{script}"
            if claim in memo["claims"]:
                continue
            memo["claims"].append(claim)
            reward = quest_reward(db, script)
            if reward:
                grant_reward_dict(memo, reward, memo["race"],
                                  f"quest {_quest_title(db, memo['home'], script)}")
    finally:
        memo.pop("_db", None)


# ---------------------------------------------------------------- report

def _quest_title(db, home, script) -> str:
    quest = (_rules(db, home).get("quests") or {}).get(script) or {}
    return quest.get("title") or script


_SCENE_NAMES: dict[str, str] = {}


def _scene_name(scene) -> str:
    if not _SCENE_NAMES:
        _SCENE_NAMES.update(catalog.encounter_scene_names())
    return _SCENE_NAMES.get(str(scene).lower()) or str(scene)


def zone_quests(db, home: str, race: str = "") -> list[str]:
    """The quests started or finished by conversations of ``home`` and the
    zones it leads to (``catalog.quest_action_index``); with ``race``, only
    those a conversation for that race / its faction starts (by the start
    conversation's audience, ``catalog_data._audience``)."""
    faction = catalog.FACTION_OF_RACE.get(race, "")

    def for_race(row):
        who = catalog_data._audience(str(row.get("name") or ""))
        return ((not who["race"] or who["race"].lower() == race.lower()) and
                (not who["faction"] or who["faction"].lower() == faction.lower()))

    templates = {str(catalog_data.zone(z).get("server_template") or "").lower()
                 for z in zone_scope(home)}
    templates.discard("")
    found = []
    for script, kinds in catalog.quest_action_index(db).items():
        rows = (kinds.get("start") or []) + (kinds.get("complete") or [])
        script = script.split(")")[0].strip()
        starts = kinds.get("start") or []
        if race and starts and not any(for_race(r) for r in starts):
            continue
        if script and script not in found and any(
                str(r.get("template") or "").lower() in templates for r in rows):
            found.append(script)
    return sorted(found)


_NAMES: dict[str, str] = {}


def _name(db, kind, guid) -> str:
    if kind in ("gold", "xp"):
        return {"gold": "Gold", "xp": "XP"}[kind]
    if kind == "stardust":
        return f"Stardust ({guid})"
    if not guid or not _looks_like_guid(guid):
        return str(guid)
    key = str(guid).lower()
    if not _NAMES:
        from campaign_runtime.records import records
        for record in records("InventoryItemData") + records("MercenaryTemplate"):
            gid = str((record.get("m_Id") or {}).get("m_Guid") or "").lower()
            if gid:
                _NAMES[gid] = str(record.get("m_Name") or "").strip()
    if key not in _NAMES and db is not None:
        row = db.execute("SELECT name FROM card_templates WHERE lower(guid)=?",
                         (key,)).fetchone()
        _NAMES[key] = row[0] if row else ""
    return _NAMES.get(key) or str(guid)


def _looks_like_guid(value) -> bool:
    return len(str(value)) == 36 and str(value).count("-") == 4


def _offers_of(db, memo, needle):
    """``[(zone, node, entry)]`` whose offer mentions ``needle`` (a scene or
    conversation GUID, or an action text)."""
    found = []
    for zone_id in zone_scope(memo["home"]):
        for node_id, spec in (_rules(db, zone_id).get("nodes") or {}).items():
            for entry in spec.get("entries") or ():
                if needle.lower() in json.dumps(entry).lower():
                    found.append((zone_id, node_id, entry))
    return found


def _blocker(db, memo, state, script, step) -> str:
    """Why the active quest's current step did not finish: where its trigger
    is offered and whether that offer's condition holds at the end."""
    done_when = step.get("done_when") or {}
    needles = []
    for key in ("scene", "conversation", "node"):
        values = done_when.get(key)
        needles += [str(v) for v in (values if isinstance(values, list) else [values]) if v]
    if not done_when:
        needles = [f"complete_step({script},{step.get('id')})", f"complete_quest({script})"]
    places = []
    for needle in needles:
        for zone_id, node_id, entry in _offers_of(db, memo, needle):
            when = entry.get("when")
            if when is False:
                why = "its offer has no condition (when: false)"
            else:
                rules = _rules(db, zone_id)
                campaign, _part = engine.split_progress(state)
                view = (state if zone_id == memo["zone"] else
                        engine.zone_view(rules, campaign, memo["zones"].get(zone_id)))
                try:
                    holds = engine.evaluate(when, view, node_id)
                except engine.RulesError:
                    holds = False
                info = engine.layout(rules, view)["nodes"].get(node_id) or {}
                why = ("offer condition " + json.dumps(when) + " does not hold"
                       if not holds else "offered, node not reachable"
                       if not info.get("visible") else "offered but not played")
            places.append(f"{zone_id} {node_id} {entry.get('label') or entry.get('id')}: {why}")
    trigger = json.dumps(done_when) if done_when else "no done_when trigger"
    if not places:
        return f"{trigger}; no offer in {memo['home']} triggers it"
    return f"{trigger}; " + " | ".join(places[:4])


def summary(db, memo, state) -> dict[str, Any]:
    """Quests done / not done (with where each is stuck) and the tally."""
    rules_quests = _rules(db, memo["home"]).get("quests") or {}
    quests = state.get("quests") or {}
    wanted = memo.get("expected") or []
    final = bool(memo.get("done"))
    finished = [s for s in memo["quests_done"]]
    finished += [s for s, q in quests.items()
                 if q.get("status") == "finished" and s not in finished]
    open_ = []
    for script in sorted(set(wanted) | {s for s, q in quests.items()
                                        if q.get("status") == "active"}):
        if script in finished:
            continue
        progress = quests.get(script) or {}
        steps = (rules_quests.get(script) or {}).get("steps") or ()
        step = int(progress.get("step", 0) or 0)
        status = progress.get("status", "not_started")
        blocked = ""
        if not final:
            blocked = ""     # worked out once the run has stopped
        elif status == "active" and step < len(steps):
            blocked = f"step {steps[step].get('id')}: " + _blocker(
                db, memo, state, script, steps[step])
        elif status == "not_started":
            starts = (catalog.quest_action_index(db).get(script) or {}).get("start") or []
            blocked = "never started" + (
                " (starts at " + "; ".join(sorted({
                    f"{r.get('template')} {r.get('node')}: {r.get('name')}"
                    for r in starts})[:3]) + ")" if starts else
                " (no conversation starts it)")
        open_.append({"quest": script,
                      "title": (rules_quests.get(script) or {}).get("title") or script,
                      "status": status, "blocked": blocked})
    tally = sorted(({"kind": row["kind"], "guid": row["guid"],
                     "name": _name(db, row["kind"], row["guid"]),
                     "count": row["count"], "sources": row["sources"][:8]}
                    for row in memo["tally"].values() if row["count"]),
                   key=lambda r: (["gold", "xp", "chest", "card", "item", "stardust",
                                   "mercenary"].index(r["kind"])
                                  if r["kind"] in ("gold", "xp", "chest", "card", "item",
                                                   "stardust", "mercenary") else 9,
                                  r["name"].lower()))
    return {"home": memo["home"], "race": memo["race"], "faction": memo["faction"],
            "steps": memo["steps"], "limit": memo["limit"], "battles": memo["battles"],
            "conversations": memo["conversations"], "visits": memo["visits"],
            "zone": memo["zone"], "done": memo.get("done"),
            "quests_completed": [{"quest": s, "title": (rules_quests.get(s) or {})
                                  .get("title") or s} for s in finished],
            "quests_open": open_, "tally": tally, "notes": memo["notes"],
            "log": memo["log"][-40:]}


# ---------------------------------------------------------------- editor API

def api_step(db, body: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/sim/auto``: ``{zone, state, memo | (home, race, limit),
    last}`` -> ``{memo, step, summary}``. Read-only."""
    state = body.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("flags"), list):
        raise ValueError("not a simulator state")
    memo = body.get("memo")
    if memo is None:
        memo = start_memo(db, str(body.get("home") or ""), str(body.get("race") or ""),
                          int(body.get("limit") or MAX_STEPS))
    if not isinstance(memo, dict) or not isinstance(memo.get("played"), list):
        raise ValueError("not a simulate-all run")
    zone_id = str(body.get("zone") or "")
    if zone_id not in zone_scope(str(memo.get("home") or "")) or zone_id != memo.get("zone"):
        raise ValueError(f"zone {zone_id} is not where this run is ({memo.get('zone')})")
    last = body.get("last") if isinstance(body.get("last"), dict) else None
    result = next_step(db, zone_id, state, memo, last)
    summary_ = result["memo"].pop("summary", None)
    return {"memo": result["memo"], "step": result["step"], "summary": summary_}


# ---------------------------------------------------------------- offline

def run(db, home: str, race: str, limit: int = MAX_STEPS, lose: int = 0,
        lose_scope: str = "any") -> tuple[dict[str, Any], dict[str, Any]]:
    """Play the whole run here (the editor's loop without the page):
    ``(final state, summary)``.  ``lose`` battles are lost first."""
    state, memo = new_run(db, home, race, limit, lose, lose_scope)
    zone_id, last = home, None
    while True:
        result = next_step(db, zone_id, state, memo, last)
        memo, step = result["memo"], result["step"]
        if "done" in step:
            return state, memo["summary"]
        if "enter" in step:
            zone_id, state, last = step["enter"], step["state"], None
            continue
        rules = _rules(db, zone_id)
        state, log = engine.apply(rules, state, step["apply"])
        last = {"action": step["apply"], "log": log}

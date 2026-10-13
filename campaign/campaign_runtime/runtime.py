"""Run a rules-driven campaign zone: the server side of the editor's simulator.

``campaign.py``'s protocol handlers hand a request here when the campaign row
shows a zone whose rules file says ``"runtime": "rules"``. Each client event
becomes an engine action (``campaign_rules.apply``) on the champion's
progress; the resulting zone state is projected back onto the row's
``GameplayState`` (``projection.project``).

Per zone, beside the engine's node progress, ``_runtime`` keeps what only the
live protocol needs: the location the client started (``active``), a
cancelled battle (``parked``), the battle being fought (``battle``), the
conversation's latest script event (``choice``), pending battle events and
the node whose count picker waits for its answer (``count_picker``).

Thread-safety: every request for a champion runs under that champion's lock
(``progress.champion_lock``) and commits once.
"""

from __future__ import annotations

import json
import random
import re
from typing import Callable

import campaign_rules as engine
import pve_db
from campaign_runtime import (catalog_data, locate, progress,
                              projection, quests, rules_loader)

RUNTIME = "_runtime"
# Booleans the row's public data owns and the rules read as flags: Milosh's
# reading (consumed by the next battle) and Tranquil Dream's answer score.
DYNAMIC_FLAG = re.compile(r"gaal_fortune|score_\d+")
# Protocol events; any other SendEvent name is a conversation's script event.
PROTOCOL_EVENTS = frozenset({
    "accept", "boat_fail_shown", "cancel", "conv_done", "count_choice",
    "display_done", "empty_done", "enc_cancel", "force_move", "pass_node",
    "path_reveal_complete", "return_parent", "shroom_choice", "start",
    "visit_node", "visit_path", "warp_move"})


def _campaign():
    import campaign
    return campaign


# ---------------------------------------------------------------- dispatch

def owns(db, camp_id) -> str | None:
    """The rules-driven zone a campaign row shows, or None (legacy code)."""
    row = pve_db.db_campaign_protocol_row(camp_id, conn=db)
    if not row:
        return None
    _champ_id, state_json, ctype, _template_name = row
    if str(ctype or "").upper() == "QUEST":
        return None
    try:
        state = json.loads(state_json or "{}")
    except (TypeError, ValueError):
        state = {}
    zone = state.get("RulesZone")
    return zone if zone and rules_loader.runtime_enabled(zone) else None


# ---------------------------------------------------------------- context

class Zone:
    """One request's view of a champion's progress in one zone."""

    def __init__(self, db, camp_id, zone_id):
        row = pve_db.db_campaign_protocol_row(camp_id, conn=db)
        self.db, self.camp_id, self.zone_id = db, camp_id, zone_id
        self.champ_id, state_json, self.ctype, self.template = row
        self.state = json.loads(state_json or "{}")
        champ = _campaign()._get_champion(db, self.champ_id)
        self.race_id = champ[2] if champ else None
        self.user_id = champ[1] if champ else None
        loaded = progress.load(db, self.champ_id)
        if loaded is None:
            loaded = progress.fresh(self.race_id)
        self.campaign, self.zones = loaded
        # A zone part with no node progress is the same as no part: the rules
        # start that zone at its own start node. Empty parts stored by an
        # earlier legacy adoption would otherwise hide every node of the zone.
        self.zones = {zid: part for zid, part in self.zones.items()
                      if part and (part.get("nodes") or part.get("current"))}
        self.quests_before = json.loads(json.dumps(self.campaign.get("quests") or {}))
        self._layout_key, self._layout = None, None
        self._enter(zone_id)
        self.applied = _campaign()._empty_applied_updates()
        self.after: list[Callable[[dict], None]] = []
        self.teleported = None

    def _enter(self, zone_id):
        self.zone_id = zone_id
        self.entry = catalog_data.zone(zone_id)
        self.rules = rules_loader.rules(self.db, zone_id)[0]
        part = self.zones.get(zone_id)
        if _has_lives(self.rules) and int(
                ((self.campaign.get("counters") or {}).get("lives", 0)) or 0) <= 0:
            # The last life ended the previous run: entering again starts a new
            # run from the dungeon's start node with its starting lives.
            part = None
            self.zones.pop(zone_id, None)
            self.campaign.setdefault("counters", {})["lives"] = int(
                ((self.rules.get("start") or {}).get("counters") or {}).get("lives", 3))
            self.campaign["counters"]["run_streak"] = 0
        self.eng = engine.zone_view(self.rules, self.campaign, part)
        self.rt = dict((part or {}).get(RUNTIME) or {})
        # A panorama's talks count at its parent map node too: start from
        # what was already said there on the map.
        parent, parent_zone = self.rules.get("parent_node"), self.entry.get("parent")
        if parent and parent_zone in self.zones:
            seen = int(((self.zones[parent_zone].get("nodes") or {}).get(parent) or {})
                       .get("talks", 0) or 0)
            at_parent = self.eng.setdefault("nodes", {}).setdefault(parent, {})
            at_parent["talks"] = max(int(at_parent.get("talks", 0) or 0), seen)
        # Only the dungeons / panoramas this server runs from rules can be entered.
        self.children = {n: z for n, z in locate.child_zones(zone_id).items()
                         if rules_loader.runtime_enabled(z["id"])}
        # Milosh's reading lives in the row's public data (the battle setup
        # consumes it there); the engine flag follows it.
        pdata = (self.state.get("PublicState") or {}).get("Data") or {}
        flags = {f for f in (self.eng.get("flags") or ()) if not DYNAMIC_FLAG.fullmatch(f)}
        flags |= {k for k, v in pdata.items() if v is True and DYNAMIC_FLAG.fullmatch(str(k))}
        self.eng["flags"] = sorted(flags)

    def entrance_visit(self):
        """The conversation the dungeon's start node plays on its first visit."""
        return catalog_data.entrance_visit_conversation(
            self.entry, (self.rules.get("start") or {}).get("node"))

    def layout(self):
        """``engine.layout`` of the current state, cached while it is unchanged."""
        key = self.zone_id + json.dumps(self.eng, sort_keys=True, default=str)
        if key != self._layout_key:
            self._layout_key, self._layout = key, engine.layout(self.rules, self.eng)
        return self._layout

    def release_pending_teleport(self):
        """A battle's teleport with no result conversation to wait for."""
        pending = self.rt.pop("pending_teleport", None)
        if pending:
            self.eng["teleport"] = pending["teleport"]

    def apply(self, action):
        before = self.eng.get("current")
        self.eng, _log = engine.apply(self.rules, self.eng, action)
        if (action.get("type") in ("conversation_end", "encounter_result") and
                before and self.eng.get("current") != before):
            # The rules walked the player back (an impassable node held them):
            # the client's token must walk there too.
            self.rt["force_move"] = self.eng["current"]

    def store(self):
        """Fold the zone state back into the champion's progress."""
        self.campaign, zone = engine.split_progress(self.eng)
        zone[RUNTIME] = self.rt
        self.zones[self.zone_id] = zone
        progress.save(self.db, self.champ_id, self.campaign, {self.zone_id: zone})

    # -- what the client sees ------------------------------------------

    def content(self, node):
        layout_node = self.layout()["nodes"].get(node) or {}
        entry = next((e for e in (self.rules.get("nodes", {}).get(node) or {})
                      .get("entries") or () if e.get("id") == layout_node.get("entry")), None)
        return entry, projection.location_content(
            self.rules, self.eng, node, entry, self.children)

    def battle_node(self):
        started = self.eng.get("encounter") or {}
        return started.get("node") or self.rt.get("active")

    def refresh_choices(self):
        """Offer the card-selector options of the battle about to start."""
        for node_progress in (self.eng.get("nodes") or {}).values():
            node_progress.pop("choices", None)
        node = self.battle_node()
        if not node:
            return
        _entry, (ltype, _conv, scene, _cancel) = self.content(node)
        if ltype != "Encounter" or not scene:
            return
        offered = engine.mod_choices_for(self.rules, self.eng, scene, node)
        if offered:
            self.eng.setdefault("nodes", {}).setdefault(node, {})["choices"] = offered

    def project(self):
        self.refresh_choices()
        champions = {}
        for loc_scene in self._scenes():
            row = pve_db.db_encounter_scene_runtime(loc_scene, conn=self.db)
            if row and row[5] and str(row[5]).strip("0-"):
                champions[str(loc_scene).lower()] = str(row[5]).lower()
        run_losses = int(self.state.get("Losses", 0) or 0)
        self.state = projection.project(
            self.rules, self.eng, self.entry, self.state, children=self.children,
            active=self.rt.get("active"), parked=bool(self.rt.get("parked")),
            spent=self.rt.get("spent") or {}, scene_champions=champions,
            entrance_visit=self.entrance_visit(),
            gaal_nodes=frozenset(self.rules.get("gaal_nodes") or ()),
            gaal_display=self.rt.get("gaal_display"),
            force_move=self.rt.get("force_move"),
            count_picker=self.rt.get("count_picker"))
        self.state["CampID"] = self.camp_id
        self.state["ChampID"] = self.champ_id
        if _has_lives(self.rules):
            self.state["DungeonLifeAdj"] = _life_adjustment(self, run_losses)
        return self.state

    def _scenes(self):
        found = []
        for node in projection.zone_nodes(self.rules, self.entry, self.children):
            _entry, (_t, _c, scene, _a) = self.content(node)
            if scene:
                found.append(scene)
        return found

    def commit(self, handler):
        """Store progress, sync the journal, save the projected row state."""
        self.store()
        synced = quests.sync(handler, self.db, self.camp_id, self.champ_id,
                             self.state, self.quests_before,
                             self.campaign.get("quests") or {})
        self.quests_before = json.loads(json.dumps(self.campaign.get("quests") or {}))
        _campaign()._merge_applied(self.applied, synced["applied"])
        for qid, script, qstate in synced["spawned"]:
            self.after.append(lambda net, q=qid, s=script, st=qstate: _campaign().push_campspawn(
                net["handler"], q, self.champ_id, s, st, self.camp_id,
                self.template, *net["args"]))
        for qstate in synced["progressed"]:
            self.after.append(lambda net, st=qstate: _campaign().push_campupdate(
                net["handler"], self.db, st.get("CampID") or 0, self.champ_id,
                "quest_progress", "QUEST", False, st, *net["args"]))
        state = self.project()
        pve_db.db_campaign_update_state(self.camp_id, json.dumps(state), self.db)
        self.db.commit()
        return state


# ---------------------------------------------------------------- events

def _resolve_node(zone: Zone, name: str) -> str:
    for loc in zone.state.get("VisLocs") or ():
        data = (loc or {}).get("Data") or {}
        if name in (data.get("node"), data.get("name"), data.get("title"),
                    data.get("rules_node")):
            return str(data.get("rules_node") or data.get("node") or name)
    return str(name or "")


def start_location(zone: Zone, location_name: str):
    """StartLoc: the player selected / arrived at a location."""
    node = _resolve_node(zone, location_name)
    if not node:
        return
    if zone.eng.get("current") != node:
        zone.apply({"type": "visit", "node": node})
    zone.rt["active"] = node
    zone.rt.pop("parked", None)
    if zone.rt.get("count_picker") not in (None, node):
        zone.rt.pop("count_picker")          # the player went elsewhere
    if node in zone.children:
        enter_child(zone, node)


def enter_child(zone: Zone, node: str):
    """A dungeon / panorama entrance: enter the child zone; return_parent
    brings the player back to this node."""
    child = zone.children[node]["id"]
    zone.rt.pop("active", None)
    if zone.rt.get("inside") == child:
        return        # already there: a later event of the old campaign
    zone.rt["inside"] = child
    zone.eng["teleport"] = {"zone": child, "node": ""}
    zone.eng["return_to"] = {"zone": zone.zone_id, "node": node}


# -- token travel (the overworld map) ------------------------------------

def _is_fork(node: str) -> bool:
    return str(node).startswith("Fork")


def _route(zone: Zone, start: str, path_ids: list[str]) -> list[str]:
    """The nodes a ``visit_path`` route walks through, ``start`` first."""
    paths = {p["id"]: (p["from"], p["to"])
             for p in (zone.rules.get("map") or {}).get("paths") or ()}
    nodes, current = ([start] if start else []), start
    for pid in path_ids:
        ends = paths.get(pid)
        if not ends:
            continue
        current = ends[1] if current == ends[0] else ends[0] if current == ends[1] else ends[1]
        nodes.append(current)
    return nodes


def _route_stop(zone: Zone, route: list[str]) -> str:
    """Where the client's token stops (UIDungeonCharacterToken): the first
    unfinished node that is impassable or auto-starts, else the route's end."""
    locs = {(loc.get("Data") or {}).get("rules_node"): (loc.get("Data") or {})
            for loc in zone.state.get("VisLocs") or ()}
    for node in route[1:]:
        data = locs.get(node) or {}
        if (not _is_fork(node) and not data.get("completed") and
                (data.get("impassable") or data.get("autostart"))):
            return node
    return route[-1] if len(route) > 1 else ""


def _reachable(zone: Zone, node: str) -> bool:
    """A direct client event may not reveal a hidden or blocked node."""
    info = zone.layout()["nodes"].get(node)
    return bool(info and (info.get("visible") or info.get("visited"))
                and not info.get("blocked"))


def arrive(zone: Zone, node: str, route: list[str] | None = None):
    """The token has reached ``node``: record the visit and where it came from."""
    if not node or _is_fork(node) or not _reachable(zone, node):
        return
    came = None
    if route and node in route[1:]:
        prior = [n for n in route[:route.index(node, 1)] if not _is_fork(n)]
        came = prior[-1] if prior else None
    if zone.eng.get("current") != node:
        zone.apply({"type": "visit", "node": node})
        if came:
            zone.eng["came_from"] = came
    child = zone.children.get(node)
    if child and child["kind"] == "panorama":
        # A completed portal has no StartLoc: the client shows its Travel
        # button, so the trip happens on arrival.
        enter_child(zone, node)


def visit_path(zone: Zone, oparms):
    values = oparms[0] if oparms and isinstance(oparms[0], list) else oparms
    ids = [str(v) for v in (values or ()) if v]
    seen = zone.eng.setdefault("visited_paths", [])
    for pid in ids:
        if pid not in seen:
            seen.append(pid)
    route = _route(zone, str(zone.eng.get("current") or ""), ids)
    stop = _route_stop(zone, route)
    if stop and stop != zone.eng.get("current"):
        arrive(zone, stop, route)


def force_move(zone: Zone, oparms):
    """The client walked its token where the server ordered (a walk-back)."""
    wanted = zone.rt.pop("force_move", None)
    dest = _resolve_node(zone, str(oparms[1])) if oparms and len(oparms) > 1 else wanted
    if not dest:
        return
    origin = _resolve_node(zone, str(oparms[0])) if oparms else ""
    zone.apply({"type": "set_current", "node": dest, "visited": True})
    if origin:
        zone.eng["came_from"] = origin
    zone.eng.pop("pass", None)
    zone.rt.pop("active", None)


# -- Shroomkin Haus and Gaal camps ---------------------------------------

def shroom_choice(handler, zone: Zone, oparms):
    node = zone.rt.get("active") or str(zone.eng.get("current") or "")
    entry, _content = zone.content(node) if node else (None, None)
    card = str((oparms or [""])[0] or "").strip()
    info = zone.layout()["nodes"].get(node) or {}
    if (not entry or entry.get("kind") != "shroomhaus" or info.get("completed") or
            card.lower() not in {str(c).lower() for c in entry.get("choices") or ()}):
        getattr(handler, "_log_req", print)(
            f"    Shroom Haus choice rejected: card={card} node={node}")
        return
    granted = _campaign()._grant_card_reward(handler, zone.db, zone.user_id, card)
    if granted:
        zone.apply({"type": "shroom_choice", "node": node, "card": card})
        zone.rt.pop("active", None)


def gaal_accept(zone: Zone):
    """Milosh's paid reading: draw a Fortune now, show it when he is done."""
    pdata = zone.state.setdefault("PublicState", {}).setdefault("Data", {})
    pdata["gaal_fortune"] = True
    fortunes = _campaign()._fortune_card_guids(zone.db)
    if fortunes:
        pdata["gaal_fortune_guid"] = random.choice(fortunes)
        pdata["gaal_fortune_display_pending"] = True
    zone.eng["flags"] = sorted(set(zone.eng.get("flags") or ()) | {"gaal_fortune"})


def gaal_display_done(zone: Zone):
    pdata = zone.state.setdefault("PublicState", {}).setdefault("Data", {})
    pdata.pop("gaal_fortune_display_pending", None)
    zone.rt.pop("gaal_display", None)
    zone.rt.pop("active", None)


def conversation_done(handler, zone: Zone):
    """conv_done: the active location's conversation closed."""
    node = zone.rt.get("active") or zone.state.get("ALoc") or zone.eng.get("current")
    node = _resolve_node(zone, node) if node else ""
    # A dungeon entrance's visit conversation is no offer, so it is not what
    # the node's content names; it still ends as the conversation it is.
    visit = None
    if node and node == (zone.rules.get("start") or {}).get("node") and not (
            ((zone.eng.get("nodes") or {}).get(node) or {}).get("visits_done")):
        visit = zone.entrance_visit()
    if visit:
        zone.eng.setdefault("nodes", {}).setdefault(node, {})["visits_done"] = True
    entry, (ltype, conversation, _scene, _cancel) = zone.content(node) if node else (None, ("", None, None, False))
    if visit:
        ltype, conversation = "Convo", visit
    ending = zone.rt.pop("choice", "")
    pre_battle = False
    result_of = None
    if ltype == "Convo" and conversation:
        # An auto-started conversation plays once per trigger.
        zone.rt.setdefault("spent", {})[node] = conversation
        node_progress = zone.eng.setdefault("nodes", {}).setdefault(node, {})
        if node_progress.get("result") == conversation:
            node_progress.pop("result")
            result_of = node_progress.pop("result_of", None)
        elif (entry and entry.get("kind") in ("encounter", "shroomhaus") and
              entry.get("pre") == conversation):
            node_progress["pre_done"] = True
            pre_battle = True
        if re.fullmatch(r"Diff_\d+", str(ending or "")):
            # A difficulty picked in a repeat-encounter conversation: the
            # next won battle reports it as a battle event.
            zone.rt["battle_events"] = [ending]
        zone.apply({"type": "conversation_end", "node": node,
                    "conversation": conversation,
                    "name": pve_db.db_conversation_name(conversation, zone.db) or "",
                    "ending": ending, "events": [ending] if ending else [],
                    **({"result_of": result_of} if result_of else {})})
        _campaign()._merge_applied(zone.applied, _campaign()._apply_conversation_rewards(
            handler, zone.db, zone.camp_id, zone.state, conversation))
    pdata = (zone.state.get("PublicState") or {}).get("Data") or {}
    if (node in (zone.rules.get("gaal_nodes") or ()) and
            pdata.get("gaal_fortune_display_pending") and pdata.get("gaal_fortune_guid")):
        # The conversation window has closed: show the drawn Fortune card.
        zone.rt["gaal_display"] = node
        pre_battle = True
    started = zone.eng.get("encounter") or {}
    if (ltype == "Convo" and conversation and not started and
            not zone.eng.get("teleport") and
            engine.count_picker(zone.rules, zone.eng, node)):
        # After any conversation at a count-picker node (Gnomes Come Home's
        # lost gnomes): the picker shows next, on the same location.
        zone.rt["count_picker"] = node
        pre_battle = True
    if started.get("node") != node and not pre_battle:
        # (After a pre-battle conversation the player stays on the node:
        # its battle panel opens next, as with a battle the conversation
        # started.)
        zone.rt.pop("active", None)


def count_choice(handler, zone: Zone, oparms):
    """count_choice: the count picker's answer moves that many from its
    ``from`` counter into its ``to`` counter (``engine.count_picker``)."""
    node = zone.rt.get("count_picker") or ""
    raw = (oparms or [None])[0]
    try:
        count = int(raw) if not isinstance(raw, bool) and float(raw) == int(raw) else None
    except (TypeError, ValueError, OverflowError):
        count = None
    try:
        if not node or count is None:
            raise engine.RulesError("no count picker is waiting" if not node
                                    else f"count must be a whole number: {raw!r}")
        zone.apply({"type": "count_choice", "node": node, "count": count})
    except engine.RulesError as exc:
        getattr(handler, "_log_req", print)(
            f"    Count picker choice rejected: node={node} count={raw!r}: {exc}")
        if node and engine.count_picker(zone.rules, zone.eng, node):
            return          # still waiting: the picker shows again
    zone.rt.pop("count_picker", None)
    if zone.rt.get("active") == node:
        zone.rt.pop("active", None)


def battle_start(zone: Zone, oparms) -> tuple[str, dict | None, str | None] | None:
    """``start``: the battle to launch: ``(scene, encounter action, mod)``."""
    node = zone.battle_node()
    if not node:
        return None
    zone.refresh_choices()
    _entry, (ltype, _conv, scene, _cancel) = zone.content(node)
    if ltype != "Encounter" or not scene:
        return None
    offered = ((zone.eng.get("nodes") or {}).get(node) or {}).get("choices") or ()
    chosen = str((oparms or [""])[0] or "").strip().lower()
    mod = chosen if chosen in offered else None
    zone.rt["battle"] = {"node": node, "scene": scene, "mod": mod}
    started = zone.eng.get("encounter") or {}
    action = ({"scene": scene, "conversation_id": started["conversation_id"]}
              if started.get("node") == node and started.get("conversation_id")
              else None)
    return scene, action, mod


def _has_lives(rules) -> bool:
    """A dungeon runs with lives when its rules start with a ``lives`` counter."""
    counters = ((rules or {}).get("start") or {}).get("counters") or {}
    return "lives" in counters


def _dungeon_base_lives(zone: Zone) -> int:
    """The client's base lives: 3 plus each talent's ``m_DungeonLivesModifier``
    (``TalentManager.GetTotalDungeonLives``)."""
    from gamedata import DEFAULT_RECORD_STORE
    try:
        guids = json.loads(pve_db.db_campaign_talents(zone.camp_id, conn=zone.db) or "[]")
    except (TypeError, ValueError):
        guids = []
    base = 3
    for guid in guids:
        record = DEFAULT_RECORD_STORE.get("ChampionTalentData", str(guid or "").lower())
        if record is not None:
            base += int(record.field("m_DungeonLivesModifier", 0) or 0)
    return base


def _life_adjustment(zone: Zone, losses: int) -> int:
    """``GameplayState.DungeonLifeAdj`` so the client's own count reads right.

    The client shows ``base + DungeonLifeAdj - Losses`` lives, where ``Losses``
    is this run's losses. Solving for the adjustment gives the lives this
    dungeon's rules hold.
    """
    lives = int((zone.eng.get("counters") or {}).get("lives", 0) or 0)
    return lives + losses - _dungeon_base_lives(zone)


def _count_dungeon_run(zone: Zone, won: bool):
    """Lives and the run streak of a dungeon with lives: a failed battle costs a
    life, and the last life sends the champion back to the overworld node the
    dungeon leads from. A win extends the streak; a loss breaks it."""
    if not _has_lives(zone.rules):
        return
    counters = zone.eng.setdefault("counters", {})
    if won:
        counters["run_streak"] = int(counters.get("run_streak", 0) or 0) + 1
        return
    counters["run_streak"] = 0
    counters["lives"] = int(counters.get("lives", 0) or 0) - 1
    if counters["lives"] <= 0:
        # The run is lost: the client shows the lost panel, and the champion
        # leaves the dungeon when the player confirms (Forfeit).
        zone.eng["run_lost"] = True


def battle_end(zone: Zone, session, won: bool):
    """The battle finished: apply its result to the engine."""
    battle = zone.rt.pop("battle", None) or {}
    node = battle.get("node") or zone.battle_node()
    if not node:
        return
    entry, _content = zone.content(node)
    started = zone.eng.get("encounter") or {}
    scene = battle.get("scene") or started.get("scene") or (entry or {}).get("scene")
    events = list(_campaign()._battle_events(zone.db, session, zone.user_id)
                  if zone.user_id is not None else [])
    events += [str(e) for e in zone.rt.pop("battle_events", None) or ()]
    result = None
    if entry and started.get("node") != node:
        result = entry.get("victory" if won else "defeat")
    action = {"type": "encounter_result", "node": node, "scene": scene,
              "victory": bool(won), "events": events,
              "result_conversation": bool(result)}
    if battle.get("mod"):
        action["mod"] = battle["mod"]
    try:
        zone.apply(action)
    except engine.RulesError:
        action.pop("mod", None)        # the option is no longer on offer
        zone.apply(action)
    # A node-offered battle's authored result conversation plays next; its
    # ending actions run when it ends (conversation_done: result_of).
    if result:
        node_progress = zone.eng.setdefault("nodes", {}).setdefault(node, {})
        node_progress["result"] = result
        node_progress["result_of"] = entry.get("id")
    _count_dungeon_run(zone, won)
    # The client ignores a zone transition while it is still in the battle:
    # a teleport the result itself produced (a hook) waits for the client's
    # next request, back on the map.
    if zone.eng.get("teleport"):
        zone.rt["pending_teleport"] = {"teleport": zone.eng.pop("teleport")}
    zone.rt.pop("active", None)
    zone.rt.pop("parked", None)
    (zone.rt.get("spent") or {}).pop(node, None)   # a new result can auto-start


# ---------------------------------------------------------------- teleport

def handle_teleport(handler, zone: Zone):
    """Follow an engine ``teleport`` (an ending action or a zone entrance)."""
    target = zone.eng.pop("teleport", None)
    if not target:
        return
    dest, node = str(target.get("zone") or ""), str(target.get("node") or "")
    if dest.lower() == zone.zone_id.lower():
        if node:
            zone.apply({"type": "visit", "node": node})
        return
    zone.rt.pop("active", None)
    zone.store()
    zone.teleported = dest
    if rules_loader.runtime_enabled(dest):
        _enter_runtime_zone(handler, zone, dest, node)
    else:
        getattr(handler, "_log_req", print)(
            f"    Rules runtime: teleport to {dest} has no rules zone; ignored")


def _zone_row(zone: Zone, dest_entry) -> int:
    """The champion's campaign row for the destination's row type."""
    campaign = _campaign()
    row_type = locate.ROW_TYPES[dest_entry["kind"]]
    existing = pve_db.db_campaign_for_champion(zone.champ_id, row_type, zone.db)
    if existing:
        return existing[0]
    cid = campaign._new_camp_id(zone.db)
    champ = campaign._get_champion(zone.db, zone.champ_id)
    pve_db.db_create_campaign(
        cid, campaign._generate_inst_id(), zone.champ_id, champ[1] if champ else 0,
        champ[3] if champ else "", dest_entry.get("server_template") or "AZ1",
        row_type, "{}", zone.db, is_started=True)
    return cid


def _enter_runtime_zone(handler, zone: Zone, dest: str, node: str):
    entry = catalog_data.zone(dest)
    row_id = _zone_row(zone, entry)
    raw = pve_db.db_campaign_state(row_id, conn=zone.db)
    previous = json.loads(raw or "{}")
    if previous.get("RulesZone") != dest:
        previous = {k: v for k, v in previous.items()
                    if k in ("Started", "TutorialDone", "_encounter_reward_claims",
                             "_encounter_successes")}
    # The tutorial (always going first) ends with the training battle, in
    # every zone after it.
    if zone.state.get("TutorialDone"):
        previous["TutorialDone"] = True
    # A finished run starts clean next time (Fort Romor): the zone's node
    # progress and its per-run flags go, only the "finished" mark stays.
    replay = entry.get("replay") or {}
    if (replay.get("when_flag") in (zone.campaign.get("flags") or ()) and
            dest in zone.zones):
        keep = set(replay.get("keep_flags") or ())
        patterns = replay.get("clear_flags") or ()
        zone.zones.pop(dest)
        zone.campaign["flags"] = [
            f for f in zone.campaign.get("flags") or ()
            if f in keep or not any(engine.flag_matches(f, pat) for pat in patterns)]
        previous = {k: v for k, v in previous.items()
                    if k in ("Started", "TutorialDone")}
    # Each entry starts a dungeon with lives afresh: its nodes, lives and run
    # streak go back to the rules' start, so leaving and re-entering resets all.
    if _has_lives(rules_loader.rules(zone.db, dest)[0]):
        zone.zones.pop(dest, None)
        previous = {k: v for k, v in previous.items()
                    if k in ("Started", "TutorialDone")}
    row_type = locate.ROW_TYPES[entry["kind"]]
    template = (entry.get("server_template") if entry["kind"] != "panorama"
                else pve_db.db_campaign_template_name(row_id, conn=zone.db) or "AZ1")
    dest_zone = Zone.__new__(Zone)
    dest_zone.__dict__.update(zone.__dict__)
    dest_zone.camp_id, dest_zone.ctype, dest_zone.template = row_id, row_type, template
    dest_zone.state = dict(previous, Started=previous.get("Started") or _campaign()._now_utc())
    dest_zone._enter(dest)
    dest_zone.rt.pop("inside", None)       # back on this map
    if node and dest_zone.eng.get("current") != node:
        dest_zone.apply({"type": "visit", "node": node})
    dest_zone.store()
    zone.campaign = dest_zone.campaign
    for key in engine.CAMPAIGN_KEYS:
        if key in dest_zone.eng:
            zone.eng[key] = json.loads(json.dumps(dest_zone.eng[key]))
    state = dest_zone.project()
    pve_db.db_campaign_set_zone(row_id, row_type, template, json.dumps(state),
                                conn=zone.db)
    pve_db.db_campaign_set_last_campaign(zone.champ_id, row_id, zone.db)
    zone.after.append(lambda net: _campaign().push_campupdate(
        net["handler"], zone.db, row_id, zone.champ_id, "teleport", row_type,
        True, state, *net["args"]))


def activate_zone(db, champ_id: int, dest: str):
    """Make ``dest`` the zone the champion plays next (the editor's save to
    champion): point their campaign row and last campaign at it, from the
    progress already stored for it. The caller commits."""
    last = pve_db.db_champion_last_campaign_id(champ_id, conn=db)
    if not last:
        raise LookupError("this champion has no campaign yet")
    zone = Zone(db, last, dest)
    _enter_runtime_zone(None, zone, dest, "")


# ---------------------------------------------------------------- protocol

def _net(handler, comp, session_id, target, instance, conh, uid):
    return {"handler": handler, "args": (comp, session_id, target, instance, conh, uid)}


def _respond(handler, zone: Zone, state, comp, session_id, reqid, target,
             instance, conh, uid):
    campaign = _campaign()
    resp = campaign._build_input_response(zone.camp_id, state, success=True,
                                          applied=zone.applied)
    ret = campaign._send_response(handler, json.dumps(resp), comp, session_id,
                                  reqid, target, instance, conh, uid)
    net = _net(handler, comp, session_id, target, instance, conh, uid)
    for push in zone.after:
        push(net)
    return ret


def handle_query(handler, db, zone_id, env_json, comp, session_id, reqid,
                 target, instance, conh, uid, *, start=False):
    """getcampstate / startcamp: the row's projected state."""
    camp_id = env_json.get("CampID", 0)
    row = pve_db.db_campaign_protocol_row(camp_id, conn=db)
    with progress.champion_lock(row[0]):
        zone = Zone(db, camp_id, zone_id)
        if start:
            zone.state["Started"] = zone.state.get("Started") or _campaign()._now_utc()
            pve_db.db_campaign_update_state(camp_id, json.dumps(zone.state), db,
                                            started=True)
        # Back on the map after a battle: a waiting teleport goes now.
        zone.release_pending_teleport()
        handle_teleport(handler, zone)
        state = zone.commit(handler) if not zone.teleported else _commit_after_teleport(handler, zone)
        return _respond(handler, zone, state, comp, session_id, reqid, target,
                        instance, conh, uid)


def handle_forfeit(handler, db, zone_id, env_json, comp, session_id, reqid,
                   target, instance, conh, uid):
    """Forfeit: the champion gives up the dungeon run and leaves it for the
    overworld node the dungeon leads from."""
    camp_id = env_json.get("CampID", 0)
    row = pve_db.db_campaign_protocol_row(camp_id, conn=db)
    with progress.champion_lock(row[0]):
        zone = Zone(db, camp_id, zone_id)
        if _has_lives(zone.rules):
            # Forfeiting the dungeon on its map ends the run whatever lives
            # remain, as losing the last life does; entering again starts afresh.
            counters = zone.eng.setdefault("counters", {})
            counters["lives"] = 0
            counters["run_streak"] = 0
            zone.eng["run_lost"] = True
        if zone.eng.pop("run_lost", None):
            zone.eng["teleport"] = {"zone": zone.entry.get("parent") or "",
                                    "node": zone.entry.get("parent_node") or ""}
        handle_teleport(handler, zone)
        state = zone.commit(handler) if not zone.teleported else _commit_after_teleport(handler, zone)
        return _respond(handler, zone, state, comp, session_id, reqid, target,
                        instance, conh, uid)


def handle_locaction(handler, db, zone_id, env_json, comp, session_id, reqid,
                     target, instance, conh, uid):
    camp_id = env_json.get("CampID", 0)
    row = pve_db.db_campaign_protocol_row(camp_id, conn=db)
    with progress.champion_lock(row[0]):
        zone = Zone(db, camp_id, zone_id)
        zone.release_pending_teleport()
        if int(env_json.get("RAct", 0) or 0) == 0:
            start_location(zone, env_json.get("Loc", ""))
        else:
            zone.rt.pop("active", None)
        handle_teleport(handler, zone)
        state = zone.commit(handler) if not zone.teleported else _commit_after_teleport(handler, zone)
        return _respond(handler, zone, state, comp, session_id, reqid, target,
                        instance, conh, uid)


def _commit_after_teleport(handler, zone: Zone):
    """The row the request came from, after its champion left the zone."""
    state = zone.commit(handler)
    state["ALoc"] = None
    return state


def handle_sendevent(handler, db, zone_id, env_json, comp, session_id, reqid,
                     target, instance, conh, uid):
    camp_id = env_json.get("CampID", 0)
    event = str(env_json.get("Event", "") or "")
    oparms = env_json.get("OParms") or []
    log = getattr(handler, "_log_req", print)
    log(f"    Rules runtime [{zone_id}]: camp={camp_id} event={event} params={oparms}")
    row = pve_db.db_campaign_protocol_row(camp_id, conn=db)
    with progress.champion_lock(row[0]):
        zone = Zone(db, camp_id, zone_id)
        zone.release_pending_teleport()
        launch = None
        if event and event not in PROTOCOL_EVENTS:
            zone.rt["choice"] = event         # the conversation's ending so far
            if zone.rt.get("active") or zone.state.get("ALoc"):
                _campaign()._record_dungeon_script_score(
                    zone.state, zone.template, event)
            if event == "gaal_camp_accept":
                gaal_accept(zone)
        elif event == "conv_done":
            conversation_done(handler, zone)
        elif event == "empty_done":
            node = zone.rt.pop("active", None)
            if node and zone.entry.get("kind") == "overworld":
                # An empty map location is finished by exploring it.
                zone.apply({"type": "set_node", "node": node,
                            "values": {"completed": True}})
        elif event in ("visit_node", "warp_move"):
            arrive(zone, _resolve_node(zone, str((oparms or [""])[0] or "")))
        elif event == "visit_path":
            visit_path(zone, oparms)
        elif event == "force_move":
            force_move(zone, oparms)
        elif event == "shroom_choice":
            shroom_choice(handler, zone, oparms)
        elif event == "display_done":
            gaal_display_done(zone)
        elif event == "count_choice":
            count_choice(handler, zone, oparms)
        elif event == "enc_cancel":
            zone.rt.pop("active", None)
            zone.rt["parked"] = True
        elif event == "start":
            active = zone.rt.get("active")
            if active in zone.children:
                enter_child(zone, active)
            else:
                launch = battle_start(zone, oparms)
        elif event == "return_parent":
            back = zone.eng.get("return_to") or {}
            zone.eng["teleport"] = {"zone": back.get("zone") or zone.entry.get("parent") or "",
                                    "node": back.get("node") or zone.entry.get("parent_node") or ""}
        handle_teleport(handler, zone)
        state = zone.commit(handler) if not zone.teleported else _commit_after_teleport(handler, zone)
        if launch:
            # As the legacy flow: launch (gamestarted) first, then answer
            # with the state the launch recorded (ActiveEncounterGuid ...).
            scene, action, mod = launch
            campaign = _campaign()
            deck_db_id = pve_db.db_champion_last_deck_id(zone.champ_id, conn=db)
            deck_uid64 = (deck_db_id << 8) | 17 if deck_db_id else 0
            campaign._launch_encounter(handler, db, camp_id, zone.champ_id,
                                       action or scene, deck_uid64, comp, session_id,
                                       target, instance, conh, uid, mod_choice=mod)
            state = json.loads(pve_db.db_campaign_state(camp_id, conn=db) or "{}")
        return _respond(handler, zone, state, comp, session_id, reqid, target,
                        instance, conh, uid)


def handle_battle_end(handler, db, zone_id, session, camp_id, won):
    """The server-side battle result (rewards follow in campaign.py)."""
    row = pve_db.db_campaign_protocol_row(camp_id, conn=db)
    with progress.champion_lock(row[0]):
        zone = Zone(db, camp_id, zone_id)
        battle_end(zone, session, won)
        handle_teleport(handler, zone)
        zone.commit(handler)
        return zone


def handle_gameend(handler, db, zone_id, env_json, comp, session_id, reqid,
                   target, instance, conh, uid):
    """The client's campaign ``gameend``: bookkeeping, then the new state."""
    camp_id = env_json.get("CampID", 0)
    won = bool(env_json.get("Winners"))
    row = pve_db.db_campaign_protocol_row(camp_id, conn=db)
    with progress.champion_lock(row[0]):
        zone = Zone(db, camp_id, zone_id)
        state = zone.state
        if won:
            state["Wins"] = int(state.get("Wins", 0) or 0) + 1
            state["TutorialDone"] = True
            if zone.entry["kind"] == "dungeon":
                state["dungeon_win_count"] = int(state.get("dungeon_win_count", 0) or 0) + 1
        else:
            state["Losses"] = int(state.get("Losses", 0) or 0) + 1
            if zone.entry["kind"] == "dungeon":
                state["dungeon_win_count"] = 0
        state.pop("ActiveEncounterGuid", None)
        state.pop("ActiveEncounterConversationGuid", None)
        state = zone.commit(handler)
        if state.get("ALoc") is None and zone.entry["kind"] == "panorama":
            # An empty (not null) ALoc after a battle keeps the panorama's
            # portraits resolving while nothing auto-starts.
            state["ALoc"] = ""
            pve_db.db_campaign_update_state(camp_id, json.dumps(state), db)
            db.commit()
        return _respond(handler, zone, state, comp, session_id, reqid, target,
                        instance, conh, uid)

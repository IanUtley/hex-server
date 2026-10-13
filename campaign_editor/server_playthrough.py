#!/usr/bin/env python3
"""Play a race's starter run through the *server's* campaign runtime.

Unlike ``playthrough.py`` (the engine alone), this drives the real protocol
handlers in ``campaign.py`` - getcampstate / StartLoc / SendEvent / battle end
/ gameend - the way the client does, on a throwaway copy of the database, and
prints every projected state change. The zones named with ``--zones`` are run
by ``campaign_runtime`` whether or not their rules file switches them on.

    python3 campaign_editor/server_playthrough.py Human \\
        --zones az0-human,crayburn
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from paths import CAMPAIGN_ROOT, HERE, REPO  # noqa: E402  (also sets sys.path)

MAX_STEPS = 80
RACE_IDS = {"Human": 1, "Elf": 2, "Coyotle": 3, "Orc": 4, "Dwarf": 5,
            "Shin'hare": 6, "Vennen": 7, "Necrotic": 8}


class FakeHandler:
    """Collects what the server would send to the client."""
    client_reck_id = "7"
    client_uid = 1
    sid = 1
    scnt = 0

    def __init__(self, user_id):
        self.user_profile = {"id": user_id, "name": "Runtime Test"}
        self.sent: list[dict] = []

    def _log_req(self, text):
        if self.verbose:
            print("   ", text)

    verbose = False


def _setup(race: str, champion: int | None = None):
    tmp = Path(tempfile.mkdtemp(prefix="hex_runtime_"))
    path = tmp / "hconnect.db"
    # HEX_SOURCE_DB: test a copy (e.g. one the migration has already run on).
    src = sqlite3.connect(os.environ.get("HEX_SOURCE_DB") or REPO / "hconnect.db")
    dst = sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    import db as shared
    conn = sqlite3.connect(path, check_same_thread=False)
    shared._db = conn
    import static
    static.ensure_schema(conn) if hasattr(static, "ensure_schema") else None
    if champion:
        user_id = conn.execute("SELECT user_id FROM champions WHERE id=?",
                               (champion,)).fetchone()[0]
        return tmp, conn, champion, user_id
    user_id = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO champions (user_id, champion_name, race, champion_class) "
        "VALUES (?, 'Runtime Test', ?, 3)", (user_id, RACE_IDS[race]))
    conn.commit()
    return tmp, conn, cur.lastrowid, user_id


def run(race: str, zones: set[str], verbose: bool = False,
        champion: int | None = None, lose_first: bool = False) -> tuple[bool, list[str]]:
    """Play from a new champion, or (``champion``) continue an existing
    champion's saved campaign on the copy - converting a legacy save."""
    tmp, db, champ_id, user_id = _setup(race, champion)
    import campaign
    from campaign_runtime import rules_loader
    real_enabled = rules_loader.runtime_enabled
    rules_loader.runtime_enabled = lambda zone: zone in zones or real_enabled(zone)
    handler = FakeHandler(user_id)
    handler.verbose = verbose
    responses, pushes = [], []

    def send_response(h, body, *args):
        responses.append(json.loads(body))
        return "sent"

    def push_notify(h, env, *args):
        pushes.append(env)
        return "pushed"

    campaign._send_response = send_response
    campaign._push_campaign_notify = push_notify
    route: list[str] = []
    try:
        if champion:
            camp_id = db.execute("SELECT last_campaign_id FROM champions WHERE id=?",
                                 (champ_id,)).fetchone()[0]
        else:
            _deleted, camp_id = campaign.restart_champion_campaigns(db, champ_id)
        net = (0, "s", "t", "i", 0, 1)

        def call(fn, env):
            responses.clear()
            fn(handler, db, dict(env, CampID=camp_id), 0, "sess", 2, "t", "i", 0, 1)
            return (responses[-1] if responses else {}).get("CurState") or {}

        state = call(campaign._handle_getcampstate, {})
        reqs = 0
        for _ in range(MAX_STEPS):
            reqs += 1
            for env in [p for p in pushes if p.get("RequestType") == "cmpupdate"
                        and p.get("Transition")]:
                camp_id = env["CampID"]
                route.append(f"transition -> camp {camp_id} "
                             f"{env.get('TemplateType')} {env['State'].get('RulesZone', '')}")
            pushes[:] = [p for p in pushes if not (p.get("RequestType") == "cmpupdate"
                                                   and p.get("Transition"))]
            row = db.execute("SELECT campaign_type, template_name, state_json FROM campaigns "
                             "WHERE id=?", (camp_id,)).fetchone()
            state = json.loads(row[2] or "{}")
            zone = state.get("RulesZone") or f"legacy:{row[0]}:{row[1]}"
            if row[0] == "AREA":
                route.append(f"reached {row[1]} ({zone})")
                for script, raw in db.execute(
                        "SELECT template_name, state_json FROM campaigns WHERE "
                        "champion_id=? AND campaign_type='QUEST'", (champ_id,)):
                    quest = json.loads(raw or "{}")
                    route.append(f"journal {script}: objective "
                                 f"{(quest.get('Flags') or {}).get('_quest_objective_idx')}"
                                 f"{' finished' if quest.get('Finished') else ''}")
                return True, route
            locs = {(l.get("Data") or {}).get("node"): l["Data"]
                    for l in state.get("VisLocs") or ()}
            aloc = state.get("ALoc")
            if aloc and aloc in locs and not locs[aloc].get("completed", False) \
                    or (aloc in locs and locs[aloc].get("type") in ("Encounter", "ModChoiceEncounter")):
                loc = locs[aloc]
                if loc["type"] == "Convo":
                    ending = _ending_for(db, loc["conversationId"])
                    if ending:
                        call(campaign._handle_sendevent, {"Event": ending, "OParms": []})
                    state = call(campaign._handle_sendevent, {"Event": "conv_done", "OParms": []})
                    route.append(f"{zone}:{aloc} talk {loc['conversationId'][:8]}"
                                 + (f" [{ending}]" if ending else ""))
                    continue
                if loc["type"] in ("Encounter", "ModChoiceEncounter"):
                    pick = (state["PublicState"]["Data"].get("mod_choice_list") or [""])[0]
                    call(campaign._handle_sendevent, {"Event": "start", "OParms": [pick, "merc=0"]})
                    launched = [p for p in pushes if p.get("RequestType") == "gamestarted"]
                    pushes.clear()
                    if not launched:
                        route.append(f"STUCK: {zone}:{aloc} battle did not launch")
                        return False, route

                    class Session:
                        session_name = f"camp_{camp_id}"
                        session_id = 0
                    won = not lose_first
                    lose_first = False
                    campaign.handle_battle_gameend(handler, db, Session(), won, 1)
                    state = call(campaign._handle_gameend,
                                 {"Winners": [1] if won else [], "Losers": [] if won else [1]})
                    route.append(f"{zone}:{aloc} battle {loc['encounter'][:8]} "
                                 f"{'won' if won else 'LOST'}"
                                 + (f" with {pick[:8]}" if pick else ""))
                    continue
            # Explore: the next open location the player would select.
            target = next((n for n, d in locs.items()
                           if d.get("visible") and d.get("enabled", True)
                           and d.get("type") != "Empty"
                           and (not d.get("completed") or d.get("repeatable"))
                           and n != aloc), None)
            if not target:
                route.append(f"STUCK in {zone}: nothing to select (ALoc={aloc!r})")
                return False, route
            call(campaign._handle_locaction, {"RAct": 0, "Loc": target})
            route.append(f"{zone}: select {target}")
        route.append("gave up: too many steps")
        return False, route
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _bfs_paths(rules, start, goal, hidden=frozenset()):
    """Path ids from ``start`` to ``goal`` over the zone's map (forks allowed)."""
    adj = {}
    for p in (rules.get("map") or {}).get("paths") or ():
        adj.setdefault(p["from"], []).append((p["to"], p["id"]))
        adj.setdefault(p["to"], []).append((p["from"], p["id"]))
    seen, queue = {start}, [(start, [])]
    while queue:
        node, ids = queue.pop(0)
        if node == goal:
            return ids
        for nxt, pid in adj.get(node, ()):
            if nxt not in seen and nxt not in hidden:
                seen.add(nxt)
                queue.append((nxt, ids + [pid]))
    return None


def _prepare(db, user_id, zones, verbose):
    """Fake the network and switch ``zones`` on; returns the pieces the loop needs."""
    import campaign
    from campaign_runtime import rules_loader
    real_enabled = rules_loader.runtime_enabled
    rules_loader.runtime_enabled = lambda zone: zone in zones or real_enabled(zone)
    handler = FakeHandler(user_id)
    handler.verbose = verbose
    responses, pushes = [], []
    campaign._send_response = lambda h, body, *a: (responses.append(json.loads(body)), "sent")[1]
    campaign._push_campaign_notify = lambda h, env, *a: (pushes.append(env), "pushed")[1]
    # No real battles here: every win captured a troop (the taming scenes' event).
    campaign._battle_events = lambda *a, **k: ["tamed_troop"]
    return campaign, rules_loader, real_enabled, handler, responses, pushes


def explore(champion, zones: set[str], steps: int = 60, verbose: bool = False,
            race: str = "Human", enter: str | None = None):
    """Explore an existing champion's save (``champion``), or a new
    ``race`` champion played through AZ0 and Crayburn first. Returns
    ``(ok, route)``."""
    tmp, db, champ_id, user_id = _setup(race, champion)
    campaign, rules_loader, real_enabled, handler, responses, pushes = _prepare(
        db, user_id, zones, verbose)
    route = []
    try:
        if champion:
            camp_id = db.execute("SELECT last_campaign_id FROM champions WHERE id=?",
                                 (champ_id,)).fetchone()[0]
        else:
            _deleted, camp_id = campaign.restart_champion_campaigns(db, champ_id)
            ok, camp_id = _starter_run(db, campaign, handler, responses, pushes, champ_id, camp_id, route)
            if not ok:
                return False, route
        if enter:
            # Test hook: put the champion straight into a zone.
            from campaign_runtime import progress, runtime
            with progress.champion_lock(champ_id):
                zone = runtime.Zone(db, camp_id, runtime.owns(db, camp_id))
                runtime._enter_runtime_zone(handler, zone, enter, "")
                db.commit()
            camp_id = db.execute("SELECT last_campaign_id FROM champions WHERE id=?",
                                 (champ_id,)).fetchone()[0]
            route.append(f"entered {enter} (camp {camp_id})")
        return _explore_core(db, campaign, rules_loader, handler, responses, pushes,
                             champ_id, camp_id, steps, route)
    finally:
        rules_loader.runtime_enabled = real_enabled
        db.close()
        if os.environ.get("KEEP_DB"):
            print("kept test database:", tmp / "hconnect.db")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def _starter_run(db, campaign, handler, responses, pushes, champ_id, camp_id, route):
    """Play AZ0 and Crayburn until the champion is on the AZ1 map."""
    for _ in range(80):
        for env in [p for p in pushes if p.get("RequestType") == "cmpupdate" and p.get("Transition")]:
            camp_id = env["CampID"]
        pushes.clear()
        responses.clear()
        r = db.execute("SELECT campaign_type, state_json FROM campaigns WHERE id=?", (camp_id,)).fetchone()
        if r[0] == "AREA":
            route.append("reached AZ1")
            return True, camp_id
        state = json.loads(r[1] or "{}")
        locs = {(l.get("Data") or {}).get("rules_node") or (l.get("Data") or {}).get("node"): l["Data"]
                for l in state.get("VisLocs") or ()}
        aloc = state.get("ALoc")
        active = next((d for d in locs.values() if aloc and d.get("name") == aloc), None)

        def call(fn, env):
            responses.clear()
            fn(handler, db, dict(env, CampID=camp_id), 0, "sess", 2, "t", "i", 0, 1)

        if not locs:
            # A new row has no projected locations until the client queries it.
            call(campaign._handle_getcampstate, {})
            continue
        if active and active["type"] == "Convo":
            ending = _ending_for(db, active["conversationId"])
            if ending:
                call(campaign._handle_sendevent, {"Event": ending, "OParms": []})
            call(campaign._handle_sendevent, {"Event": "conv_done", "OParms": []})
        elif active and active["type"] in ("Encounter", "ModChoiceEncounter"):
            call(campaign._handle_sendevent, {"Event": "start", "OParms": ["", "merc=0"]})

            class Session:
                session_name = f"camp_{camp_id}"
                session_id = 0
            pushes.clear()
            campaign.handle_battle_gameend(handler, db, Session(), True, 1)
            call(campaign._handle_gameend, {"Winners": [1], "Losers": []})
        else:
            target = next((n for n, d in locs.items() if d.get("visible") and d.get("enabled", True)
                           and d.get("type") != "Empty" and (not d.get("completed") or d.get("repeatable"))
                           and n != aloc), None)
            if target is None:
                route.append("starter run: nothing to select")
                return False, camp_id
            call(campaign._handle_locaction, {"RAct": 0, "Loc": locs[target]["name"]})
    route.append("starter run: too many steps")
    return False, camp_id


def _explore_core(db, campaign, rules_loader, handler, responses, pushes,
                  champ_id, camp_id, steps, route):
    done = set()
    try:
        def call(fn, env):
            responses.clear()
            fn(handler, db, dict(env, CampID=camp_id), 0, "sess", 2, "t", "i", 0, 1)
            return (responses[-1] if responses else {}).get("CurState") or {}

        def row():
            r = db.execute("SELECT campaign_type, template_name, state_json FROM campaigns WHERE id=?",
                           (camp_id,)).fetchone()
            return r[0], r[1], json.loads(r[2] or "{}")

        call(campaign._handle_getcampstate, {})
        for _ in range(steps):
            for env in [p for p in pushes if p.get("RequestType") == "cmpupdate" and p.get("Transition")]:
                camp_id = env["CampID"]
                route.append(f"-> {env.get('TemplateType')} camp {camp_id} {env['State'].get('RulesZone', '')}")
            pushes[:] = [p for p in pushes if not (p.get("RequestType") == "cmpupdate" and p.get("Transition"))]
            ctype, template, state = row()
            zone = state.get("RulesZone") or f"legacy:{ctype}:{template}"
            rules = rules_loader.rules(db, state["RulesZone"])[0] if state.get("RulesZone") else {}
            locs = {(l.get("Data") or {}).get("rules_node") or (l.get("Data") or {}).get("node"): l["Data"]
                    for l in state.get("VisLocs") or ()}
            aloc = state.get("ALoc")
            active = next((d for n, d in locs.items() if aloc and d.get("name") == aloc), None)
            if active:
                n = active.get("rules_node") or active["node"]
                t = active["type"]
                if t == "Convo":
                    ending = _ending_for(db, active["conversationId"])
                    if ending:
                        call(campaign._handle_sendevent, {"Event": ending, "OParms": []})
                    call(campaign._handle_sendevent, {"Event": "conv_done", "OParms": []})
                    route.append(f"{zone}:{n} talk {active['conversationId'][:8]}" + (f" [{ending}]" if ending else ""))
                elif t in ("Encounter", "ModChoiceEncounter"):
                    pick = (state["PublicState"]["Data"].get("mod_choice_list") or [""])[0]
                    call(campaign._handle_sendevent, {"Event": "start", "OParms": [pick, "merc=0"]})
                    if not [p for p in pushes if p.get("RequestType") == "gamestarted"]:
                        route.append(f"STUCK: {zone}:{n} battle did not launch")
                        return False, route

                    class Session:
                        session_name = f"camp_{camp_id}"
                        session_id = 0
                    pushes.clear()
                    campaign.handle_battle_gameend(handler, db, Session(), True, 1)
                    call(campaign._handle_gameend, {"Winners": [1], "Losers": []})
                    route.append(f"{zone}:{n} battle won")
                elif t == "ShroomHaus":
                    card = (active.get("choices") or [""])[0]
                    call(campaign._handle_sendevent, {"Event": "shroom_choice", "OParms": [card]})
                    route.append(f"{zone}:{n} shroom haus -> {card[:8]}")
                elif t == "ModDisplay":
                    call(campaign._handle_sendevent, {"Event": "display_done", "OParms": []})
                    route.append(f"{zone}:{n} gaal fortune shown")
                elif t in ("Dungeon", "Panorama"):
                    call(campaign._handle_sendevent, {"Event": "start", "OParms": []})
                    route.append(f"{zone}:{n} enter {t}")
                else:
                    call(campaign._handle_sendevent, {"Event": "empty_done", "OParms": []})
                    route.append(f"{zone}:{n} explored")
                done.add((zone, n))
                continue
            if ctype == "PANORAMA" and state.get("RulesZone") and state["RulesZone"] not in (
                    "az0-human", "az0-elf") and not state["RulesZone"].startswith("az0-"):
                npcs = [(n, d) for n, d in locs.items()
                        if d.get("visible") and d["type"] == "Convo" and (zone, n) not in done]
                if npcs:
                    n, d = npcs[0]
                    call(campaign._handle_locaction, {"RAct": 0, "Loc": d["name"]})
                    route.append(f"{zone}: speak to {d['name']}")
                    continue
                call(campaign._handle_sendevent, {"Event": "return_parent", "OParms": []})
                route.append(f"{zone}: leave")
                done.add((zone, "left"))
                continue
            if ctype not in ("AREA", "DUNGEON"):
                route.append(f"stopped in {zone}")
                return True, route
            current = state.get("LastNode")
            # Empty nodes are walked over too: a visible one reveals the nodes behind it.
            targets = [(n, d) for n, d in locs.items()
                       if d.get("visible") and d.get("enabled", True)
                       and not d["name"].startswith("Fork")
                       and (zone, n, d.get("conversationId") or d.get("encounter") or d["type"]) not in done
                       and (not d.get("completed") or d.get("repeatable"))]
            targets = [(n, d) for n, d in targets
                       if _bfs_paths(rules, state.get("LastNode_id") or current, n) is not None]
            if not targets:
                route.append(f"explored everything reachable in {zone} (at {current})")
                waiting = [(n, d["type"], "done" if d.get("completed") else "open")
                           for n, d in locs.items() if d.get("visible") and not d["name"].startswith("Fork")
                           and (zone, n) not in done]
                route.append(f"  still visible: {waiting[:25]}")
                quests = {}
                for script, raw in db.execute("SELECT template_name, state_json FROM campaigns "
                                              "WHERE champion_id=? AND campaign_type='QUEST'", (champ_id,)):
                    q = json.loads(raw or "{}")
                    quests[script] = (q.get("Flags") or {}).get("_quest_objective_idx"), bool(q.get("Finished"))
                route.append(f"  journal: {quests}")
                return True, route
            n, d = targets[0]
            ids = _bfs_paths(rules, current, n) or []
            call(campaign._handle_sendevent, {"Event": "visit_path", "OParms": [ids]})
            call(campaign._handle_sendevent, {"Event": "visit_node", "OParms": [d["name"]]})
            _ctype, _tpl, after = row()
            if after.get("RulesZone") == state.get("RulesZone"):     # still on this map
                call(campaign._handle_locaction, {"RAct": 0, "Loc": d["name"]})
            route.append(f"{zone}: travel {current} -> {d['name']} ({d['type']})")
            done.add((zone, n, d.get("conversationId") or d.get("encounter") or d["type"]))
            if d["type"] in ("Dungeon", "Panorama"):
                done.add((zone, n))      # a portal is used once per run
        route.append("stopped: step limit")
        return True, route
    finally:
        pass


def _ending_for(db, conversation_guid):
    """The ending a player would usually pick: the first scripted one that
    has an ending action (accept a battle, travel on ...)."""
    rows = db.execute(
        "SELECT trigger_json FROM campaign_node_conversations WHERE LOWER(conversation_guid)=? "
        "UNION ALL SELECT conditions_json FROM quest_conversations WHERE LOWER(conversation_guid)=?",
        (str(conversation_guid).lower(), str(conversation_guid).lower())).fetchall()
    for (raw,) in rows:
        try:
            actions = (json.loads(raw or "{}") or {}).get("actions") or {}
        except (TypeError, ValueError):
            continue
        for key in actions:
            if key not in ("*",) and not key.startswith("choice_stay"):
                return key
    return ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("race", nargs="?", default="Human")
    parser.add_argument("--zones", default="")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--lose-first", action="store_true",
                        help="lose the first battle (checks the retry path)")
    parser.add_argument("--enter", help="with --explore: first enter this zone (test hook)")
    parser.add_argument("--explore", type=int, metavar="STEPS",
                        help="explore the overworld of --champion's save for N steps")
    parser.add_argument("--champion", type=int,
                        help="continue this existing champion's save instead")
    args = parser.parse_args()
    zones = {z for z in args.zones.split(",") if z}
    if args.explore:
        ok, route = explore(args.champion, zones, args.explore, args.verbose, args.race,
                            args.enter)
        print("\n".join(route))
        sys.exit(0 if ok else 1)
    ok, route = run(args.race, zones, args.verbose, args.champion, args.lose_first)
    print("\n".join(route))
    print("REACHED AZ1" if ok else "DID NOT REACH AZ1")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

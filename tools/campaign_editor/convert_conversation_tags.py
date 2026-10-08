#!/usr/bin/env python3
"""Turn implicit node-conversation selectors into editable tags.

campaign_node_conversations rows carry seed-derived keys that campaign.py
reads in code (``visit: first|repeat``, ``state: fortune``, ``outcome``)
plus name heuristics (faction words, a trailing race, "Step 1"). This
rewrites them as tags, node by node, reproducing the order
``_az1_node_conversation`` applies them in:

* ``visit: first`` (no state)         -> ``!talkedAt(N,2)``
* a "Step 1" row (no visit: first)    -> ``!talkedAt(N,2)``
* ``state: fortune`` + ``visit: first`` -> ``flag(gaal_fortune)``, ``!talkedAt(N)``
* ``state: fortune`` + ``visit: repeat`` -> ``flag(gaal_fortune)``, ``talkedAt(N)``
* ``state: fortune``                  -> ``flag(gaal_fortune)``
* ``visit: repeat``                   -> ``talkedAt(N)``
* ``outcome: success|fail``           -> ``outcome:success|fail``
* "Ardent"/"Aria"/"Underworld" in the name -> ``faction(...)``; a trailing
  race -> ``ChampionIsRace(...)``

Every converted node is checked against campaign.py's own selectors
(``_az1_node_conversation``, ``_az1_pre_encounter_conversation``,
``_az1_outcome_conversation``, ``_az1_node_is_repeatable``) for first and
repeat visits, with and without a fortune, both factions and both battle
results; only nodes whose selection is unchanged are written, to
``campaign_data/<template>.json`` (applied at server start).

campaign.py is imported, which opens (and seeds) the database named by
HEX_DB_PATH - so this must point at a copy:

    sqlite3 hconnect.db ".backup /tmp/copy.db"
    HEX_DB_PATH=/tmp/copy.db python3 tools/campaign_editor/convert_conversation_tags.py [--write]
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LEGACY_KEYS = ("visit", "state", "outcome")


def _guard() -> None:
    target = os.environ.get("HEX_DB_PATH", "")
    if not target or Path(target).resolve() == (REPO / "hconnect.db").resolve():
        raise SystemExit("Set HEX_DB_PATH to a copy of hconnect.db: importing "
                         "campaign.py seeds the database it opens.")


def canonical(node: str) -> str:
    match = re.fullmatch(r"Node([A-Z])", node)
    return f"Node00{match.group(1)}" if match else node


def faction_of(name: str) -> str:
    """The explicit faction in an authored name (_conversation_matches_faction)."""
    label = name.lower()
    if "underworld" in label:
        return "Underworld"
    if "ardent" in label or "aria" in label:
        return "Ardent"
    return ""


def audience(name: str, offers, factions_present: set[str] | None = None) -> list[str]:
    """Faction/race tags for a row. campaign.py drops the faction filter
    when no row suits the champion's faction, so a faction tag is only
    added when the node also has rows the other faction can use."""
    faction = faction_of(name)
    tags = []
    if faction and (factions_present is None or
                    {"", "Ardent" if faction == "Underworld" else "Underworld"} & factions_present):
        tags.append(f"faction({faction})")
    race = offers.race_of(name)
    return tags + ([f"ChampionIsRace({race})"] if race else [])


STRATEGIES = tuple(
    # (faction tags on: "all" rows | "results" only | "none"), "Step 1" rule,
    # race tags (node conversations are not race-filtered by campaign.py)
    (factions, step_one, races, keep_fallback)
    for keep_fallback in (False, True)
    for races in (False, True) for step_one in (True, False)
    for factions in ("all", "results", "none"))


def convert_node(node: str, rows: list[tuple], factions: str = "all",
                 step_one: bool = True, races: bool = False,
                 keep_fallback: bool = False) -> list[dict] | None:
    """Tags and priorities for one node's rows (DB order), or ``None`` when
    the node uses none of the implicit selectors.

    ``factions`` limits where name factions become ``faction(...)`` tags
    (rows left untagged keep campaign.py's own faction handling, e.g. the
    pre-battle scoring); ``step_one`` converts the "Step 1" first-visit name
    rule; ``races`` adds race tags; ``keep_fallback`` leaves each faction's
    first row untagged (and without the old keys): campaign.py falls back to
    that row when nothing matches, and on some quest hubs it is itself a
    repeat/start conversation."""
    import offers
    triggers = [dict(r[1]) for r in rows]
    if not any(any(k in t for k in LEGACY_KEYS) for t in triggers) and \
            not any(audience(r[3] or "", offers) for r in rows):
        return None
    # Faction filtering is per selector: result rows and the others.
    present_results = {faction_of(r[3] or "") for r, t in zip(rows, triggers) if t.get("outcome")}
    present_other = {faction_of(r[3] or "") for r, t in zip(rows, triggers) if not t.get("outcome")}
    n = canonical(node)
    first = [i for i, t in enumerate(triggers)
             if t.get("visit") == "first" and not t.get("state") and not t.get("outcome")]
    step1 = [] if first or not step_one else [
        i for i, (t, r) in enumerate(zip(triggers, rows))
        if not t.get("outcome") and
        re.search(r"\bstep\s*1(?:\D|$)", str(t.get("label") or r[3] or ""), re.I)]
    fallback: dict[int, set[str]] = {}     # row -> factions it is the fallback for
    if keep_fallback:
        for side in ("Ardent", "Underworld"):
            mine = [i for i, r in enumerate(rows) if faction_of(r[3] or "") in ("", side)]
            fallback.setdefault((mine or list(range(len(rows))))[0], set()).add(side)
    result = []
    for i, (guid, trigger, priority, name) in enumerate(rows):
        if i in fallback and not trigger.get("outcome"):
            # Only its audience: offered (ahead of the other generic rows)
            # whenever no specific first/fortune rule wins.
            new = {k: v for k, v in trigger.items() if k not in LEGACY_KEYS and k != "tags"}
            tags = [t for t in audience(name or "", offers, present_other)
                    if (factions != "none" and t.startswith("faction(")) or
                    (races and t.startswith("ChampionIsRace("))]
            # A neutral row that is only one faction's fallback.
            sides = fallback[i]
            only = ([f"faction({next(iter(sides))})"]
                    if len(sides) == 1 and not any(t.startswith("faction(") for t in tags)
                    else [])
            if trigger.get("visit") == "repeat":
                # Also the node's repeat conversation: offered on a first
                # visit (as the fallback) or a repeat one, in repeat order.
                new["tags"] = [tags + only + [f"!talkedAt({n})"], tags + [f"talkedAt({n})"]]
                result.append({"guid": guid, "trigger": new, "priority": 300 + i, "name": name})
                continue
            if tags or only:
                new["tags"] = tags + only
            result.append({"guid": guid, "trigger": new, "priority": 3900 + i, "name": name})
            continue
        tags = audience(name or "", offers,
                        present_results if trigger.get("outcome") else present_other)
        if factions == "none" or (factions == "results" and not trigger.get("outcome")):
            tags = [t for t in tags if not t.startswith("faction(")]
        if not races:
            tags = [t for t in tags if not t.startswith("ChampionIsRace(")]
        visit, state = trigger.get("visit"), trigger.get("state")
        if trigger.get("outcome"):
            rank, tags = 40, [f"outcome:{trigger['outcome']}"] + tags
        elif i in first or i in step1:
            rank, tags = 0, ["!talkedAt(%s,2)" % n] + tags
        elif state == "fortune" and visit == "first":
            rank, tags = 1, ["flag(gaal_fortune)", f"!talkedAt({n})"] + tags
        elif state == "fortune" and visit == "repeat":
            rank, tags = 1, ["flag(gaal_fortune)", f"talkedAt({n})"] + tags
        elif state == "fortune":
            rank, tags = 2, ["flag(gaal_fortune)"] + tags
        elif visit == "repeat":
            rank, tags = 3, [f"talkedAt({n})"] + tags
        else:
            rank = 40          # untagged unless it has an audience
        # Untagged and result rows keep their original relative order (the
        # selector's fallback is the first of them).
        new = {k: v for k, v in trigger.items() if k not in LEGACY_KEYS and k != "tags"}
        if tags:
            new["tags"] = tags
        result.append({"guid": guid, "trigger": new,
                       "priority": (rank * 100 + i) if rank < 40 else 4000 + i,
                       "name": name})
    return result


def _states(campaign, db):
    """(label, champion id, area state) combinations to compare."""
    champs = {}
    for cid, race in db.execute("SELECT id, race FROM champions"):
        champs.setdefault(campaign._RACE_FACTIONS.get(race), cid)
    for faction, champ in sorted(champs.items(), key=lambda x: str(x[0])):
        for talks in (0, 1, 2):
            for fortune in (False, True):
                yield (faction, talks, fortune), champ, talks, fortune


def battle_nodes(db) -> set[str]:
    """Nodes with an opponent scene: only they use the pre-battle selector."""
    import catalog
    found = set()
    for name, champion in db.execute("SELECT name, ai_champion_guid FROM encounter_scenes"):
        node = catalog.scene_node_id(name or "")
        if node and str(champion or "").strip("0-"):
            found.add(node)
    return found


BATTLE_NODES: set[str] = set()


def _selection(campaign, db, node, template, champ, talks, fortune):
    state = {"PublicState": {"Data": {"conversation_visits": {node: talks, canonical(node): talks},
                                      "gaal_fortune": fortune}}, "VisLocs": []}
    # The pre-battle selector only runs while first entering the battle.
    battle = template == "AZ1" and canonical(node) in BATTLE_NODES and talks == 0
    return (campaign._az1_node_conversation(db, node, state, template, champ),
            campaign._az1_pre_encounter_conversation(db, node, champ, state)
            if battle else None,
            campaign._az1_outcome_conversation(db, node, True, champ, state)
            if template == "AZ1" else None,
            campaign._az1_outcome_conversation(db, node, False, champ, state)
            if template == "AZ1" else None,
            campaign._az1_node_is_repeatable(db, node, template))


# ------------------------------------------------------------ quest rows

def quest_tags(campaign, script: str, role: str, faction: str,
               objectives: list) -> list[str] | None:
    """When a quest_conversations row is offered, as tags (campaign.py
    _build_az1_panorama_state / _apply_az1_quest_markers), or ``None`` when
    the role cannot be expressed (left to the old rules)."""
    side = [f"faction({faction})"] if faction in ("Ardent", "Underworld") else []
    if role == "start":
        prerequisites = campaign._AZ1_QUEST_START_PREREQUISITES.get(script, ())
        return [f"!questStarted({script})"] + \
            [f"completedQuest({p})" for p in prerequisites] + side
    if role == "not_complete":
        return [f"questActive({script})"] + side
    if role == "complete":
        # _quest_is_turnin_ready: the current objective is the last one and
        # it is a conversation.
        last = objectives[-1] if objectives else {}
        if str((last or {}).get("type") or "").lower() not in ("conversation", "convo"):
            return None
        steps = len(objectives) - 1
        return [f"questActive({script})"] + \
            ([f"completedQuest({script},{steps})"] if steps > 0 else []) + side
    return None


def _set_quests(db, champ, user, states, objectives):
    db.execute("DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST'", (champ,))
    for script, status in states.items():
        if status is None:
            continue
        index = status if isinstance(status, int) else len(objectives.get(script) or [1]) - 1
        db.execute("INSERT INTO campaigns (user_id, champion_id, campaign_type, template_name, "
                   "is_started, state_json) VALUES (?, ?, 'QUEST', ?, 1, ?)",
                   (user, champ, script, json.dumps({
                       "Finished": "2026-01-01T00:00:00Z" if status == "done" else None,
                       "Flags": {"_quest_objective_idx": index,
                                 "_quest_objectives": objectives.get(script) or []}})))


def _quest_selection(campaign, db, champ, node):
    panorama = campaign._build_az1_panorama_state(db, 0, champ, "", node, {"VisLocs": []})
    pano = sorted(((l.get("Data") or {}).get("node"), (l.get("Data") or {}).get("conversationId"),
                   bool((l.get("Data") or {}).get("givequest")),
                   bool((l.get("Data") or {}).get("turninquest")))
                  for l in panorama.get("VisLocs", []))
    area = {"VisLocs": [{"Data": {"node": node, "name": node, "visible": True,
                                  "type": "Convo"}}], "PublicState": {"Data": {}}}
    campaign._apply_az1_quest_markers(db, champ, area)
    data = area["VisLocs"][0]["Data"]
    return pano, (data.get("conversationId"), bool(data.get("givequest")),
                  bool(data.get("turninquest")))


def convert_quests(campaign, db, write: bool) -> None:
    import random
    objectives = {r[0]: json.loads(r[1] or "[]") for r in db.execute(
        "SELECT script_name, objectives_json FROM quest_templates")}
    rows = db.execute("SELECT rowid, campaign_template, quest_script, conversation_guid, node_id, "
                      "role, faction, conditions_json FROM quest_conversations").fetchall()
    plan = {}
    for rowid, template, script, guid, node, role, faction, raw in rows:
        tags = quest_tags(campaign, script, str(role or "").lower(), str(faction or ""),
                          objectives.get(script) or [])
        if tags is not None:
            conditions = json.loads(raw or "{}")
            conditions["tags"] = tags
            plan[rowid] = (template, script, guid, node, conditions)
    print(f"quest rows: {len(plan)} of {len(rows)} expressible as tags")
    champs = {}
    for cid, race, user in db.execute("SELECT id, race, user_id FROM champions"):
        champs.setdefault(campaign._RACE_FACTIONS.get(race), (cid, user))
    rng = random.Random(7)
    differences = []
    nodes = sorted({r[4] for r in rows if r[1] == "AZ1" and r[4]})
    for node in nodes:
        scripts = sorted({r[2] for r in rows if r[1] == "AZ1" and r[4] == node})
        def options(script):
            return [None, "done"] + list(range(len(objectives.get(script) or [1])))
        combos = [{s: None for s in scripts}, {s: "done" for s in scripts}]
        for script in scripts:
            for option in options(script):
                combos.append({**{s: None for s in scripts}, script: option})
                combos.append({**{s: "done" for s in scripts}, script: option})
        combos += [{s: rng.choice(options(s)) for s in scripts} for _ in range(40)]
        for faction, (champ, user) in champs.items():
            for combo in combos:
                db.execute("SAVEPOINT quest_trial")
                _set_quests(db, champ, user, combo, objectives)
                before = _quest_selection(campaign, db, champ, node)
                for rowid, (_t, _s, _g, _n, conditions) in plan.items():
                    db.execute("UPDATE quest_conversations SET conditions_json=? WHERE rowid=?",
                               (json.dumps(conditions), rowid))
                after = _quest_selection(campaign, db, champ, node)
                db.execute("ROLLBACK TO SAVEPOINT quest_trial")
                db.execute("RELEASE SAVEPOINT quest_trial")
                if before != after:
                    differences.append((node, faction, combo, before, after))
    # Accepted: the old map marker showed "!" for a start row whose
    # _AZ1_QUEST_START_PREREQUISITES were unmet (the panorama would not
    # offer it); with the prerequisite as a tag both agree.
    def prerequisite_marker(diff):
        _node, _faction, combo, before, after = diff
        unmet = any(combo.get(p) != "done"
                    for s, ps in campaign._AZ1_QUEST_START_PREREQUISITES.items()
                    if combo.get(s) is None for p in ps)
        return (unmet and before[0] == after[0] and before[1][0] == after[1][0] and
                before[1][2] == after[1][2] and before[1][1] and not after[1][1])
    accepted = [d for d in differences if prerequisite_marker(d)]
    differences = [d for d in differences if not prerequisite_marker(d)]
    if accepted:
        print(f"accepted {len(accepted)} marker fixes: no quest-giver mark for a start "
              "whose prerequisites are unmet")
    print(f"quest selection compared over {len(nodes)} nodes: {len(differences)} differences")
    for node, faction, combo, before, after in differences[:8]:
        changed = {k: v for k, v in combo.items() if v is not None}
        print(f"  {node} {faction} {changed}:\n    before {before}\n    after  {after}")
    if write and not differences:
        for template in sorted({p[0] for p in plan.values()}):
            path = REPO / "campaign_data" / f"{template}.json"
            data = (json.loads(path.read_text(encoding="utf-8")) if path.exists() else
                    {"schema_version": 1, "description": f"{template} campaign table edits",
                     "tables": {}})
            table = data.setdefault("tables", {}).setdefault("quest_conversations", [])
            keyed = {(r["quest_script"], str(r["conversation_guid"]).lower(), r["campaign_template"]): r
                     for r in table}
            for _rowid, (t, script, guid, _node, conditions) in plan.items():
                if t != template:
                    continue
                key = (script, guid.lower(), template)
                entry = keyed.get(key)
                if entry and (entry.get("conditions_json") or {}).get("tags"):
                    continue    # tagged in the editor already
                if entry is None:
                    entry = {"quest_script": script, "conversation_guid": guid,
                             "campaign_template": template}
                    table.append(entry)
                    keyed[key] = entry
                entry["conditions_json"] = conditions
            import campaign_data
            bad = campaign_data.validate(data)
            if bad:
                raise SystemExit(f"{path.name}: {bad[:3]}")
            path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
            print(f"wrote {path}")


def main():
    _guard()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="write the unchanged-selection nodes to campaign_data/")
    parser.add_argument("--quests", action="store_true",
                        help="convert quest_conversations roles instead")
    args = parser.parse_args()
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(HERE))
    import campaign        # noqa: E402 (opens HEX_DB_PATH)
    import campaign_data   # noqa: E402
    import db as dbmod     # noqa: E402
    db = dbmod._db
    # Server code commits mid-way; trials are rolled back (this is a copy).
    db.commit = lambda *a, **k: None
    if args.quests:
        convert_quests(campaign, db, args.write)
        return
    BATTLE_NODES.update(battle_nodes(db))
    passed, failed = {}, {}
    for template in ("AZ1", "AZ2"):
        nodes = [r[0] for r in db.execute(
            "SELECT DISTINCT node_id FROM campaign_node_conversations "
            "WHERE campaign_template=? ORDER BY node_id", (template,))]
        for node in nodes:
            rows = campaign._az1_node_conversation_rows(db, node, template)
            if convert_node(node, rows) is None:
                continue
            if not any(any(k in r[1] for k in LEGACY_KEYS) for r in rows) and \
                    any((r[1] or {}).get("tags") for r in rows):
                continue      # converted already (campaign_data applied)
            states = list(_states(campaign, db))
            before = [_selection(campaign, db, node, template, c, t, f) for _k, c, t, f in states]
            best = None
            for factions, step_one, races, keep_fallback in STRATEGIES:
                plan = convert_node(node, rows, factions, step_one, races, keep_fallback)
                # The server connection autocommits: trial inside a savepoint.
                db.execute("SAVEPOINT conversion_trial")
                for row in plan:
                    db.execute("UPDATE campaign_node_conversations SET trigger_json=?, priority=? "
                               "WHERE campaign_template=? AND node_id=? AND conversation_guid=?",
                               (json.dumps(row["trigger"]), row["priority"], template, node, row["guid"]))
                after = [_selection(campaign, db, node, template, c, t, f) for _k, c, t, f in states]
                db.execute("ROLLBACK TO SAVEPOINT conversion_trial")
                db.execute("RELEASE SAVEPOINT conversion_trial")
                diffs = [(k, b, a) for (k, *_), b, a in zip(states, before, after) if b != a]
                if best is None or len(diffs) < len(best[2]):
                    best = (node, plan, diffs)
                if not diffs:
                    break
            (failed if best[2] else passed).setdefault(template, []).append(best)
    for template in ("AZ1", "AZ2"):
        ok = passed.get(template, [])
        bad = failed.get(template, [])
        print(f"{template}: {len(ok)} nodes convert with unchanged selection, {len(bad)} differ")
        for node, _plan, diffs in bad:
            (faction, talks, fortune), b, a = diffs[0]
            print(f"  {node}: e.g. {faction} talks={talks} fortune={fortune}: "
                  f"before {b} after {a}  ({len(diffs)} cases)")
    if not args.write:
        return
    for template, nodes in passed.items():
        path = REPO / "campaign_data" / f"{template}.json"
        data = (json.loads(path.read_text(encoding="utf-8")) if path.exists() else
                {"schema_version": 1, "description": f"{template} campaign table edits",
                 "tables": {}})
        table = data.setdefault("tables", {}).setdefault("campaign_node_conversations", [])
        by_key = {(r["campaign_template"], str(r["conversation_guid"]).lower()): r for r in table}
        for node, plan, _diffs in nodes:
            for row in plan:
                key = (template, row["guid"].lower())
                existing = by_key.get(key)
                if existing and (existing.get("trigger_json") or {}).get("tags"):
                    continue        # already tagged in the editor: keep it
                entry = existing or {"campaign_template": template,
                                     "conversation_guid": row["guid"]}
                entry["trigger_json"] = row["trigger"]
                entry["priority"] = row["priority"]
                if not existing:
                    table.append(entry)
                    by_key[key] = entry
        bad = campaign_data.validate(data)
        if bad:
            raise SystemExit(f"{path.name}: {bad[:3]}")
        path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()

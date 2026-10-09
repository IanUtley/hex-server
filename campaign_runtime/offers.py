"""What each node offers, derived from the server's table data.

Offers are content, not rules: they come from ``campaign_node_conversations``,
``quest_conversations`` and ``encounter_scenes`` (after any startup table
overrides), and are merged into the rules as ``nodes.<id>.entries`` only for
evaluation. Only explicit metadata is used; a conversation that the tables
cannot select is kept with ``"when": false`` and a ``review`` note.

Editor tags (``trigger_json.tags``, see ``campaign_rules.parse_tag``) are the
explicit form: a tagged conversation is offered when all its tags hold, ahead
of the name-derived defaults; ``outcome:success|fail`` makes it an encounter
result conversation.
"""

from __future__ import annotations

import re
from typing import Any

FACTIONS = ("Ardent", "Underworld")
RACES = ("Human", "Elf", "Coyotle", "Orc", "Vennen", "Necrotic", "Shin'hare",
         "Dwarf")

# Offers the server builds in code rather than from tables; each cites its
# campaign.py source so it can move into table data later.
CODE_OFFERS: dict[tuple[str, str], list[dict]] = {}


def faction_of(text: str) -> str:
    for faction in FACTIONS:
        if re.search(rf"\b{faction}\b", text or "", re.I):
            return faction
    return ""


def race_of(text: str) -> str:
    """Race variant from the authored name suffix (``... - Quest End - Dwarf``)."""
    tail = str(text or "").rsplit(" - ", 1)[-1].strip().lower()
    return next((race for race in RACES if race.lower() == tail), "")


def _canonical(value: str) -> str:
    return canonical_node(value)


def canonical_node(value: str) -> str:
    """``NodeR`` -> ``Node00R``: conversation names abbreviate letter nodes.

    The tables keep the authored ``NodeR`` spelling (the server falls back to
    it in ``_az1_node_conversation_rows``); the map uses ``Node00R``.
    """
    match = re.fullmatch(r"Node([A-Z])", str(value or ""))
    return f"Node00{match.group(1)}" if match else str(value or "")


def with_audience(condition, faction="", race=""):
    if not faction and not race:
        return condition
    parts = [{"race": race}] if race else [{"faction": faction}]
    if condition:
        parts.insert(0, condition)
    return {"all": parts} if len(parts) > 1 else parts[0]


def _engine():
    import catalog
    return catalog._rules_module()


def tags_of(conversation: dict[str, Any]) -> list[str]:
    return _engine().trigger_tags(conversation.get("trigger"))


def outcome_of(conversation: dict[str, Any]) -> str:
    """``success``/``fail`` from an outcome tag or the seeded trigger."""
    outcome, _rest = _engine().split_outcome(tags_of(conversation))
    return outcome or str((conversation.get("trigger") or {}).get("outcome") or "")


def condition_tags(conversation: dict[str, Any]) -> list[str]:
    """Tags other than the outcome: the conversation's selection condition."""
    return _engine().split_outcome(tags_of(conversation))[1]


def actions_of(conversation: dict[str, Any]) -> dict[str, list[str]]:
    """Ending actions (``trigger_json.actions``): ``{ending|"*": [action]}``."""
    actions = (conversation.get("trigger") or {}).get("actions")
    return actions if isinstance(actions, dict) else {}


def drives_battles(conversations) -> bool:
    """Whether any of a node's conversations starts a battle from an ending
    (``encounter(...)``, possibly conditional)."""
    return any(re.match(r"\s*encounter\s*\(", str(action), re.I)
               for conversation in conversations
               for listed in actions_of(conversation).values()
               for action in listed or ())


def race_tag(tags) -> str:
    """The race a plain tag list requires (``ChampionIsRace(X)``), or ``""``."""
    if not isinstance(tags, list):
        return ""
    for tag in tags:
        match = re.fullmatch(r"\s*ChampionIsRace\s*\((.+)\)\s*", str(tag), re.I)
        if match:
            return match.group(1).strip()
    return ""


_CHECK_TAMED = re.compile(r"^\s*check_?tamed\s*\(\s*\)\s*$", re.I)
_SCENE_FLAG_ACTION = re.compile(r"^\s*(set|clear)_flag\s*\(\s*([^()\s][^()]*?)\s*\)\s*$")
SCENE_ACTION_EVENTS = {"pre": "encounter_start", "victory": "encounter_victory",
                       "defeat": "encounter_defeat"}


def is_check_tamed(action: Any) -> bool:
    """``check_tamed()``: the server looks for a tamed troop in the
    opponent's void when the battle is won."""
    return bool(_CHECK_TAMED.match(str(action or "")))


def tamed_flag(scene_guid: Any) -> str:
    """The flag ``check_tamed()`` sets: ``tamed:<first 8 of the scene GUID>``."""
    return f"tamed:{str(scene_guid or '').lower()[:8]}"


def tamed_scene(encounter: dict[str, Any]) -> bool:
    """A scene whose victory actions run ``check_tamed()``."""
    return any(is_check_tamed(action) for action in
               ((encounter.get("actions") or {}).get("victory") or ()))


def scene_action_hooks(encounters: Any) -> list[dict[str, Any]]:
    """Rules hooks for scene actions (``set_flag`` / ``clear_flag`` before the
    battle and on victory / defeat; ``check_tamed()`` on victory, which the
    simulator models as a win carrying the ``tamed_troop`` battle event)."""
    hooks = []
    for encounter in encounters:
        guid = str(encounter.get("guid") or "")
        if not guid:
            continue
        for trigger, listed in (encounter.get("actions") or {}).items():
            if trigger not in SCENE_ACTION_EVENTS:
                continue
            effects = []
            for action in listed or ():
                match = _SCENE_FLAG_ACTION.match(str(action))
                if match:
                    effects.append({f"{match.group(1)}_flag": match.group(2)})
                elif trigger == "victory" and is_check_tamed(action):
                    flag = tamed_flag(guid)
                    hooks.append({"id": flag,
                                  "on": {"event": "encounter_victory",
                                         "scene": guid, "events": "tamed_troop"},
                                  "do": [{"set_flag": flag}]})
            if effects:
                hooks.append({"id": f"scene:{guid[:8]}:{trigger}",
                              "on": {"event": SCENE_ACTION_EVENTS[trigger],
                                     "scene": guid},
                              "do": effects})
    return hooks


def build_offers(zone: str, data: dict[str, Any],
                 quests: dict[str, Any], known_conversations=None
                 ) -> tuple[dict[str, list], dict[str, list]]:
    """Return ``(entries by node, quests started by conversation GUID)``."""
    # NodeR -> Node00R is the overworlds' spelling; dungeons use NodeA...
    canonical_node = _canonical if zone in ("AZ1", "AZ2") else (lambda value: value)
    entries: dict[str, list] = {}
    starts: dict[str, list] = {}
    for node_id, rows in sorted(data["quest_links"].items()):
        node_id = canonical_node(node_id)
        for row in rows:
            if not row["enabled"]:
                continue
            quest = row["quest"]
            steps = (quests.get(quest) or {}).get("steps") or []
            faction = row["faction"] or (row["conditions"] or {}).get("faction", "")
            if row["role"] == "start":
                when = {"quest": quest, "is": "not_started"}
                starts.setdefault(row["guid"], []).append(quest)
            elif row["role"] == "complete":
                when = {"quest": quest, "is": "active",
                        "step_at_least": max(0, len(steps) - 1)}
            else:  # not_complete
                when = {"quest": quest, "is": "active",
                        "step_below": max(1, len(steps) - 1)}
            race = race_of(row["name"])
            tags = (row["conditions"] or {}).get("tags")
            entries.setdefault(node_id, []).append({
                "id": f"{quest}:{row['role']}:{race or faction or 'all'}:{row['npc']}",
                "kind": "conversation", "npc": row["npc"],
                # Editor tags (conditions_json.tags) say exactly when it is offered.
                "when": tags if tags else with_audience(when, faction, race),
                "conversation": row["guid"], "label": row["name"],
                "repeatable": row["role"] == "not_complete",
                "source": "quest_conversations",
            })
            actions = (row.get("trigger") or {}).get("actions")
            if actions:
                entries[node_id][-1]["actions"] = actions

    node_ids = {canonical_node(n) for n in
                set(data["encounters"]) | set(data["node_conversations"])}
    for node_id in sorted(node_ids):
        node_entries = entries.setdefault(node_id, [])
        conversations = [
            c for raw, rows in data["node_conversations"].items()
            if canonical_node(raw) == node_id for c in rows if c["enabled"]]
        taken = {e.get("conversation") for e in node_entries}
        conversations = [c for c in conversations if c["guid"] not in taken]
        all_battles = [e for raw, rows in data["encounters"].items()
                       if canonical_node(raw) == node_id for e in rows if e["battle"]]
        if drives_battles(conversations):
            # The node's conversations start its battles from their endings
            # (encounter(...)): offer them as plain conversations instead of
            # pairing each with the node's battle scene.
            all_battles = []
        # Per-race scenes (zones.json scene_regex) are offered to their race.
        race_battles = {e["race"]: e for e in all_battles if e.get("race")}
        battles = [e for e in all_battles if not e.get("race")]
        used = set()
        # Explicitly tagged pre-battle / plain conversations win while their
        # tags hold; the name-derived defaults below stay the fallback.
        # With no battle scene on the node, an outcome-tagged conversation
        # follows a battle a conversation started (``encounter(...)``): it is
        # offered like any tagged conversation, its outcome tag included.
        no_scene = not all_battles
        tagged = [c for c in conversations
                  if tags_of(c) and (not outcome_of(c) or no_scene)]
        results = [{"outcome": outcome_of(c), "conversation": c["guid"],
                    "label": c["name"], "when": condition_tags(c),
                    "actions": actions_of(c)}
                   for c in conversations if outcome_of(c)]
        raced = set()
        for conversation in tagged:
            used.add(conversation["guid"])
            when = tags_of(conversation) if outcome_of(conversation) else \
                condition_tags(conversation)
            race = race_tag(when)
            entry = {"id": f"tagged:{conversation['guid'][:8]}",
                     "label": conversation["name"], "when": when,
                     "source": "campaign_node_conversations",
                     "tags": tags_of(conversation)}
            if actions_of(conversation):
                entry["actions"] = actions_of(conversation)
            battle = race_battles.get(race) or (battles or [None])[0]
            if battle:
                raced.add(race)
                entry.update(kind="encounter", scene=battle["guid"],
                             pre=conversation["guid"])
                # The result conversations for the same audience.
                for key, out in (("victory", "success"), ("defeat", "fail")):
                    match = next((r for r in results if r["outcome"] == out and
                                  race_tag(r["when"]) == race), None)
                    if match:
                        entry[key] = match["conversation"]
                        used.add(match["conversation"])
                        if key == "victory" and match.get("actions"):
                            entry["victory_actions"] = match["actions"]
            else:
                entry.update(kind="conversation",
                             conversation=conversation["guid"],
                             repeatable=conversation["trigger"].get(
                                 "visit") == "repeat")
            node_entries.append(entry)
        for race, battle in sorted(race_battles.items()):
            if race not in raced:
                node_entries.append({
                    "id": f"battle:{battle['guid'][:8]}", "kind": "encounter",
                    "scene": battle["guid"], "label": battle["title"],
                    "when": [f"ChampionIsRace({race})"],
                    "source": "encounter_scenes"})
        for faction in (FACTIONS if any(faction_of(c["name"]) for c in conversations)
                        else ("",)):
            mine = [c for c in conversations if c["guid"] not in used and
                    (not faction or faction_of(c["name"]) in ("", faction))]
            outcome = {}   # default result: prefer an untagged conversation
            for c in sorted(mine, key=lambda c: bool(condition_tags(c))):
                if outcome_of(c):
                    outcome.setdefault(outcome_of(c), c)
            pre = [c for c in mine if not outcome_of(c) and
                   c["trigger"].get("visit") != "repeat"]
            for battle in battles[:1]:
                entry = {
                    "id": f"battle:{battle['guid'][:8]}{':' + faction if faction else ''}",
                    "kind": "encounter", "scene": battle["guid"],
                    "label": battle["title"], "when": with_audience(None, faction),
                    "source": "encounter_scenes",
                }
                if pre:
                    entry["pre"] = pre[0]["guid"]
                    used.add(pre[0]["guid"])
                for key, out in (("victory", "success"), ("defeat", "fail")):
                    if out in outcome:
                        entry[key] = outcome[out]["guid"]
                        used.add(outcome[out]["guid"])
                if any(r["when"] for r in results):
                    entry["results"] = results
                    used.update(r["conversation"] for r in results)
                node_entries.append(entry)
        if len(battles) > 1:
            for battle in battles[1:]:
                node_entries.append({
                    "id": f"battle:{battle['guid'][:8]}", "kind": "encounter",
                    "scene": battle["guid"], "label": battle["title"],
                    "when": False, "source": "encounter_scenes",
                    "review": "alternative battle scene on this node; "
                              "no table metadata selects it"})
        first_plain = True
        for conversation in conversations:
            if conversation["guid"] in used:
                continue
            faction = faction_of(conversation["name"])
            entry = {"id": f"conv:{conversation['guid'][:8]}",
                     "kind": "conversation",
                     "conversation": conversation["guid"],
                     "label": conversation["name"],
                     "source": "campaign_node_conversations"}
            if actions_of(conversation):
                entry["actions"] = actions_of(conversation)
            if conversation["trigger"].get("visit") == "repeat":
                entry["when"] = with_audience(
                    {"visits_at_least": [node_id, 2]}, faction)
                entry["repeatable"] = True
                node_entries.insert(0, entry)
                continue
            if first_plain and not battles:
                entry["when"] = with_audience(None, faction)
                first_plain = faction != ""  # one default per faction
            else:
                entry["when"] = False
                entry["review"] = ("no table metadata selects this "
                                   "conversation; set a condition")
            if known_conversations is not None and \
                    conversation["guid"] not in known_conversations:
                entry["review"] = "conversation GUID not in Records"
            node_entries.append(entry)

    for (offer_zone, node_id), extra in CODE_OFFERS.items():
        if offer_zone != zone:
            continue
        for template in extra:
            entry = dict(template)
            script, index = entry.pop("quest_scene")
            steps = (quests.get(script) or {}).get("steps") or []
            scene = ((steps[index].get("done_when") or {}).get("scene")
                     if index < len(steps) else "")
            if scene:
                entry.update(scene=scene, source="campaign.py")
                entries.setdefault(node_id, []).insert(0, entry)
    return {k: v for k, v in entries.items() if v}, starts

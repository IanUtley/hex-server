"""Declarative campaign node state: conditions, hooks and a derived layout.

A rules file (``campaign/campaign_rules/<ZONE>.json``) states, per node, *when* it is
visible, hidden, blocked, completed, retryable or quest-marked, plus the
hooks and quest steps that set the flags and quest progress those conditions
read. What a node *offers* (conversations, encounters, rewards) is table
data, not rules: callers merge it in as ``nodes.<id>.entries`` before
evaluating. This module is pure: no database, no globals, and inputs are
never mutated, so the editor's simulator and the server can share it safely
across threads.

Node gates (each a condition)::

    visible   - shown even when no visited neighbour reveals it
    hidden    - never shown (even if visited)
    blocked   - not revealed by neighbours and not enterable unless visited
    completed - the node's completion; when present, finishing the node's
                content does not complete it - the condition does (a won
                taming encounter stays retryable until its flag is set)
    retryable - shown as replayable
    marker    - quest marker
    givequest / turninquest - the client's quest-giver "!" / turn-in "?"
                indicators (panorama NPCs)
    autostart - its conversation plays as soon as the state arrives

A panorama's nodes are its NPCs (no paths). The client hides a completed
NPC, so a panorama NPC is shown when ``visible`` and not ``completed``.

Progress state (plain JSON)::

    {"flags": [...], "faction": "Ardent", "race": "Elf", "current": "Node003",
     "quests": {"az01_tamed": {"status": "active", "step": 0}},
     "keywords": ["Stealth"],
     "nodes": {"Node003": {"visits": 1, "completed": false,
                           "failed": false, "retryable": true}},
     "visited_paths": ["Path_Node001_Node002"]}

Map visibility is *derived* from that state on every call (``layout``):
hooks change flags, quests, counters and node progress, so the layout can never
drift from the rules that produce it. Counters are local to a zone's rules
state and can be read by ``counter(Name[,N])`` tags.

Conditions::

    {"flag": "x"}  {"not": c}  {"all": [c, ...]}  {"any": [c, ...]}
    {"quest": "script", "is": "not_started|active|finished|started",
     "step_at_least": n, "step_below": n}
    {"faction": "Ardent"}  {"race": "Dwarf"}  {"visited": "Node003"}  {"completed": "Node003"}
    {"visits_at_least": ["Node003", 2]}  {"talked": ["Node003", 1]}
    ["counter(answer_score)"]  (counter is at least 1; add `,N` for a threshold)
    ["keyword(Stealth)"]  (the champion has the status, or a positive
                            campaign counter named Stealth)
    {"outcome": "success|fail", "node": "Node005"}   (last encounter result;
                                                     node defaults to context)

A condition may also be a *tag list*: every tag must hold (see ``parse_tag``);
or a list of tag lists ("or" groups): at least one group must hold::

    ["faction(Ardent)", "!ChampionIsRace(Elf)", "outcome:success",
     "completedQuest(az01_tamed,1)", "conversation(AZ1 - Node 7 - ..., choice_go)"]
    [["questStarted(az01_q_fort_romor)"], ["questStarted(az01_q_usurper)"]]

Finishing a conversation sets the flags ``conversation(<name>)`` and, when
the ending is known, ``conversation(<name>,<ending>)``; ``<ending>`` is the
ending answer's ServerScriptEvent name (or ``A<answer id>`` when the answer
fires none - only the editor's simulator can tell those apart).

Effects (hook ``do`` lists)::

    {"set_flag": "x"} {"clear_flag": "x"}
    {"start_quest": "s"} {"advance_quest": "s"} {"complete_quest": "s"}
    {"increment_counter": {"name": "answer_score", "amount": 1}}
    {"clear_counter": "answer_score"}
    {"complete_node": "Node003"} {"reset_node": "Node003"}

Actions (``apply``)::

    {"type": "visit", "node": "Node003"}
    {"type": "conversation_end", "node": "Node003", "conversation": guid,
     "name": "AZ1 - Node 3 - ...", "ending": "choice_battle_yes",
     "events": ["choice_battle_yes"]}

Ending actions (an entry's ``actions``: ``{"<ending>"|"*": ["teleport(crayburn)"]}``,
from table data ``trigger_json.actions``) run when the conversation ends;
``teleport(Zone[,Node])`` is reported in ``state["teleport"]`` and
``encounter(SceneGuid[,ConversationGuid])`` in ``state["encounter"]``;
``start_quest(S)`` / ``complete_quest(S)`` / ``set_flag(X)`` apply at once.
Every entry for the finished conversation whose ``when`` held runs its
actions, so one conversation can start several quests. The
optional encounter conversation is shown at battle start as an encounter mod.
    {"type": "encounter_result", "node": "Node003", "scene": guid,
     "victory": true, "events": ["tamed_troop"], "mod": card_guid,
     "result_conversation": true}
      (``result_conversation``: the victory conversation is played next, so
      its actions wait for ``conversation_end`` with ``result_of`` = the
      battle entry's id)
      (``mod``: the card picked on the card-selector panel; it must be one
      ``layout(...)["nodes"][n]["mod_choices"]`` offered, and its option's
      ``pre`` plus ``victory`` / ``defeat`` actions run)

Card-selector battles (``rules["mod_choices"]``: scene GUID ->
``scene_mod_choices(mods_json)``) come from a scene's ``"choice": true``
mod entry: the options whose ``when`` holds are offered; none offered means
a normal encounter.
    {"type": "set_node", "node": "Node003", "values": {"completed": true}}
    {"type": "set_keyword", "keyword": "Stealth", "present": true|false}
    {"type": "clear_node", "node": "Node003"}  {"type": "clear_encounter"}
    {"type": "set_current", "node": "Node003", "visited": true}  (editor state edits)

``entries`` (merged from table data) are ``{"id", "kind": "conversation|
encounter|panorama|empty", "when", "conversation"|"scene", "repeatable"}``;
the first whose ``when`` holds is what the node offers now.
    {"type": "script_event", "name": "choice_swim", "node": "Node012"}
    {"type": "shroom_choice", "node": "Node004", "card": card_guid}  (a
     ``shroomhaus`` entry: its keeper's conversation ``pre``, then a choice)
    {"type": "count_choice", "node": "Node057", "count": 5}  (a count picker:
     moves ``count`` from its ``from`` counter into its ``to`` counter)

``rules["count_pickers"]`` maps a node to ``{"from": counter, "to": counter}``:
after a conversation there, while ``from`` is at least 1, the node offers a
count picker (0..``from``); see ``count_picker``.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
MAX_CASCADE = 32
QUEST_STATUSES = ("not_started", "active", "finished")


class RulesError(ValueError):
    """A rules file or action that cannot be evaluated."""


# ---------------------------------------------------------------- loading

def load_rules(path: str | Path) -> dict[str, Any]:
    rules = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = validate(rules)
    errors = [p for p in problems if p["level"] == "error"]
    if errors:
        raise RulesError("; ".join(p["text"] for p in errors[:5]))
    return rules


def _keyword_names(value: Any) -> list[str]:
    """Normalize champion status names; a count map keeps only positive ones."""
    if isinstance(value, dict):
        values = []
        for name, count in value.items():
            try:
                present = int(count or 0) > 0
            except (TypeError, ValueError):
                present = bool(count)
            if present:
                values.append(name)
    elif isinstance(value, str):
        values = [value]
    else:
        values = value or ()
    found = {}
    for value in values:
        name = str(value or "").strip()
        if name:
            found.setdefault(name.casefold(), name)
    return sorted(found.values(), key=str.casefold)


def initial_state(rules: dict[str, Any], *, faction: str = "",
                  race: str = "") -> dict[str, Any]:
    start = rules.get("start") or {}
    node = start.get("node", "")
    state = {
        "flags": sorted(set(start.get("flags") or ())),
        "faction": faction or start.get("faction", ""),
        "race": race or start.get("race", ""),
        "current": node,
        "quests": copy.deepcopy(start.get("quests") or {}),
        "counters": {str(name): int(value or 0)
                     for name, value in (start.get("counters") or {}).items()},
        "keywords": _keyword_names(start.get("keywords")),
        "nodes": {},
        "visited_paths": [],
    }
    if node:
        state["nodes"][node] = {"visits": 1, "completed": bool(
            start.get("node_completed", True)), "failed": False,
            "retryable": False}
    return state


# ---------------------------------------------------------------- progress
# A champion's progress is campaign-wide (flags, quests, counters ...: a quest
# started in AZ1 finishes in a panorama) plus one node-progress part per zone.

CAMPAIGN_KEYS = ("flags", "quests", "counters", "keywords", "faction", "race",
                 "gold_spent", "return_to")
ZONE_KEYS = ("current", "nodes", "visited_paths", "came_from", "encounter",
             "pass", "run_lost")


def zone_view(rules: dict[str, Any], campaign: dict[str, Any] | None,
              zone: dict[str, Any] | None) -> dict[str, Any]:
    """The engine state for one zone: the campaign-wide part plus that zone's
    node progress. A zone entered for the first time starts from its rules'
    ``start`` (node, plus its start flags/quests merged into the campaign's)."""
    campaign = copy.deepcopy(campaign or {})
    if zone is None:
        fresh = initial_state(rules, faction=campaign.get("faction", ""),
                              race=campaign.get("race", ""))
        zone = {k: fresh[k] for k in ZONE_KEYS if k in fresh}
        campaign["flags"] = sorted(set(campaign.get("flags") or ()) |
                                   set(fresh["flags"]))
        for script, progress in fresh["quests"].items():
            (campaign.setdefault("quests", {})).setdefault(script, progress)
        for name, value in fresh["counters"].items():
            campaign.setdefault("counters", {}).setdefault(name, value)
    state = {"flags": [], "quests": {}, "counters": {}, "keywords": [],
             "faction": "", "race": "", "nodes": {}, "visited_paths": [],
             "current": ""}
    state.update({k: v for k, v in campaign.items() if k in CAMPAIGN_KEYS})
    state.update(copy.deepcopy({k: v for k, v in zone.items() if k in ZONE_KEYS}))
    return state


def split_progress(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(campaign-wide part, zone part)`` of an engine state. Transient
    outputs (``teleport``) belong to neither and are left to the caller."""
    campaign = {k: copy.deepcopy(state[k]) for k in CAMPAIGN_KEYS if k in state}
    zone = {k: copy.deepcopy(state[k]) for k in ZONE_KEYS if k in state}
    return campaign, zone


# ---------------------------------------------------------------- tags

_TAG = re.compile(r"^\s*(?:(!|not\s+))?\s*([A-Za-z_]+)\s*(?:\((.*)\)|:\s*(\S+))\s*$",
                  re.S)
TAG_NAMES = ("faction", "ChampionIsRace", "outcome", "completedQuest",
             "conversation", "flag", "questActive", "questStarted", "visited",
             "completed", "talkedAt", "counter", "keyword", "cameFrom")
_TAG_LOOKUP = {name.lower(): name for name in TAG_NAMES}


def conversation_flag(name: str, ending: str = "") -> str:
    """The flag a finished conversation sets (``conversation(Name,Ending)``)."""
    name = str(name or "").strip()
    return f"conversation({name},{ending})" if ending else f"conversation({name})"


def parse_tag(tag: str, node: str = "") -> dict[str, Any]:
    """One editor tag -> a condition. ``!`` (or ``not``) negates it.

    ``faction(X)`` ``ChampionIsRace(X)`` ``outcome:success|fail``
    ``completedQuest(Script)`` (finished) ``completedQuest(Script,N)`` (step
    N, 1-based, done) ``conversation(Name)`` ``conversation(Name,Ending)``
    ``flag(X)`` ``questActive(Script)`` ``questStarted(Script)``
    ``visited(Node)`` ``completed(Node)`` ``talkedAt(Node)`` /
    ``talkedAt(Node,N)`` (at least 1 / N conversations finished at the node:
    the server's ``conversation_visits``; ``!talkedAt(Node)`` is a first
    visit's conversation, ``talkedAt(Node)`` a repeat visit's).
    ``counter(Name)`` / ``counter(Name,N)`` holds when the named counter is
    at least 1 / N; the editor state table shows its exact current value.
    ``keyword(Name)`` holds when the champion has one or more counters of that
    name (for example ``keyword(Stealth)`` or ``keyword(Burning)``).
    ``cameFrom(Node)`` holds when the player arrived at the current node from
    ``Node`` (the node before it on the route they travelled).
    """
    match = _TAG.match(str(tag or ""))
    if not match:
        raise RulesError(f"unreadable tag {tag!r}")
    negated, name, args, colon = match.groups()
    kind = _TAG_LOOKUP.get(name.lower())
    if kind is None:
        raise RulesError(f"unknown tag {name!r}")
    raw = (colon if colon is not None else args or "").strip()
    if kind == "faction":
        condition = {"faction": raw}
    elif kind == "ChampionIsRace":
        condition = {"race": raw}
    elif kind == "outcome":
        if raw.lower() not in ("success", "fail"):
            raise RulesError(f"outcome must be success or fail: {tag!r}")
        condition = {"outcome": raw.lower()}
        if node:
            condition["node"] = node
    elif kind == "completedQuest":
        script, _, step = (part.strip() for part in raw.rpartition(",")) \
            if "," in raw else (raw, "", "")
        if not step:
            condition = {"quest": script, "is": "finished"}
        elif step.isdigit() and int(step) > 0:
            condition = {"quest": script, "is": "started",
                         "step_at_least": int(step)}
        else:
            raise RulesError(f"completedQuest step must be 1 or more: {tag!r}")
    elif kind == "conversation":
        # Names never contain commas; the ending is everything after the last.
        conversation, _, ending = raw.rpartition(",") if "," in raw else (raw, "", "")
        condition = {"flag": conversation_flag(conversation, ending.strip())}
    elif kind == "flag":
        condition = {"flag": raw}
    elif kind == "questActive":
        condition = {"quest": raw, "is": "active"}
    elif kind == "questStarted":
        condition = {"quest": raw, "is": "started"}
    elif kind == "visited":
        condition = {"visited": raw}
    elif kind == "talkedAt":
        node_id, _, count = (part.strip() for part in raw.partition(","))
        if count and not count.isdigit():
            raise RulesError(f"talkedAt count must be a number: {tag!r}")
        condition = {"talked": [node_id, int(count or 1)]}
    elif kind == "counter":
        counter_name, _, minimum = raw.partition(",")
        counter_name, minimum = counter_name.strip(), minimum.strip()
        if not counter_name:
            raise RulesError(f"counter needs a name: {tag!r}")
        if minimum and not minimum.isdigit():
            raise RulesError(f"counter threshold must be a number: {tag!r}")
        condition = {"counter": counter_name, "at_least": int(minimum or 1)}
    elif kind == "keyword":
        condition = {"keyword": raw}
    elif kind == "cameFrom":
        condition = {"came_from": raw}
    else:
        condition = {"completed": raw}
    if not raw:
        raise RulesError(f"tag needs a value: {tag!r}")
    return {"not": condition} if negated else condition


_ACTION = re.compile(r"^\s*([A-Za-z_]+)\s*\((.*)\)\s*$", re.S)
ACTION_NAMES = ("teleport", "encounter", "complete_quest", "complete_step",
                "start_quest", "set_flag", "clear_flag", "spend_gold", "let_pass", "clear_counter",
                "set_counter", "move_counter")
# ``<action> if <tag>[, <tag>...]``: the action runs only while every tag holds.
_ACTION_IF = re.compile(r"^(.*\))\s+if\s+(.+)$", re.S | re.I)
ACTION_ALIASES = {"startquest": "start_quest", "completequest": "complete_quest",
                  "setflag": "set_flag", "spendgold": "spend_gold",
                  "letpass": "let_pass", "unflag": "clear_flag",
                  "clearflag": "clear_flag", "clearcounter": "clear_counter"}


def flag_matches(flag: str, pattern: str) -> bool:
    """``clear_flag`` patterns: an exact flag, or ``prefix*`` for every flag
    starting with ``prefix``."""
    pattern = str(pattern or "")
    if pattern.endswith("*"):
        return str(flag).startswith(pattern[:-1])
    return str(flag) == pattern


def split_action_condition(action: Any) -> tuple[str, list[str] | None]:
    """``"encounter(X) if cameFrom(Node016)"`` -> ``("encounter(X)",
    ["cameFrom(Node016)"])``; an unconditional action has ``None``."""
    text = str(action or "").strip()
    match = _ACTION_IF.match(text)
    if not match:
        return text, None
    tags = [tag.strip() for tag in match.group(2).split(",") if tag.strip()]
    return match.group(1).strip(), tags


def parse_action(action: str) -> dict[str, Any]:
    """One ending action -> ``{"teleport": {"zone": z, "node": n}}``.

    A trailing ``if <tag>[, <tag>...]`` makes it conditional: the result
    carries ``"if": [tags]`` and the action runs only while they all hold.
    ``let_pass()``: the player may leave this impassable node once (the pass
    ends when they travel to another node).

    ``start_quest(script)``: mark that quest started (``questStarted(script)``
    holds afterwards). ``complete_quest(script)``: finish that quest
    (``completedQuest(script)`` holds; its authored reward is granted by the
    server when the quest completes). ``set_flag(X)``: ``flag(X)`` holds
    afterwards; ``clear_flag(X)`` (``unflag(X)``) removes it, and
    ``clear_flag(prefix*)`` every flag starting with ``prefix`` (reset a
    dungeon's flags). ``spend_gold(N)``: the answer's price (a bribe, a fortune);
    the server takes N gold, the simulator adds it to ``gold_spent``.
    ``teleport(Zone)`` / ``teleport(Zone,Node)``: leave for another editor
    zone (``AZ1``, ``crayburn``, ``az0-human`` ...), arriving at ``Node`` (or
    the zone's start). ``encounter(SceneGuid[,ConversationGuid])``: start
    that battle at the conversation's node (a sparring offer accepted),
    optionally showing the supplied conversation as an encounter mod when
    battle setup begins; its result is an ``encounter_result`` there and
    ``outcome:`` tags read it.
    ``complete_step(Quest,Step)``: finish the quest's current step when its
    id is ``Step`` (an encounter's or conversation's answer completes a
    step); any other step is left alone.
    """
    action, condition = split_action_condition(action)
    if condition is not None:
        if not condition:
            raise RulesError(f"'if' needs at least one tag: {action!r}")
        for tag in condition:
            parse_tag(tag)              # RulesError for an unreadable tag
        parsed = parse_action(action)
        parsed["if"] = condition
        return parsed
    match = _ACTION.match(str(action or ""))
    if match and match.group(1).lower() in ACTION_ALIASES:
        match = _ACTION.match(
            ACTION_ALIASES[match.group(1).lower()] + "(" + match.group(2) + ")")
    if not match or match.group(1).lower() not in ACTION_NAMES:
        raise RulesError(f"unknown action {action!r}")
    if match.group(1).lower() == "let_pass":
        if match.group(2).strip():
            raise RulesError(f"let_pass takes no arguments: {action!r}")
        return {"let_pass": True}
    if match.group(1).lower() == "set_counter":
        parts = [part.strip() for part in match.group(2).split(",")]
        try:
            value = int(parts[1])
        except (IndexError, ValueError):
            value = None
        if len(parts) != 2 or not parts[0] or value is None or value < 0:
            raise RulesError(f"set_counter needs a counter and a whole number >= 0: {action!r}")
        return {"set_counter": (parts[0], value)}
    if match.group(1).lower() == "move_counter":
        parts = [part.strip() for part in match.group(2).split(",")]
        if len(parts) != 2 or not all(parts):
            raise RulesError(f"move_counter needs a source and a target counter: {action!r}")
        return {"move_counter": tuple(parts)}
    if match.group(1).lower() == "clear_counter":
        name = match.group(2).strip()
        if not name or "," in name:
            raise RulesError(f"clear_counter needs one counter name: {action!r}")
        return {"clear_counter": name}
    if match.group(1).lower() == "spend_gold":
        try:
            amount = int(match.group(2).strip())
        except ValueError:
            amount = 0
        if amount <= 0:
            raise RulesError(f"spend_gold needs a whole number > 0: {action!r}")
        return {"spend_gold": amount}
    if match.group(1).lower() == "complete_step":
        parts = [part.strip() for part in match.group(2).split(",")]
        if len(parts) != 2 or not all(parts):
            raise RulesError(f"complete_step needs a quest and a step: {action!r}")
        return {"complete_step": tuple(parts)}
    if match.group(1).lower() in ("complete_quest", "start_quest", "set_flag",
                                  "clear_flag"):
        value = match.group(2).strip()
        what = ("flag" if match.group(1).lower() in ("set_flag", "clear_flag")
                else "quest script")
        if not value or "," in value:
            raise RulesError(
                f"{match.group(1).lower()} needs one {what}: {action!r}")
        return {match.group(1).lower(): value}
    if match.group(1).lower() == "encounter":
        parts = [part.strip() for part in match.group(2).split(",")]
        if len(parts) > 2:
            raise RulesError(
                "encounter accepts a scene and optional conversation: "
                f"{action!r}")
        scene = parts[0]
        if not scene:
            raise RulesError(f"encounter needs a scene: {action!r}")
        encounter = {"scene": scene}
        if len(parts) == 2:
            if not parts[1]:
                raise RulesError(
                    f"encounter conversation cannot be empty: {action!r}")
            encounter["conversation_id"] = parts[1]
        return {"encounter": encounter}
    zone, _, node = (part.strip() for part in match.group(2).partition(","))
    if not zone:
        raise RulesError(f"teleport needs a zone: {action!r}")
    return {"teleport": {"zone": zone, "node": node}}


def ending_actions(actions: Any, ending: str = "") -> list[str]:
    """Actions for a finished conversation: ``*`` (any ending) first, then
    the chosen ending's."""
    if not isinstance(actions, dict):
        return []
    found = list(actions.get("*") or ())
    found += list(actions.get(ending) or ()) if ending else []
    return list(dict.fromkeys(found))


MOD_CHOICE_TRIGGERS = ("pre", "victory", "defeat")


def scene_mod_choices(mods: Any) -> list[dict[str, Any]]:
    """The pre-battle card options a scene's ``mods_json`` offers.

    A mod entry with ``"choice": true`` lists cards the player picks one of
    (the client's card-selector battle panel). Each option is
    ``{"guid", "when"?, "pre"?, "victory"?, "defeat"?}``: offered while its
    ``when`` tags hold; its flag actions run when it was the one chosen.
    """
    options, seen = [], set()
    for entry in mods if isinstance(mods, list) else ():
        if not isinstance(entry, dict) or not entry.get("choice"):
            continue
        for card in entry.get("mods") or ():
            guid = str((card or {}).get("guid") or "").strip().lower()
            if not guid or guid in seen:
                continue
            seen.add(guid)
            option = {"guid": guid}
            if card.get("when"):
                option["when"] = card["when"]
            for trigger in MOD_CHOICE_TRIGGERS:
                if card.get(trigger):
                    option[trigger] = list(card[trigger])
            options.append(option)
    return options


def offered_mod_choices(options: Iterable[dict[str, Any]], holds) -> list[str]:
    """GUIDs of the options whose ``when`` holds (``holds(tags) -> bool``).
    Empty means the battle starts as a normal encounter."""
    return [option["guid"] for option in options or ()
            if not option.get("when") or holds(option["when"])]


def chosen_scene_mods(mods: Any, chosen: str | None) -> list[dict[str, Any]]:
    """``mods_json`` as battle setup applies it: a choice entry keeps only
    the chosen card (as a plain mod), or is dropped when none of its cards
    was chosen."""
    chosen = str(chosen or "").strip().lower()
    applied = []
    for entry in mods if isinstance(mods, list) else ():
        if not isinstance(entry, dict):
            continue
        if not entry.get("choice"):
            applied.append(entry)
            continue
        picked = [{"guid": chosen} for card in entry.get("mods") or ()
                  if chosen and str((card or {}).get("guid") or "").lower() == chosen]
        if picked:
            item = {k: v for k, v in entry.items() if k not in ("choice", "mods")}
            item["mods"] = picked[:1]
            applied.append(item)
    return applied


def is_groups(tags: Any) -> bool:
    """``[[...], [...]]``: "or" groups of tag lists (any group may hold)."""
    return isinstance(tags, list) and bool(tags) and all(
        isinstance(group, list) for group in tags)


def _clean(tags) -> list[str]:
    return [str(t).strip() for t in tags or () if str(t).strip()]


def trigger_tags(trigger: Any) -> list:
    """Editor tags on a conversation's ``trigger_json``: a tag list, or
    "or" groups (a list of tag lists)."""
    tags = (trigger or {}).get("tags") if isinstance(trigger, dict) else None
    if is_groups(tags):
        return [_clean(group) for group in tags]
    return _clean(tags if isinstance(tags, list) else ())


def split_outcome(tags: Any) -> tuple[str, list]:
    """``(outcome, other tags)``: a plain ``outcome:success|fail`` tag makes a
    conversation an encounter-result conversation; the rest select it. With
    "or" groups the outcome must be the same in every group."""
    if is_groups(tags):
        parts = [split_outcome(group) for group in tags]
        outcomes = {outcome for outcome, _rest in parts}
        if len(outcomes) == 1 and "" not in outcomes:
            return outcomes.pop(), [rest for _outcome, rest in parts]
        return "", tags
    outcome, rest = "", []
    for tag in tags or ():
        condition = parse_tag(tag)
        if "outcome" in condition and not outcome:
            outcome = condition["outcome"]
        else:
            rest.append(tag)
    return outcome, rest


def tags_condition(tags: Any, node: str = "") -> dict[str, Any]:
    """A tag list -> ``{"all": [...]}``; "or" groups -> ``{"any": [...]}``.
    An empty list always holds."""
    if is_groups(tags):
        return {"any": [tags_condition(group, node) for group in tags]}
    parts = [parse_tag(tag, node) for tag in tags or ()]
    return parts[0] if len(parts) == 1 else {"all": parts}


MAX_GROUPS = 16


def _leaf_tags(condition) -> list[str] | None:
    """Tags for one condition with no ``all``/``any``/``not`` (``None`` if
    it has no tag form)."""
    if "flag" in condition:
        flag = condition["flag"]
        return [flag if flag.startswith("conversation(") else f"flag({flag})"]
    if "faction" in condition:
        return [f"faction({condition['faction']})"]
    if "race" in condition:
        return [f"ChampionIsRace({condition['race']})"]
    if "outcome" in condition:
        return [f"outcome:{condition['outcome']}"]
    if "visited" in condition:
        return [f"visited({condition['visited']})"]
    if "completed" in condition:
        return [f"completed({condition['completed']})"]
    if "talked" in condition:
        node, count = condition["talked"]
        return [f"talkedAt({node})" if int(count) == 1 else f"talkedAt({node},{count})"]
    if "counter" in condition:
        name = condition["counter"]
        count = int(condition.get("at_least", 1))
        return [f"counter({name})" if count == 1 else f"counter({name},{count})"]
    if "keyword" in condition:
        return [f"keyword({condition['keyword']})"]
    if "came_from" in condition:
        return [f"cameFrom({condition['came_from']})"]
    if "quest" in condition:
        quest, wanted = condition["quest"], condition.get("is", "started")
        tags = {"finished": [f"completedQuest({quest})"],
                "active": [f"questActive({quest})"],
                "started": [f"questStarted({quest})"],
                "not_started": [f"!questStarted({quest})"]}.get(wanted)
        if tags is None:
            return None
        if "step_at_least" in condition:
            step = int(condition["step_at_least"])
            tags = ([f"questActive({quest})"] if wanted == "active" else []) + (
                [f"completedQuest({quest},{step})"] if step > 0 else
                [f"questStarted({quest})"])
        if "step_below" in condition:
            tags = [f"questActive({quest})",
                    f"!completedQuest({quest},{int(condition['step_below'])})"]
        return tags
    return None


def _negate_tag(tag: str) -> str:
    tag = tag.strip()
    if tag.startswith("!"):
        return tag[1:].strip()
    if tag.lower().startswith("not "):
        return tag[4:].strip()
    return f"!{tag}"


def _dnf(condition, negated=False) -> list[list[str]] | None:
    """Disjunctive normal form: a list of "or" groups, each a tag list."""
    if condition in (None, {}, True, []):
        return [[]] if not negated else None
    if isinstance(condition, list):
        condition = tags_condition(condition)
    if not isinstance(condition, dict):
        return None
    if "not" in condition and len(condition) == 1:
        return _dnf(condition["not"], not negated)
    combine_all = ("all" in condition) != negated
    children = condition.get("all") if "all" in condition else condition.get("any")
    if children is not None and len(condition) == 1:
        parts = [_dnf(child, negated) for child in children]
        if any(part is None for part in parts):
            return None
        if not combine_all:            # or: concatenate the groups
            return [group for part in parts for group in part]
        groups = [[]]                  # and: cross product of the groups
        for part in parts:
            groups = [left + right for left in groups for right in part]
            if len(groups) > MAX_GROUPS:
                return None
        return groups
    tags = _leaf_tags(condition)
    if tags is None:
        return None
    if not negated:
        return [tags]
    return [[_negate_tag(tag)] for tag in tags]   # not(a and b) = !a or !b


def condition_tags(condition: Any) -> list | None:
    """The tag form of ``condition``: a tag list, "or" groups when it needs
    ``any``, or ``None`` when it has no tag form (unknown leaves, or more
    than ``MAX_GROUPS`` groups)."""
    groups = _dnf(condition)
    if groups is None:
        return None
    unique = []
    for group in groups:
        group = list(dict.fromkeys(group))
        if group not in unique:
            unique.append(group)
    return unique[0] if len(unique) == 1 else unique


# ---------------------------------------------------------------- conditions

def _quest(state, script):
    entry = (state.get("quests") or {}).get(script) or {}
    return entry.get("status", "not_started"), int(entry.get("step", 0) or 0)


def _node(state, node):
    return (state.get("nodes") or {}).get(node) or {}


def _same_node(left: Any, right: Any) -> bool:
    """Node ids compare without their zero padding (NodeR == Node00R)."""
    def key(value):
        text = str(value or "").strip()
        match = re.fullmatch(r"(?i)node0*([0-9A-Z]+)", text)
        return match.group(1).upper() if match else text.upper()
    return bool(left) and key(left) == key(right)


def evaluate(condition: Any, state: dict[str, Any], node: str = "") -> bool:
    """Evaluate one condition; ``None``/``{}``/``[]`` means always.

    ``node`` is the node the condition belongs to (``outcome`` reads it).
    """
    if condition in (None, {}, [], True):
        return True
    if condition is False:
        return False
    if isinstance(condition, list):
        condition = tags_condition(condition, node)
    if not isinstance(condition, dict):
        raise RulesError(f"condition must be an object: {condition!r}")
    if "all" in condition:
        return all(evaluate(c, state, node) for c in condition["all"])
    if "any" in condition:
        return any(evaluate(c, state, node) for c in condition["any"])
    if "not" in condition:
        return not evaluate(condition["not"], state, node)
    if "outcome" in condition:
        where = condition.get("node") or node or state.get("current") or ""
        return _node(state, where).get("outcome") == condition["outcome"]
    if "flag" in condition:
        # ``prefix*`` holds when any set flag starts with the prefix.
        pattern = condition["flag"]
        return any(flag_matches(flag, pattern) for flag in state.get("flags") or ())
    if "keyword" in condition:
        wanted = str(condition["keyword"]).casefold()
        if wanted in {name.casefold() for name in
                      _keyword_names(state.get("keywords"))}:
            return True
        # Some campaign effects persist status counters in the same counter
        # map used by the rules engine. A positive matching counter is also
        # evidence that the champion has that counter-backed status.
        for name, value in (state.get("counters") or {}).items():
            if str(name).casefold() != wanted:
                continue
            try:
                if int(value or 0) > 0:
                    return True
            except (TypeError, ValueError) as exc:
                raise RulesError("keyword counters must be integers") from exc
        return False
    if "quest" in condition:
        status, step = _quest(state, condition["quest"])
        wanted = condition.get("is", "started")
        if wanted == "started":
            ok = status in ("active", "finished")
        elif wanted in QUEST_STATUSES:
            ok = status == wanted
        else:
            raise RulesError(f"unknown quest status {wanted!r}")
        if ok and "step_at_least" in condition:
            ok = status == "finished" or step >= int(condition["step_at_least"])
        if ok and "step_below" in condition:
            ok = status == "active" and step < int(condition["step_below"])
        return ok
    if "faction" in condition:
        return str(state.get("faction") or "").lower() == \
            str(condition["faction"]).lower()
    if "race" in condition:
        return str(state.get("race") or "").lower() == \
            str(condition["race"]).lower()
    if "visited" in condition:
        return int(_node(state, condition["visited"]).get("visits", 0)) > 0
    if "completed" in condition:
        return bool(_node(state, condition["completed"]).get("completed"))
    if "talked" in condition:
        node, count = condition["talked"]
        return int(_node(state, node).get("talks", 0)) >= int(count)
    if "counter" in condition:
        counters = state.get("counters") or {}
        try:
            value = int(counters.get(condition["counter"], 0) or 0)
            minimum = int(condition.get("at_least", 1))
        except (TypeError, ValueError) as exc:
            raise RulesError("counter values and thresholds must be integers") from exc
        return value >= minimum
    if "came_from" in condition:
        return _same_node(state.get("came_from"), condition["came_from"])
    if "visits_at_least" in condition:
        node, count = condition["visits_at_least"]
        return int(_node(state, node).get("visits", 0)) >= int(count)
    raise RulesError(f"unknown condition {sorted(condition)}")


def _tag_groups(condition) -> list[list[Any]]:
    """``condition`` as "or" groups of tags: an editor tag list / groups as
    written, else its tag form (``condition_tags``), else one opaque part."""
    if condition is False:
        return [[False]]
    if isinstance(condition, list):
        if is_groups(condition):
            return [list(group) for group in condition]
        return [list(condition)]
    tags = condition_tags(condition)
    if tags is None:
        return [[condition]]
    return [list(group) for group in tags] if is_groups(tags) else [list(tags)]


def explain_when(condition: Any, state: dict[str, Any],
                 node: str = "") -> dict[str, Any]:
    """Why ``condition`` holds or not, tag by tag (the simulator's red tags).

    The condition is flattened to "or" groups of "and" tags (``_tag_groups``)
    and every tag is evaluated on its own with ``evaluate``. ``holds`` is
    ``evaluate(condition)`` itself. For a condition that does not hold,
    ``closest`` is the group with the fewest failing tags (the first on a
    tie: the cause shown first) and ``blocked_by`` lists the failing tags of
    every group, the closest group's first, without repeats. A part with no
    tag form is reported as its JSON text.
    """
    holds = evaluate(condition, state, node)
    groups = []
    for group in _tag_groups(condition):
        results = []
        for part in group:
            if isinstance(part, str):
                label = part.strip()
                try:
                    ok = evaluate([part], state, node)
                except RulesError:
                    ok = False
            else:
                label = json.dumps(part, sort_keys=True)
                ok = evaluate(part, state, node)
            results.append({"tag": label, "holds": bool(ok)})
        groups.append({"tags": results,
                       "holds": all(r["holds"] for r in results)})
    if holds:
        return {"holds": True, "groups": groups, "closest": None, "blocked_by": []}
    failing = [sum(not r["holds"] for r in g["tags"]) for g in groups]
    closest = min(range(len(groups)), key=lambda i: failing[i]) if groups else None
    order = ([closest] if closest is not None else []) + [
        i for i in range(len(groups)) if i != closest]
    blocked = []
    for i in order:
        for result in groups[i]["tags"]:
            if not result["holds"] and result["tag"] not in blocked:
                blocked.append(result["tag"])
    return {"holds": False, "groups": groups, "closest": closest,
            "blocked_by": blocked}


def explain_entries(rules: dict[str, Any], state: dict[str, Any],
                    node_id: str) -> list[dict[str, Any]]:
    """Each of a node's entries, in offer order: ``{"id", "holds",
    "blocked_by"}`` (``blocked_by`` is empty while its ``when`` holds)."""
    spec = (rules.get("nodes") or {}).get(node_id) or {}
    result = []
    for entry in spec.get("entries") or ():
        why = explain_when(entry.get("when"), state, node_id)
        result.append({"id": entry.get("id"), "holds": why["holds"],
                       "blocked_by": why["blocked_by"]})
    return result


# ---------------------------------------------------------------- node offers

def active_entry(rules: dict[str, Any], state: dict[str, Any],
                 node: str) -> dict[str, Any] | None:
    """The first node entry whose ``when`` holds: what the node offers now."""
    for entry in ((rules.get("nodes") or {}).get(node) or {}).get("entries") or ():
        if evaluate(entry.get("when"), state, node):
            return entry
    return None


GATES = ("visible", "hidden", "blocked", "completed", "retryable", "marker",
         "givequest", "turninquest", "autostart", "impassable")


def _has_gate(rules, node_id, key):
    return key in ((rules.get("nodes") or {}).get(node_id) or {})


def node_status(rules: dict[str, Any], state: dict[str, Any],
                node_id: str) -> tuple[bool, bool]:
    """Effective ``(completed, retryable)``: gates override progress."""
    spec = (rules.get("nodes") or {}).get(node_id) or {}
    progress = _node(state, node_id)
    completed = (evaluate(spec["completed"], state, node_id) if "completed" in spec
                 else bool(progress.get("completed")))
    retryable = (evaluate(spec["retryable"], state, node_id) if "retryable" in spec
                 else bool(progress.get("retryable")))
    if node_id in (rules.get("retryable_nodes") or ()):
        # A node that enters a dungeon or panorama is never finished: it can
        # always be entered again (see rules_loader), so nothing completes it.
        return False, True
    return completed, retryable and not completed


def _finish_node(rules, node, node_id, *, repeatable=False):
    """Content finished: complete it unless a completion gate decides."""
    if _has_gate(rules, node_id, "completed") or _has_gate(rules, node_id, "retryable") or repeatable:
        node.update({"completed": False, "retryable": True, "failed": False})
    else:
        node.update({"completed": True, "retryable": False, "failed": False})


# ---------------------------------------------------------------- actions

def _set_quest(state, script, status, step):
    state.setdefault("quests", {})[script] = {"status": status, "step": step}


def _effect(rules, state, effect, log, queue):
    (kind, value), = effect.items()
    if kind == "set_flag":
        flags = set(state.get("flags") or ())
        flags.add(value)
        state["flags"] = sorted(flags)
    elif kind == "clear_flag":
        state["flags"] = sorted(set(state.get("flags") or ()) - {value})
    elif kind == "increment_counter":
        if not isinstance(value, dict):
            raise RulesError("increment_counter needs name and amount")
        name = str(value.get("name") or "").strip()
        try:
            amount = int(value.get("amount"))
            current = int((state.get("counters") or {}).get(name, 0) or 0)
        except (TypeError, ValueError) as exc:
            raise RulesError("counter name, value and amount must be valid") from exc
        if not name:
            raise RulesError("increment_counter needs a counter name")
        counters = state.setdefault("counters", {})
        counters[name] = current + amount
    elif kind == "set_counter":
        name, amount = value
        state.setdefault("counters", {})[name] = int(amount)
    elif kind == "move_counter":
        source, target = value
        counters = state.setdefault("counters", {})
        moved = int(counters.get(source, 0) or 0)
        counters[source] = 0
        counters[target] = int(counters.get(target, 0) or 0) + moved
    elif kind == "clear_counter":
        # Reset a counter to 0 (it stays defined, so reads still see a number).
        name = str(value or "").strip()
        if not name:
            raise RulesError("clear_counter needs a counter name")
        state.setdefault("counters", {})[name] = 0
    elif kind == "start_quest":
        status, _step = _quest(state, value)
        if status != "not_started":
            return
        _set_quest(state, value, "active", 0)
        queue.append({"type": "quest_start", "quest": value})
    elif kind == "advance_quest":
        _advance_quest(rules, state, value, log, queue)
    elif kind == "complete_quest":
        status, step = _quest(state, value)
        if status == "finished":
            return
        _set_quest(state, value, "finished", step)
        queue.append({"type": "quest_complete", "quest": value})
    elif kind == "complete_step":
        script, step_id = value
        status, step = _quest(state, script)
        steps = ((rules.get("quests") or {}).get(script) or {}).get("steps") or ()
        if status == "active" and step < len(steps) and steps[step].get("id") == step_id:
            _advance_quest(rules, state, script, log, queue)
    elif kind == "complete_node":
        node = state.setdefault("nodes", {}).setdefault(value, {})
        node.update({"completed": True, "retryable": False, "failed": False})
    elif kind == "reset_node":
        node = state.setdefault("nodes", {}).setdefault(value, {})
        node.update({"completed": False, "retryable": False, "failed": False})
    else:
        raise RulesError(f"unknown effect {kind!r}")
    log.append({"effect": effect})


def _advance_quest(rules, state, script, log, queue):
    status, step = _quest(state, script)
    if status != "active":
        return
    steps = ((rules.get("quests") or {}).get(script) or {}).get("steps") or ()
    if step + 1 >= len(steps):
        _set_quest(state, script, "finished", step)
        queue.append({"type": "quest_complete", "quest": script})
    else:
        _set_quest(state, script, "active", step + 1)
        queue.append({"type": "quest_step", "quest": script, "step": step + 1})


def _matches(trigger: dict[str, Any], event: dict[str, Any]) -> bool:
    """A hook ``on`` matches when every field it names equals the event's.
    A list in the trigger matches any of its values (a quest step finished
    by either faction's conversation)."""
    if trigger.get("event") != event.get("type"):
        return False
    for key, wanted in trigger.items():
        if key == "event":
            continue
        options = wanted if isinstance(wanted, (list, tuple)) else (wanted,)
        actual = event.get(key)
        if isinstance(actual, (list, tuple, set)):
            if not any(option in actual for option in options):
                return False
        elif not any(str(actual or "").lower() == str(option).lower()
                     for option in options):
            return False
    return True


def _quest_step_events(rules, state, event):
    """Advance active quests whose current step this event satisfies."""
    advanced = []
    for script, spec in (rules.get("quests") or {}).items():
        status, step = _quest(state, script)
        steps = spec.get("steps") or ()
        if status != "active" or step >= len(steps):
            continue
        done_when = steps[step].get("done_when")
        if done_when and _matches(done_when, event) and evaluate(
                steps[step].get("requires"), state):
            advanced.append(script)
    return advanced


def _run_actions(state, actions, log, node="", rules=None):
    """Ending actions; a teleport is left in ``state["teleport"]`` for the
    caller (it changes zone, which is outside this zone's rules), a started
    battle in ``state["encounter"]`` until its ``encounter_result``."""
    for action in actions or ():
        parsed = parse_action(action)
        if parsed.get("if") and not evaluate(parsed["if"], state, node):
            continue
        if parsed.get("let_pass") and node:
            state["pass"] = node
        if "teleport" in parsed:
            state["teleport"] = parsed["teleport"]
        if "encounter" in parsed:
            state["encounter"] = dict(parsed["encounter"], node=node)
            if rules is not None:
                _dispatch(rules, state, {"type": "encounter_start", "node": node,
                                         "scene": parsed["encounter"]["scene"]}, log)
        if parsed.get("clear_counter"):
            counters = state.setdefault("counters", {})
            counters[parsed["clear_counter"]] = 0
        for kind in ("set_counter", "move_counter"):
            if kind in parsed:
                _effect(rules or {}, state, {kind: parsed[kind]}, log, [])
        if "spend_gold" in parsed:
            state["gold_spent"] = int(state.get("gold_spent", 0) or 0) + parsed["spend_gold"]
        if parsed.get("clear_flag"):
            state["flags"] = sorted(f for f in state.get("flags") or ()
                                    if not flag_matches(f, parsed["clear_flag"]))
        if "complete_step" in parsed:
            queue: list[dict[str, Any]] = []
            _effect(rules or {}, state, {"complete_step": parsed["complete_step"]},
                    log, queue)
            for event in queue:
                _dispatch(rules or {}, state, event, log)
        for kind in ("start_quest", "complete_quest", "set_flag"):
            if kind in parsed:
                queue: list[dict[str, Any]] = []
                _effect(rules or {}, state, {kind: parsed[kind]}, log, queue)
                for event in queue:       # quest_start / quest_complete hooks
                    _dispatch(rules or {}, state, event, log)
        log.append({"action": action})


def impassable_now(rules: dict[str, Any], state: dict[str, Any],
                   node_id: str) -> bool:
    """The node's ``impassable`` gate holds: it is not completed and no
    ``let_pass()`` is letting the player leave it on this visit."""
    spec = (rules.get("nodes") or {}).get(node_id) or {}
    if "impassable" not in spec or state.get("pass") == node_id:
        return False
    completed, _retryable = node_status(rules, state, node_id)
    return not completed and evaluate(spec["impassable"], state, node_id)


def _walk_back_if_held(rules, state, node_id, log):
    """The client cannot leave an impassable node: when a conversation (or a
    lost battle) ends there without a battle started or a pass granted, the
    server walks the player back to the node they came from."""
    started = state.get("encounter") or {}
    if (not node_id or state.get("current") != node_id or
            started.get("node") == node_id or
            not impassable_now(rules, state, node_id)):
        return
    back = state.get("came_from")
    if back and back != node_id:
        state["current"] = back
        state["came_from"] = node_id
        log.append({"walked_back": back, "from": node_id})


def _dispatch(rules, state, event, log):
    """Run hooks and quest-step checks for one event, then its cascade."""
    queue = [event]
    processed = 0
    while queue:
        processed += 1
        if processed > MAX_CASCADE:
            raise RulesError("hook cascade exceeded %d events" % MAX_CASCADE)
        current = queue.pop(0)
        log.append({"event": current})
        for hook in rules.get("hooks") or ():
            if _matches(hook.get("on") or {}, current) and evaluate(
                    hook.get("when"), state):
                log.append({"hook": hook.get("id", "")})
                for effect in hook.get("do") or ():
                    _effect(rules, state, effect, log, queue)
        for script in _quest_step_events(rules, state, current):
            _advance_quest(rules, state, script, log, queue)
            log.append({"quest_step": script})


def count_picker(rules: dict[str, Any], state: dict[str, Any],
                 node: str) -> dict[str, Any] | None:
    """The count picker ``node`` offers now, or None.

    ``{"from", "to", "available"}``: the player picks 0..``available`` (the
    ``from`` counter) to move into ``to`` (Gnomes Come Home: the gnomes still
    lost -> the gnomes carried). Offered only while ``from`` is at least 1.
    """
    spec = (rules.get("count_pickers") or {}).get(node)
    if not isinstance(spec, dict) or not spec.get("from") or not spec.get("to"):
        return None
    try:
        available = int((state.get("counters") or {}).get(spec["from"], 0) or 0)
    except (TypeError, ValueError):
        return None
    if available < 1:
        return None
    return {"from": str(spec["from"]), "to": str(spec["to"]), "available": available}


def apply(rules: dict[str, Any], state: dict[str, Any],
          action: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply one player action; returns ``(new_state, log)``."""
    state = copy.deepcopy(state)
    state.pop("teleport", None)      # reported once, by the action that set it
    log: list[dict[str, Any]] = []
    kind = action.get("type")
    node_id = action.get("node", "")
    nodes = state.setdefault("nodes", {})
    if kind == "visit":
        if not node_id:
            raise RulesError("visit needs a node")
        previous = state.get("current")
        node = nodes.setdefault(node_id, {})
        node["visits"] = int(node.get("visits", 0)) + 1
        state["current"] = node_id
        if previous and previous != node_id:
            state["came_from"] = previous      # cameFrom(Node)
            if state.get("pass") and state["pass"] != node_id:
                state.pop("pass", None)        # a let_pass() lasts one visit
        if previous and previous != node_id:
            path = path_id(rules, previous, node_id)
            if path and path not in state.setdefault("visited_paths", []):
                state["visited_paths"].append(path)
        entry = active_entry(rules, state, node_id)
        if (entry is None or entry.get("kind") == "empty") and not \
                _has_gate(rules, node_id, "completed"):
            # Nothing to resolve here: arriving completes the location.
            node.update({"completed": True, "retryable": False})
        _dispatch(rules, state, {"type": "visit", "node": node_id}, log)
    elif kind == "conversation_end":
        entry = active_entry(rules, state, node_id) if node_id else None
        wanted = action.get("conversation")
        if wanted and (entry is None or entry.get("conversation") != wanted):
            # A conversation chosen directly (not the node's current offer):
            # its own entry's actions and completion still apply.
            entry = next((e for e in ((rules.get("nodes") or {}).get(node_id) or {})
                          .get("entries") or () if e.get("conversation") == wanted),
                         entry)
        # Every entry for this conversation whose ``when`` held as it ended
        # runs its actions (one conversation can start several quests, e.g.
        # Tamed plus the faction quest), as the server grants every row.
        ran = [entry] if entry is not None and entry.get("conversation") == (
            action.get("conversation") or entry.get("conversation")) else []
        if not ran and wanted:
            # A conversation no offer covers (e.g. a scene's visit conversation)
            # takes its actions from the zone's conversation_actions override.
            override = ((rules.get("conversation_actions") or {}).get(str(wanted).lower())
                        or {})
            if override:
                ran = [{"actions": override, "conversation": wanted}]
        if ran:
            ran += [e for e in ((rules.get("nodes") or {}).get(node_id) or {})
                    .get("entries") or ()
                    if e is not entry and e.get("kind") == "conversation"
                    and e.get("conversation") == entry.get("conversation")
                    and e.get("actions") and evaluate(e.get("when"), state, node_id)]
        for name in action.get("events") or ():
            _dispatch(rules, state, {"type": "script_event", "name": name,
                                     "node": node_id}, log)
        ending = str(action.get("ending") or "")
        done: set[str] = set()
        for each in ran:
            todo = [a for a in ending_actions(each.get("actions"), ending)
                    if a not in done]
            done.update(todo)
            _run_actions(state, todo, log, node_id, rules)
        if action.get("result_of"):
            # A battle's victory conversation (``encounter_result`` with
            # ``result_conversation``) runs its actions as it ends.
            battle = next((e for e in ((rules.get("nodes") or {}).get(node_id) or {})
                           .get("entries") or () if e.get("id") == action["result_of"]), None)
            if battle and battle.get("victory") == wanted:
                _run_actions(state, ending_actions(battle.get("victory_actions"), ending),
                             log, node_id, rules)
        name = action.get("name") or action.get("conversation") or (
            entry or {}).get("conversation", "")
        if name:
            for flag in (conversation_flag(name), conversation_flag(
                    name, str(action.get("ending") or "").strip())):
                _effect(rules, state, {"set_flag": flag}, log, [])
        if node_id:   # the server's conversation_visits
            talked = nodes.setdefault(node_id, {})
            talked["talks"] = int(talked.get("talks", 0)) + 1
            parent = rules.get("parent_node")
            if parent and parent != node_id:
                # A panorama's talks also count at the map node it hangs off
                # (``talkedAt(Node017)`` tags refer to that node).
                at_parent = nodes.setdefault(parent, {})
                at_parent["talks"] = int(at_parent.get("talks", 0)) + 1
        # A conversation whose ending starts a battle (encounter(...)) does not
        # finish its node: the battle's result does, as its victory completes it.
        started_battle = (state.get("encounter") or {}).get("node") == node_id
        if entry is not None and entry.get("kind") == "conversation" and not \
                started_battle and (
                not action.get("conversation") or
                entry.get("conversation") == action.get("conversation")):
            _finish_node(rules, nodes.setdefault(node_id, {}), node_id,
                         repeatable=bool(entry.get("repeatable")))
        _dispatch(rules, state, {
            "type": "conversation_end", "node": node_id,
            "conversation": action.get("conversation") or (
                entry or {}).get("conversation", "")}, log)
        _walk_back_if_held(rules, state, node_id, log)
    elif kind == "encounter_result":
        entry = active_entry(rules, state, node_id)
        started = state.get("encounter") or {}
        if started.get("node") == node_id:
            # A battle a conversation started: the node's content is that
            # conversation, so the result only records the outcome.
            entry = {"scene": started["scene"],
                     "repeatable": bool(rules.get("battles_repeat"))}
        scene = action.get("scene") or (entry or {}).get("scene", "")
        # The card picked on the selector panel (a "choice" scene mod);
        # validated against what was on offer before the battle.
        chosen = str(action.get("mod") or "").strip().lower()
        option = None
        if chosen:
            if chosen not in mod_choices_for(rules, state, scene, node_id):
                raise RulesError(f"mod {chosen} is not offered for scene {scene}")
            option = next(o for o in (rules.get("mod_choices") or {}).get(
                str(scene).lower()) or () if o["guid"] == chosen)
        state.pop("encounter", None)
        if started.get("node") != node_id:       # a battle the node itself offers
            _dispatch(rules, state, {"type": "encounter_start", "node": node_id,
                                     "scene": scene}, log)
        victory = bool(action.get("victory"))
        if option:
            _run_actions(state, option.get("pre"), log, node_id, rules)
            _run_actions(state, option.get("victory" if victory else "defeat"),
                         log, node_id, rules)
        events = list(action.get("events") or ())
        node = nodes.setdefault(node_id, {})
        node["outcome"] = "success" if victory else "fail"
        result_event = {"type": "encounter_victory" if victory else
                        "encounter_defeat", "node": node_id, "scene": scene,
                        "events": events, "mod": chosen}
        if victory:
            # An overworld battle stays replayable after a win (the client shows
            # it as Repeatable) unless a completion gate, such as a taming flag,
            # decides otherwise. A dungeon battle completes when won.
            node["won"] = True
            _finish_node(rules, node, node_id,
                         repeatable=bool(rules.get("battles_repeat")) or bool(
                             (entry or {}).get("repeatable")))
            # The victory conversation's actions (a won final battle may
            # teleport the player out of the dungeon). The live server plays
            # that conversation and runs them when it ends
            # (``result_conversation``); the simulator runs them here.
            if not action.get("result_conversation"):
                _run_actions(state, ending_actions((entry or {}).get("victory_actions")),
                             log, node_id, rules)
        else:
            # A loss can be retried only once the node has had a win; otherwise
            # losing every fight would walk the player through the overworld.
            node.update({"failed": True, "retryable": bool(node.get("won"))})
        # Hooks run after the result so a completion gate can read the flags
        # they set (taming sets its flag on the same victory).
        _dispatch(rules, state, result_event, log)
        if not victory:
            _walk_back_if_held(rules, state, node_id, log)
    elif kind == "shroom_choice":
        # A Shroomkin Haus card was taken: the node is done (one-time).
        entry = active_entry(rules, state, node_id)
        if entry is None or entry.get("kind") != "shroomhaus":
            raise RulesError(f"{node_id} is not a Shroomkin Haus")
        if str(action.get("card") or "").lower() not in {
                str(c).lower() for c in entry.get("choices") or ()}:
            raise RulesError(f"card {action.get('card')} is not offered at {node_id}")
        nodes.setdefault(node_id, {}).update(
            {"completed": True, "retryable": False, "failed": False})
        _dispatch(rules, state, {"type": "shroom_choice", "node": node_id,
                                 "card": str(action.get("card"))}, log)
    elif kind == "count_choice":
        picker = count_picker(rules, state, node_id)
        if picker is None:
            raise RulesError(f"{node_id} offers no count picker now")
        count = action.get("count")
        if isinstance(count, bool) or not isinstance(count, int):
            raise RulesError(f"count must be a whole number: {count!r}")
        if not 0 <= count <= picker["available"]:
            raise RulesError(f"count {count} is outside 0..{picker['available']}")
        counters = state.setdefault("counters", {})
        counters[picker["from"]] = picker["available"] - count
        counters[picker["to"]] = int(counters.get(picker["to"], 0) or 0) + count
        _dispatch(rules, state, {"type": "count_choice", "node": node_id,
                                 "count": count}, log)
    elif kind == "script_event":
        _dispatch(rules, state, {"type": "script_event",
                                 "name": action.get("name", ""),
                                 "node": node_id}, log)
    elif kind == "set_flag":
        _effect(rules, state, {"set_flag": action["flag"]}, log, [])
    elif kind == "clear_flag":
        _effect(rules, state, {"clear_flag": action["flag"]}, log, [])
    elif kind == "set_keyword":
        name = str(action.get("keyword") or "").strip()
        if not name:
            raise RulesError("set_keyword needs a name")
        names = {value.casefold(): value for value in
                 _keyword_names(state.get("keywords"))}
        if action.get("present", True):
            names[name.casefold()] = name
        else:
            names.pop(name.casefold(), None)
        state["keywords"] = sorted(names.values(), key=str.casefold)
    elif kind == "set_quest":
        status = action.get("status", "active")
        if status not in QUEST_STATUSES:
            raise RulesError(f"unknown quest status {status!r}")
        before, _ = _quest(state, action["quest"])
        _set_quest(state, action["quest"], status, int(action.get("step", 0)))
        if status == "active" and before == "not_started":
            _dispatch(rules, state, {"type": "quest_start",
                                     "quest": action["quest"]}, log)
    elif kind == "set_node":
        # Editor: set node progress fields directly.
        allowed = {"visits", "talks", "completed", "failed", "retryable", "outcome"}
        values = {k: v for k, v in (action.get("values") or {}).items() if k in allowed}
        node = nodes.setdefault(node_id, {})
        for key, value in values.items():
            if value is None:
                node.pop(key, None)
            else:
                node[key] = value
    elif kind == "clear_node":
        nodes.pop(node_id, None)
        state["visited_paths"] = [p for p in state.get("visited_paths") or ()
                                  if node_id not in p.split("_")[1:]]
    elif kind == "clear_encounter":
        state.pop("encounter", None)
    elif kind == "set_current":
        # Editor: move the champion. ``visited`` also counts the node as
        # visited (its paths open), without playing its content.
        state["current"] = node_id
        if action.get("visited") and node_id:
            node = nodes.setdefault(node_id, {})
            node["visits"] = max(1, int(node.get("visits", 0) or 0))
    elif kind == "set_faction":
        state["faction"] = action.get("faction", "")
        if "race" in action:
            state["race"] = action.get("race", "")
    else:
        raise RulesError(f"unknown action {kind!r}")
    return state, log


# ---------------------------------------------------------------- layout

def path_id(rules, a, b):
    for path in (rules.get("map") or {}).get("paths") or ():
        if {path["from"], path["to"]} == {a, b}:
            return path["id"]
    return ""


def _neighbours(rules) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for path in (rules.get("map") or {}).get("paths") or ():
        result.setdefault(path["from"], set()).add(path["to"])
        result.setdefault(path["to"], set()).add(path["from"])
    return result


def _opens_neighbours(rules, state, node_id) -> bool:
    """Arrival at an unfinished action node does not draw its paths."""
    completed, retryable = node_status(rules, state, node_id)
    if completed or state.get("pass") == node_id:
        return True         # finished, or let_pass(): the player may leave
    if impassable_now(rules, state, node_id):
        return False        # the client's token stops at an impassable node
    if retryable:
        return True
    entry = active_entry(rules, state, node_id)
    return entry is None or entry.get("kind") == "empty"


_ENTRY_CONVERSATIONS = ("conversation", "pre", "victory", "defeat")


def offered_conversations(rules: dict[str, Any], state: dict[str, Any],
                          node_id: str) -> tuple[list[str], list[str]]:
    """``(now, holding)`` conversation GUIDs for a node: what its current
    offer plays, and every conversation whose offer condition holds."""
    spec = (rules.get("nodes") or {}).get(node_id) or {}
    now, holding = [], []
    first = True
    for entry in spec.get("entries") or ():
        if not evaluate(entry.get("when"), state, node_id):
            continue
        found = [entry[k] for k in _ENTRY_CONVERSATIONS if entry.get(k)]
        found += [r["conversation"] for r in entry.get("results") or ()
                  if r.get("conversation") and evaluate(r.get("when"), state, node_id)]
        if first:
            now, first = list(found), False
        holding += [g for g in found if g not in holding]
    return now, holding


def mod_choices_for(rules: dict[str, Any], state: dict[str, Any], scene: str,
                    node_id: str = "") -> list[str]:
    """The card options the selector panel offers for ``scene`` now
    (``rules["mod_choices"]``: scene GUID -> ``scene_mod_choices`` list)."""
    options = (rules.get("mod_choices") or {}).get(str(scene or "").lower())
    return offered_mod_choices(
        options, lambda tags: evaluate(tags, state, node_id)) if options else []


def _battle_scene(state, node_id, entry):
    """The scene a battle at ``node_id`` would launch: one a conversation
    started there, else the node's active encounter entry."""
    started = state.get("encounter") or {}
    if started.get("node") == node_id:
        return started.get("scene")
    return entry.get("scene") if entry and entry.get("kind") == "encounter" else None


def layout(rules: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Derive the client-visible map for ``state``."""
    node_rules = rules.get("nodes") or {}
    neighbours = _neighbours(rules)
    forks = {n for n in neighbours if n.startswith("Fork")}
    visited = {n for n, v in (state.get("nodes") or {}).items()
               if int(v.get("visits", 0)) > 0}
    if state.get("current"):
        visited.add(state["current"])

    def gate(node_id, key):
        spec = node_rules.get(node_id) or {}
        return key in spec and evaluate(spec[key], state, node_id)

    blocked = {n for n in node_rules if gate(n, "blocked")}
    hidden = {n for n in node_rules if gate(n, "hidden")}
    forced = {n for n in node_rules if gate(n, "visible")}
    markers = {n for n in node_rules if gate(n, "marker")}
    flags_of = {key: {n for n in node_rules if gate(n, key)}
                for key in ("givequest", "turninquest", "autostart")}
    impassable = {n for n in node_rules if impassable_now(rules, state, n)}
    # Paths out of a node are drawn only once it no longer gates travel.
    revealed = set(visited)
    for node_id in visited:
        if _opens_neighbours(rules, state, node_id):
            revealed |= {n for n in neighbours.get(node_id, ())
                         if not (n in blocked and n not in visited)}
    all_nodes = set(neighbours) | set(node_rules)
    nodes = {}
    for node_id in sorted(all_nodes):
        progress = _node(state, node_id)
        completed, retryable = node_status(rules, state, node_id)
        if node_id in forks:
            visible = True
        else:
            visible = (node_id in revealed or node_id in forced or
                       bool(progress.get("completed")))
            if node_id in blocked and node_id not in visited:
                visible = False
            if node_id in hidden:
                visible = False
        entry = active_entry(rules, state, node_id)
        offered_now, offered = offered_conversations(rules, state, node_id)
        give_marker = bool(entry and entry.get("marker") == "give")
        turnin_marker = bool(entry and entry.get("marker") == "turnin")
        if rules.get("npc_zone") and node_id in node_rules:
            # A panorama's NPCs: shown while they offer something, never
            # "completed" (the client hides a completed NPC), and the offer
            # itself marks a quest-giver ("!") or a turn-in ("?").
            visible = node_id in forced or entry is not None
            if node_id in hidden:
                visible = False
            completed, retryable = False, entry is not None
        nodes[node_id] = {
            "visible": visible,
            "enabled": visible and node_id not in forks,
            "blocked": node_id in blocked,
            "hidden": node_id in hidden,
            "marker": node_id in markers,
            **{key: node_id in nodes_with for key, nodes_with in flags_of.items()},
            "givequest": node_id in flags_of["givequest"] or give_marker,
            "turninquest": node_id in flags_of["turninquest"] or turnin_marker,
            "visited": node_id in visited,
            "completed": completed,
            "retryable": retryable,
            # Drawn red by the client until completed (UIDungeonMapNode).
            "impassable": node_id in impassable and not completed,
            "failed": bool(progress.get("failed")),
            "entry": entry.get("id") if entry else None,
            "offered_now": offered_now, "offered": offered,
            # Card-selector options for this node's battle (none: a
            # normal encounter panel).
            "mod_choices": mod_choices_for(
                rules, state, _battle_scene(state, node_id, entry) or "", node_id),
        }
    paths = []
    for path in (rules.get("map") or {}).get("paths") or ():
        a, b = nodes.get(path["from"], {}), nodes.get(path["to"], {})
        locked = bool(a.get("visible") and b.get("visible") and not (
            (path["from"] in visited and _opens_neighbours(
                rules, state, path["from"])) or
            (path["to"] in visited and _opens_neighbours(
                rules, state, path["to"])) or
            path["from"] in forks or path["to"] in forks))
        paths.append({
            "id": path["id"],
            "shown": bool(a.get("visible") and b.get("visible")) and not locked,
            "locked": locked,
            "visited": path["id"] in (state.get("visited_paths") or ()),
        })
    return {"current": state.get("current"), "nodes": nodes, "paths": paths}


# ---------------------------------------------------------------- validation

def _conditions(value) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        value = tags_condition(value)
    if isinstance(value, dict):
        yield value
        for key in ("all", "any"):
            for child in value.get(key) or ():
                yield from _conditions(child)
        if "not" in value:
            yield from _conditions(value["not"])


def validate(rules: dict[str, Any], *, known_conversations=None,
             known_scenes=None) -> list[dict[str, str]]:
    """Structural checks plus set-but-never-read / read-but-never-set flags."""
    problems = []

    def add(level, where, text):
        problems.append({"level": level, "where": where, "text": text})

    if rules.get("schema_version") != SCHEMA_VERSION:
        add("error", "file", f"schema_version must be {SCHEMA_VERSION}")
    map_nodes = set()
    for path in (rules.get("map") or {}).get("paths") or ():
        map_nodes |= {path["from"], path["to"]}
    quests = rules.get("quests") or {}
    flags_read, flags_set = set(), set(rules.get("start", {}).get("flags") or ())
    sample = initial_state(rules) if "start" in rules else {}

    def check_condition(where, condition, node_id=""):
        if isinstance(condition, list):
            try:
                condition = tags_condition(condition, node_id)
            except RulesError as exc:
                add("error", where, str(exc))
                return
        for node in _conditions(condition):
            if "flag" in node and not node["flag"].startswith("conversation("):
                flags_read.add(node["flag"])
            if "quest" in node and node["quest"] not in quests:
                add("warn", where, f"condition names unknown quest {node['quest']!r}")
        try:
            evaluate(condition, sample, node_id)
        except RulesError as exc:
            add("error", where, str(exc))

    for node_id, spec in (rules.get("nodes") or {}).items():
        if map_nodes and node_id not in map_nodes:
            add("warn", node_id, "node is not on the map")
        for key in sorted(set(spec) - set(GATES) - {"entries", "review", "note"}):
            add("error", node_id, f"unknown node key {key!r}")
        for key in GATES:
            if key in spec:
                check_condition(f"{node_id}.{key}", spec[key], node_id)
        ids = set()
        for entry in spec.get("entries") or ():
            where = f"{node_id}.{entry.get('id', '?')}"
            if entry.get("id") in ids:
                add("error", where, "duplicate entry id")
            ids.add(entry.get("id"))
            if entry.get("kind") not in ("conversation", "encounter",
                                         "panorama", "empty", "shroomhaus"):
                add("error", where, f"unknown entry kind {entry.get('kind')!r}")
            check_condition(where, entry.get("when"), node_id)
            for key in ("actions", "victory_actions"):
                for actions in (entry.get(key) or {}).values():
                    for action in actions:
                        try:
                            parse_action(action)
                        except RulesError as exc:
                            add("error", where, str(exc))
            conversations = [entry.get(k) for k in
                             ("conversation", "pre", "victory", "defeat")
                             if entry.get(k)]
            for guid in conversations:
                if known_conversations is not None and guid not in known_conversations:
                    add("error", where, f"conversation {guid} is not in Records")
            if (entry.get("scene") and known_scenes is not None and
                    entry["scene"] not in known_scenes):
                add("error", where, f"scene {entry['scene']} is unknown")
    for index, hook in enumerate(rules.get("hooks") or ()):
        where = f"hook {hook.get('id', index)}"
        check_condition(where, hook.get("when"))
        if not (hook.get("on") or {}).get("event"):
            add("error", where, "hook needs on.event")
        for effect in hook.get("do") or ():
            if len(effect) != 1:
                add("error", where, f"effect must have one key: {effect}")
                continue
            (kind, value), = effect.items()
            if kind in ("set_flag",):
                flags_set.add(value)
            if kind == "clear_counter" and not str(value or "").strip():
                add("error", where, "clear_counter needs a counter name")
            if kind == "increment_counter":
                if not isinstance(value, dict) or not str(value.get("name") or "").strip():
                    add("error", where, "increment_counter needs a counter name")
                else:
                    try:
                        int(value.get("amount"))
                    except (TypeError, ValueError):
                        add("error", where, "increment_counter amount must be an integer")
            if kind.endswith("_quest") and value not in quests:
                add("warn", where, f"effect names unknown quest {value!r}")
    for node_id, spec in (rules.get("count_pickers") or {}).items():
        where = f"count picker {node_id}"
        if not isinstance(spec, dict) or not all(
                isinstance(spec.get(k), str) and spec[k].strip() for k in ("from", "to")):
            add("error", where, "count picker needs from and to counter names")
        elif spec["from"] == spec["to"]:
            add("error", where, "count picker from and to must differ")
    # Card-selector options read flags in ``when`` and set them in actions.
    for scene, options in (rules.get("mod_choices") or {}).items():
        for option in options or ():
            where = f"mod choice {str(scene)[:8]}:{str(option.get('guid'))[:8]}"
            check_condition(where, option.get("when"))
            for trigger in MOD_CHOICE_TRIGGERS:
                for action in option.get(trigger) or ():
                    try:
                        parsed = parse_action(action)
                    except RulesError as exc:
                        add("error", where, str(exc))
                        continue
                    if parsed.get("set_flag"):
                        flags_set.add(parsed["set_flag"])
    # Conversation endings set flags too (set_flag(X) ending actions).
    for spec in (rules.get("nodes") or {}).values():
        for entry in spec.get("entries") or ():
            for key in ("actions", "victory_actions"):
                for listed in (entry.get(key) or {}).values():
                    for action in listed or ():
                        try:
                            parsed = parse_action(action)
                        except RulesError:
                            continue
                        if parsed.get("set_flag"):
                            flags_set.add(parsed["set_flag"])
    for flag in sorted(flags_read - flags_set):
        if flag.endswith("*") and any(flag_matches(s, flag) for s in flags_set):
            continue            # a prefix read is satisfied by a flag it matches
        add("warn", "flags", f"flag {flag!r} is read but never set")
    for flag in sorted(flags_set - flags_read):
        if any(flag_matches(flag, r) for r in flags_read if r.endswith("*")):
            continue            # a prefix read covers this flag
        add("info", "flags", f"flag {flag!r} is set but never read")
    return problems

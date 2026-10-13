"""Static campaign content read from ``hex-private/Records/`` (shared by the server's
campaign runtime and the campaign editor).

Each section is parsed once per process; every cache is built under its own
lock, so the readers are thread-safe.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

CAMPAIGN_ROOT = Path(__file__).resolve().parents[1]
REPO = CAMPAIGN_ROOT.parent
RECORDS = REPO / "hex-private" / "Records"


# Counter-backed champion statuses exposed by the client. The editor still
# accepts any name, but these are useful suggestions for keyword(Name).
_CHAMPION_STATUS_KEYWORDS = ("Burning", "Dazed", "Stealth", "Vulnerable")


_LOCK = threading.Lock()


_CONVERSATION_LOCK = threading.Lock()  # separate: builds call records()


_SECTIONS: dict[str, list[dict[str, Any]]] = {}


_CONVERSATIONS: dict[str, dict[str, Any]] | None = None


def _parse_record(raw: str) -> dict[str, Any] | None:
    import sys
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    # Records lines are relaxed JSON (trailing commas, integer object keys);
    # the seed pipeline's parser handles both. Use PyYAML's C implementation
    # for the uncommon YAML fallback; ConversationTemplate is large enough
    # that the pure-Python loader dominates a cold editor load.
    from AssetExtraction.gamedata_seed import parse_record, yaml
    cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
    try:
        value = json.loads(cleaned)
    except (TypeError, json.JSONDecodeError):
        loader = getattr(yaml, "CSafeLoader", None) if yaml is not None else None
        if loader is None:
            return parse_record(raw)
        try:
            value = yaml.load(cleaned, Loader=loader)
        except Exception:
            return parse_record(raw)
    # Preserve the seed parser's support for JSON or YAML encoded inside a
    # quoted string, and its handling of non-object records.
    if isinstance(value, str):
        return parse_record(raw)
    return value if isinstance(value, dict) else None


def records(section: str) -> list[dict[str, Any]]:
    """All parsed records of one Records section, loaded once per process."""
    with _LOCK:
        cached = _SECTIONS.get(section)
        if cached is not None:
            return cached
        parsed = []
        path = RECORDS / f"{section}.jsonl"
        with path.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index == 0 or not line.strip():
                    continue  # section marker
                value = json.loads(line)
                record = _parse_record(value if isinstance(value, str) else line)
                if record:
                    parsed.append(record)
        _SECTIONS[section] = parsed
        return parsed


def champion_status_keywords() -> list[str]:
    """Known counter-backed champion statuses, confirmed by game records."""
    available = {str(record.get("m_Name") or "").strip()
                 for record in records("CardCounterTemplate")}
    return [name for name in _CHAMPION_STATUS_KEYWORDS if name in available]


def _guid(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("m_Guid")
    return str(value or "").lower()


def _short_type(value: Any) -> str:
    return str(value or "").rsplit(".", 1)[-1]


def champion_names() -> dict[str, str]:
    return {_guid(r.get("m_Id")): str(r.get("m_Name") or "").strip()
            for r in records("ChampionTemplate") if r.get("m_Name")}


def _qualifiers(items: Any) -> list[dict[str, Any]]:
    """Summarize Challenge restrictions (prerequisites / qualifiers)."""
    result = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        fields = {key[2:]: value for key, value in item.items()
                  if key.startswith("m_") and key != "m_EventOwnerRestriction"}
        result.append({"type": _short_type(item.get("_t")), "fields": fields})
    return result


def _events(items: Any) -> list[dict[str, Any]]:
    result = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        entry = {"type": _short_type(item.get("_t"))}
        for key, value in item.items():
            if key.startswith("m_") and value not in ("", None, [], {}):
                entry[key[2:]] = value
        result.append(entry)
    return result


def _conversation_summary(record: dict[str, Any]) -> dict[str, Any]:
    questions = {}
    for qid, question in (record.get("m_Questions") or {}).items():
        questions[str(qid)] = {
            "text": question.get("m_QuestionText", ""),
            "answers": [str(a) for a in question.get("m_AnswerIds") or ()],
            "speaker_override": question.get("m_OwnerNameOverride") or "",
        }
    answers = {}
    for aid, answer in (record.get("m_Answers") or {}).items():
        answers[str(aid)] = {
            "text": answer.get("m_AnswerText", ""),
            "next": answer.get("m_NextQuestionId"),
            "prerequisites": _qualifiers(answer.get("m_Prerequisites")),
            "events": (_events(answer.get("m_OnSelectedEvents")) +
                       _events(answer.get("m_EventsToFireWhenSelected"))),
        }
    return {
        "guid": _guid(record.get("m_Id")),
        "name": record.get("m_Name", ""),
        "owner": record.get("m_OwnerName", ""),
        "champion": _guid(record.get("m_ChampionId")),
        "appearance": record.get("m_Appearance", ""),
        "start": str(record.get("m_StartingQuestionId", 0)),
        "questions": questions,
        "answers": answers,
    }


def conversations() -> dict[str, dict[str, Any]]:
    global _CONVERSATIONS
    with _CONVERSATION_LOCK:
        if _CONVERSATIONS is None:
            built = {}
            for record in records("ConversationTemplate"):
                summary = _conversation_summary(record)
                if summary["guid"]:
                    built[summary["guid"]] = summary
            _CONVERSATIONS = built
        return _CONVERSATIONS


def ending_id(answer_id: str, answer: dict[str, Any]) -> tuple[str, bool]:
    """``(id, server_visible)`` for an answer that ends the conversation.

    The client reports an answer to the server only through its
    ServerScriptEvent, so that event name is the ending's id; an ending
    without one is ``A<answer id>`` and only the simulator can choose it.
    """
    for event in answer.get("events") or ():
        if event.get("type") == "ServerScriptEvent" and event.get("EventName"):
            return str(event["EventName"]), True
    return f"A{answer_id}", False


def _ends(answer: dict[str, Any]) -> bool:
    try:
        return answer.get("next") is None or int(answer["next"]) < 0
    except (TypeError, ValueError):
        return True


def conversation_endings(found: dict[str, Any]) -> list[dict[str, Any]]:
    question_of = {aid: qid for qid, q in found["questions"].items()
                   for aid in q["answers"]}
    endings = []
    for aid, answer in found["answers"].items():
        if not _ends(answer):
            continue
        ending, visible = ending_id(aid, answer)
        endings.append({"answer": aid, "question": question_of.get(aid, ""),
                        "text": answer["text"], "ending": ending,
                        "event": visible, "server_visible": visible})
    # A single ending is implied by conv_done itself.
    if len(endings) == 1:
        endings[0]["server_visible"] = True
    return endings


def _endings_of(guid: Any) -> list[dict[str, Any]]:
    found = conversations().get(str(guid or "").lower())
    return conversation_endings(found) if found else []


def conversation_hooks(guid: str) -> dict[str, list[Any]]:
    """Native script events and prerequisites authored on a conversation."""
    found = conversations().get(str(guid or "").lower()) or {}
    events, prerequisites = [], []
    for answer in (found.get("answers") or {}).values():
        events.extend(answer["events"])
        prerequisites.extend(answer["prerequisites"])
    return {"events": events, "prerequisites": prerequisites}


def quest_templates() -> list[dict[str, Any]]:
    result = []
    for record in records("QuestTemplate"):
        objectives = []
        for objective in record.get("m_Objectives") or ():
            if not isinstance(objective, dict):
                continue
            objectives.append({
                "type": _short_type(objective.get("_t")),
                "location": objective.get("m_QuestLocationId", ""),
                "id": objective.get("m_ID", ""),
                "text": objective.get("m_Title") or "",
                "encounter": _guid(objective.get("m_EncounterId")),
                "dungeon": _guid(objective.get("m_DungeonId")),
            })
        result.append({
            "guid": _guid(record.get("m_Id")),
            "script": record.get("m_ScriptName") or record.get("m_Name", ""),
            "title": record.get("m_Title") or record.get("m_Name", ""),
            "objectives": objectives,
        })
    return result


_SCENE_KINDS = ("PANORAMA", "DUNGEON", "DIALOG", "REWARD", "CHOICE")


def _scene_kind(name: str, battle: bool) -> str:
    """Scene role from its authored name suffix (``... - PANORAMA``)."""
    upper = str(name or "").upper()
    for kind in _SCENE_KINDS:
        if upper.endswith(kind) or f" {kind} " in f"{upper} ":
            return kind.lower()
    # No opponent and no role suffix: a conversation/lore node.
    return "battle" if battle else "scene"


def scene_template(name: str) -> dict[str, Any] | None:
    for record in records("SceneData"):
        if record.get("m_Name") == name and record["_t"].endswith("DungeonTemplate"):
            return record
    return None


def encounter_scene_names() -> dict[str, str]:
    return {_guid(r.get("m_Id")): str(r.get("m_Name") or r.get("m_Title") or "")
            for r in records("SceneData") if r["_t"].endswith("EncounterScene")}


_RECORD_SCENES: list[dict[str, Any]] | None = None


_SCENES_LOCK = threading.Lock()   # separate: building calls records()


def _records_encounters() -> list[dict[str, Any]]:
    """Encounter scenes straight from Records (opponent = the AI deck)."""
    global _RECORD_SCENES
    with _SCENES_LOCK:
        if _RECORD_SCENES is None:
            # Opponent: EncounterDeck -> its champion, else its DeckTemplate's
            # champion, else that deck's name.
            names = champion_names()
            templates = {_guid(r.get("m_Id")): r for r in records("DeckTemplate")}
            decks = {}
            for r in records("EncounterDeck"):
                template = templates.get(_guid(r.get("m_DeckTemplateId"))) or {}
                champion = _guid(r.get("m_ChampionId")).strip("0-") and _guid(r.get("m_ChampionId"))
                champion = champion or _guid(template.get("m_ChampionId"))
                decks[_guid(r.get("m_Id"))] = (names.get(champion) or
                                               str(template.get("m_DeckName") or ""))
            built = []
            for r in records("SceneData"):
                if not r["_t"].endswith("EncounterScene"):
                    continue
                ai = [p for p in r.get("m_EncounterPlayers") or ()
                      if isinstance(p, dict) and p.get("m_IsAI")]
                deck = _guid((ai[0].get("m_EncounterDeck") if ai else None))
                battle = bool(deck.strip("0-"))
                name = str(r.get("m_Name") or "")
                built.append({
                    "guid": _guid(r.get("m_Id")), "name": name,
                    "title": str(r.get("m_Title") or name), "champion": "",
                    "champion_name": decks.get(deck, ""), "deck": deck,
                    "battle": battle, "kind": _scene_kind(name, battle), "race": "",
                    "mods": [{"title": m.get("m_Title", "")} for m in r.get("m_Mods") or ()
                             if isinstance(m, dict)],
                    "rewards": {}, "source": "Records"})
            _RECORD_SCENES = built
        return _RECORD_SCENES

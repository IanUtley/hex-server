"""Metadata-driven card-choice effects.

The client implements ``DoubleChoiceAbilityEffectTemplate`` by creating
temporary choice cards, exposing the built-in ``ChooseAndPlay`` ability, and
continuing the parent ability after the selected choice card is played.  The
server keeps the same three pieces of state explicitly so the protocol works
for both campaign and PvP sessions.
"""

import json
import random
import re
import struct

import game_engine

from .._shared import next_game_card_uid, owner_uid
from ..fields import effect_field, effect_template, effect_template_value
from .registry import leaf_register


CHOOSE_AND_PLAY_ABILITY = "7db268ea-c960-68ba-be49-712d760d7ba4"
CHOICE_TARGET_TEMPLATE = "6f83ae25-2c6d-42af-8635-b7a4174b0405"


def extract_card_uids(raw):
    """Return Card SessionCardIds embedded in an activation transaction."""
    result = []
    if not isinstance(raw, bytes):
        return result
    for match in re.finditer(
            rb"m_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});", raw):
        try:
            uid64 = struct.unpack("<Q", bytes.fromhex(
                match.group(1).decode()))[0]
        except (TypeError, ValueError, struct.error):
            continue
        if (uid64 & 0xFF) == 1:
            result.append(int(uid64))
    return result


def _resource_guids(value):
    result = []
    for item in value or []:
        if isinstance(item, dict):
            item = item.get("m_Guid") or item.get("guid")
        if item:
            guid = str(item).lower()
            if guid != "0" * 36:
                result.append(guid)
    return result


def _clear_choice_zone(game, session, db, pl_t, ai_t, handler, bstate):
    """Mirror ``Session.ClearChoiceZone`` before a second choice."""
    rows = db.execute(
        "SELECT card_uid, user_id, template_guid, card_type "
        "FROM game_cards WHERE session_id=? AND location='choosing'",
        (session.session_id,)).fetchall()
    for uid, card_owner, template_guid, card_type in rows:
        db.execute(
            "UPDATE game_cards SET location='PlayedResources', position=0 "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(uid)))
        scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
        player = owner_uid(card_owner, pl_t, ai_t, bstate)
        game.push_card_moved(
            scid, player, game_engine.ECardCollections.PlayedResources,
            game_engine.ECardLocations.Top, 0)
    if rows and (bstate or {}).get("pvp"):
        # Choice tokens were delivered privately in PvP.  Remember the old
        # token IDs so the second-stage prompt can keep their cleanup private
        # as well; the selected token has already left Choosing and remains a
        # public SpellCardPlayed event.
        bstate.setdefault("private_choice_uids", []).extend(
            int(uid) for uid, _owner, _template, _type in rows)
    if rows:
        db.commit()


def _create_choice_cards(game, session, db, handler, pl_t, ai_t, bstate,
                         owner_id, template_guids):
    """Create the temporary Choice cards and publish their card definitions."""
    cards = []
    existing = {row[1] for row in db.execute(
        "PRAGMA table_info(game_cards)").fetchall()}
    for template_guid in template_guids:
        row = db.execute(
            "SELECT card_type, abilities_json, attributes FROM card_templates "
            "WHERE guid=?", (template_guid,)).fetchone()
        if not row:
            continue
        card_uid = next_game_card_uid(db, session.session_id)
        next_id = db.execute(
            "SELECT COALESCE(MAX(id), 10000) + 1 FROM game_cards "
            "WHERE session_id=?", (session.session_id,)).fetchone()[0]
        columns = [
            "id", "session_id", "user_id", "card_uid", "template_guid",
            "card_template_id", "location", "position", "card_state",
            "card_abilities", "card_type", "card_attributes",
        ]
        values = [
            next_id, session.session_id, int(owner_id), card_uid,
            template_guid, template_guid, "choosing", 0, 0,
            row[1] or "[]", row[0] or "Choice", int(row[2] or 0),
        ]
        for column, value in (("owner_user_id", int(owner_id)),
                              ("original_template_guid", template_guid)):
            if column in existing:
                columns.append(column)
                values.append(value)
        db.execute(
            "INSERT INTO game_cards ({}) VALUES ({})".format(
                ",".join(columns), ",".join("?" for _ in columns)), values)
        cards.append((int(card_uid), template_guid, row[0] or "Choice"))
    db.commit()

    player = owner_uid(owner_id, pl_t, ai_t, bstate)
    for card_uid, template_guid, card_type in cards:
        scid = game_engine.SessionCardId(game_engine.UID(card_uid))
        _tpl, ctype, name, cost, attack, defense, gems = handler._card_full_data(
            game, scid, template_guid)
        game.push_card_moved(
            scid, player, game_engine.ECardCollections.Choosing,
            game_engine.ECardLocations.Top, 1)
        game.push_card_updated(
            scid, player, game_engine.ECardCollections.Choosing, ctype,
            template_id=template_guid, card_name=name, cost=cost,
            attack=attack, defense=defense, gems=gems, state=0)
    return [card_uid for card_uid, _template_guid, _card_type in cards]


def _pending_choice(bstate, owner_id, source_uid, ability_guid,
                    choice_uids, resume_effect_order, target_map, variables):
    return {
        "kind": "double_choice",
        "owner_id": int(owner_id),
        "source_uid": int(source_uid) if source_uid is not None else 0,
        "ability_guid": str(ability_guid).lower(),
        "choice_uids": [int(uid) for uid in choice_uids],
        "resume_effect_order": int(resume_effect_order),
        "target_map": {
            str(key): value for key, value in (target_map or {}).items()
        },
        "variables": dict(variables or {}),
    }


@leaf_register("DoubleChoiceAbilityEffectTemplate")
def double_choice(game, session, db, handler, pl_t, ai_t, bstate,
                  effect_guid, param):
    """Create a random first choice or the remaining second choice."""
    typed = effect_template(effect_guid) or {}
    second = bool(typed.get("m_SecondChoice"))
    owner_id = int((bstate or {}).get("resolving_owner_id", 0) or 0)
    source_uid = (bstate or {}).get("resolving_source_uid")
    ability_guid = (bstate or {}).get("resolving_ability", "")

    # The authoritative client clears every existing choice token before
    # creating either stage of a DoubleChoice effect.
    _clear_choice_zone(game, session, db, pl_t, ai_t, handler, bstate)

    if second:
        template_guids = list((bstate or {}).pop(
            "double_choice_remaining_guids", []))
        if not template_guids:
            return "double choice: no remaining choices"
    else:
        choices = _resource_guids(effect_template_value(
            db, bstate, effect_guid, "m_Choices", []))
        if not choices:
            return "double choice: no choices"
        count = effect_field(db, bstate, effect_guid, "m_NumOptions", 0)
        count = max(0, min(int(count), len(choices)))
        shuffled = list(choices)
        selected = []
        for _ in range(count):
            selected.append(shuffled.pop(random.randrange(len(shuffled))))
        template_guids = selected
        bstate["double_choice_remaining_guids"] = shuffled

    choice_uids = _create_choice_cards(
        game, session, db, handler, pl_t, ai_t, bstate, owner_id,
        template_guids)
    if not choice_uids:
        return "double choice: no card templates"

    # AI-controlled triggers use the same random choice policy as the client
    # AI, but do not create a human-facing pause.
    if not bstate.get("pvp") and owner_id == 0:
        chosen_uid = random.choice(choice_uids)
        play_choice_card(game, session, db, handler, pl_t, ai_t, bstate,
                         chosen_uid, owner_id)
        return f"double choice: AI chose {hex(chosen_uid)}"

    pending = _pending_choice(
        bstate, owner_id, source_uid, ability_guid, choice_uids,
        int((bstate or {}).get("resolving_effect_order", 0)) + 1,
        (bstate or {}).get("ability_target_map") or {},
        (bstate or {}).get("ability_variables") or {})
    bstate["pending_choice"] = pending
    bstate["resolution_paused"] = True
    prompt = getattr(handler, "_prompt_choice_cards", None)
    if callable(prompt):
        prompt(game, session, pl_t, ai_t, bstate, pending)
    return f"double choice: awaiting {len(choice_uids)} choices"


def play_choice_card(game, session, db, handler, pl_t, ai_t, bstate,
                     chosen_uid, owner_id):
    """Play one generated choice card for free, matching PlayChoiceCard."""
    row = db.execute(
        "SELECT template_guid, card_type, user_id, location, card_state "
        "FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(chosen_uid))).fetchone()
    if not row or row[3] != "choosing" or int(row[2] or 0) != int(owner_id):
        return False
    db.execute(
        "UPDATE game_cards SET location='PlayedResources', position=0, "
        "card_state=0 WHERE session_id=? AND card_uid=?",
        (session.session_id, int(chosen_uid)))
    db.commit()
    player = owner_uid(owner_id, pl_t, ai_t, bstate)
    scid = game_engine.SessionCardId(game_engine.UID(int(chosen_uid)))
    _tpl, ctype, name, cost, attack, defense, gems = handler._card_full_data(
        game, scid, row[0])
    game.push_card_updated(
        scid, player, game_engine.ECardCollections.PlayedResources, ctype,
        template_id=row[0], card_name=name, cost=cost, attack=attack,
        defense=defense, gems=gems, state=0)
    game.push_card_moved(
        scid, player, game_engine.ECardCollections.PlayedResources,
        game_engine.ECardLocations.Top, 0)
    # Choice cards are not normal resources.  The client emits
    # SpellCardPlayed from Session.PlayChoiceCard and does not grant resource
    # points or charge for this free play.
    game.push_spell_card_played(scid, player)
    return True


def resolve_choice_card_abilities(game, session, db, handler, pl_t, ai_t,
                                  bstate, chosen_uid, source_uid, owner_id):
    """Resolve automatic abilities on a choice card against its real parent.

    The client gives a generated Choice card a parent link to the ability's
    source card.  Its ``SourceCard`` accessor then resolves ``this`` to that
    real parent, which is why Soul Cavalry/Armaments transform Soul Marble
    rather than the temporary choice card itself.
    """
    row = db.execute(
        "SELECT card_abilities FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session.session_id, int(chosen_uid))).fetchone()
    try:
        ability_guids = [str(value).lower() for value in
                         (json.loads(row[0] or "[]") if row else []) if value]
    except (TypeError, ValueError, json.JSONDecodeError):
        ability_guids = []
    if not ability_guids:
        return []

    from ..resolution import resolve_ability
    logs = []
    for ability_guid in ability_guids:
        meta = db.execute(
            "SELECT is_manual FROM card_abilities_meta "
            "WHERE ability_guid=? LIMIT 1", (ability_guid,)).fetchone()
        if meta and int(meta[0] or 0):
            continue
        logs.append(resolve_ability(
            handler, game, session, db, pl_t, ai_t, bstate,
            ability_guid, source_uid, owner_id, target_map={}))
        if bstate.get("resolution_paused"):
            break
    return logs


__all__ = [
    "CHOOSE_AND_PLAY_ABILITY", "CHOICE_TARGET_TEMPLATE",
    "extract_card_uids", "double_choice", "play_choice_card",
    "resolve_choice_card_abilities",
]

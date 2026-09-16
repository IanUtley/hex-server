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
from ..builder import AbilityContinuation
from ..fields import effect_field, effect_template, effect_template_value
from .registry import effect


CHOOSE_AND_PLAY_ABILITY = "7db268ea-c960-68ba-be49-712d760d7ba4"
# Built-in copy ability referenced by the printed Iconoclast ability.  The
# client uses this relationship to display the source card's localized text.
CHOICE_COPY_ABILITY = "d5b56bd5-4d06-995d-6487-d1db36e29853"
CHOICE_TARGET_TEMPLATE = "6f83ae25-2c6d-42af-8635-b7a4174b0405"


def ai_choice_prefer_missing(db, session, bstate, choice_uids):
    """Choose a generated resource option whose threshold color is missing.

    Choice cards are temporary instances, so inspect their authored ability
    graph/effect parameters rather than card names or display text.  If no
    threshold can be derived (or all offered colors are already present), use
    the session RNG through the normal choice helper's equivalent policy.
    """
    candidates = [int(uid) for uid in choice_uids]
    if not candidates:
        raise IndexError("cannot choose from an empty sequence")
    threshold = (bstate or {}).get("ai_threshold", {}) or {}
    missing = []
    from pvp_db import (db_card_source_info, db_card_template_ability_payload,
                        db_ability_effect_type_params)
    for uid in candidates:
        row = db_card_source_info(session.session_id, uid, conn=db)
        if not row:
            continue
        try:
            ability_guids = json.loads(
                db_card_template_ability_payload(row[0], conn=db) or "[]")
        except (TypeError, ValueError):
            ability_guids = []
        if isinstance(ability_guids, dict):
            ability_guids = ability_guids.get("abilities", [])
        colors = set()
        for ability_guid in ability_guids or []:
            if isinstance(ability_guid, dict):
                ability_guid = ability_guid.get("m_Guid") or ability_guid.get("guid")
            if not ability_guid:
                continue
            for effect_type, raw_param in db_ability_effect_type_params(
                    str(ability_guid).lower(), conn=db):
                if effect_type != "CardModifierAbilityEffectTemplate":
                    continue
                try:
                    param = json.loads(raw_param or "{}")
                except (TypeError, ValueError):
                    param = {}
                if param.get("property") != "threshold":
                    continue
                shard = param.get("shard")
                if shard:
                    flag = game_engine.SHARD_TO_FLAG.get(
                        str(shard).rsplit(".", 1)[-1].lower())
                    if flag:
                        colors.add(int(flag))
                else:
                    # Compatibility with pre-metadata BOM rows.
                    match = re.search(r"\[([A-Za-z]+)\]",
                                      str(param.get("text", "")))
                    if match:
                        flag = game_engine.SHARD_TO_FLAG.get(
                            match.group(1).lower())
                        if flag:
                            colors.add(int(flag))
        if colors and all(int(threshold.get(flag, threshold.get(str(flag), 0)) or 0) <= 0
                          for flag in colors):
            missing.append(uid)
    pool = missing or candidates
    rng = (bstate or {}).get("_rules_rng")
    if rng is not None and hasattr(rng, "next"):
        return pool[int(rng.next(len(pool)))]
    return random.choice(pool)


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
    from pvp_db import (db_choice_card_rows,
                        db_move_choice_to_played_resources)
    rows = db_choice_card_rows(session.session_id, conn=db)
    for uid, card_owner, template_guid, card_type in rows:
        db_move_choice_to_played_resources(session.session_id, int(uid), conn=db)
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
    from pvp_db import (db_copy_template_payload, db_next_game_card_row_id,
                        db_insert_generated_card)
    for template_guid in template_guids:
        row = db_copy_template_payload(template_guid, conn=db)
        if not row:
            continue
        card_uid = next_game_card_uid(db, session.session_id)
        db_insert_generated_card(
            session.session_id, int(owner_id), card_uid, template_guid,
            "choosing", row[0] or "Choice", row[1], row[2],
            db_next_game_card_row_id(session.session_id, conn=db), conn=db,
            position=0, card_state=0, owner_user_id=int(owner_id),
            original_template_guid=template_guid)
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
    pending = AbilityContinuation.from_state(
        bstate, ability_guid=ability_guid, source_uid=source_uid,
        owner_id=owner_id, target_map=target_map, variables=variables,
        resume_effect_order=resume_effect_order).to_dict()
    pending.update({"kind": "double_choice",
                    "choice_uids": list(dict.fromkeys(
                        int(uid) for uid in choice_uids))})
    return pending


def _double_choice_legacy(game, session, db, handler, pl_t, ai_t, bstate,
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
    # ``pvp`` describes the transport/session shape, not who controls this
    # ability. Practice sessions can use the PvP-shaped state adapter while
    # still having an AI owner (user_id=0). The client AI never opens a human
    # chooser for its own generated choices, so owner identity is the
    # authoritative gate here.
    if owner_id == 0:
        chosen_uid = ai_choice_prefer_missing(
            db, session, bstate, choice_uids)
        play_choice_card(game, session, db, handler, pl_t, ai_t, bstate,
                         chosen_uid, owner_id)
        # The generated choice card carries the authored threshold/resource
        # effect.  The client resolves that automatic ability as part of
        # PlayChoiceCard; omitting this step leaves the shard on the chain
        # while never granting the selected threshold.
        resolve_choice_card_abilities(
            game, session, db, handler, pl_t, ai_t, bstate,
            chosen_uid, source_uid, owner_id)
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


@effect("DoubleChoiceAbilityEffectTemplate")
def double_choice(effect):
    """Run the prompt/continuation flow through the context boundary."""
    return effect.double_choice()


def play_choice_card(game, session, db, handler, pl_t, ai_t, bstate,
                     chosen_uid, owner_id):
    """Play one generated choice card for free, matching PlayChoiceCard."""
    from pvp_db import (db_card_source_info, db_move_choice_to_played_resources)
    row = db_card_source_info(session.session_id, int(chosen_uid), conn=db)
    if not row or row[2] != "choosing" or int(row[3] or 0) != int(owner_id):
        return False
    db_move_choice_to_played_resources(session.session_id, int(chosen_uid), conn=db)
    db.commit()
    player = owner_uid(owner_id, pl_t, ai_t, bstate)
    scid = game_engine.SessionCardId(game_engine.UID(int(chosen_uid)))
    _tpl, ctype, name, cost, attack, defense, gems = handler._card_full_data(
        game, scid, row[0])
    game.push_card_moved(
        scid, player, game_engine.ECardCollections.PlayedResources,
        game_engine.ECardLocations.Top, 0)
    game.push_card_updated(
        scid, player, game_engine.ECardCollections.PlayedResources, ctype,
        template_id=row[0], card_name=name, cost=cost, attack=attack,
        defense=defense, gems=gems, state=0)
    # Choice cards are not normal resources.  The client emits
    # SpellCardPlayed from Session.PlayChoiceCard and does not grant resource
    # points or charge for this free play.
    game.push_spell_card_played(scid, player)
    return True


def resolve_choice_card_abilities(game, session, db, handler, pl_t, ai_t,
                                   bstate, chosen_uid, source_uid, owner_id,
                                   *, resolver=None):
    """Resolve automatic abilities on a choice card against its real parent.

    The client gives a generated Choice card a parent link to the ability's
    source card.  Its ``SourceCard`` accessor then resolves ``this`` to that
    real parent, which is why Soul Cavalry/Armaments transform Soul Marble
    rather than the temporary choice card itself.
    """
    from pvp_db import db_card_ability_payload, db_ability_activation_metadata
    payload = db_card_ability_payload(session.session_id, int(chosen_uid), conn=db)
    try:
        ability_guids = [str(value).lower() for value in
                         (json.loads(payload or "[]") if payload else []) if value]
    except (TypeError, ValueError, json.JSONDecodeError):
        ability_guids = []
    if not ability_guids:
        return []

    if resolver is None:
        from ..resolution import resolve_ability
    logs = []
    for ability_guid in ability_guids:
        meta = db_ability_activation_metadata(ability_guid, conn=db)
        if meta and int(meta[4] or 0):
            continue
        if resolver is None:
            logs.append(resolve_ability(
                handler, game, session, db, pl_t, ai_t, bstate,
                ability_guid, source_uid, owner_id, target_map={}))
        else:
            logs.append(resolver(
                handler, game, session, db, pl_t, ai_t, bstate,
                ability_guid, source_uid, owner_id, target_map={},
                variables={}))
        if bstate.get("resolution_paused"):
            break
    return logs


__all__ = [
    "CHOOSE_AND_PLAY_ABILITY", "CHOICE_COPY_ABILITY",
    "CHOICE_TARGET_TEMPLATE",
    "extract_card_uids", "double_choice", "play_choice_card",
    "resolve_choice_card_abilities",
]

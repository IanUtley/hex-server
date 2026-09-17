"""RulesPort attribute grants and their client projection."""

from __future__ import annotations

import json
import re
import game_engine


def _flags(value, text=""):
    names = {
        "flight": game_engine.ECardAttributes.Flight,
        "speed": game_engine.ECardAttributes.Speed,
        "cantattack": game_engine.ECardAttributes.CantAttack,
        "can't attack": game_engine.ECardAttributes.CantAttack,
        "cantblock": game_engine.ECardAttributes.CantBlock,
        "can't block": game_engine.ECardAttributes.CantBlock,
        "cantbeblocked": game_engine.ECardAttributes.CantBeBlocked,
        "can't be blocked": game_engine.ECardAttributes.CantBeBlocked,
        "firststrike": game_engine.ECardAttributes.FirstStrike,
        "dualstrike": game_engine.ECardAttributes.DualStrike,
        "swiftstrike": game_engine.ECardAttributes.FirstStrike,
        "skyguard": game_engine.ECardAttributes.SkyGuard,
        "lifedrain": game_engine.ECardAttributes.SpiritDrain,
        "spiritdrain": game_engine.ECardAttributes.SpiritDrain,
        "steadfast": game_engine.ECardAttributes.Steadfast,
        "spellshield": game_engine.ECardAttributes.SpellShield,
        "rage": game_engine.ECardAttributes.Rage,
        "cantreadyautomatically": game_engine.ECardAttributes.CantReadyAutomatically,
        "can't ready": game_engine.ECardAttributes.CantReadyAutomatically,
        "defensive": game_engine.ECardAttributes.Defensive,
    }
    bits = 0
    raw = str(value or text or "")
    for part in re.split(r"[|, ]+", raw.lower()):
        bits |= int(names.get(part, 0) or 0)
    if value and not bits:
        for name, flag in names.items():
            if name in raw.lower():
                bits |= int(flag)
    if text and "can't attack or block" in text.lower():
        bits |= int(game_engine.ECardAttributes.CantAttack | game_engine.ECardAttributes.CantBlock)
    if text and "can't ready" in text.lower():
        bits |= int(game_engine.ECardAttributes.CantReadyAutomatically)
    return bits


def attribute_bits_from_flags(flags):
    """Decode the typed attribute field without consulting legacy helpers."""
    return _flags(flags)


def apply_attribute_grant(context, target, param):
    if target is None:
        return 0
    bits = _flags(param.get("attribute_flags"), param.get("text"))
    if not bits:
        return 0
    from pvp_db import (db_card_attribute_value, db_set_card_attribute_value,
                        db_card_owner_id, db_card_source_info,
                        db_card_mutation_field, db_set_card_mutation_field)
    uid = int(target)
    temporary = str(param.get("duration") or "") in {
        "EndOfTurn", "BeginningOfOwnersTurn", "AfterCardsReadyOnPlayersTurn"}
    column = "temporary_attributes" if temporary else "card_attributes"
    current = int(db_card_attribute_value(
        context.session.session_id, uid, column, conn=context.db) or 0)
    db_set_card_attribute_value(context.session.session_id, uid, column,
                                current | bits, conn=context.db)
    if temporary:
        boundary = {"EndOfTurn": "end_turn",
                    "BeginningOfOwnersTurn": "start_turn",
                    "AfterCardsReadyOnPlayersTurn": "prep"}.get(
                        str(param.get("duration") or ""))
        if boundary:
            try:
                buffs = json.loads(db_card_mutation_field(
                    context.session.session_id, uid, "temporary_buffs",
                    conn=context.db) or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                buffs = {}
            if not isinstance(buffs, dict):
                buffs = {}
            owner = param.get("source_owner_id")
            if owner is None:
                owner = context.bstate.get("resolving_owner_id", 0)
            metadata = buffs.setdefault("__attribute_expirations", {})
            for bit in (1 << n for n in range(bits.bit_length()) if bits & (1 << n)):
                metadata[str(bit)] = {"owner": int(owner or 0),
                                      "boundary": boundary}
            db_set_card_mutation_field(
                context.session.session_id, uid, "temporary_buffs",
                json.dumps(buffs, separators=(",", ":"), sort_keys=True),
                conn=context.db)
    context.db.commit()
    row = db_card_source_info(context.session.session_id, uid, conn=context.db)
    if row and row[0]:
        scid = game_engine.SessionCardId(game_engine.UID(uid))
        _tpl, card_type, _name, cost, attack, defense, gems = \
            context.handler._card_full_data(context.game, scid, row[0])
        owner = db_card_owner_id(context.session.session_id, uid, conn=context.db)
        from .runtime_helpers import card_collection_for_location, owner_uid
        context.game.push_card_updated(
            scid, owner_uid(owner or 0, context.player_uid, context.ai_uid,
                            context.bstate),
            card_collection_for_location(row[2]), card_type,
            template_id=row[0], attack=attack, defense=defense, cost=cost,
            gems=gems, attributes=current | bits,
            nulling=str(row[2]).lower() == "deck")
    return bits

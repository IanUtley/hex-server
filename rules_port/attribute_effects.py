"""RulesPort attribute grants and their client projection."""

from __future__ import annotations

import json
import re
import game_engine


_BOON_FLAGS = (
    int(game_engine.ECardAttributes.Juggernaught),
    int(game_engine.ECardAttributes.Flight),
    int(game_engine.ECardAttributes.DualStrike),
    int(game_engine.ECardAttributes.SpiritDrain),
    int(game_engine.ECardAttributes.Rage),
    int(game_engine.ECardAttributes.CantBeBlocked),
    int(game_engine.ECardAttributes.Speed),
    int(game_engine.ECardAttributes.SpellShield),
    int(game_engine.ECardAttributes.Steadfast),
    int(game_engine.ECardAttributes.FirstStrike),
)
_BOON_INT_ATTRIBUTES = {
    int(game_engine.ECardAttributes.DualStrike): "Lethal",
    int(game_engine.ECardAttributes.Rage): "Rage",
    int(game_engine.ECardAttributes.CantBeBlocked): "Feral",
}


def attribute_bits_from_flags(flags):
    """Decode typed ``m_AttributeFlags`` (C# enum member names) to bits.

    Records writes the client enum member names verbatim (``Juggernaught``,
    ``CantReadyAutomatically``, ``EntersPlayExhausted``, ...), so the client
    enum in :mod:`domain.enums` stays the single authority for the
    name-to-bit map instead of a hand-maintained subset that silently drops
    the keywords it does not list.
    """
    bits = 0
    for token in re.split(r"[|, ]+", flags if isinstance(flags, str) else ""):
        if token:
            bits |= int(getattr(game_engine.ECardAttributes, token, 0) or 0)
    return bits


def _flags(value, text=""):
    """Attribute bits for one CardModifier operand.

    Typed flags win; localized game text (lower case, HTML-wrapped) is only
    consulted for effects whose typed field is missing.
    """
    bits = attribute_bits_from_flags(value)
    if bits:
        return bits
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
    raw = re.sub(r"<[^<>]*>", " ", str(value or text or "")).lower()
    for part in re.split(r"[|, ]+", raw):
        if part:
            bits |= int(names.get(part, 0) or 0)
    if not bits:
        # Localized text wraps the keyword in HTML tags ("<b>Flight</b>"), so
        # token equality after tag stripping is not enough; fall back to a
        # substring search for the authored keyword names.
        for name, flag in names.items():
            if name and name in raw:
                bits |= int(flag)
    if text and "can't attack or block" in text.lower():
        bits |= int(game_engine.ECardAttributes.CantAttack | game_engine.ECardAttributes.CantBlock)
    if text and "can't ready" in text.lower():
        bits |= int(game_engine.ECardAttributes.CantReadyAutomatically)
    return bits


def _mutation_payload(context, uid, column):
    from pvp_db import db_card_mutation_field
    try:
        payload = json.loads(db_card_mutation_field(
            context.session.session_id, uid, column, conn=context.db) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _boon_card_state(context, uid):
    """Return the current attribute and IntAttr view used by GetBoonFor."""
    from pvp_db import db_card_attribute_rows

    rows = db_card_attribute_rows(
        context.session.session_id, [uid], conn=context.db)
    attrs = int(rows[0][0] or 0) if rows else 0

    scid = game_engine.SessionCardId(game_engine.UID(uid))
    card_def = getattr(context.game, "card_defs", {}).get(scid)
    if card_def is None:
        from pvp_db import db_card_source_info
        row = db_card_source_info(
            context.session.session_id, uid, conn=context.db)
        if row and row[0]:
            context.handler._card_full_data(context.game, scid, row[0])
            card_def = getattr(context.game, "card_defs", {}).get(scid)
    if card_def is not None:
        attrs |= int(getattr(card_def, "attributes", 0) or 0)

    int_attrs = {}
    if card_def is not None:
        int_attrs.update(getattr(card_def, "int_attrs", {}) or {})
        if getattr(card_def, "lethal", False):
            int_attrs.setdefault("Lethal", 1)
    for column in ("permanent_buffs", "temporary_buffs"):
        values = _mutation_payload(context, uid, column).get("int_attrs", {})
        if not isinstance(values, dict):
            continue
        for name, value in values.items():
            try:
                int_attrs[name] = max(
                    int(int_attrs.get(name, 0) or 0), int(value or 0))
            except (TypeError, ValueError):
                continue
    return attrs, int_attrs


def _choose_boon(context, uid):
    """Choose one client-defined Boon, rerolling duplicate non-Rage boons."""
    import random

    from .combat_rules import card_int_attr

    attrs, int_attrs = _boon_card_state(context, uid)
    # Dynamic IntAttrs are also consulted by combat rules and may not yet be
    # present on the cached CardDef used for the client duplicate check.
    lethal = int_attrs.get("Lethal", int_attrs.get("lethal", 0))
    feral = int_attrs.get("Feral", int_attrs.get("feral", 0))
    if not lethal:
        lethal = card_int_attr(context.db, context.session.session_id,
                               uid, "Lethal")
    if not feral:
        feral = card_int_attr(context.db, context.session.session_id,
                              uid, "Feral")

    def already_has(flag):
        if flag == int(game_engine.ECardAttributes.DualStrike):
            return bool(lethal)
        if flag == int(game_engine.ECardAttributes.CantBeBlocked):
            return bool(feral)
        return bool(attrs & flag)

    rng = context.bstate.get("_rules_rng")
    while True:
        if rng is not None and callable(getattr(rng, "next", None)):
            index = int(rng.next(len(_BOON_FLAGS)))
        else:
            index = random.randrange(len(_BOON_FLAGS))
        flag = _BOON_FLAGS[index]
        # C# intentionally allows additional Rage stacks.
        if (flag == int(game_engine.ECardAttributes.Rage) or
                not already_has(flag)):
            return flag


def _grant_boon_int_attribute(context, uid, attr, column):
    from pvp_db import db_set_card_mutation_field

    payload = _mutation_payload(context, uid, column)
    attrs = payload.setdefault("int_attrs", {})
    if not isinstance(attrs, dict):
        attrs = payload["int_attrs"] = {}
    old = int(attrs.get(attr, 0) or 0)
    attrs[attr] = old + 1 if attr == "Rage" else 1
    db_set_card_mutation_field(
        context.session.session_id, uid, column,
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        conn=context.db)


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
    granted_bits = bits
    int_attr = None
    boon = None
    if (bits == int(game_engine.ECardAttributes.Boon) and
            str(param.get("operation") or "Add").lower() == "add"):
        boon = _choose_boon(context, uid)
        int_attr = _BOON_INT_ATTRIBUTES.get(boon)
        if int_attr:
            _grant_boon_int_attribute(
                context, uid, int_attr,
                "temporary_buffs" if temporary else "permanent_buffs")
            granted_bits = 0
        else:
            granted_bits = boon
    if granted_bits:
        db_set_card_attribute_value(context.session.session_id, uid, column,
                                    current | granted_bits, conn=context.db)
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
            for bit in (1 << n for n in range(granted_bits.bit_length())
                        if granted_bits & (1 << n)):
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
        card_def = getattr(context.game, "card_defs", {}).get(scid)
        # The immediate CardUpdated must announce the attribute it just
        # granted even when the cached CardDef was projected before the DB
        # write (the legacy leaf always pushed ``current | bits``).
        projected_attributes = (
            int(getattr(card_def, "attributes", 0) or 0)
            | current | granted_bits)
        projected_int_attrs = dict(
            getattr(card_def, "int_attrs", {}) or {})
        # _card_full_data reconstructs permanent IntAttrs. Temporary IntAttrs
        # live in the parallel temporary-buffs payload and need to be included
        # in this immediate CardUpdated too.
        temporary_int_attrs = _mutation_payload(
            context, uid, "temporary_buffs").get("int_attrs", {})
        if isinstance(temporary_int_attrs, dict):
            for name, value in temporary_int_attrs.items():
                try:
                    if name == "Rage":
                        projected_int_attrs[name] = int(
                            projected_int_attrs.get(name, 0) or 0) + int(value or 0)
                    else:
                        projected_int_attrs[name] = max(
                            int(projected_int_attrs.get(name, 0) or 0),
                            int(value or 0))
                except (TypeError, ValueError):
                    continue
        owner = db_card_owner_id(context.session.session_id, uid, conn=context.db)
        from .runtime_helpers import card_collection_for_location, owner_uid
        context.game.push_card_updated(
            scid, owner_uid(owner or 0, context.player_uid, context.ai_uid,
                            context.bstate),
            card_collection_for_location(row[2]), card_type,
            template_id=row[0], attack=attack, defense=defense, cost=cost,
            gems=gems, attributes=projected_attributes,
            int_attrs=projected_int_attrs,
            nulling=str(row[2]).lower() == "deck")
    return int((boon or 0) if int_attr else granted_bits)

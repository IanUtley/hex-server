"""RulesPort-owned counter persistence and client projection."""

from __future__ import annotations

import json
import game_engine


TUNNELING_COUNTER_GUID = "def75520-0b8b-447f-8705-b34e71043890"
# Universal keyword ability represented in Records as a TurnStarted trigger.
# Its lifecycle is handled by the native tunneling service, so generic trigger
# discovery must not resolve it a second time.
TUNNELING_ABILITY_GUID = "a4fc4440-4f02-4f40-b786-214ad0205dad"


def _counter_guid(db, name):
    from pvp_db import db_counter_template_id
    return (str(db_counter_template_id(name, conn=db) or
                (TUNNELING_COUNTER_GUID if name == "tunneling" else ""))
            .lower())


def _champion_owner(context, uid):
    """Return the controller id owning a champion SessionCardId, else None.

    Champion counters live in the persisted battle state keyed by champion
    identity, and every later projection (owner UID, template, health) is
    derived from this owner id.  The controller must therefore come from the
    PvP champion map or the PvE handler fields; converting a participant UID
    with ``int()`` is undefined for ``game_engine.UID`` and previously raised
    TypeError, dropping every champion counter into the card path.
    """
    from .runtime_helpers import champion_owner_id
    return champion_owner_id(context.handler, context.bstate, uid)


def _secret(guid):
    try:
        from gamedata import DEFAULT_RECORD_STORE
        record = DEFAULT_RECORD_STORE.get("CardCounterTemplate", guid)
        return bool(record and int(record.field("m_Secret", 0) or 0))
    except (AttributeError, TypeError, ValueError):
        return False


def _project(context, uid, owner, guid, old, new, champion=False):
    from .runtime_helpers import card_collection_for_location, owner_uid
    recipient = owner_uid(owner, context.player_uid, context.ai_uid,
                          context.bstate)
    scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
    if champion:
        template = (getattr(context.handler, "_player_champ_guid", None)
                    if owner == context.bstate.get("pids", [owner])[0]
                    else getattr(context.handler, "_ai_champ_guid", None))
        # The champion HUD reads its effect icons (Stealth, Burning, Dazed,
        # Vulnerable) from the cached CardRepresentation counters, and the
        # client only stores those from a CardUpdated.  Champions sit in the
        # cache as collection None (the battle-start seed), and a Champions
        # collection here would be both suppressed by ``push_card_updated``
        # and read as a CardMoved, so keep the collection the client already
        # has while carrying the new counters.
        collection = game_engine.ECardCollections.None_
        card_type = game_engine.ECardTypes.Champion
        health = int(context.bstate.get(
            ((context.bstate.get("pvp_health_map") or {}).get(owner) or
             f"hp_{owner}"), 20))
        context.game.push_card_updated(
            scid, recipient, collection, card_type,
            template_id=template, defense=health, counters={guid: int(new)},
            secret_counter_guids={guid} if _secret(guid) else set(),
            secret_counter_owner_uid=recipient)
    else:
        from pvp_db import (db_card_source_info, db_card_owner_id,
                            db_card_mutation_field)
        row = db_card_source_info(context.session.session_id, int(uid), conn=context.db)
        if not row:
            return
        # ``db_card_source_info`` returns (template, type, location, owner);
        # the persisted counters live in ``permanent_buffs`` and must be read
        # through the typed mutation accessor.  Indexing row[4] raised
        # IndexError and aborted the turn-start (Tunneling) lifecycle.
        try:
            counts = json.loads(db_card_mutation_field(
                context.session.session_id, int(uid), "permanent_buffs",
                conn=context.db) or "{}").get("counters", {})
        except (TypeError, ValueError, json.JSONDecodeError):
            counts = {}
        counts = counts if isinstance(counts, dict) else {}
        encoded = {_counter_guid(context.db, str(name)): int(value or 0)
                   for name, value in counts.items()}
        tpl, card_type, _name, cost, attack, defense, gems = \
            context.handler._card_full_data(context.game, scid, row[0])
        owner = db_card_owner_id(context.session.session_id, int(uid), conn=context.db)
        context.game.push_card_updated(
            scid, owner_uid(owner if owner is not None else 0,
                            context.player_uid, context.ai_uid, context.bstate),
            card_collection_for_location(row[2]),
            game_engine.card_type_from_db(card_type),
            template_id=tpl, cost=cost, attack=attack, defense=defense,
            gems=gems, counters=encoded, nulling=str(row[2]).lower() == "deck",
            secret_counter_guids={key for key in encoded if _secret(key)},
            secret_counter_owner_uid=owner_uid(owner or 0,
                                               context.player_uid,
                                               context.ai_uid, context.bstate))
    context.game.push_card_counters_changed(
        scid, game_engine.ResourceId.from_str(guid), int(new), int(old),
        private_player_uid=recipient)


def counter_spell(context):
    """Port of ``CounterSpellAbilityEffectTemplate`` / ``Session.CounterCard``.

    The effect only counters a card still held on the chain (CastSpells) and
    must respect the authored ``CantBeInterrupted`` int-attribute.  The Python
    native branch previously imported a function that did not exist and
    raised ImportError for every counter spell (43 Records rows).
    """
    target = context.resolved_target()
    if target is None:
        return "counter spell: no target"
    target = int(target)
    from pvp_db import db_card_location
    location = str(db_card_location(
        context.session.session_id, target, conn=context.db) or "").lower()
    if location != "castspells":
        return "counter spell: target not on chain"
    from rules_port.combat_rules import card_int_attr
    if card_int_attr(context.db, context.session.session_id, target,
                     "CantBeInterrupted") > 0:
        return "counter spell: cannot be interrupted"
    # Session.CounterCard removes the ability from the chain; the card itself
    # is discarded by the resolution boundary.
    port = getattr(context.session, "_rules_port_session", None)
    if port is not None:
        try:
            port.chain.remove_ability(target)
            port.forget_projected_chain(target)
        except (AttributeError, TypeError, ValueError):
            pass
    result = context.discard(target)
    context._emit_trigger(
        "CardCounteredEvent", target,
        context.bstate.get("resolving_owner_id", 0),
        event_source_collection="CastSpells",
        event_destination_collection="discard")
    return f"countered {hex(target)}; {result}"


def change_counter(context, target, name, counter_guid, amount, operation):
    """Apply one typed counter operation and emit its client projection."""
    from pvp_db import db_card_mutation_field, db_set_card_mutation_field
    target = int(target)
    name = str(name or "counter").lower()
    guid = str(counter_guid or _counter_guid(context.db, name)).lower()
    champion_owner = _champion_owner(context, target)
    if champion_owner is not None:
        values = context.bstate.setdefault("champion_counters", {}).setdefault(str(target), {})
        old = int(values.get(guid, 0) or 0)
        if operation == "set":
            new = int(amount or 0)
        elif operation in ("remove", "subtract", "clear", "removeall"):
            new = 0 if operation in ("clear", "removeall") else max(0, old - int(amount or 0))
        else:
            new = old + int(amount or 0)
        if new:
            values[guid] = new
        else:
            values.pop(guid, None)
        _project(context, target, champion_owner, guid, old, new, champion=True)
        return old, new
    raw = db_card_mutation_field(context.session.session_id, target,
                                 "permanent_buffs", conn=context.db)
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        data = {}
    counters = data.setdefault("counters", {})
    old = int(counters.get(name, 0) or 0)
    if operation == "set":
        new = int(amount or 0)
    elif operation in ("remove", "subtract", "clear", "removeall"):
        new = 0 if operation in ("clear", "removeall") else max(0, old - int(amount or 0))
    else:
        new = old + int(amount or 0)
    if new:
        counters[name] = new
    else:
        counters.pop(name, None)
    data.setdefault("counter_guids", {})[name] = guid
    db_set_card_mutation_field(context.session.session_id, target,
                               "permanent_buffs", json.dumps(data), conn=context.db)
    context.db.commit()
    from pvp_db import db_card_owner_id
    owner = db_card_owner_id(context.session.session_id, target, conn=context.db)
    _project(context, target, owner if owner is not None else 0, guid, old, new)
    return old, new

"""RulesPort-owned lethal troop transition and Deathcry scheduling."""

from __future__ import annotations

import json
import game_engine


def _ability_list(db, session_id, card_uid, template_guid):
    from pvp_db import (db_ability_activation_metadata,
                        db_ability_trigger_metadata,
                        db_card_ability_payload, db_template_ability_payload)
    values = []
    for raw in (db_template_ability_payload(template_guid, conn=db),
                db_card_ability_payload(session_id, card_uid, conn=db)):
        try:
            values.extend(json.loads(raw or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    result = []
    for guid in dict.fromkeys(str(value).lower() for value in values):
        trigger = db_ability_trigger_metadata(guid, conn=db)
        activation = db_ability_activation_metadata(guid, conn=db)
        if not trigger or "CardEnteredZone" not in str(trigger[2] or ""):
            continue
        condition = activation[5] if activation else ""
        result.append((guid, condition or ""))
    return result


def kill_troop(context, target, *, cause="effect"):
    """Move a troop to discard and resolve its typed Deathcry abilities."""
    from pvp_db import db_card_death_info, db_kill_card_to_discard
    target = int(target)
    row = db_card_death_info(context.session.session_id, target, conn=context.db)
    if not row:
        return "death: no card"
    template_guid, owner, attrs = row[0], int(row[1] or 0), int(row[2] or 0)
    if attrs & int(game_engine.ECardAttributes.Immortal) and cause in ("damage", "effect"):
        return "immortal survives"
    if context._emit_trigger("CardWouldEnterZoneEvent", target, owner):
        return "death replaced"
    db_kill_card_to_discard(
        context.session.session_id, target,
        int(game_engine.ECardStates.CameOutThisTurn |
            game_engine.ECardStates.Tapped |
            game_engine.ECardStates.Attacking |
            game_engine.ECardStates.HasAttacked |
            game_engine.ECardStates.Blocking |
            game_engine.ECardStates.HasBlocked |
            game_engine.ECardStates.Damaged),
        int(game_engine.ECardStates.Dead), conn=context.db)
    context.db.commit()
    scid = game_engine.SessionCardId(game_engine.UID(target))
    tpl, card_type, _name, cost, attack, defense, gems = \
        context.handler._card_full_data(context.game, scid, template_guid)
    from .runtime_helpers import card_collection_for_location, owner_uid
    recipient = owner_uid(owner, context.player_uid, context.ai_uid,
                          context.bstate)
    context.game.push_card_moved(
        scid, recipient, card_collection_for_location("discard"),
        game_engine.ECardLocations.Top, 0)
    context.game.push_card_updated(
        scid, recipient, card_collection_for_location("discard"),
        game_engine.card_type_from_db(card_type), template_id=tpl,
        attack=attack, defense=defense, cost=cost, gems=gems)
    context._emit_trigger("CardExitedZoneEvent", target, owner)
    context._emit_trigger(
        "CardEnteredZoneEvent", target, owner,
        event_source_collection="warzone",
        event_destination_collection="discard",
        event_previous_state=int(game_engine.ECardStates.Dead))
    if cause == "sacrifice":
        context._emit_trigger("CardSacrificedEvent", target, owner)
    # Deathcry resolution belongs to the trigger engine: the warzone->discard
    # CardEnteredZoneEvent above already discovers the card's authored
    # Deathcry triggers and resolves them through the chain with their real
    # condition metadata (``m_UsesPreviousState``), ONE-SHOT consumption, and
    # response window.  Resolving them a second time here made a return-to-play
    # Deathcry fire twice and left the queued chain item stranded, which
    # blocked the enters-play trigger that followed the return (Moon'ariu
    # Sensei's Deploy draw never resolved).
    return f"killed {hex(target)}"


def state_based_deaths(context):
    """Apply the C# state-based lethal-troop pass through RulesPort.

    This is intentionally separate from ``kill_troop``: the state-based
    action decides the complete candidate set and effective defense, while
    ``kill_troop`` owns the ordered zone/deathcry event sequence.
    """
    from pvp_db import db_warzone_troop_state_rows

    # Troops a Lethal source damaged this step (recorded by
    # ``damage_effects.deal_damage``) die even when their remaining defense is
    # above zero, exactly like the client's per-turn ``LethalDamageTaken``.
    lethal_marks = {int(uid) for uid in
                    (context.bstate.pop("_lethal_damage_uids", None) or ())}
    dead = []
    rows = db_warzone_troop_state_rows(
        context.session.session_id, conn=context.db)
    for card_uid, _template_guid, base_def, def_mod, damage, perm_json, temp_json in rows:
        defense = int(base_def or 0) + int(def_mod or 0) - int(damage or 0)
        for serialized in (perm_json, temp_json):
            try:
                values = json.loads(serialized or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                values = {}
            defense += int(values.get("def", 0) or 0)
        try:
            from rules_port.static_rules import effective_deltas
            defense += int(effective_deltas(
                context.db, context.session.session_id,
                context.bstate or {}, int(card_uid)).get("def", 0) or 0)
        except Exception:
            if (context.bstate or {}).get("_rules_port_native_effect") or \
                    (context.bstate or {}).get("_rules_port_attached"):
                raise
        lethal = int(card_uid) in lethal_marks and defense > 0
        if defense <= 0 or lethal:
            old_native = context.bstate.get("_rules_port_native_effect")
            context.bstate["_rules_port_native_effect"] = True
            try:
                result = kill_troop(
                    context, int(card_uid),
                    cause="damage" if lethal else "state")
            finally:
                if old_native is None:
                    context.bstate.pop("_rules_port_native_effect", None)
                else:
                    context.bstate["_rules_port_native_effect"] = old_native
            if str(result).startswith("killed"):
                dead.append(int(card_uid))
    return dead


def champion_would_lose(context, owner_id):
    """Dispatch the native champion survival/replacement event once."""
    owner_id = int(owner_id or 0)
    seen = context.bstate.setdefault("_champion_would_lose_seen", [])
    if owner_id in seen:
        return ""
    if context.bstate.get("pvp"):
        champions = context.bstate.get("champ_map") or {}
        champion_uid = champions.get(str(owner_id), champions.get(owner_id))
    else:
        champion = (getattr(context.handler, "_player_champ_scid", None)
                    if owner_id else
                    getattr(context.handler, "_ai_champ_scid", None))
        champion_uid = (getattr(getattr(champion, "uid", champion),
                                "uid64", champion)
                        if champion is not None else None)
    if champion_uid is None:
        return ""
    seen.append(owner_id)
    old_native = context.bstate.get("_rules_port_native_effect")
    context.bstate["_rules_port_native_effect"] = True
    try:
        return context._emit_trigger(
            "ChampionWouldLoseEvent", int(champion_uid), owner_id)
    finally:
        if old_native is None:
            context.bstate.pop("_rules_port_native_effect", None)
        else:
            context.bstate["_rules_port_native_effect"] = old_native

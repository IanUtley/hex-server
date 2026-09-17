"""RulesPort-owned damage application and replacement lifecycle."""

from __future__ import annotations

import json
import game_engine


def _consume_shields(context, target, dealer, amount, combat):
    from pvp_db import db_card_damage_shield_fields, db_set_card_mutation_field
    row = db_card_damage_shield_fields(
        context.session.session_id, int(target), conn=context.db)
    stores = []
    for index, raw in enumerate(row or ()):
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict) and isinstance(data.get("damage_shields"), list):
            stores.append((index, data))
    remaining = max(0, int(amount or 0))
    for index, data in stores:
        kept = []
        changed = False
        for shield in data["damage_shields"]:
            if remaining <= 0:
                kept.append(shield)
                continue
            if not isinstance(shield, dict):
                continue
            value = max(0, int(shield.get("amount", 0) or 0))
            if not value or (shield.get("only_combat") and not combat):
                if value:
                    kept.append(shield)
                continue
            shield_dealer = shield.get("dealer")
            if ((shield_dealer is not None and dealer is None) or
                    (shield_dealer is not None and dealer is not None and
                     int(shield_dealer) != int(dealer))):
                kept.append(shield)
                continue
            blocked = min(remaining, value)
            remaining -= blocked
            changed = True
            value -= blocked
            if value and not shield.get("one_shot"):
                shield["amount"] = value
                kept.append(shield)
        if changed:
            data["damage_shields"] = kept
            db_set_card_mutation_field(
                context.session.session_id, int(target),
                "permanent_buffs" if index == 0 else "temporary_buffs",
                json.dumps(data), conn=context.db)
    if stores:
        context.db.commit()
    return remaining


def _damage_multiplier(context, dealer, combat):
    if dealer is None:
        return 1
    from pvp_db import db_card_owner_id
    from .static_rules import controller_flags, rule_modifiers
    flags = set()
    for rule in rule_modifiers(
            context.db, context.session.session_id, context.bstate, int(dealer)):
        if rule.get("property") != "damagemultiplier":
            continue
        if int(rule.get("value", 0) or 0) <= 1:
            continue
        if rule.get("combatdamageonly") and not combat:
            continue
        if rule.get("noncombatdamageonly") and combat:
            continue
        flags.add("double_damage")
    owner = db_card_owner_id(
        context.session.session_id, int(dealer), conn=context.db)
    if owner is not None:
        flags |= controller_flags(
            context.db, context.session.session_id, context.bstate, int(owner))
    return 2 if ("double_damage" in flags or
                 (combat and "double_combat_damage" in flags) or
                 (not combat and "double_noncombat_damage" in flags)) else 1


def _damage_immune(context, target, dealer, combat):
    if dealer is None:
        return False
    from .static_rules import rule_modifiers
    from .targeting import _source_card
    from .filters import records_filter_matches
    target_view = _source_card(
        context.db, context.session.session_id, int(target),
        context.bstate.get("resolving_owner_id", 0))
    dealer_view = _source_card(
        context.db, context.session.session_id, int(dealer),
        context.bstate.get("resolving_owner_id", 0))
    for rule in rule_modifiers(
            context.db, context.session.session_id, context.bstate,
            int(target)):
        if rule.get("property") != "damageimmunity":
            continue
        if bool(rule.get("iscombatdamage")) != bool(combat):
            continue
        spec = rule.get("filter") or rule.get("cardfilter")
        if not spec or records_filter_matches(
                dealer_view, spec, source=target_view,
                context=dict(context.bstate or {})):
            return True
    return False


def _damage_prevented(context, target, combat):
    from .static_rules import effective_stats
    flags = effective_stats(
        context.db, context.session.session_id, context.bstate,
        int(target))[3]
    return ("prevent_combat_damage" in flags if combat else
            "prevent_noncombat_damage" in flags)


def deal_damage(context, target, amount):
    from pvp_db import (db_add_card_damage,
                        db_card_owner_id, db_card_source_info)

    target = int(target)
    amount = max(0, int(amount or 0))
    if amount <= 0:
        return "damage: amount 0"
    owner = context.target_owner(target, default=None)
    is_champion = owner is not None and not db_card_source_info(
        context.session.session_id, target, conn=context.db)
    if owner is None:
        return "damage: no card"
    dealer = context.bstate.get("resolving_source_uid")
    dealer_owner = context.target_owner(dealer, default=owner) if dealer is not None else owner
    combat = bool(context.bstate.get("combat_damage"))
    if dealer is not None and not context.bstate.get("_resolving_would_deal"):
        context.bstate["_resolving_would_deal"] = True
        try:
            replaced = context._emit_trigger(
                "CardWouldDealDamageEvent", int(dealer), int(dealer_owner),
                target_card_id=target,
                event_tac={"damage": amount,
                           "is_combat_damage": int(combat)})
        finally:
            context.bstate.pop("_resolving_would_deal", None)
        if replaced:
            return "replaced"
    if context._emit_trigger("CardWouldBeDamagedEvent", target, int(owner)):
        return "replaced"

    dealer = context.bstate.get("resolving_source_uid")
    combat = bool(context.bstate.get("combat_damage"))
    amount = _consume_shields(context, target, dealer, amount, combat)
    if amount <= 0:
        return "damage: shield prevented"
    if _damage_immune(context, target, dealer, combat):
        return "damage: immunity prevented"
    if _damage_prevented(context, target, combat):
        return "damage: prevention prevented"
    amount *= _damage_multiplier(context, dealer, combat)

    def emit_dealt_damage():
        # The client raises CardDealtDamageEvent after damage has actually
        # been applied.  This event drives follow-up abilities such as Welf's
        # champion power; the replacement events above must not count as
        # damage dealt.
        if dealer is None:
            return
        context._emit_trigger(
            "CardDealtDamageEvent", int(dealer), int(dealer_owner),
            target_card_id=target,
            event_tac={"damage": int(amount),
                       "is_combat_damage": int(combat)})

    if is_champion:
        key = ((context.bstate.get("pvp_health_map") or {}).get(int(owner))
               if context.bstate.get("pvp") else
               ("player_health" if int(owner) else "ai_health"))
        key = key or (f"hp_{int(owner)}" if context.bstate.get("pvp") else
                      ("player_health" if int(owner) else "ai_health"))
        current = int(context.bstate.get(key, 20) or 0)
        new = max(0, current - amount)
        context.bstate[key] = new
        setattr(context.game, key, new)
        event = game_engine.ChampionHealthChangedSessionEventArgs()
        from .runtime_helpers import owner_uid
        event.player_id = owner_uid(owner, context.player_uid,
                                    context.ai_uid, context.bstate)
        event.old_damage_value = current
        event.new_damage_value = new
        context.game._push(event)
        emit_dealt_damage()
        return f"champion {current}->{new}"

    from .static_rules import effective_stats
    stats = effective_stats(
        context.db, context.session.session_id, context.bstate, target)
    if not db_card_source_info(context.session.session_id, target, conn=context.db):
        return "damage: no card"
    db_add_card_damage(context.session.session_id, target, amount,
                       conn=context.db)
    context.db.commit()
    # update_card_state provides the complete card projection; no state bits
    # are changed, so this is an intentional projection-only call.
    context.update_card_state(target, commit=False)
    emit_dealt_damage()
    remaining = int(effective_stats(
        context.db, context.session.session_id, context.bstate, target)[1] or 0)
    if remaining <= 0:
        # Combat damage is simultaneous.  The RulesPort combat resolver runs
        # the state-based lethal/deathcry pass after every combatant has
        # assigned damage, so a lethal hit cannot remove a blocker midway
        # through the same combat.  Ordinary effect damage retains its
        # immediate lethal transition.
        if context.bstate.get("combat_damage"):
            return "survives"
        context.destroy(target)
        return "killed"
    return "survives"

"""RulesPort-owned damage application and replacement lifecycle."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import wraps
from threading import RLock
import game_engine


_DAMAGE_LOCK = RLock()


def serialized_damage(function):
    """Serialize shield read/modify/write and allow nested replacement damage."""
    @wraps(function)
    def call(*args, **kwargs):
        with _DAMAGE_LOCK:
            return function(*args, **kwargs)
    return call


@serialized_damage
def expire_champion_shields(state):
    """Card.ClearEndOfTurnDamageShields for synthetic champion cards."""
    shields = state.get("damage_shields", {})
    for uid, entries in list(shields.items()):
        kept = [entry for entry in entries if entry.get("lasts_indefinitely")]
        if kept:
            shields[uid] = kept
        else:
            shields.pop(uid, None)


@dataclass
class DamageOutcome:
    """Per-call damage accounting; prevention consumes combat assignment too."""

    dealt: int = 0
    absorbed: int = 0


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
    champion_shields = context.bstate.get("damage_shields", {})
    if str(target) in champion_shields:
        stores.append((None, {"damage_shields": champion_shields[str(target)]}))
    remaining = max(0, int(amount or 0))
    prevented = []
    persisted = False
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
            prevented.append(blocked)
            changed = True
            value -= blocked
            if value and not shield.get("one_shot"):
                shield["amount"] = value
                kept.append(shield)
        if changed:
            data["damage_shields"] = kept
            if index is None:
                champion_shields[str(target)] = kept
            else:
                db_set_card_mutation_field(
                    context.session.session_id, int(target),
                    "permanent_buffs" if index == 0 else "temporary_buffs",
                    json.dumps(data), conn=context.db)
                persisted = True
    if persisted:
        context.db.commit()
    # Persist consumption before trigger discovery can inspect/re-enter it.
    for blocked in prevented:
        if dealer is not None:
            context._emit_trigger(
                "DamagePreventedEvent", int(dealer),
                context.target_owner(dealer, default=None), target_card_id=int(target),
                event_tac={"DamagePrevented": blocked})
    return remaining


def card_damage_multiplier(db, session_id, state, uid, combat):
    """Evaluate independent C# multiplier attributes, preserving zero and Set."""
    from pvp_db import db_card_mutation_field
    from .static_rules import rule_modifiers
    factors = {"all": 1, "combat": 1, "noncombat": 1}
    names = {"damagemultiplier": "all", "combatdamagemultiplier": "combat",
             "noncombatdamagemultiplier": "noncombat"}
    for column in ("permanent_buffs", "temporary_buffs"):
        try:
            data = json.loads(db_card_mutation_field(
                session_id, int(uid), column, conn=db) or "{}")
        except (TypeError, ValueError):
            data = {}
        for name, value in (data.get("int_attrs", {}) if isinstance(data, dict)
                            else {}).items():
            bucket = names.get(str(name).lower())
            if bucket is not None:
                factors[bucket] = max(0, int(value))
    for rule in rule_modifiers(db, session_id, state, int(uid)):
        if rule.get("property") != "damagemultiplier":
            continue
        bucket = ("combat" if rule.get("combatdamageonly") else
                  "noncombat" if rule.get("noncombatdamageonly") else "all")
        value = max(0, int(rule.get("value", rule.get("amount", 1))))
        factors[bucket] = value * (1 if rule.get("replaceexistingvalue")
                                  else factors[bucket])
    return factors["all"] * factors["combat" if combat else "noncombat"]


def _damage_multiplier(context, dealer, combat):
    if dealer is None:
        return 1
    from .runtime_helpers import champion_uid_for_owner
    factor = card_damage_multiplier(
        context.db, context.session.session_id, context.bstate, dealer, combat)
    owner = context.target_owner(dealer, default=None)
    champion = (champion_uid_for_owner(context.handler, context.bstate, owner)
                if owner is not None else None)
    if champion is not None and int(champion) != int(dealer):
        factor *= card_damage_multiplier(
            context.db, context.session.session_id, context.bstate, champion, combat)
    return factor


def _damage_immune(context, target, dealer, combat):
    if dealer is None:
        return False
    from .static_rules import rule_modifiers
    from .targeting import _source_card
    from .filters import records_filter_matches
    rules = [rule for rule in rule_modifiers(
        context.db, context.session.session_id, context.bstate, int(target))
        if rule.get("property") == "damageimmunity"
        and bool(rule.get("iscombatdamage")) == bool(combat)
        and (rule.get("filter") or rule.get("cardfilter"))]
    if not rules:
        return False
    target_view = _source_card(
        context.db, context.session.session_id, int(target),
        context.bstate.get("resolving_owner_id", 0))
    dealer_view = _source_card(
        context.db, context.session.session_id, int(dealer),
        context.bstate.get("resolving_owner_id", 0))
    for rule in rules:
        spec = rule.get("filter") or rule.get("cardfilter")
        if spec and records_filter_matches(
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


def _source_is_lethal(context, dealer):
    """Return whether a damage source projects the printed Lethal keyword.

    Lethal is a continuous rule flag (``static_rules.effective_stats``), not an
    attribute bit.  The C# client marks ``LethalDamageTaken`` on any troop a
    Lethal source damages and lets the state-based sweep destroy it, so the
    marker is projected here instead of being folded into the damage amount.
    """
    if dealer is None:
        return False
    from .static_rules import effective_stats
    flags = effective_stats(
        context.db, context.session.session_id, context.bstate, int(dealer))[3]
    return "lethal" in (flags or ())


@serialized_damage
def deal_damage(context, target, amount, *, outcome=None, only_minimum=False):
    from pvp_db import db_add_card_damage, db_card_source_info

    target = int(target)
    amount = max(0, int(amount or 0))
    outcome = outcome if outcome is not None else DamageOutcome()
    outcome.dealt = outcome.absorbed = 0
    if amount <= 0:
        return "damage: amount 0"
    owner = context.target_owner(target, default=None)
    target_info = db_card_source_info(context.session.session_id, target, conn=context.db)
    is_champion = owner is not None and not target_info
    if owner is None:
        return "damage: no card"
    if target_info and "Troop" not in str(target_info[1] or ""):
        return "damage: target cannot take damage"
    dealer = context.bstate.get("resolving_source_uid")
    dealer_owner = context.target_owner(dealer, default=owner) if dealer is not None else owner
    combat = bool(context.bstate.get("combat_damage"))
    outcome.absorbed = amount
    # Session.DamageCard tests immunity before spending consumable shields,
    # then scales damage, spends shields, and dispatches replacement events.
    if _damage_immune(context, target, dealer, combat):
        return "damage: immunity prevented"
    if _damage_prevented(context, target, combat):
        return "damage: prevention prevented"
    # Combat outgoing factors were applied before blocker assignment.
    if not combat:
        amount *= _damage_multiplier(context, dealer, combat)
    before_shields = amount
    amount = _consume_shields(context, target, dealer, amount, combat)
    prevented = before_shields - amount
    if amount <= 0:
        return "damage: shield prevented"
    from .static_rules import effective_stats
    target_health_before = (int(effective_stats(
        context.db, context.session.session_id, context.bstate, target)[1] or 0)
                            if not is_champion else None)
    damage_before_minimum = int(amount)
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
    if context._emit_trigger(
            "CardWouldBeDamagedEvent", target, int(owner),
            event_tac={"damage": amount, "is_combat_damage": int(combat)}):
        return "replaced"

    if only_minimum and not is_champion:
        from .static_rules import effective_stats
        health = int(effective_stats(
            context.db, context.session.session_id, context.bstate, target)[1] or 0)
        amount = min(amount, 1 if _source_is_lethal(context, dealer) else max(0, health))
    outcome.dealt = amount
    outcome.absorbed = amount + prevented
    # SpiritDrain applies to effect damage as well as combat, and heals only
    # damage remaining after prevention and minimum-to-kill assignment.
    if dealer is not None and amount > 0:
        from .static_rules import effective_stats
        attributes = effective_stats(
            context.db, context.session.session_id, context.bstate, int(dealer))[2]
        if int(attributes or 0) & int(game_engine.ECardAttributes.SpiritDrain):
            context.gain_health(int(dealer_owner), amount)

    def emit_dealt_damage():
        # The client raises CardDealtDamageEvent after damage has actually
        # been applied.  This event drives follow-up abilities such as Welf's
        # champion power; the replacement events above must not count as
        # damage dealt.
        if amount > 0 and dealer is not None:
            from .statistics import add_card_stat
            add_card_stat(context.bstate, int(dealer), dealer_owner,
                          "DamageDealt", amount)
            if int(owner) != int(dealer_owner):
                add_card_stat(context.bstate, int(dealer), dealer_owner,
                              "DamageDealtToOpponent", amount)
                add_card_stat(
                    context.bstate, int(dealer), dealer_owner,
                    "CombatDamageDealtToOpponent" if combat else
                    "NonCombatDamageDealtToOpponent", amount)
            if context.bstate.get("resolving_ability"):
                from .statistics import add_ability_stat, ability_stat
                add_ability_stat(context.bstate, "DamageDealt", amount)
                context.bstate["_ability_damage_dealt"] = ability_stat(
                    context.bstate, "DamageDealt")
        if amount > 0:
            from .statistics import (add_card_stat,
                                     record_ability_card_list)
            add_card_stat(context.bstate, target, owner,
                          "DamageTaken", amount)
            if combat:
                add_card_stat(context.bstate, target, owner,
                              "CombatDamageTaken", amount)
            record_ability_card_list(
                context.bstate, "DamagedCards", target)
            if (dealer is not None and not is_champion and
                    target_health_before is not None):
                excess = (damage_before_minimum - 1
                          if _source_is_lethal(context, dealer) else
                          damage_before_minimum - target_health_before)
                if excess > 0:
                    add_card_stat(context.bstate, int(dealer), dealer_owner,
                                  "ExcessDamageDealt", excess)
        if dealer is not None:
            context._emit_trigger(
                "CardDealtDamageEvent", int(dealer), int(dealer_owner),
                target_card_id=target,
                event_tac={"damage": int(amount),
                           "DamageDealt": int(amount),
                           "is_combat_damage": int(combat)})
        context._emit_trigger(
            "CardDamagedEvent", target, int(owner),
            event_tac={"damage": int(amount), "is_combat_damage": int(combat)})

    if is_champion:
        key = ((context.bstate.get("pvp_health_map") or {}).get(int(owner))
               if context.bstate.get("pvp") else
               ("player_health" if int(owner) else "ai_health"))
        key = key or (f"hp_{int(owner)}" if context.bstate.get("pvp") else
                      ("player_health" if int(owner) else "ai_health"))
        current = int(context.bstate.get(key, 20) or 0)
        # C# ``DamageChampion`` sets ``CurrentDefenseValue - amount`` with no
        # clamp and lets the state-based action test ``<= 0`` afterwards.  The
        # clamp let a simultaneous lifelinker "rescue" a champion the same
        # strike had already killed: 2 health took 10 damage (clamped to 0),
        # then the blocker's lifedrain healed it back to 4.
        new = current - amount
        context.bstate[key] = new
        setattr(context.game, key, new)
        event = game_engine.ChampionHealthChangedSessionEventArgs()
        from .runtime_helpers import owner_uid
        event.player_id = owner_uid(owner, context.player_uid,
                                    context.ai_uid, context.bstate)
        event.old_damage_value = current
        event.new_damage_value = new
        context.game._push(event)
        from .effect_lifetimes import expire_damage_bound_intattrs, expire_grants
        expire_damage_bound_intattrs(
            context.db, context.session.session_id, context.bstate, target)
        expire_grants(
            context.db, context.session.session_id, context.bstate,
            damaged_uid=target, handler=context.handler)
        context._push_champion_intattrs(owner, target)
        emit_dealt_damage()
        return f"champion {current}->{new}"

    from .static_rules import effective_stats
    target_info = db_card_source_info(
        context.session.session_id, target, conn=context.db)
    if not target_info:
        return "damage: no card"
    is_troop = "Troop" in str(target_info[1] or "")
    db_add_card_damage(context.session.session_id, target, amount,
                       conn=context.db)
    context.db.commit()
    from .effect_lifetimes import expire_damage_bound_intattrs, expire_grants
    expire_damage_bound_intattrs(
        context.db, context.session.session_id, context.bstate, target)
    expire_grants(
        context.db, context.session.session_id, context.bstate,
        damaged_uid=target, handler=context.handler)
    # update_card_state provides the complete card projection; no state bits
    # are changed, so this is an intentional projection-only call.
    context.update_card_state(target, commit=False)
    emit_dealt_damage()
    remaining = int(effective_stats(
        context.db, context.session.session_id, context.bstate, target)[1] or 0)
    # Lethal: any damage a Lethal source deals to a troop is lethal to that
    # troop even when it is below the troop's remaining defense.  The C#
    # client records ``LethalDamageTaken`` and destroys the troop in the
    # state-based sweep; mirror that by deferring a combat Lethal hit to the
    # shared state-based pass while ordinary effect damage still transitions
    # immediately.
    lethal_hit = (amount > 0 and is_troop
                  and _source_is_lethal(context, dealer))
    if remaining <= 0 or lethal_hit:
        if context.bstate.get("combat_damage"):
            if lethal_hit and remaining > 0:
                marked = context.bstate.setdefault("_lethal_damage_uids", [])
                if target not in marked:
                    marked.append(target)
            return "survives"
        context.destroy(target)
        return "killed"
    return "survives"

"""RulesPort-owned authored card-battle effect."""

from __future__ import annotations


def _first_target(values):
    if values is None:
        return None
    if not isinstance(values, (tuple, list, set)):
        values = (values,)
    for value in values:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _secondary_target(context, effect):
    """Resolve the first combatant through the referenced effect instance."""
    if effect is None:
        return None
    index = (effect.get("secondary_target_index", -1)
             if isinstance(effect, dict)
             else getattr(effect, "secondary_target_index", -1))
    try:
        index = int(index)
    except (TypeError, ValueError):
        return None
    if index < 0:
        return None

    ability = getattr(context, "ability", None)
    referenced = None
    for candidate in getattr(ability, "ordered_effects", ()) or ():
        instance_id = (candidate.get("effect_instance_id", -1)
                       if isinstance(candidate, dict)
                       else getattr(candidate, "effect_instance_id", -1))
        try:
            if int(instance_id) == index:
                referenced = candidate
                break
        except (TypeError, ValueError):
            continue
    if referenced is None:
        return None
    target_index = (referenced.get("target_index", -1)
                    if isinstance(referenced, dict)
                    else getattr(referenced, "target_index", -1))
    try:
        target_index = int(target_index)
    except (TypeError, ValueError):
        return None
    if target_index < 0:
        return None

    activation = getattr(ability, "activation", None)
    target_map = getattr(activation, "target_map", None) or {}
    value = target_map.get(target_index, target_map.get(str(target_index)))
    if value is None:
        target_map = context.bstate.get("ability_target_map") or {}
        value = target_map.get(target_index, target_map.get(str(target_index)))
    return _first_target(value)


def battle_cards(context, effect=None):
    """Have the resolved cards deal their authored combat damage."""
    attacker = _secondary_target(context, effect)
    defender = context.resolved_target()
    if effect is None:
        # Keep direct compatibility callers working.  The native dispatch
        # always supplies the Records effect instance and follows its target
        # mapping instead of inferring participants from stored target state.
        attacker = context.bstate.get("resolving_source_uid")
    if attacker is None or defender is None:
        return "battle: need two cards"
    attacker, defender = int(attacker), int(defender)

    def attack(uid):
        from .static_rules import effective_stats
        return int(effective_stats(
            context.db, context.session.session_id, context.bstate,
            int(uid))[0] or 0)

    def as_source(source_uid, operation):
        had_source = "resolving_source_uid" in context.bstate
        previous_source = context.bstate.get("resolving_source_uid")
        context.bstate["resolving_source_uid"] = int(source_uid)
        try:
            return operation()
        finally:
            if had_source:
                context.bstate["resolving_source_uid"] = previous_source
            else:
                context.bstate.pop("resolving_source_uid", None)

    attacker_attack = attack(attacker)
    defender_attack = attack(defender)
    result = as_source(
        attacker, lambda: context.damage(defender, attacker_attack))
    logs = [f"{hex(attacker)} deals {attacker_attack} to "
            f"{hex(defender)} -> {result}"]
    # Battle2Cards carries the reciprocal-damage rule in the typed Records
    # template; never infer it from the ability's display text.
    if bool(context.template_value("m_FightBack", 0)):
        as_source(attacker, lambda: context._emit_authored_event(
            "CardBattledEvent", defender))
        if attacker != defender:
            reverse = as_source(
                defender,
                lambda: context.damage(attacker, defender_attack))
            as_source(defender, lambda: context._emit_authored_event(
                "CardBattledEvent", attacker))
            logs.append(
                f"{hex(defender)} deals {defender_attack} to "
                f"{hex(attacker)} -> {reverse}")
    return "; ".join(logs)

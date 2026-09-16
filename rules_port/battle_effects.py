"""RulesPort-owned authored card-battle effect."""

from __future__ import annotations


def battle_cards(context):
    """Have the resolved cards deal their authored combat damage."""
    source = context.bstate.get("resolving_source_uid")
    target = context.resolved_target()
    stored = ((context.bstate.get("stored_targets") or {}).get(
        context.bstate.get("resolving_ability")) or [])
    uids = []
    for value in (source, target, *stored):
        if value is not None and int(value) not in uids:
            uids.append(int(value))
    if len(uids) < 2:
        return "battle: need two cards"
    attacker, defender = uids[0], uids[-1]

    from pvp_db import db_card_mutation_info
    def card_type(uid):
        row = db_card_mutation_info(
            context.session.session_id, int(uid), conn=context.db)
        return str(row[2] if row else "Champion").lower()

    def attack(uid):
        from .static_rules import effective_stats
        return int(effective_stats(
            context.db, context.session.session_id, context.bstate,
            int(uid))[0] or 0)

    # A champion source with a remembered troop target is the authored talent
    # form “that troop deals its ATK to you”.
    if "champion" in card_type(attacker) and "troop" in card_type(defender):
        previous = context.bstate.get("resolving_source_uid")
        context.bstate["resolving_source_uid"] = defender
        try:
            result = context.damage(attacker, attack(defender))
        finally:
            if previous is None:
                context.bstate.pop("resolving_source_uid", None)
            else:
                context.bstate["resolving_source_uid"] = previous
        return f"{hex(defender)} deals {attack(defender)} to you -> {result}"

    result = context.damage(defender, attack(attacker))
    context._emit_authored_event("CardBattledEvent", defender)
    logs = [f"{hex(attacker)} deals {attack(attacker)} to {hex(defender)} -> {result}"]
    # Battle2Cards carries the reciprocal-damage rule in the typed Records
    # template; never infer it from the ability's display text.
    if bool(context.template_value("m_FightBack", 0)):
        reverse = context.damage(attacker, attack(defender))
        previous = context.bstate.get("resolving_source_uid")
        context.bstate["resolving_source_uid"] = defender
        try:
            context._emit_authored_event("CardBattledEvent", attacker)
        finally:
            context.bstate["resolving_source_uid"] = previous
        logs.append(f"{hex(defender)} deals {attack(defender)} to {hex(attacker)} -> {reverse}")
    return "; ".join(logs)

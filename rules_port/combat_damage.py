"""RulesPort-native combat damage and state-based-death resolution."""

from __future__ import annotations

from dataclasses import dataclass

import game_engine

from .combat import Combat, CombatId, CombatPhase, CombatResolver
from .damage_effects import deal_damage


def _apply_lifelink(context, source_uid, amount):
    if not amount:
        return
    from pvp_db import db_card_owner_id
    from .static_rules import effective_stats
    values = effective_stats(
        context.db, context.session.session_id, context.bstate, int(source_uid))
    if not (int(values[2] or 0) & int(game_engine.ECardAttributes.SpiritDrain)):
        return
    owner = db_card_owner_id(
        context.session.session_id, int(source_uid), conn=context.db)
    if owner is None:
        return
    if context.bstate.get("pvp"):
        key = f"hp_{int(owner)}"
    else:
        key = "player_health" if int(owner) else "ai_health"
    current = int(context.bstate.get(key, 20) or 0)
    new_value = min(20, current + int(amount))
    if new_value == current:
        return
    context.bstate[key] = new_value
    setattr(context.game, key, new_value)
    from .runtime_helpers import owner_uid
    event = game_engine.ChampionHealthChangedSessionEventArgs()
    event.player_id = owner_uid(owner, context.player_uid,
                                context.ai_uid, context.bstate)
    event.old_damage_value = current
    event.new_damage_value = new_value
    context.game._push(event)
    context.emit_champion_healed(owner, current, new_value)


@dataclass
class _Combatant:
    uid: int
    attack: int = 0
    attributes: int = 0
    is_troop: bool = True
    in_warzone: bool = True
    damage_champion_multiplier: int = 1
    damage_multiplier: int = 1
    combat_damage_multiplier: int = 1
    rule_flags: set = None

    @property
    def session_card_id(self):
        return self.uid

    @property
    def combat_damage(self):
        # C# Card.CalculateTotalCombatDamageToDeal: attack x DamageMultiplier
        # x CombatDamageMultiplier (card and champion).  The champion factors
        # are folded in by the caller when known.
        return max(0, int(self.attack) *
                   max(0, int(self.damage_multiplier or 1)) *
                   max(0, int(self.combat_damage_multiplier or 1)))

    @property
    def firststrike(self):
        return bool(self.attributes & int(game_engine.ECardAttributes.FirstStrike))

    @property
    def dualstrike(self):
        return bool(self.attributes & int(game_engine.ECardAttributes.DualStrike))

    @property
    def lethal(self):
        return "lethal" in (self.rule_flags or set())

    @property
    def crush(self):
        return "crush" in (self.rule_flags or set())

    @property
    def juggernaut(self):
        return bool(self.attributes & int(game_engine.ECardAttributes.Juggernaught))

    def cares_about_combat_phase(self, phase):
        """Mirror ``Card.CaresAboutCombatPhase`` for a live card fact."""
        if phase == CombatPhase.FIRST_STRIKE:
            return self.firststrike or self.dualstrike
        return not self.firststrike or self.dualstrike


def _fact(db, session_id, uid, battle_state=None):
    from pvp_db import db_card_location, db_card_source_info
    from .static_rules import effective_stats
    from .combat_rules import card_int_attr
    values = effective_stats(db, session_id, battle_state or {}, int(uid))
    if not db_card_source_info(session_id, int(uid), conn=db):
        return None
    return _Combatant(
        int(uid), attack=max(0, int(values[0] or 0)),
        attributes=int(values[2] or 0), rule_flags=set(values[3] or ()),
        damage_multiplier=max(0, card_int_attr(
            db, session_id, int(uid), "DamageMultiplier")) or 1,
        combat_damage_multiplier=max(0, card_int_attr(
            db, session_id, int(uid), "CombatDamageMultiplier")) or 1,
        in_warzone=str(db_card_location(session_id, int(uid), conn=db) or "").lower() == "warzone")


def resolve(context, *, first_strike=False, attacker_key="player_attackers",
            blocker_key="ai_blockers"):
    """Resolve persisted combat declarations using the port algorithm."""
    import game_engine

    previous_native = context.bstate.get("_rules_port_native_effect")
    context.bstate["_rules_port_native_effect"] = True

    attackers = {int(uid): int(defender) for uid, defender in
                 (context.bstate.get(attacker_key) or {}).items()}
    blockers = {int(uid): [int(value) for value in values]
                for uid, values in (context.bstate.get(blocker_key) or {}).items()}
    order = {int(uid): [int(value) for value in values]
             for uid, values in (context.bstate.get("player_damage_order") or {}).items()}
    if not attackers:
        if previous_native is None:
            context.bstate.pop("_rules_port_native_effect", None)
        else:
            context.bstate["_rules_port_native_effect"] = previous_native
        return context.bstate
    phase = CombatPhase.FIRST_STRIKE if first_strike else CombatPhase.STANDARD
    for attacker_uid, defender_uid in attackers.items():
        attacker = _fact(context.db, context.session.session_id, attacker_uid,
                          context.bstate)
        if attacker is None:
            continue
        defender = _Combatant(defender_uid, is_troop=False)
        combat = Combat(
            instigator=context.player_uid, defender=defender,
            combat_id=CombatId(attacker_uid, attacker_uid & 0xFFFF),
            attacker=attacker)
        blocker_facts = []
        for blocker_uid in order.get(attacker_uid, blockers.get(attacker_uid, ())):
            fact = _fact(context.db, context.session.session_id, blocker_uid,
                         context.bstate)
            if fact is not None:
                blocker_facts.append(fact)
        combat.blockers = blocker_facts
        combat.flags |= 8 if blocker_facts else 0
        old_source = context.bstate.get("resolving_source_uid")
        old_combat = context.bstate.get("combat_damage")

        def damage(source, target, amount, only_minimum):
            source_uid = int(getattr(source, "uid", source))
            target_uid = int(getattr(target, "uid", target))
            allocated = int(amount or 0)
            # C# DamageCard(onlyDoMinimumToKill): the attacker's excess damage
            # is held back only while more blockers remain.  For the last
            # blocker (or a Juggernaut) the full remaining damage is dealt, so
            # clamping unconditionally under-reported the damage event.
            if getattr(target, "is_troop", False) and only_minimum:
                if getattr(source, "lethal", False):
                    # A Lethal source assigns a single damage (its damage is
                    # lethal regardless of the blocker's defense), leaving the
                    # remainder for the next blocker or Crush/Juggernaut
                    # overflow, matching the client's DamageCard.
                    allocated = 1
                else:
                    from .static_rules import effective_stats
                    stats = effective_stats(
                        context.db, context.session.session_id,
                        context.bstate, target_uid)
                    allocated = min(allocated, max(0, int(stats[1] or 0)))
            context.bstate["resolving_source_uid"] = source_uid
            context.bstate["combat_damage"] = True
            try:
                result = deal_damage(context, target_uid, allocated)
                if str(result).startswith(("champion ", "survives", "killed")):
                    _apply_lifelink(context, source_uid, allocated)
            finally:
                if old_source is None:
                    context.bstate.pop("resolving_source_uid", None)
                else:
                    context.bstate["resolving_source_uid"] = old_source
                if old_combat is None:
                    context.bstate.pop("combat_damage", None)
                else:
                    context.bstate["combat_damage"] = old_combat
            return allocated if str(result).startswith(("champion ", "survives", "killed")) else 0

        CombatResolver.resolve(combat, phase, damage)
    # State-based actions are evaluated only after all combat damage for this
    # step has been assigned. This preserves simultaneous damage and keeps
    # Deathcries out of the middle of an unrelated combatant's assignment.
    from .death_effects import state_based_deaths
    state_based_deaths(context)
    context.bstate.pop("player_damage_order", None)
    if previous_native is None:
        context.bstate.pop("_rules_port_native_effect", None)
    else:
        context.bstate["_rules_port_native_effect"] = previous_native
    return context.bstate

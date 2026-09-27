"""RulesPort-native combat damage and state-based-death resolution."""

from __future__ import annotations

from dataclasses import dataclass, field

import game_engine

from .combat import Combat, CombatId, CombatPhase, CombatResolver
from .damage_effects import DamageOutcome, deal_damage, serialized_damage


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
    rule_flags: set[str] = field(default_factory=set)

    @property
    def session_card_id(self):
        return self.uid

    @property
    def combat_damage(self):
        # C# Card.CalculateTotalCombatDamageToDeal: attack x DamageMultiplier
        # x CombatDamageMultiplier (card and champion).  The champion factors
        # are folded in by the caller when known.
        return max(0, int(self.attack) *
                   max(0, int(self.damage_multiplier)) *
                   max(0, int(self.combat_damage_multiplier)))

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


def _fact(db, session_id, uid, battle_state=None, context=None):
    from pvp_db import db_card_location, db_card_source_info
    from .static_rules import effective_stats
    values = effective_stats(db, session_id, battle_state or {}, int(uid))
    if not db_card_source_info(session_id, int(uid), conn=db):
        return None
    from .damage_effects import card_damage_multiplier, _damage_multiplier
    multiplier = (_damage_multiplier(context, uid, True) if context is not None else
                  card_damage_multiplier(db, session_id, battle_state or {}, uid, True))
    return _Combatant(
        int(uid), attack=max(0, int(values[0] or 0)),
        attributes=int(values[2] or 0), rule_flags=set(values[3] or ()),
        damage_multiplier=multiplier,
        in_warzone=str(db_card_location(session_id, int(uid), conn=db) or "").lower() == "warzone")


@serialized_damage
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
    # Attacks whose declared blockers have all left play (see
    # ``combat.remove_troop_from_combat``) keep the client's sticky
    # ``AttackBlocked`` fact across the damage steps.
    blocked_attackers = {str(uid) for uid, flag in
                         (context.bstate.get("blocked_attackers")
                          or {}).items() if flag}
    if not attackers:
        if previous_native is None:
            context.bstate.pop("_rules_port_native_effect", None)
        else:
            context.bstate["_rules_port_native_effect"] = previous_native
        return context.bstate
    phase = CombatPhase.FIRST_STRIKE if first_strike else CombatPhase.STANDARD
    # The client's combat presentation is driven by BeginCombatResolution /
    # CombatPhaseResolved / EndCombatResolution.  OnCombatPhaseResolved refuses
    # to run without a preceding Begin, and ChampionHealthChanged is deferred
    # into the combat animation group until End publishes it, so a native
    # damage step that omits the trio leaves the champion's health display
    # unchanged even though the server applied the damage.
    game = context.game
    game.push_begin_combat_resolution()
    try:
        for attacker_uid, defender_uid in attackers.items():
            attacker = _fact(context.db, context.session.session_id, attacker_uid,
                              context.bstate, context=context)
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
                             context.bstate, context=context)
                if fact is not None:
                    blocker_facts.append(fact)
            combat.blockers = blocker_facts
            # C# ``Combat.DeclareBlockers`` sets ``ECombatFlags.AttackBlocked`` from
            # the declaration and never clears it.  A blocker that has since left
            # play (killed in the Swiftstrike step and returned by a Deathcry, or
            # bounced) is gone from the live list but the attack stays blocked, so
            # it deals no champion damage without Crush.
            blocked = bool(blocker_facts) or str(attacker_uid) in blocked_attackers
            combat.flags |= 8 if blocked else 0
            game.push_combat_phase_resolved(
                combat.combat_id,
                game_engine.SessionCardId(game_engine.UID(int(attacker_uid))),
                game_engine.SessionCardId(game_engine.UID(int(defender_uid))),
                [game_engine.SessionCardId(game_engine.UID(int(fact.uid)))
                 for fact in blocker_facts],
                phase=int(game_engine.ECombatPhase.FirstStrike if first_strike
                          else game_engine.ECombatPhase.Standard))
            old_source = context.bstate.get("resolving_source_uid")
            old_combat = context.bstate.get("combat_damage")

            def damage(source, target, amount, only_minimum):
                source_uid = int(getattr(source, "uid", source))
                target_uid = int(getattr(target, "uid", target))
                outcome = DamageOutcome()
                context.bstate["resolving_source_uid"] = source_uid
                context.bstate["combat_damage"] = True
                try:
                    deal_damage(context, target_uid, int(amount or 0),
                                outcome=outcome, only_minimum=only_minimum)
                finally:
                    if old_source is None:
                        context.bstate.pop("resolving_source_uid", None)
                    else:
                        context.bstate["resolving_source_uid"] = old_source
                    if old_combat is None:
                        context.bstate.pop("combat_damage", None)
                    else:
                        context.bstate["combat_damage"] = old_combat
                # Session.DamageCard's out parameter includes shield prevention;
                # blocked damage cannot be assigned to the next blocker again.
                return outcome.absorbed

            CombatResolver.resolve(combat, phase, damage)
        # State-based actions are evaluated only after all combat damage for this
        # step has been assigned. This preserves simultaneous damage and keeps
        # Deathcries out of the middle of an unrelated combatant's assignment.
        from .death_effects import state_based_deaths
        state_based_deaths(context)
        context.bstate.pop("player_damage_order", None)
    finally:
        game.push_end_combat_resolution()
    if previous_native is None:
        context.bstate.pop("_rules_port_native_effect", None)
    else:
        context.bstate["_rules_port_native_effect"] = previous_native
    return context.bstate

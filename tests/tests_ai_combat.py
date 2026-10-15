"""Rules of the ported client combat solver (ai_combat.py)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import ai_combat as ac  # noqa: E402
import game_engine  # noqa: E402

ECardAttributes = game_engine.ECardAttributes


class StubCard:
    """The CardInfo surface the solver reads."""

    def __init__(self, uid, attack, defense, attributes=0):
        self.card_uid = uid
        self.attack_base = attack
        self.defense_base = defense
        self.attributes = attributes
        self.card_type = "Troop"

    def effective_attack(self, in_play=False):
        return self.attack_base

    def effective_defense(self, in_play=False):
        return self.defense_base

    def has_attribute(self, flag):
        return bool(self.attributes & int(flag))

    def is_troop(self):
        return True


class StubEvaluator:
    """Flat values: every card is worth 1, losing a card costs 0.75."""

    class Personality:
        values = {"Attack": 1.0, "Defense": 0.75, "Timidness": 0.95,
                  "Aggressiveness": 0.75, "DamageParityValue": 10.0}

    personality = Personality()
    player_health = 20

    def get_card_value(self, card):
        return 1.0

    def loss_value(self, card):
        return 0.75


def hint(card):
    return ac.Hint(card, StubEvaluator())


def test_lethal_attack_is_chumped_even_when_the_trade_is_not_worth_it():
    """A 3-power attacker kills a 2-life champion; the 0-power blocker stops
    the damage, so the solver must block it despite the value gate."""
    attacker = hint(StubCard(1, 3, 3))
    blockers = [hint(StubCard(201, 0, 1)), hint(StubCard(202, 2, 1))]
    solved = ac.solve_block([attacker], blockers, 2, StubEvaluator())
    assert set(solved) == {1} and len(solved[1]) == 1, solved


def test_a_blocker_is_used_at_most_once():
    attackers = [hint(StubCard(1, 1, 1)), hint(StubCard(2, 1, 1))]
    blockers = [hint(StubCard(201, 0, 4))]
    solved = ac.solve_block(attackers, blockers, 2, StubEvaluator())
    used = [b for uids in solved.values() for b in uids]
    assert used.count(201) <= 1, solved


def test_unblockable_attacker_is_never_blocked():
    """ValidBlock: a CantBeBlocked attacker takes no blocker."""
    attacker = hint(StubCard(1, 3, 3, int(ECardAttributes.CantBeBlocked)))
    result = ac.AttackResult([attacker], [], 2, ac.CombatBuff(), {})
    assert not result.valid_block(attacker, hint(StubCard(9, 0, 4)), None,
                                  None, None)


def test_flyer_is_only_blocked_by_flyers_or_skyguard():
    """ValidBlock: a Flight attacker needs Flight or SkyGuard blockers."""
    flyer = hint(StubCard(1, 2, 2, int(ECardAttributes.Flight)))
    ground = hint(StubCard(9, 0, 4))
    result = ac.AttackResult([flyer], [], 2, ac.CombatBuff(), {})
    assert not result.valid_block(flyer, ground, None, None, None)
    sky = hint(StubCard(10, 0, 4, int(ECardAttributes.SkyGuard)))
    assert result.valid_block(flyer, sky, None, None, None)


def test_unblockable_attacker_always_attacks():
    """DetermineBestAttack: CantBeBlocked troops are committed first."""
    unblockable = hint(StubCard(1, 1, 1, int(ECardAttributes.CantBeBlocked)))
    chosen = ac.determine_best_attack([unblockable], [], 2, StubEvaluator())
    assert [h.uid for h in chosen] == [1]


def test_direct_damage_value_is_lethal_at_the_champion_defense():
    """GetValueOfDirectDamage: damage that kills is worth 10000."""
    assert ac.direct_damage_value(2, 2) == 10000.0
    assert ac.direct_damage_value(1, 5) > 0.0


def test_attacker_orders_damage_through_the_blocker_that_gives_most_value():
    """The attacking AI kills the high-value blocker first when its damage
    cannot kill both: a 2/2 attacker through a 0/2 (valuable) and a 0/1."""
    from ai_combat import Hint, best_damage_order

    class ValueByUid(StubEvaluator):
        def get_card_value(self, card):
            return 5.0 if int(card.card_uid) == 2 else 1.0

    evaluator = ValueByUid()
    attacker = Hint(StubCard(1, 2, 2), evaluator)
    valuable = Hint(StubCard(2, 0, 2), evaluator)
    cheap = Hint(StubCard(3, 0, 1), evaluator)
    ordered = best_damage_order(attacker, [cheap, valuable], evaluator)
    assert [h.uid for h in ordered][0] == 2, [h.uid for h in ordered]

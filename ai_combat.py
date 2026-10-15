"""Port of the client AI's combat solvers (Game/Shared/AI/AICombat.cs).

Blocking: ``solve_block`` assigns the AI's blockers to the player's attackers.
Each attacker can take up to four blockers (dogpile), and every assignment is
scored by ``AttackResult``'s search: the lowest-value line wins, and a combat
buff (an ability that adds attack, defence or first strike for one combat)
is only applied when it changes the result.

Attacking: ``determine_best_attack`` picks the AI's attack set.  Unblockable
troops always attack; flyers and ground troops are solved separately against
the blockers that can stop them.

The solvers work on ``Hint`` wrappers, one per card.  A ``Hint`` exposes the
same surface as the client's ``AIHints``: ``attack(attacking)``,
``defense(attacking)``, ``value``, ``loss_value``, ``combat_value`` and
``has_attribute``.  Card stats and attributes come from ``ai_eval.CardInfo``
and values from the Python ``CardEvaluator``.
"""

from __future__ import annotations

import math

import game_engine

ECardAttributes = game_engine.ECardAttributes

# Sentinel the client uses for "this combat cannot happen".
INVALID = -1
# Value the client assigns to a combat that kills the champion outright.
LETHAL_VALUE = 1000000.0


class CombatBuff:
    """AICombat.AICombatBuff: a one-combat stat or first-strike bonus."""

    def __init__(self, other: "CombatBuff | None" = None):
        self.team = False
        self.value = 0.0
        self.target = None
        self.attack = 0
        self.defense = 0
        self.swiftstrike = False
        self.id = None
        if other is not None:
            # A copy keeps the bonus but not the result of an earlier search.
            self.team = other.team
            self.attack = other.attack
            self.defense = other.defense
            self.swiftstrike = other.swiftstrike

    @property
    def used(self) -> bool:
        return self.target is not None

    @property
    def valid(self) -> bool:
        return self.id is not None

    @property
    def interesting(self) -> bool:
        return self.attack > 0 or self.defense > 0 or self.swiftstrike

    def __bool__(self) -> bool:
        return self.target is None and self.interesting


class Hint:
    """One card as the combat solver sees it (AIHints)."""

    def __init__(self, card, evaluator, *, card_value_fn=None):
        self.card = card
        self.uid = int(card.card_uid)
        self.evaluator = evaluator
        self._card_value_fn = card_value_fn
        self.combat_buff: CombatBuff | None = None
        self.combat_key = 0

    # -- stats --------------------------------------------------------------
    def attack(self, attacking: bool = True) -> int:
        """Attack power.  Attacking adds rage when the card is not yet
        attacking; any combat buff adds its attack.  Clamped at zero."""
        num = self.card.effective_attack(in_play=True)
        if attacking and not getattr(self.card, "attacking", False):
            num += int(getattr(self.card, "rage_value", 0) or 0)
        if self.combat_buff is not None:
            num += self.combat_buff.attack
        return max(0, num)

    def defense(self, attacking: bool = True) -> int:
        """Defence.  A combat buff adds its defence.  Clamped at zero."""
        num = self.card.effective_defense(in_play=True)
        if self.combat_buff is not None:
            num += self.combat_buff.defense
        return max(0, num)

    def has_attribute(self, flag) -> bool:
        if self.card.has_attribute(flag):
            return True
        return (flag == ECardAttributes.FirstStrike and
                self.combat_buff is not None and self.combat_buff.swiftstrike)

    # -- values ---------------------------------------------------------------
    @property
    def value(self) -> float:
        base = self._value()
        if self.combat_buff is not None:
            return base + self.combat_buff.value
        return base

    @property
    def loss_value(self) -> float:
        base = self.evaluator.loss_value(self.card)
        if self.combat_buff is not None:
            return base + self.combat_buff.value
        return base

    def _value(self) -> float:
        if self._card_value_fn is not None:
            return self._card_value_fn(self.card)
        return self.evaluator.get_card_value(self.card)

    def combat_value(self, troop_ratio: float = 1.0) -> float:
        """AICardEvaluator.CalculateCombatValue: the order the solvers sort
        and window by.  Troops weigh printed attack and defence by the
        personality, scaled by the opponent/own troop ratio."""
        values = self.evaluator.personality.values
        num = 0.0
        if self.card.is_troop():
            num += self.card.attack_base * values["Attack"]
            num += self.card.defense_base * values["Defense"]
            if self.card.has_attribute(ECardAttributes.CantAttack) and \
                    self.card.has_attribute(ECardAttributes.CantBlock):
                num *= 0.1
            num *= troop_ratio
        return num


class WindowList:
    """AICombat.WindowList: an ordered list of hints with combat keys."""

    def __init__(self, cards=None):
        self.cards = list(cards or [])

    @property
    def valid(self) -> bool:
        return len(self.cards) > 0

    def sort_by_value(self, troop_ratio: float = 1.0) -> None:
        """Selection sort, highest combat value first (the client's order)."""
        ordered: list = []
        remaining = list(self.cards)
        while len(ordered) < len(self.cards):
            best = None
            for hint in remaining:
                if hint not in ordered:
                    best = hint
            for hint in remaining:
                if hint not in ordered and \
                        hint.combat_value(troop_ratio) > best.combat_value(troop_ratio):
                    best = hint
            ordered.append(best)
        self.cards = ordered

    def set_combat_keys(self) -> None:
        for number, hint in enumerate(self.cards, start=1):
            hint.combat_key = number

    def get_window(self, leading: int, trailing: int) -> list:
        if leading + trailing >= len(self.cards):
            return list(self.cards)
        window = list(self.cards[:leading])
        window.extend(self.cards[len(self.cards) - trailing:])
        return window

    def pop(self):
        hint = self.cards[0]
        self.remove(hint)
        return hint

    def remove(self, hint) -> None:
        if hint in self.cards:
            self.cards.remove(hint)


class AttackResult:
    """AICombat.AttackResult: the best block for one set of attackers.

    ``value`` is the total value of the combats (lower is better for the
    attacker and higher for the AI when it defends; the search keeps the
    minimum).  ``used_blockers`` is the blocker set the best line uses.
    """

    def __init__(self, attackers, blockers, defender_defense, buff, combat_results,
                 *, erosion=1.0, toxified=False, troop_ratio=1.0):
        self.attackers = list(attackers)
        self.blockers = list(blockers)
        self.defender_defense = int(defender_defense)
        self.erosion = erosion
        self.toxified = toxified
        self.combat_results = combat_results
        self.buff = CombatBuff(buff) if buff is not None and not buff.used else CombatBuff()
        self.value = math.nan
        self.buffed_value = math.nan
        self.used_blockers: list = []
        self.combat_buff_target = None
        self._calculate()

    # -- valuation ------------------------------------------------------------
    def _calculate(self) -> None:
        self._test_combat(list(self.attackers), list(self.blockers))

    def _test_combat(self, attackers: list, blockers: list) -> None:
        """Try every assignment of the four blocker slots to the attackers
        (and to "no attacker" for each slot) and keep the lowest-value line.
        Each attacker's own combat is valued by ``value_attack``."""
        while len(blockers) < 4:
            blockers.append(None)
        attackers = attackers + [None]
        count = len(attackers)
        for i in range(count):
            for j in range(count):
                for k in range(count):
                    for l in range(count):
                        self._try_assignment(attackers, blockers, i, j, k, l)

    def _try_assignment(self, attackers, blockers, i, j, k, l) -> None:
        total_fail = False
        num_damage = 0
        total = 0.0
        self.buff.value = 0.0
        for m, attacker in enumerate(attackers):
            b0 = blockers[0] if i == m else None
            b1 = blockers[1] if j == m else None
            b2 = blockers[2] if k == m else None
            b3 = blockers[3] if l == m else None
            value, unblocked = self.value_attack(attacker, b0, b1, b2, b3)
            total += value
            if unblocked == INVALID:
                total_fail = True
            else:
                num_damage += unblocked
        if num_damage >= self.defender_defense:
            total = LETHAL_VALUE
        else:
            total += direct_damage_value(num_damage, self.defender_defense,
                                         self.evaluator_personality())
        if total_fail or not (math.isnan(self.value) or total < self.value):
            return
        self.value = total
        self.used_blockers = []
        if i == 0 and blockers[0] is not None:
            self.used_blockers.append(blockers[0])
        if j == 0 and blockers[1] is not None:
            self.used_blockers.append(blockers[1])
        if k == 0 and blockers[2] is not None:
            self.used_blockers.append(blockers[2])
        if l == 0 and blockers[3] is not None:
            self.used_blockers.append(blockers[3])
        if self.buff.value > 0.0:
            self.buffed_value = self.buff.value
            if self.buff.target is not None and attackers[0] is not None \
                    and self.buff.target == attackers[0].uid:
                self.combat_buff_target = self.buff.target
        else:
            self.buffed_value = math.nan

    def evaluator_personality(self):
        return self.combat_results["_personality"]

    @property
    def score(self) -> float:
        if not math.isnan(self.buffed_value):
            return self.buffed_value
        if not math.isnan(self.value):
            return self.value
        return 0.0

    # -- one attacker's combat -------------------------------------------
    def value_attack(self, attacker, b0, b1, b2, b3):
        """ValueAttack: the memoised combat result, plus the combat-buff
        search when this attacker could use a buff."""
        value, unblocked = self.combat_result(attacker, b0, b1, b2, b3)
        if unblocked != INVALID and attacker is not None and self.buff.interesting:
            # Re-run the combat as if the buff were on this attacker.
            attacker.combat_buff = self.buff
            buffed_value, buffed_unblocked = self.combat_result(
                attacker, b0, b1, b2, b3, memo=False)
            gain_buffed = direct_damage_value(
                buffed_unblocked, self.defender_defense, self.evaluator_personality())
            gain_plain = direct_damage_value(
                unblocked, self.defender_defense, self.evaluator_personality())
            delta = buffed_value - value + gain_buffed - gain_plain
            if delta > self.buff.value:
                self.buff.value = delta
                if not self.buff.team:
                    self.buff.target = attacker.uid
                value, unblocked = buffed_value, buffed_unblocked
            attacker.combat_buff = None
        return value, unblocked

    def combat_result(self, attacker, b0, b1, b2, b3, memo: bool = True):
        """CombatResultLookup: value and unblocked damage of one combat,
        memoised on the attacker, its buff, and the blocker keys."""
        if attacker is None:
            return 0.0, 0
        blockers = [b for b in (b0, b1, b2, b3) if b is not None]
        key = (attacker.combat_buff is not None, attacker.combat_key,
               tuple(b.combat_key for b in blockers))
        if memo and key in self.combat_results:
            return self.combat_results[key]
        if self.valid_block(attacker, b0, b1, b2, b3):
            unblocked = [0]
            value = self.internal_value_attack(attacker, b0, b1, b2, b3, unblocked)
            result = (value, unblocked[0])
        else:
            result = (0.0, INVALID)
        if memo:
            self.combat_results[key] = result
        return result

    # -- block legality -----------------------------------------------------
    def valid_block(self, attacker, b0, b1, b2, b3) -> bool:
        """ValidBlock: structural blocking rules (CantBeBlocked, feral needs
        two blockers, flyers need flying or skyguard blockers, and the
        artifact / coyotle "can't be blocked" text)."""
        blockers = [b for b in (b0, b1, b2, b3) if b is not None]
        if not blockers:
            return True
        if attacker is None:
            return True
        if attacker.has_attribute(ECardAttributes.CantBeBlocked):
            return False
        if getattr(attacker.card, "feral", False) and len(blockers) < 2:
            return False
        if attacker.card.has_attribute(ECardAttributes.Flight):
            for blocker in blockers:
                if not (blocker.card.has_attribute(ECardAttributes.Flight) or
                        blocker.card.has_attribute(ECardAttributes.SkyGuard)):
                    return False
        return True

    # -- the combat itself -----------------------------------------------
    def destroys_blocker(self, attacker, blocker, remaining_damage: int) -> bool:
        """DestroysBlocker: the attacker's damage kills this blocker."""
        if blocker.has_attribute(ECardAttributes.Immortal):
            return False
        if blocker.card.has_attribute(ECardAttributes.PreventCombatDamage):
            return False
        return (blocker.defense(False) <= remaining_damage or
                (remaining_damage > 0 and bool(getattr(attacker.card, "lethal", False))))

    def internal_value_attack(self, attacker, b0, b1, b2, b3, unblocked) -> float:
        """internal_ValueAttack: first strike, the damage race, blocker
        losses, and the attacker's own risk.  ``unblocked[0]`` receives the
        damage that reaches the champion."""
        num = 0
        value = 0.0
        if attacker is None:
            return value
        prevents = attacker.card.has_attribute(ECardAttributes.PreventCombatDamage)
        remaining = attacker.attack(True)
        blockers = [b0, b1, b2, b3]
        if all(b is None for b in blockers):
            num = remaining
        else:
            if not attacker.has_attribute(ECardAttributes.FirstStrike):
                first_strike_damage = sum(
                    b.attack(False) for b in blockers
                    if b is not None and b.has_attribute(ECardAttributes.FirstStrike))
                if first_strike_damage >= attacker.defense(True) and not prevents:
                    return -(attacker.loss_value * self.erosion)
            if attacker.has_attribute(ECardAttributes.FirstStrike):
                for slot in range(4):
                    b = blockers[slot]
                    if b is not None and self.destroys_blocker(attacker, b, remaining):
                        value += b.value
                        remaining -= (1 if attacker.card.has_attribute(
                            ECardAttributes.Lethal) else b.defense(False))
                        blockers[slot] = None
            else:
                open_slots = sum(1 for b in blockers if b is None)
                for slot in range(4):
                    b = blockers[slot]
                    if b is not None and self.destroys_blocker(attacker, b, remaining):
                        value += b.value
                        remaining -= (1 if bool(getattr(attacker.card, "lethal", False))
                                      else b.defense(False))
                        open_slots += 1
                if open_slots < 4:
                    remaining = 0
            blocker_attack = sum(b.attack(False) for b in blockers if b is not None)
            lethal_blocker = any(
                b is not None and b.attack(False) > 0 and
                bool(getattr(b.card, "lethal", False)) for b in blockers)
            if blocker_attack >= attacker.defense(True) and \
                    not attacker.card.has_attribute(ECardAttributes.Immortal):
                value -= attacker.loss_value * self.erosion
            if lethal_blocker and not attacker.card.has_attribute(ECardAttributes.Immortal):
                value -= attacker.loss_value * self.erosion * 0.33
            if attacker.card.has_attribute(ECardAttributes.Juggernaught) and \
                    remaining > 0 and not prevents:
                num = remaining
        if attacker.card.has_attribute(ECardAttributes.SpiritDrain):
            value += num * 0.25
        if remaining == 0:
            value -= 0.1
        for b in blockers:
            if b is not None:
                value += 0.1
        if num > 0:
            if getattr(attacker.card, "toxify", False) and self.toxified:
                value += 10000.0
            unblocked[0] += num
        return value


def direct_damage_value(damage: int, defender_life: int, personality=None) -> float:
    """AIFunctions.GetValueOfDirectDamage: the value of damage to the champion.
    Lethal damage is worth 10000; otherwise each point costs
    ``DamageParityValue / life`` while the champion stays above 1."""
    if defender_life <= damage:
        return 10000.0
    parity = 10.0
    if personality is not None:
        parity = personality.values["DamageParityValue"]
    total = 0.0
    life = defender_life
    for _ in range(int(damage)):
        if life - 1.0 > 0.0:
            total += parity / life
            life -= 1
    return total


def _results_table(personality):
    table = {}
    table["_personality"] = personality
    return table


def solve_block(attackers, blockers, defender_defense, evaluator, *,
                toxified=False, troop_ratio=1.0):
    """AICombat.SolveBlock.  ``attackers`` are the player's attacking hints,
    ``blockers`` the AI's blocking hints.  Returns ``{attacker_uid: [blocker
    uids]}``; attackers that are not in the result are unblocked."""
    personality = evaluator.personality
    attacker_window = WindowList(attackers)
    blocker_window = WindowList(blockers)
    attacker_window.sort_by_value(troop_ratio)
    blocker_window.sort_by_value(troop_ratio)
    attacker_window.set_combat_keys()
    blocker_window.set_combat_keys()
    results = _results_table(personality)
    assignment: dict = {}
    remaining_blockers = list(blockers)
    while attacker_window.valid:
        window_attackers = attacker_window.get_window(3, 0)
        window_blockers = blocker_window.get_window(3, 1)
        result = AttackResult(window_attackers, window_blockers, defender_defense,
                              CombatBuff(), results, toxified=toxified,
                              troop_ratio=troop_ratio)
        attacker = attacker_window.pop()
        assignment.setdefault(attacker.uid, [])
        for blocker in result.used_blockers:
            if blocker in remaining_blockers:
                remaining_blockers.remove(blocker)
            blocker_window.remove(blocker)
            assignment[attacker.uid].append(blocker.uid)
    # One attacker taking a single blocker is only worth blocking when the
    # attacker is worth at least twice the blocker (the client's threshold).
    # Survival is not gated by value: when the unblocked attack would kill the
    # champion, the chump block stands even if it is not worth the card.
    incoming = sum(a.attack(True) for a in attackers)
    lethal_unblocked = incoming >= int(defender_defense)
    if len(assignment) == 1 and not lethal_unblocked:
        (attacker_uid, blocker_uids), = assignment.items()
        if len(blocker_uids) == 1:
            attacker = next((a for a in attackers if a.uid == attacker_uid), None)
            blocker = next((b for b in blockers if b.uid == blocker_uids[0]), None)
            if attacker is not None and blocker is not None and \
                    attacker.value < blocker.value * 2.0:
                assignment.clear()
    return {uid: uids for uid, uids in assignment.items() if uids}


def recur_combat(attackers, blockers, defender_defense, results, *,
                 toxified=False, troop_ratio=1.0) -> AttackResult:
    """AICombat.RecurCombat: the best line over removing one attacker at a
    time, so a weaker attacker can be left out of a bad combat."""
    best = AttackResult(attackers, blockers, defender_defense, CombatBuff(),
                        results, toxified=toxified, troop_ratio=troop_ratio)
    for attacker in attackers:
        if attacker is None:
            continue
        rest = [a for a in attackers if a is not attacker]
        candidate = recur_combat(rest, blockers, defender_defense, results,
                                 toxified=toxified, troop_ratio=troop_ratio)
        if candidate.score > best.score:
            best = candidate
    return best


def solve_attack(attackers, blockers, defender_defense, evaluator, *,
                 toxified=False, troop_ratio=1.0, buff=None):
    """AICombat.SolveAttack: pick the attackers whose combats are worth it.
    Timidness discounts each further commitment."""
    personality = evaluator.personality
    results = _results_table(personality)
    life = int(defender_defense)
    timidness = personality.values["Timidness"] * (0.9 ** len(attackers))
    total = 0.0
    attacker_window = WindowList(attackers)
    blocker_window = WindowList(blockers)
    attacker_window.sort_by_value(troop_ratio)
    blocker_window.sort_by_value(troop_ratio)
    attacker_window.set_combat_keys()
    blocker_window.set_combat_keys()
    chosen: list = []
    remaining_blockers = list(blockers)
    while attacker_window.valid:
        window = recur_combat(attacker_window.get_window(3, 0),
                              blocker_window.get_window(3, 1), life, results,
                              toxified=toxified, troop_ratio=troop_ratio)
        attacker = attacker_window.pop()
        if attacker in window.attackers:
            for blocker in window.used_blockers:
                if blocker in remaining_blockers:
                    remaining_blockers.remove(blocker)
                blocker_window.remove(blocker)
            timidness /= 0.9
            total += window.score
            chosen.append(attacker)
    if total <= 0.0:
        return []
    return chosen


def determine_best_attack(attackers, blockers, defender_defense, evaluator, *,
                          toxified=False, troop_ratio=1.0):
    """AICombat.DetermineBestAttack: attack with unblockable troops and solve
    the flyers and ground troops separately against the blockers that can
    stop them.  Returns the attacker hints chosen to attack."""
    unblockable = []
    flyers = []
    ground = []
    for hint in attackers:
        if hint.card.has_attribute(ECardAttributes.CantBeBlocked):
            unblockable.append(hint)
        elif hint.card.has_attribute(ECardAttributes.Flight):
            flyers.append(hint)
        else:
            ground.append(hint)
    chosen = list(solve_attack(unblockable, [], defender_defense, evaluator,
                               toxified=toxified, troop_ratio=troop_ratio))
    flying_blockers = [b for b in blockers
                       if b.card.has_attribute(ECardAttributes.Flight) or
                       b.card.has_attribute(ECardAttributes.SkyGuard)]
    chosen += solve_attack(flyers, flying_blockers, defender_defense, evaluator,
                           toxified=toxified, troop_ratio=troop_ratio)
    # Ground attackers can be blocked by any blocker, flyers included.
    chosen += solve_attack(ground, list(blockers), defender_defense, evaluator,
                           toxified=toxified, troop_ratio=troop_ratio)
    return chosen


def best_damage_order(attacker, blockers, evaluator, *, defender_defense=20,
                      toxified=False):
    """Damage order for one combat, chosen by the attacking AI.

    The attacker assigns its damage to the defending blockers in order, and
    each blocker it destroys adds to its value.  Try every order of up to four
    blockers and keep the one with the highest combat value for the attacker
    (``internal_value_attack``).  Ties keep the original order.  Blockers past
    the fourth slot cannot be in one combat, so they keep their place after
    the searched four.
    """
    import itertools

    blockers = list(blockers)
    if len(blockers) <= 1 or attacker is None:
        return blockers
    searched = blockers[:4]
    rest = blockers[4:]
    results = _results_table(evaluator.personality)
    scorer = AttackResult([], [], defender_defense, CombatBuff(), results,
                          toxified=toxified)
    best = None
    best_value = None
    for order in itertools.permutations(searched):
        padded = list(order) + [None] * (4 - len(order))
        unblocked = [0]
        value = scorer.internal_value_attack(
            attacker, padded[0], padded[1], padded[2], padded[3], unblocked)
        if best_value is None or value > best_value:
            best, best_value = list(order), value
    return best + rest

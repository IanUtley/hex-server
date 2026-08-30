"""Data-driven AI hand evaluator, ported from the client's
HexClient/Assembly-CSharp-firstpass/Game/Shared/AI/AICardEvaluator.cs +
AIHints.cs + AIPersonality.cs.

The client AI evaluates every card in hand (playability, value, buff/removal
classification) and then picks the best sequence of plays (BoardStack).  Our
previous AI only pattern-matched "troop or damage action"; this module gives
it the same value/playability model so it plays removal, buffs, lifegain and
constants from the gamedata rather than hardcoded card names.

Everything is data-driven: card fields come from card_templates/game_cards and
ability classification from ability_effects (the ported gamedata), mirroring
how the client's TemplateManager feeds AICardEvaluator.
"""

import json
import math
import random

from db import _db, log_req
from domain.enums import ECardAttributes, ECardShards


# ---------------------------------------------------------------------------
# Personality (AIPersonality.cs)
# ---------------------------------------------------------------------------

class Personality:
    """Values/weights from AIPersonality.InitializeInternalValues plus the
    deck-personality overrides (UpdatePersonality)."""

    RARITY_VALUES = {
        "Land": 1.0, "Common": 1.0, "Uncommon": 1.4, "Rare": 2.0,
        "Legendary": 3.0, "Epic": 1.4, "PvE": 1.4, "Promo": 1.4,
    }

    def __init__(self, deck_personality=None, attitude="Aggressive"):
        v = self.values = {}
        v["Attack"] = 1.0
        v["Defense"] = 0.75
        v["Threshold"] = 0.2
        v["Cost"] = 1.0
        v["CostGrowth"] = 0.35
        v["Hand"] = 3.0
        v["Resource"] = 2.0
        v["Rarity"] = 1.0
        v["RarityGrowth"] = 1.2
        v["Removal"] = 1.0
        v["Buff"] = 1.0
        v["HealthMultiplier"] = 18.0
        v["HealthExpansion"] = 4.0
        v["HealthStableLevel"] = 14.0
        v["BounceValue"] = 0.25
        v["BluffLiklihood"] = 15.0
        v["FailureToAttackLiklihood"] = 14.0
        v["Timidness"] = 0.95
        v["AbilityValue"] = 0.2
        v["Aggressiveness"] = 0.75
        v["BurnHandLimit"] = 2.0
        v["DamageParityValue"] = 10.0
        self.deck_personality = deck_personality
        self.attitude = attitude if attitude in {
            "Aggressive", "Comfortable", "Defensive"
        } else "Aggressive"
        self._apply_deck_personality(deck_personality)
        # AttributesMatrix (AIPersonality.cs): keyword -> per-attack/def value.
        self.attributes_value = {
            "LifeDrain": ("attack", 0.25),
            "Flight": ("attack", 0.25),
            "SkyGuard": ("defense", 0.25),
            "Crush": ("attack", 0.25),
            "Steadfast": ("defense", 0.25),
            "SpellShield": ("flat", 1.0),
            "Swiftstrike": ("attack", 0.25),
            "Rage": ("rage", 0.25),
            "Lethal": ("nondefense", 1.0),
        }
        # High-value targets (IsHighValueTarget) — creatures whose removal is
        # worth 1.5x normal value.  These are the client's curated list; keep
        # it because it is a behaviour table, not a card-mechanics exception.
        self.high_value_targets = [
            "royal falconer", "eternal guardian", "goremaster",
            "the killipede", "gareth kay", "bride of the damned",
            "replipopper 4000",
        ]
        self.card_noise_range = 50

    @property
    def minimum_x_value(self):
        """AIPersonality.MinimumXValue: Aggressive=3, Comfortable=4,
        Defensive=5 — the minimum combat value before committing a troop."""
        return {"Aggressive": 3, "Comfortable": 4, "Defensive": 5}.get(
            self.attitude, 4)

    def _apply_deck_personality(self, name):
        v = self.values
        if name == "Aggressive":
            v["Timidness"] = 0.75
            v["Aggressiveness"] = 0.5
            v["DamageParityValue"] = 14.0
        elif name == "BigThreats":
            v["Cost"] = 1.25
            v["CostGrowth"] = 0.5
        elif name == "BuildArmy":
            v["Cost"] = 0.5
            v["CostGrowth"] = 0.25
        elif name == "Burn":
            v["BurnHandLimit"] = 0.0
            v["DamageParityValue"] = 20.0
        elif name == "HandAdvantage":
            v["Hand"] = 5.0

    def value_at_health(self, health):
        v = self.values
        if health <= 0:
            return -2147483648.0
        return (v["HealthMultiplier"]
                * math.log(v["HealthExpansion"] * health + 1.0))


# ---------------------------------------------------------------------------
# Card model helpers (thin wrapper over the DB rows we already have)
# ---------------------------------------------------------------------------

def _card_type_flags(card_type):
    """Translate a DB card_type string to a set of client-style type flags."""
    flags = set()
    if not card_type:
        return flags
    for part in card_type.split("|"):
        part = part.strip()
        if part == "Resource":
            flags.add("Resource")
        elif part == "Troop":
            flags.add("Troop")
        elif part == "Artifact":
            flags.add("Artifact")
        elif part == "Constant":
            flags.add("Constant")
        elif part == "BasicAction":
            flags.add("BasicAction")
        elif part == "QuickAction":
            flags.add("QuickAction")
        elif part == "Quick":
            flags.add("QuickAction")
    return flags


def _parse_buffs_json(raw):
    """temporary_buffs/permanent_buffs look like {'atk': 2, 'def': 1, ...}."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


class CardInfo:
    """A single game_cards row joined with its template, plus computed
    effective attack/defense and an attribute flag mask."""

    def __init__(self, row):
        # row: game_cards joined card_templates (see _load_ai_hand)
        (self.card_uid, self.template_guid, self.location, self.card_type,
         self.name, self.rarity, self.cost, self.attack_base,
         self.defense_base, self.threshold_json, self.abilities_json,
         self.attributes, self.subtype, self.variable_cost,
         self.current_resources_granted, self.max_resources_granted,
         self.card_state, self.card_damage, self.permanent_buffs,
         self.temporary_buffs, self.temporary_attributes) = row[:21]
        self.card_uid = int(self.card_uid)
        self.cost = int(self.cost or 0)
        self.attack_base = int(self.attack_base or 0)
        self.defense_base = int(self.defense_base or 0)
        self.card_damage = int(self.card_damage or 0)
        self.variable_cost = int(self.variable_cost or 0)
        self.max_resources_granted = int(self.max_resources_granted or 0)
        self.current_resources_granted = int(
            self.current_resources_granted or 0)
        self.attributes = int(self.attributes or 0) | int(
            self.temporary_attributes or 0)
        self.type_flags = _card_type_flags(self.card_type)
        self.ability_guids = self._load_ability_guids()
        self._effects_cache = None

    def _load_ability_guids(self):
        if not self.abilities_json:
            return []
        try:
            return [str(g).lower() for g in json.loads(self.abilities_json)]
        except Exception:
            return []

    # -- type predicates ---------------------------------------------------
    def is_resource(self):
        return "Resource" in self.type_flags

    def is_troop(self):
        return "Troop" in self.type_flags

    def is_artifact(self):
        return "Artifact" in self.type_flags

    def is_constant(self):
        return "Constant" in self.type_flags

    def is_action(self):
        return ("BasicAction" in self.type_flags
                or "QuickAction" in self.type_flags)

    def is_quick_action(self):
        return "QuickAction" in self.type_flags

    def is_basic_action(self):
        return "BasicAction" in self.type_flags

    # -- stats -------------------------------------------------------------
    def effective_attack(self, in_play=False):
        a = self.attack_base
        for b in (_parse_buffs_json(self.permanent_buffs),
                  _parse_buffs_json(self.temporary_buffs)):
            a += int(b.get("atk", 0) or 0)
        if in_play:
            a += int(self.card_attack_mod if hasattr(self, "card_attack_mod")
                     else 0)
        return max(0, a)

    def effective_defense(self, in_play=False):
        d = self.defense_base
        for b in (_parse_buffs_json(self.permanent_buffs),
                  _parse_buffs_json(self.temporary_buffs)):
            d += int(b.get("def", 0) or 0)
        if in_play:
            d += int(self.card_defense_mod if hasattr(self, "card_defense_mod")
                     else 0)
            d -= self.card_damage
        return max(0, d)

    def has_attribute(self, flag):
        return (self.attributes & flag) == flag

    # -- ability metadata --------------------------------------------------
    def effects(self):
        """[(effect_type, param_dict)] from ability_effects for this card."""
        if self._effects_cache is not None:
            return self._effects_cache
        out = []
        for ag in self.ability_guids:
            for e in _db.execute(
                    "SELECT effect_type, param FROM ability_effects "
                    "WHERE ability_guid=? ORDER BY effect_order",
                    (ag,)).fetchall():
                try:
                    pm = json.loads(e[1]) if e[1] else {}
                except Exception:
                    pm = {}
                out.append((e[0], pm if isinstance(pm, dict) else {}))
        self._effects_cache = out
        return out

    def has_effect_type(self, effect_type):
        return any(t == effect_type for t, _ in self.effects())

    def modifier_value(self, prop):
        """Sum of CardModifierAbilityEffectTemplate amounts for a property
        (attack/defense/damage), mirroring GetValueForModifier."""
        total = 0
        for etype, pm in self.effects():
            if etype != "CardModifierAbilityEffectTemplate":
                continue
            if (pm.get("property") or "").lower() == prop.lower():
                total += int(pm.get("amount", 0) or 0)
        return total

    def is_doomed_at_end_of_turn(self):
        """Mirror AICardEvaluator.IsDoomedAtEndOfTurn: a troop summoned until
        end of turn (summon token leaves)."""
        if not self.is_troop():
            return False
        for etype, pm in self.effects():
            if etype == "SummonTokenTroopAbilityEffectTemplate":
                dur = (pm.get("duration") or "").lower()
                if "end" in dur and "turn" in dur:
                    return True
        return False


# ---------------------------------------------------------------------------
# Hints (AIHints.cs): per-card classification
# ---------------------------------------------------------------------------

class RemovalParams:
    def __init__(self):
        self.hard = False
        self.sweeper = False
        self.one_sided = False
        self.debuff = False
        self.threshold = 0
        self.attacker = False
        self.exhaust = False
        self.lockdown = False
        self.faction_threshold = None


class BuffParams:
    def __init__(self):
        self.attack = 0
        self.defense = 0
        self.permanent = False
        self.swiftstrike = False
        self.crush = False
        self.flight = False
        self.speed = False
        self.rage = False
        self.affects_multiple_targets = False


class Hints:
    """AIHints for one card: buff/removal classification + value."""

    def __init__(self, card, personality, evaluator):
        self.card = card
        self.personality = personality
        self.evaluator = evaluator
        self.removal = None
        self.buff = None
        self.lure = False
        self._value = None
        self._analyze()

    def _analyze(self):
        card = self.card
        for ag in card.ability_guids:
            self._find_buffs(ag)
            self._find_removal(ag)
            self._find_tricks(ag)
        # Ragefire / Chronic Madness escalation: threshold = 2 * escalation
        # count (AIHints.Ragefire).  The escalation counter lives on the
        # game_cards row; default 0 means base damage.
        esc = getattr(card, "escalation_uses", 0) or 0
        if self.removal is not None and self.removal.threshold:
            self.removal.threshold += 2 * esc if card.name.lower() == "ragefire" else 0
        self.multiplier = 1.0
        if self.removal is not None:
            self.multiplier *= self.personality.values["Removal"]
        if self.buff is not None:
            self.multiplier *= self.personality.values["Buff"]

    def _find_buffs(self, ag):
        card = self.card
        perm_atk = perm_def = tmp_atk = tmp_def = 0
        for etype, pm in self.evaluator.effects_for(ag):
            if etype != "CardModifierAbilityEffectTemplate":
                continue
            prop = (pm.get("property") or "").lower()
            amount = int(pm.get("amount", 0) or 0)
            if prop == "attack":
                if (pm.get("duration") or "").lower() == "permanent":
                    perm_atk += amount
                else:
                    tmp_atk += amount
            elif prop == "defense":
                if (pm.get("duration") or "").lower() == "permanent":
                    perm_def += amount
                else:
                    tmp_def += amount
        # A permanent buff (e.g. "this troop gets +1/+1") counts as a buff the
        # AI wants in play; a temporary buff counts as a combat trick.
        if perm_atk > 0 or perm_def > 0:
            self.buff = BuffParams()
            self.buff.attack = perm_atk
            self.buff.defense = perm_def
            self.buff.permanent = True
        elif tmp_atk > 0 or tmp_def > 0:
            self.buff = BuffParams()
            self.buff.attack = tmp_atk
            self.buff.defense = tmp_def
        # Defense debuffs double as removal (AIHints.FindBuffs: -def -> removal
        # with threshold = -value).
        if perm_def < 0 or tmp_def < 0:
            if self.removal is None:
                self.removal = RemovalParams()
            self.removal.debuff = True
            self.removal.threshold = max(1, -(perm_def + tmp_def))

    def _find_removal(self, ag):
        card = self.card
        effects = self.evaluator.effects_for(ag)
        hard_kinds = ("DestroyCardAbilityEffectTemplate",
                      "VoidCardAbilityEffectTemplate",
                      "ReturnToHandAbilityEffectTemplate",
                      "MoveCardToZoneEffectTemplate",
                      "TransformCardAbilityEffectTemplate")
        for etype, pm in effects:
            if etype in hard_kinds:
                if self.removal is None:
                    self.removal = RemovalParams()
                self.removal.hard = True
                # MoveCardToZone: destination decides whether it is removal.
                if etype == "MoveCardToZoneEffectTemplate":
                    dest = (pm.get("destination") or "").lower()
                    if dest not in ("hand", "deck", "void"):
                        self.removal.hard = False
                text = ((card.name or "") + " " +
                        json.dumps(pm)).lower()
                if ("all" in text or "each" in text) \
                        and "each troop" in text:
                    self.removal.sweeper = True
                if "opposing" in text or "opponent" in text:
                    self.removal.one_sided = True
        for etype, pm in effects:
            text = json.dumps(pm).lower()
            if etype == "CardModifierAbilityEffectTemplate" \
                    and ("damage" in text and "each troop" in text):
                if self.removal is None:
                    self.removal = RemovalParams()
                self.removal.sweeper = True
        # Damage: use the same gamedata logic as _spell_damage_info.
        dmg = card.modifier_value("damage")
        if dmg <= 0:
            # TAC/parameterised damage ("Deal ESC:2 damage to target champion
            # or troop.", "Deal X damage ...") — extract the fixed part.
            for etype, pm in effects:
                text = json.dumps(pm).lower()
                if "damage" not in text:
                    continue
                m = __import__("re").search(r'deal\s+(\d+)\s+damage', text)
                if m:
                    dmg = int(m.group(1))
                    break
                m_esc = __import__("re").search(r'esc:\s*(\d+)', text)
                if m_esc:
                    dmg = int(m_esc.group(1))
                    break
        if dmg > 0:
            if self.removal is None:
                self.removal = RemovalParams()
            self.removal.threshold = dmg
        elif card.variable_cost and not card.is_troop():
            # Variable-X damage spells (Burn to the Ground): threshold is
            # decided at play time; mark the removal so the AI casts it.
            if dmg == 0 and any(
                    etype == "CardModifierAbilityEffectTemplate"
                    for etype, _ in effects):
                if self.removal is None:
                    self.removal = RemovalParams()
                self.removal.threshold = 0
        # Tap/exhaust effects (TapCardAbilityEffectTemplate).
        if card.has_effect_type("TapCardAbilityEffectTemplate"):
            if self.removal is None:
                self.removal = RemovalParams()
            self.removal.exhaust = True

    def _find_tricks(self, ag):
        for etype, pm in self.evaluator.effects_for(ag):
            text = json.dumps(pm).lower()
            if ("must block" in text or "must attack" in text
                    or "lure" in text):
                self.lure = True

    @property
    def value(self):
        if self._value is None:
            self._value = self.evaluator.calculate_card_value(self)
        return self._value


# ---------------------------------------------------------------------------
# Evaluator (AICardEvaluator.cs)
# ---------------------------------------------------------------------------

class CardEvaluator:
    """Board/play evaluation for the AI's hand.  Construction reads the AI's
    hand + warzone + opponent warzone from the DB (single snapshot)."""

    def __init__(self, handler, session, battle_state, ai_uid, player_uid,
                 player_champ_uid=None):
        self.handler = handler
        self.session = session
        self.bstate = battle_state
        self.ai_uid = ai_uid
        self.player_uid = player_uid
        self.player_champ_uid = player_champ_uid
        # The human's game_cards user id (profile id, e.g. 5 — not 1).
        self.player_db_id = int((handler.user_profile or {}).get("id", 5)
                                or 5)
        self.resources = int(battle_state.get("ai_resources", 0))
        self.total_resources = int(battle_state.get("ai_total_resources", 0))
        self.threshold = battle_state.get("ai_threshold", {}) or {}
        deck_p = getattr(handler, "_ai_deck_personality", None)
        # An unconfigured handler retains the historical Aggressive fallback;
        # battle setup explicitly writes None for an authored Default deck
        # personality, allowing the campaign attitude to drive the evaluator.
        if not hasattr(handler, "_ai_deck_personality"):
            deck_p = "Aggressive"
        attitude = (getattr(handler, "_ai_campaign_personality", None)
                    or getattr(handler, "_ai_personality", None)
                    or "Aggressive")
        self.personality = Personality(deck_p, attitude=attitude)
        self.ai_health = int(battle_state.get("ai_health", 20))
        self.player_health = int(battle_state.get("player_health", 20))
        self._effects_cache = {}
        self.hand = self._load_hand()
        self.ai_warzone = self._load_warzone(0)
        self.player_warzone = self._load_warzone(self.player_db_id)
        self.player_hand_count = self._hand_count(self.player_db_id)
        self._hints = {}

    # -- DB loads ----------------------------------------------------------
    def _load_hand(self):
        rows = _db.execute(
            "SELECT gc.card_uid, gc.template_guid, gc.location, "
            "       ct.card_type, ct.name, ct.rarity, ct.cost, ct.attack, "
            "       ct.defense, ct.threshold_json, ct.abilities_json, "
            "       ct.attributes, ct.subtype, ct.variable_cost, "
            "       ct.current_resources_granted, ct.max_resources_granted, "
            "       gc.card_state, gc.card_damage, gc.permanent_buffs, "
            "       gc.temporary_buffs, gc.temporary_attributes "
            "FROM game_cards gc JOIN card_templates ct "
            "  ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='hand' "
            "ORDER BY gc.position",
            (self.session.session_id,)).fetchall()
        return [CardInfo(r) for r in rows]

    def _load_warzone(self, user_id):
        rows = _db.execute(
            "SELECT gc.card_uid, gc.template_guid, gc.location, "
            "       ct.card_type, ct.name, ct.rarity, ct.cost, ct.attack, "
            "       ct.defense, ct.threshold_json, ct.abilities_json, "
            "       ct.attributes, ct.subtype, ct.variable_cost, "
            "       ct.current_resources_granted, ct.max_resources_granted, "
            "       gc.card_state, gc.card_damage, gc.permanent_buffs, "
            "       gc.temporary_buffs, gc.temporary_attributes, "
            "       gc.card_attack_mod, gc.card_defense_mod "
            "FROM game_cards gc JOIN card_templates ct "
            "  ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='warzone'",
            (self.session.session_id, user_id)).fetchall()
        cards = []
        for r in rows:
            c = CardInfo(r)
            c.card_attack_mod = int(r[21] or 0)
            c.card_defense_mod = int(r[22] or 0)
            cards.append(c)
        return cards

    def _hand_count(self, user_id):
        row = _db.execute(
            "SELECT COUNT(*) FROM game_cards "
            "WHERE session_id=? AND user_id=? AND location='hand'",
            (self.session.session_id, user_id)).fetchone()
        return int(row[0] or 0) if row else 0

    # -- ability effect cache ----------------------------------------------
    def effects_for(self, ability_guid):
        if ability_guid in self._effects_cache:
            return self._effects_cache[ability_guid]
        out = []
        for e in _db.execute(
                "SELECT effect_type, param FROM ability_effects "
                "WHERE ability_guid=? ORDER BY effect_order",
                (ability_guid,)).fetchall():
            try:
                pm = json.loads(e[1]) if e[1] else {}
            except Exception:
                pm = {}
            out.append((e[0], pm if isinstance(pm, dict) else {}))
        self._effects_cache[ability_guid] = out
        return out

    # -- hints / value -----------------------------------------------------
    def hints_for(self, card):
        if card.card_uid not in self._hints:
            self._hints[card.card_uid] = Hints(card, self.personality, self)
        return self._hints[card.card_uid]

    def get_card_value(self, card):
        return self.hints_for(card).value

    def get_list_value(self, cards):
        return sum(self.get_card_value(c) for c in cards)

    def is_high_value_target(self, card):
        name = (card.name or "").lower()
        return any(t in name for t in self.personality.high_value_targets)

    def calculate_template_value(self, card):
        """CalculateTemplateValue(ResourceId): rarity + cost/threshold value."""
        p = self.personality
        v = p.values
        num = 0.25
        rarity = p.RARITY_VALUES.get(card.rarity)
        if rarity is not None:
            num += (rarity ** v["RarityGrowth"]) * v["Rarity"]
        if card.is_troop():
            num += self._threshold_value(card) * v["Threshold"]
            num += (card.cost ** v["CostGrowth"]) * v["Cost"]
            num += card.attack_base * v["Attack"]
            num += card.defense_base * v["Defense"]
            num *= 1.0  # CardValueMatrix[Troop]
        elif card.is_artifact() or card.is_action() or card.is_constant():
            num += self._threshold_value(card) * v["Threshold"]
            num += (card.cost ** v["CostGrowth"]) * v["Cost"]
        else:
            num += v["Resource"]
            num += 7 - self.total_resources
        if self.is_high_value_target(card):
            num *= 1.5
        return num

    @staticmethod
    def _threshold_value(card):
        """Sum of (requirement^2) from threshold_json list."""
        total = 0
        try:
            req = json.loads(card.threshold_json or "{}")
            for s in req.get("list", []) or []:
                total += 1
        except Exception:
            pass
        return total

    def calculate_card_value(self, hint):
        """CalculateCardValue(AIHints): full per-card value."""
        card = hint.card
        p = self.personality
        v = p.values
        num = 0.0
        rarity = p.RARITY_VALUES.get(card.rarity)
        if rarity is not None:
            num += (rarity ** v["RarityGrowth"]) * v["Rarity"]
        # AbilitiesMatrix: CurrentResourceModifier abilities add flat value.
        for ag in card.ability_guids:
            for etype, pm in self.effects_for(ag):
                if etype == "ReplenishResourcesAbilityEffectTemplate":
                    num += 0.5
        for ag in card.ability_guids:
            for etype, pm in self.effects_for(ag):
                if (pm.get("uses_per_game") or 0) > 0:
                    num += 1.0
        if card.is_troop():
            num += self._threshold_value(card) * v["Threshold"]
            num += (card.cost ** v["CostGrowth"]) * v["Cost"]
            num += hint.card.effective_attack() * v["Attack"]
            num += hint.card.effective_defense() * v["Defense"]
            for attr, (kind, weight) in p.attributes_value.items():
                if self._card_has_keyword(card, attr):
                    if kind == "attack":
                        num += hint.card.effective_attack() * weight
                    elif kind == "defense":
                        num += hint.card.effective_defense() * weight
                    elif kind == "rage":
                        num += int(getattr(card, "rage", 0) or 0) * weight
                    elif kind == "nondefense":
                        if card.attack_base == 0:
                            num += 0.0
                        else:
                            num += max(1.0, 4.0 - card.defense_base)
                    else:
                        num += weight
            if card.has_attribute(ECardAttributes.Inspire):
                num *= 1.25
            if card.has_attribute(ECardAttributes.Unique):
                num *= 1.05
            if (card.has_attribute(ECardAttributes.CantAttack)
                    and not card.has_attribute(ECardAttributes.CantBlock)):
                num *= 0.1
            if card.is_doomed_at_end_of_turn():
                num *= 0.25
            for ag in card.ability_guids:
                # Enters-play abilities are already in the stats; other
                # abilities add AbilityValue.
                if not self._is_enters_play_ability(ag):
                    num += v["AbilityValue"]
            num *= 1.0  # CardValueMatrix[Troop]
        elif card.is_artifact():
            num += (card.cost ** v["CostGrowth"]) * v["Cost"]
        elif card.is_action():
            num += self._threshold_value(card) * v["Threshold"]
            num += (card.cost ** v["CostGrowth"]) * v["Cost"]
            if hint.buff is not None and hint.buff.affects_multiple_targets:
                for c in self.ai_warzone:
                    if c.is_troop() and self._can_attack(c):
                        num += math.sqrt(self.get_card_value(c)) / 4.0
        elif card.is_constant():
            num += self._threshold_value(card) * v["Threshold"]
            num += (card.cost ** v["CostGrowth"]) * v["Cost"]
        else:
            num += v["Resource"]
            num += 7 - self.total_resources
        if self.is_high_value_target(card):
            num *= 1.5
        num *= hint.multiplier
        # CardNoiseRange: +/- 0.5 (client uses a small RNG; deterministic here).
        num += random.uniform(-0.5, 0.5)
        return num

    def _card_has_keyword(self, card, keyword):
        """Attribute-based keyword detection (Flight/Steadfast/etc.) plus
        gamedata GrantAbility/gem fallback for the few non-attribute
        keywords (LifeDrain, Lethal, Rage, Crush)."""
        attr_map = {
            "Flight": ECardAttributes.Flight,
            "SkyGuard": ECardAttributes.SkyGuard,
            "Crush": ECardAttributes.Juggernaught,
            "Steadfast": ECardAttributes.Steadfast,
            "Swiftstrike": ECardAttributes.FirstStrike,
            "Rage": ECardAttributes.Rage,
        }
        if keyword in attr_map and card.has_attribute(attr_map[keyword]):
            return True
        if keyword == "SpellShield" and card.has_attribute(
                ECardAttributes.SpellShield):
            return True
        # Keyword grants via abilities (data-driven text scan of gamedata).
        for ag in card.ability_guids:
            for etype, pm in self.effects_for(ag):
                text = json.dumps(pm).lower()
                if keyword.lower() in text:
                    return True
        return False

    def _is_enters_play_ability(self, ability_guid):
        for etype, pm in self.effects_for(ability_guid):
            if etype in ("CardModifierAbilityEffectTemplate",
                         "SummonTokenTroopAbilityEffectTemplate"):
                text = json.dumps(pm).lower()
                if "enters play" in text or "deploy" in text:
                    return True
        return False

    def _can_attack(self, card):
        if card.has_attribute(ECardAttributes.CantAttack):
            return False
        if card.card_state is not None and int(card.card_state or 0) & 2:
            return False  # Tapped (ECardStates.Tapped = 2)
        return True

    # -- playability (AICardEvaluator.IsPlayable) --------------------------
    def can_pay(self, cost, variable=False):
        if variable:
            cost += 1  # 1X cost means X+1 (client m_VariableCostDouble=0)
        return self.resources + self._on_board_resources() >= cost

    def _on_board_resources(self):
        """OnBoardResources: resources generated by the AI's warzone
        (e.g. a resource-generating troop)."""
        total = 0
        for c in self.ai_warzone:
            for ag in c.ability_guids:
                for etype, pm in self.effects_for(ag):
                    if etype == "ReplenishResourcesAbilityEffectTemplate":
                        total += int(pm.get("amount", 0) or 0)
        return total

    def is_playable(self, card):
        """AIPlayableParams: True / NeedsResources / False."""
        if card is None:
            return "False"
        cost = card.cost
        if card.variable_cost and not card.is_troop():
            cost += 1
        if not self.handler._thresholds_met(card.threshold_json,
                                            self.threshold):
            return "False"
        if card.is_action():
            for ag in card.ability_guids:
                if self._action_needs_target(card, ag) and not self._has_legal_target(card, ag):
                    return "False"
        if self.resources + self._on_board_resources() >= cost:
            return "True"
        return "NeedsResources"

    def get_theoretical_distance(self, card):
        """GetTheoriticalDistance: how far (resources + thresholds) this card
        is from being castable, counting shards still in hand."""
        if self.is_playable(card) == "True":
            return 0.0
        total = self.total_resources
        extra = {}
        for c in self.hand:
            if c.is_resource():
                total += c.max_resources_granted
                for flag, count in self._thresholds_provided(c).items():
                    extra[flag] = extra.get(flag, 0) + count
        dist = 0.0
        cost = card.cost
        if card.variable_cost and not card.is_troop():
            cost += 1
        if total < cost:
            dist += cost - total
        try:
            req = json.loads(card.threshold_json or "{}")
            fmt = {0: 0, 1: 4, 2: 8, 3: 16, 4: 32, 5: 64}
            need = {}
            for s in req.get("list", []) or []:
                flag = fmt.get(s, s)
                need[flag] = need.get(flag, 0) + 1
            for flag, count in need.items():
                have = int(self.threshold.get(flag, 0)) + int(
                    extra.get(flag, 0))
                if have < count:
                    dist += count - have
        except Exception:
            pass
        return dist

    @staticmethod
    def _thresholds_provided(card):
        """Thresholds a resource card grants when played (from gamedata)."""
        out = {}
        try:
            t = json.loads(card.threshold_json or "{}")
            for idx in t.get("values", []) or []:
                if idx:
                    out[idx] = out.get(idx, 0) + 1
        except Exception:
            pass
        return out

    def get_theoretical_value(self, card):
        """GetTheoriticalValue: card value, discounted by how far it is from
        being castable (the client's 3/(3+distance) discount)."""
        val = self.get_card_value(card)
        if self.is_playable(card) == "True":
            return val
        dist = self.get_theoretical_distance(card)
        return val * (3.0 / (3.0 + dist))

    def _action_needs_target(self, card, ag):
        metadata_targets = self._metadata_action_targets(card, ag)
        if metadata_targets is not None:
            return True
        for etype, pm in self.effects_for(ag):
            if etype in ("DestroyCardAbilityEffectTemplate",
                         "VoidCardAbilityEffectTemplate",
                         "MoveCardToZoneEffectTemplate",
                         "TransformCardAbilityEffectTemplate",
                         "TapCardAbilityEffectTemplate"):
                text = json.dumps(pm).lower()
                if "target" in text or "choose" in text:
                    return True
            if etype == "CardModifierAbilityEffectTemplate":
                text = json.dumps(pm).lower()
                if "target" in text or "choose" in text:
                    return True
                if ("target" in text and ("troop" in text or "card" in text)
                        and "opposing" in text):
                    return True
        return False

    def _has_legal_target(self, card, ag):
        metadata_targets = self._metadata_action_targets(card, ag)
        if metadata_targets is not None:
            return bool(metadata_targets)
        # The opponent champion is always a legal target for damage/removal
        # effects that can hit champions (Burn targets a champion OR troop).
        effects = self.effects_for(ag)
        text_all = json.dumps(effects).lower()
        friendly = not ("opposing" in text_all or "opponent" in text_all)
        for etype, pm in self.effects_for(ag):
            if etype == "CardModifierAbilityEffectTemplate":
                text = json.dumps(pm).lower()
                if ("damage" in text and "target" in text
                        and ("champion" in text or "player" in text)):
                    return True
            if etype in ("DestroyCardAbilityEffectTemplate",
                         "VoidCardAbilityEffectTemplate",
                         "MoveCardToZoneEffectTemplate"):
                text = json.dumps(pm).lower()
                if "target" in text and "card" in text:
                    return True
        if friendly:
            for c in self.ai_warzone:
                if c.is_troop():
                    return True
        for c in self.player_warzone:
            if c.is_troop():
                return True
        return False

    # -- board builder (AICardEvaluator.IsBoardBuilder + GetBestBoardBuilder)
    def is_board_builder(self, card, pre_combat):
        if card.is_resource():
            return True
        if card.is_troop():
            if card.effective_defense() < 1 and not card.has_attribute(
                    ECardAttributes.Inspire):
                return False
            if card.is_doomed_at_end_of_turn() and not pre_combat:
                return False
        if card.is_troop() or card.is_constant() or card.is_artifact():
            return True
        if card.is_basic_action():
            # Non-quick actions: removal, lifegain and buffs are worth playing
            # if they have a legal target (or are targetless).
            if card.variable_cost and not self.can_pay(
                    card.cost, variable=True):
                return False
            if card.is_quick_action():
                return False
            return True
        return False

    def get_best_board_builder(self, pre_combat=True, include_resources=True):
        """BoardStack: simulate play sequences (one resource + affordable
        cards) and return the first card of the best sequence, or None."""
        candidates = []
        has_resource = False
        for card in self.hand:
            if card.is_resource():
                has_resource = True
                if not include_resources:
                    continue
            if self.is_playable(card) == "True" and self.is_board_builder(
                    card, pre_combat):
                candidates.append(card)
        if not candidates and not has_resource:
            return None
        if len(candidates) > 10:
            # Drop duplicate templates beyond the first (client does this).
            seen = set()
            keep = []
            for card in candidates:
                if card.template_guid in seen:
                    continue
                seen.add(card.template_guid)
                keep.append(card)
            candidates = keep
        resources = self.resources
        best = self._best_stack(candidates, resources, has_resource,
                                used=False, depth=0)
        order = best[0]
        return order[0] if order else None

    def _best_stack(self, cards, resources, has_resource, used, depth):
        """Return (play_order_list, value).  Mirrors GetBestBoardStack with a
        depth cap of 5 (client caps at >5 by evaluating the current stack)."""
        if depth >= 5:
            return (cards[:0], 0.0)
        best = ([], 0.0)
        for i, card in enumerate(cards):
            rest = cards[:i] + cards[i + 1:]
            if card.is_resource():
                if used:
                    continue  # one resource per turn (Used flag)
                new_res = resources + card.current_resources_granted
                order, val = self._best_stack(
                    rest, new_res, has_resource, True, depth + 1)
            else:
                cost = card.cost
                if card.variable_cost and not card.is_troop():
                    cost += 1
                if cost > resources:
                    continue
                order, val = self._best_stack(
                    rest, resources - cost, has_resource, used, depth + 1)
            value = self.get_card_value(card) + val
            if value > best[1]:
                best = ([card] + order, value)
        return best

    # -- removal / threat helpers (used by the tactical layer) --------------
    def get_worry_value(self):
        p = self.personality
        stable = p.value_at_health(int(p.values["HealthStableLevel"]))
        now = p.value_at_health(self.ai_health)
        return max(0.0, stable - now)

    # -- combat valuation (AICombat.cs) ------------------------------------

    def loss_value(self, card):
        """AICombat's LossValue: card value scaled by aggressiveness, with
        ForceAttack troops worth almost nothing to lose (they must attack)."""
        val = self.get_card_value(card)
        if card.is_troop() and card.has_attribute(ECardAttributes.ForceAttack):
            val = 0.25
        return val * self.personality.values["Aggressiveness"]

    def _damage_through(self, attacker, blockers, first_strike=False):
        """Simulate one attacker vs a blocker list (order matters, mirroring
        internal_ValueAttack): the attacker's power destroys blockers in order
        (lethal or damage >= defense); remainder hits the champion.  Returns
        (unblocked_damage, blocker_value_gained, attacker_dies,
         attacker_value_lost)."""
        atk = attacker.effective_attack()
        a_def = attacker.effective_defense(in_play=True)
        a_attrs = attacker.attributes
        a_val = self.loss_value(attacker)
        blocker_value = 0.0
        dies = False
        for b in blockers:
            b_def = b.effective_defense(in_play=True)
            b_atk = b.effective_attack()
            if atk >= b_def and not b.has_attribute(ECardAttributes.Immortal):
                # Attacker destroys this blocker.
                blocker_value += self.loss_value(b)
                atk -= b_def
                if b_atk >= a_def and not (
                        a_attrs & ECardAttributes.Immortal):
                    dies = True
            else:
                # Blocker survives; attacker is stopped.
                atk = 0
                if b_atk >= a_def and not (
                        a_attrs & ECardAttributes.Immortal):
                    dies = True
                break
        return max(0, atk), blocker_value, dies, a_val if dies else 0.0

    def value_attack(self, attacker, blockers):
        """internal_ValueAttack for the single-blocker case: value of sending
        this troop into these blockers."""
        if not blockers:
            return attacker.effective_attack(), 0.0
        dmg, bval, dies, aval = self._damage_through(attacker, blockers)
        value = bval - aval
        if dmg > 0:
            value += dmg * self.personality.values["DamageParityValue"] / 20.0
        if dmg == 0:
            value -= 0.1
        value += 0.1 * len(blockers)
        return dmg, value

    def alpha_strike_wins(self, player_health, attackers, blockers):
        """AICardEvaluator.AlphaStrikeWins: can all our attackers deal lethal
        to the opponent champion through the blockers they would need to
        fight?  Each attacker deals damage through to the champion only if no
        blocker survives its hit."""
        remaining = player_health
        unused = list(blockers)
        # Process attackers strongest-first so blocker assignment is sensible.
        ordered = sorted(attackers, key=lambda c: -c.effective_attack())
        for a in ordered:
            best = None
            for b in unused:
                if not self._can_block(b, a):
                    continue
                dmg, _ = self.value_attack(a, [b])
                if best is None or dmg > best[0]:
                    best = (dmg, b)
            if best is not None and best[0] == 0:
                unused.remove(best[1])
                continue
            dmg, _ = self.value_attack(a, [best[1]] if best else [])
            if best is not None:
                unused.remove(best[1])
            remaining -= dmg
            if remaining <= 0:
                return True
        return remaining <= 0

    @staticmethod
    def _can_block(blocker, attacker):
        if blocker.has_attribute(ECardAttributes.CantBlock):
            return False
        if attacker.has_attribute(ECardAttributes.CantBeBlocked):
            return False
        if attacker.has_attribute(ECardAttributes.Flight):
            return bool(blocker.attributes & (
                ECardAttributes.Flight | ECardAttributes.SkyGuard))
        return True

    def is_dangerous(self, card):
        if not card.is_troop():
            return False
        if card.has_attribute(ECardAttributes.CantAttack):
            return False
        if card.effective_defense(in_play=True) == 1 \
                and len(card.ability_guids) > 1:
            return True
        if (card.effective_attack() > 3 and card.effective_defense() > 1) \
                or card.effective_defense() > 3:
            return True
        if self.player_hand_count == 0:
            return True
        return False

    # -- removal matching (AICardEvaluator.GetRemovalFor + HaveRemoval) ----

    def have_removal(self):
        """HaveRemoval: any playable hand card classified as removal."""
        for card in self.hand:
            if self.is_playable(card) in ("True", "NeedsResources"):
                h = self.hints_for(card)
                if h.removal is not None:
                    return True
        return False

    def lockdown_removal(self):
        """ConsiderLockdown: a playable quick exhaust/lockdown card when the
        opponent has an untapped troop that can attack (so it can't this
        turn).  Returns (card, target_uid) or None."""
        has_threat = False
        for c in self.player_warzone:
            if (c.is_troop()
                    and not c.has_attribute(ECardAttributes.CantAttack)
                    and not (c.card_state is not None
                             and int(c.card_state or 0)
                             & __import__("game_engine").ECardStates.Tapped)):
                has_threat = True
                break
        if not has_threat:
            return None
        for card in self.hand:
            if self.is_playable(card) != "True":
                continue
            h = self.hints_for(card)
            if h.removal is not None and h.removal.exhaust:
                target = self.choose_action_target(card)
                if target is not None:
                    return card, target
        return None

    def find_removal_for(self, target, quick=False):
        """GetRemovalFor(target): return (removal_card, x_cost, target_uid)
        for the best playable removal in hand against this target, or
        (None, 0, None)."""
        if target.is_troop():
            if (target.has_attribute(ECardAttributes.CantAttack)
                    or target.has_attribute(ECardAttributes.Immortal)):
                return None, 0, None
        for card in self.hand:
            if self.is_playable(card) not in ("True", "NeedsResources"):
                continue
            if quick and not card.is_quick_action():
                continue
            h = self.hints_for(card)
            if h.removal is None:
                continue
            if self._can_target(card, target):
                # Hard removal (destroy/void/bounce) handles any targetable card.
                if h.removal.hard:
                    return card, 0, self.choose_action_target(card)
                # Exhaust/lockdown removal (tap a threat, "can't attack"): the
                # strongest opposing troop is the best target.
                if h.removal.exhaust and target.is_troop():
                    return card, 0, self.choose_action_target(card)
                threshold = h.removal.threshold
                x_cost = 0
                if card.variable_cost and not card.is_troop():
                    # Burn-to-the-ground style: X = defense to kill.
                    if self.resources - card.cost - 1 >= target.effective_defense(
                            in_play=True):
                        x_cost = target.effective_defense(in_play=True)
                        threshold = x_cost
                    else:
                        continue
                if threshold > 0 and threshold >= target.effective_defense(
                        in_play=True):
                    return card, x_cost, self.choose_action_target(card)
        return None, 0, None

    def _can_target(self, card, target):
        """Rough target legality for a removal card: damage/removal effects can
        hit opposing troops (and the champion where the text allows)."""
        if target.is_troop():
            return True
        for ag in card.ability_guids:
            for etype, pm in self.effects_for(ag):
                text = json.dumps(pm).lower()
                if etype == "CardModifierAbilityEffectTemplate":
                    if ("damage" in text and "target" in text
                            and ("champion" in text or "player" in text)):
                        return True
                if etype in ("DestroyCardAbilityEffectTemplate",
                             "VoidCardAbilityEffectTemplate",
                             "MoveCardToZoneEffectTemplate"):
                    if "target" in text and "card" in text:
                        return True
        return False

    def burn_to_win(self):
        """BurnToWin: a playable damage spell whose threshold (fixed or X at
        full resources) reaches the opponent's current health."""
        health = int(self.bstate.get("player_health", 20))
        for card in self.hand:
            if self.is_playable(card) != "True":
                continue
            h = self.hints_for(card)
            if h.removal is None or h.removal.debuff or h.removal.sweeper:
                continue
            if card.variable_cost and not card.is_troop():
                affordable = self.resources - card.cost - 1
                if affordable >= health:
                    return card
            elif h.removal.threshold >= health:
                return card
        return None

    def have_reasonable_counter(self, target):
        """HaveReasonableCounter: a warzone troop that survives or trades with
        the threat (so removal isn't wasted on an answerable threat)."""
        if target.has_attribute(ECardAttributes.Flight):
            return any(c.has_attribute(ECardAttributes.Flight)
                       or c.has_attribute(ECardAttributes.SkyGuard)
                       for c in self.ai_warzone if c.is_troop())
        for c in self.ai_warzone:
            if not c.is_troop():
                continue
            if c.effective_defense(in_play=True) > target.effective_attack():
                return True
            if c.effective_attack() >= target.effective_defense(in_play=True):
                return True
        return False

    def threatening_targets(self):
        """BuildBoard's removal target list: dangerous troops, non-troops
        (constants/artifacts), high-value or legendary cards without
        spellshield, plus the champion.  Sorted by card value descending."""
        targets = []
        for c in self.player_warzone:
            if c.has_attribute(ECardAttributes.SpellShield):
                continue
            if ((self.get_worry_value() > 0 or self.is_dangerous(c))
                    and not self.have_reasonable_counter(c)):
                targets.append(c)
            if not c.is_troop():
                targets.append(c)
            if self.is_high_value_target(c):
                targets.append(c)
            if (c.rarity or "").lower() == "legendary":
                targets.append(c)
        # de-dupe keeping highest value
        seen = {}
        for c in targets:
            if c.card_uid not in seen or self.get_card_value(c) > seen[
                    c.card_uid][1]:
                seen[c.card_uid] = (c, self.get_card_value(c))
        ordered = sorted(seen.values(), key=lambda kv: -kv[1])
        return [c for c, _ in ordered]

    # -- sweeping (AITactical.ConsiderSweeping) ----------------------------

    def best_sweeper(self):
        """Best playable board-wipe in hand: a removal whose effect hits all
        opposing troops (sweeper flag) and whose value gain outweighs our own
        losses.  Returns (card, x_cost) or None."""
        our_troops = [c for c in self.ai_warzone if c.is_troop()]
        opp_troops = [c for c in self.player_warzone if c.is_troop()]
        if not opp_troops:
            return None
        best = None
        best_gain = 0.0
        for card in self.hand:
            if self.is_playable(card) != "True":
                continue
            h = self.hints_for(card)
            if h.removal is None or not h.removal.sweeper:
                continue
            gain = 0.0
            loss = 0.0
            threshold = h.removal.threshold
            x_cost = 0
            if card.variable_cost and not card.is_troop():
                x_cost = max(0, self.resources - card.cost - 1)
                threshold = x_cost
            if threshold <= 0:
                continue
            for c in opp_troops:
                if c.effective_defense(in_play=True) <= threshold:
                    gain += self.loss_value(c)
            if not h.removal.one_sided:
                for c in our_troops:
                    if c.effective_defense(in_play=True) <= threshold:
                        loss += self.loss_value(c)
            net = gain - loss
            if net > best_gain and net > 0:
                best = (card, x_cost)
                best_gain = net
        return best

    # -- attitude shift (AITactical.ConsiderAttitutudeChange) --------------

    def update_attitude(self):
        """Aggressive -> Defensive at <=10 life vs a healthier opponent;
        Defensive -> Comfortable above 10 life; Comfortable -> Aggressive
        when ahead.  Mirrors ConsiderAttitutudeChange."""
        attitude = self.personality.attitude
        my_hp = self.ai_health
        opp_hp = self.player_health
        if attitude == "Aggressive":
            if my_hp <= 10 and opp_hp > my_hp:
                self.personality.attitude = "Defensive"
        elif attitude == "Comfortable":
            if opp_hp < my_hp:
                self.personality.attitude = "Aggressive"
            if my_hp <= 10 and opp_hp > my_hp:
                self.personality.attitude = "Defensive"
        elif attitude == "Defensive":
            if my_hp > 10:
                self.personality.attitude = "Comfortable"

    # -- targeting help ----------------------------------------------------
    def _metadata_action_targets(self, card, ag):
        """Return legal explicit targets from the card's target metadata."""
        row = _db.execute(
            "SELECT target_template_ids FROM card_abilities_meta "
            "WHERE ability_guid=?", (ag,)).fetchone()
        if not row or not row[0]:
            return None
        try:
            template_ids = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        found_explicit = False
        from abilities.framework.targeting import (
            legal_targets, target_uses_both_players,
        )
        champions = (self.handler._champion_targets()
                     if callable(getattr(self.handler, "_champion_targets", None))
                     else [])
        for template_id in template_ids or []:
            target = _db.execute(
                "SELECT target_kind, is_auto_target, explicit "
                "FROM target_templates WHERE template_id=?",
                (str(template_id),)).fetchone()
            kind = (target[0] if target else "") or ""
            auto = int(target[1] or 0) if target else 0
            explicit = int(target[2] or 0) if target else 0
            if (auto or not explicit or kind in (
                    "PlayerTargetTemplate", "AbilitySourceCardTargetTemplate",
                    "AbilityCreatedTargetTemplate")):
                continue
            found_explicit = True
            # ``legal_targets`` takes the database owner id, while the AI
            # turn APIs pass its wire UID (UID(type=3, instance=1000)).
            # Passing that wrapper through makes the filter tree fail when it
            # compares IsControlledBy/IsNotControlledBy ownership.
            ai_owner_id = 0
            candidates = legal_targets(
                _db, self.session.session_id, ai_owner_id, str(template_id),
                card.card_uid,
                both_players=target_uses_both_players(_db, str(template_id)),
                champions=champions, battle_state=self.bstate)
            if candidates:
                return [int(uid) for uid in candidates]
        return [] if found_explicit else None

    def choose_action_target(self, card):
        """Pick a target for a hand action using gamedata effect params:
        prefer the weakest opposing troop a damage/removal effect can kill,
        else the opponent champion.  None = no target needed."""
        for ag in card.ability_guids:
            candidates = self._metadata_action_targets(card, ag)
            if candidates:
                cards = {c.card_uid: c for c in
                         self.ai_warzone + self.player_warzone}
                available = [cards[uid] for uid in candidates if uid in cards]
                if available:
                    return max(available, key=lambda c: (
                        c.effective_attack(), c.effective_defense(), c.card_uid
                    )).card_uid
                return candidates[0]
        for ag in card.ability_guids:
            for etype, pm in self.effects_for(ag):
                if etype == "TapCardAbilityEffectTemplate":
                    troops = [c for c in self.player_warzone if c.is_troop()]
                    if troops:
                        return max(troops, key=lambda c: (
                            c.effective_attack(), c.card_uid)).card_uid
                if etype in ("DestroyCardAbilityEffectTemplate",
                             "VoidCardAbilityEffectTemplate",
                             "MoveCardToZoneEffectTemplate",
                             "TransformCardAbilityEffectTemplate"):
                    troops = [c for c in self.player_warzone if c.is_troop()]
                    if troops:
                        return min(troops, key=lambda c: (
                            c.effective_defense(), c.card_uid)).card_uid
                    text = json.dumps(pm).lower()
                    if "target" in text and "card" in text:
                        return self.player_champ_uid
                if etype == "CardModifierAbilityEffectTemplate":
                    prop = (pm.get("property") or "").lower()
                    text = json.dumps(pm).lower()
                    if prop == "damage":
                        troops = [c for c in self.player_warzone if c.is_troop()]
                        if troops:
                            return min(troops, key=lambda c: (
                                c.effective_defense(), c.card_uid)).card_uid
                        if "champion" in text or "player" in text:
                            return self.player_champ_uid
        return None


def best_play_for_ai(handler, session, battle_state, ai_uid, player_uid,
                     pre_combat=True):
    """Top-level entry: evaluate the AI hand and return the card to play
    (CardInfo) or None.  The caller moves it to the chain."""
    try:
        ev = build_evaluator(handler, session, battle_state, ai_uid, player_uid)
        return ev.get_best_board_builder(pre_combat)
    except Exception as exc:
        log_req(f"    ai_eval error: {exc!r}")
        return None


def build_evaluator(handler, session, battle_state, ai_uid, player_uid):
    """Build the CardEvaluator once per play decision so the caller can reuse
    its targeting/removal helpers."""
    champ_scid = getattr(handler, "_player_champ_scid", None)
    player_champ_uid = None
    if champ_scid is not None:
        try:
            player_champ_uid = int(champ_scid.uid.uid64)
        except Exception:
            player_champ_uid = None
    return CardEvaluator(handler, session, battle_state, ai_uid, player_uid,
                         player_champ_uid=player_champ_uid)

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

    def __init__(self, deck_personality=None, attitude="Comfortable"):
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
        } else "Comfortable"
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
        Defensive=5 — the resource reserve for X-cost card play and preferred
        X spending. This is not a combat attack threshold."""
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
        # Accept pre-double-X snapshots used by in-process callers that
        # construct the older tuple shape directly.
        if len(row) in (21, 23):
            row = tuple(row[:14]) + (0,) + tuple(row[14:])
        (self.card_uid, self.template_guid, self.location, self.card_type,
         self.name, self.rarity, self.cost, self.attack_base,
         self.defense_base, self.threshold_json, self.abilities_json,
         self.attributes, self.subtype, self.variable_cost,
         self.variable_cost_double,
         self.current_resources_granted, self.max_resources_granted,
         self.card_state, self.card_damage, self.permanent_buffs,
         self.temporary_buffs, self.temporary_attributes) = row[:22]
        self.card_uid = int(self.card_uid)
        self.cost = int(self.cost or 0)
        self.attack_base = int(self.attack_base or 0)
        self.defense_base = int(self.defense_base or 0)
        self.card_damage = int(self.card_damage or 0)
        self.card_attack_mod = 0
        self.card_defense_mod = 0
        # Optional trailing instance columns: warzone rows carry the two
        # stat-mod columns first, then the live ability/attribute lists.
        # Legacy callers that pass the shorter shapes simply skip this block.
        tail = tuple(row[22:])
        if tail and isinstance(tail[0], int):
            self.card_attack_mod = int(tail[0] or 0)
            self.card_defense_mod = int(tail[1] or 0) if len(tail) > 1 else 0
            tail = tail[2:]
        self.instance_abilities_json = tail[0] if tail else None
        self.instance_attributes = int(tail[1] or 0) if len(tail) > 1 else 0
        self.variable_cost = int(self.variable_cost or 0)
        self.variable_cost_double = int(self.variable_cost_double or 0)
        self.has_variable_cost = bool(
            self.variable_cost or self.variable_cost_double)
        self.variable_cost_multiplier = (
            2 if self.variable_cost_double else
            1 if self.variable_cost else 0)
        self.max_resources_granted = int(self.max_resources_granted or 0)
        self.current_resources_granted = int(
            self.current_resources_granted or 0)
        self.attributes = (int(self.attributes or 0)
                           | int(self.temporary_attributes or 0)
                           | int(self.instance_attributes or 0))
        self.type_flags = _card_type_flags(self.card_type)
        self.ability_guids = self._load_ability_guids()
        self.granted_ability_guids = self._load_granted_ability_guids()
        self._effects_cache = None

    def _load_ability_guids(self):
        if not self.abilities_json:
            return []
        try:
            return [str(g).lower() for g in json.loads(self.abilities_json)]
        except Exception:
            return []

    def _load_granted_ability_guids(self):
        """Live abilities beyond the printed template list (grants).

        ``game_cards.card_abilities`` is the authoritative per-instance list;
        anything not on the printed template was granted during the game.
        """
        if not self.instance_abilities_json:
            return ()
        try:
            instance = [str(g).lower()
                        for g in json.loads(self.instance_abilities_json)]
        except Exception:
            return ()
        printed = {guid.lower() for guid in self.ability_guids}
        return tuple(guid for guid in dict.fromkeys(instance)
                     if guid and guid not in printed)

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
            a += self.card_attack_mod
        return max(0, a)

    def effective_defense(self, in_play=False):
        d = self.defense_base
        for b in (_parse_buffs_json(self.permanent_buffs),
                  _parse_buffs_json(self.temporary_buffs)):
            d += int(b.get("def", 0) or 0)
        if in_play:
            d += self.card_defense_mod
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
        from pvp_db import db_ability_effect_type_params
        for ag in self.ability_guids:
            for e in db_ability_effect_type_params(ag, conn=_db):
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
        self.random_transform = False
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
        self.conscript = False
        self.conscript_value = 0.0
        self._value = None
        self._analyze()

    def _analyze(self):
        card = self.card
        for ag in card.ability_guids:
            self._find_buffs(ag)
            self._find_removal(ag)
            self._find_tricks(ag)
            self._find_conscript(ag)
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
            if (etype == "TransformCardAtRandomAbilityEffectTemplate"
                    and self.evaluator.random_transform_target_intent(ag)
                    == "opponent"):
                if self.removal is None:
                    self.removal = RemovalParams()
                self.removal.hard = True
                self.removal.random_transform = True
                continue
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
        elif card.has_variable_cost and not card.is_troop():
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

    def _find_conscript(self, ag):
        from pvp_db import db_ability_trigger_metadata
        ability_meta = db_ability_trigger_metadata(
            ag, conn=self.evaluator._connection())
        if (not ability_meta or bool(ability_meta[0])
                or bool(ability_meta[1])):
            return
        for effect_guid, effect_type in self.evaluator.effect_metadata_for(ag):
            if effect_type != "ConscriptAbilityEffectTemplate":
                continue
            self.conscript = True
            if self.card.is_action():
                self.conscript_value += self.evaluator.conscript_output_value(
                    self.card, ag, effect_guid)

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

    # Large enough to always lose a tie between same-name troops, small
    # relative to real value swings so it never overrides a genuine target.
    REDUNDANT_GRANT_PENALTY = 1000.0

    def __init__(self, handler, session, battle_state, ai_uid, player_uid,
                 player_champ_uid=None, ai_owner_id=0,
                 player_owner_id=None):
        self.handler = handler
        self.session = session
        self.bstate = battle_state
        self.ai_uid = ai_uid
        self.player_uid = player_uid
        self.player_champ_uid = player_champ_uid
        # Practice AI cards live under owner 0. In a two-seat PvP simulation
        # either player can be the evaluator's AI side, so keep both database
        # owners explicit while preserving the legacy profile fallback.
        self.ai_owner_id = int(ai_owner_id)
        profile_id = int((handler.user_profile or {}).get("id", 5) or 5)
        self.player_db_id = int(
            profile_id if player_owner_id is None else player_owner_id)
        self.resources = int(battle_state.get("ai_resources", 0))
        self.total_resources = int(battle_state.get("ai_total_resources", 0))
        self.threshold = battle_state.get("ai_threshold", {}) or {}
        # An absent deck strategy means EDeckPersonality.Default: apply no
        # deck-specific overrides. FRA setup infers a strategy from its deck
        # when no explicit encounter value is authored.
        deck_p = getattr(handler, "_ai_deck_personality", None)
        attitude = (getattr(handler, "_ai_campaign_personality", None)
                    or getattr(handler, "_ai_personality", None)
                    or "Comfortable")
        self.personality = Personality(deck_p, attitude=attitude)
        self.ai_health = int(battle_state.get("ai_health", 20))
        self.player_health = int(battle_state.get("player_health", 20))
        self._effects_cache = {}
        self._effect_metadata_cache = {}
        self._random_transform_intent_cache = {}
        self._summon_effect_cache = {}
        self._template_value_cache = {}
        self.hand = self._load_hand()
        self.ai_warzone = self._load_warzone(self.ai_owner_id)
        self.player_warzone = self._load_warzone(self.player_db_id)
        self.player_hand_count = self._hand_count(self.player_db_id)
        self._hints = {}
        self._granted_guids_cache = {}

    def _connection(self):
        """Return the connection belonging to this evaluator's host.

        Test harnesses and dual-seat simulations intentionally replace the
        handler connection per match.  Capturing the process-global ``db._db``
        at module import time can therefore point at a closed or unrelated
        SQLite connection.  Prefer the host's connection, while retaining the
        module alias for lightweight evaluator doubles that have no handler
        database attribute.
        """
        connection = getattr(self, "_db", None)
        if connection is None:
            connection = getattr(getattr(self, "handler", None), "_db", None)
        return connection if connection is not None else _db

    # -- DB loads ----------------------------------------------------------
    def _load_hand(self):
        from pvp_db import db_ai_evaluator_card_rows
        rows = db_ai_evaluator_card_rows(
            self.session.session_id, self.ai_owner_id, "hand",
            conn=self._connection())
        return [CardInfo(r) for r in rows]

    def _load_warzone(self, user_id):
        from pvp_db import db_ai_evaluator_card_rows
        rows = db_ai_evaluator_card_rows(
            self.session.session_id, user_id, "warzone",
            conn=self._connection())
        cards = []
        for r in rows:
            c = CardInfo(r)
            c.card_attack_mod = int(r[22] or 0)
            c.card_defense_mod = int(r[23] or 0)
            cards.append(c)
        return cards

    def _source_card_for_ability(self, source_uid):
        """Load the card that authored a triggered/nested ability.

        A Runic ability may resolve after its source has moved to discard or
        CastSpells, so it is not necessarily present in the evaluator's hand
        or Warzone snapshots.  Keep the lookup metadata-only and leave target
        candidates to the live Warzone snapshots above.
        """
        try:
            source_uid = int(source_uid)
        except (TypeError, ValueError):
            return None
        for card in self.hand + self.ai_warzone + self.player_warzone:
            if card.card_uid == source_uid:
                return card
        from pvp_db import db_ai_evaluator_card_row
        row = db_ai_evaluator_card_row(
            self.session.session_id, source_uid, conn=self._connection())
        return CardInfo(row) if row is not None else None

    def _hand_count(self, user_id):
        from pvp_db import db_hand_count
        return db_hand_count(
            self.session.session_id, user_id, conn=self._connection())

    # -- ability effect cache ----------------------------------------------
    def effects_for(self, ability_guid):
        if ability_guid in self._effects_cache:
            return self._effects_cache[ability_guid]
        out = []
        from pvp_db import db_ability_effect_type_params
        for e in db_ability_effect_type_params(
                ability_guid, conn=self._connection()):
            try:
                pm = json.loads(e[1]) if e[1] else {}
            except Exception:
                pm = {}
            out.append((e[0], pm if isinstance(pm, dict) else {}))
        self._effects_cache[ability_guid] = out
        return out

    def troop_summon_effects(self, card):
        """Return metadata for effects that summon troops into the Warzone.

        This intentionally classifies by the authored effect template and
        destination. Card names and display text are not part of the decision.
        """
        key = (str(card.template_guid).lower(),
               tuple(str(guid).lower() for guid in card.ability_guids))
        if key in self._summon_effect_cache:
            return list(self._summon_effect_cache[key])

        effects = []
        summon_types = {
            "SummonTokenTroopAbilityEffectTemplate",
            "SummonXTokenTroopsAbilityEffectTemplate",
        }
        for ability_guid in card.ability_guids:
            for effect_type, params in self.effects_for(ability_guid):
                if effect_type not in summon_types:
                    continue
                collection = str(
                    params.get("collection")
                    or params.get("card_collection") or "")
                if collection.casefold() != "warzone":
                    continue
                amount_variable = (params.get("amount_variable")
                                   or params.get("amount_field"))
                if not amount_variable:
                    try:
                        if int(params.get("amount", 0) or 0) <= 0:
                            continue
                    except (TypeError, ValueError):
                        continue
                template_guid = str(
                    params.get("token_guid")
                    or params.get("card_template_id") or "").strip().lower()
                if not template_guid:
                    continue
                attributes = 0
                try:
                    from pvp_db import db_card_template_field
                    card_type = str(db_card_template_field(
                        template_guid, "card_type",
                        conn=self._connection()) or "")
                    if card_type.casefold() != "troop":
                        continue
                    attributes = int(db_card_template_field(
                        template_guid, "attributes",
                        conn=self._connection()) or 0)
                except (TypeError, ValueError):
                    attributes = 0

                def authored_bool(value):
                    if isinstance(value, str):
                        return value.strip().casefold() in (
                            "1", "true", "yes")
                    return bool(value)

                effects.append({
                    "ability_guid": str(ability_guid).lower(),
                    "effect_type": effect_type,
                    "template_guid": template_guid or None,
                    "attributes": attributes,
                    "enters_play_exhausted": authored_bool(
                        params.get("enters_play_exhausted",
                                   params.get("exhausted", False))),
                    "enters_play_attacking": authored_bool(
                        params.get("enters_play_attacking", False)),
                })
        self._summon_effect_cache[key] = tuple(effects)
        return list(effects)

    def random_transform_target_intent(self, ability_guid):
        """Return the C# AI's side preference from authored ability metadata."""
        ability_guid = str(ability_guid).lower()
        if ability_guid in self._random_transform_intent_cache:
            return self._random_transform_intent_cache[ability_guid]
        intent = "opponent"
        try:
            from pvp_db import db_ability_activation_metadata
            row = db_ability_activation_metadata(
                ability_guid, conn=self._connection())
            raw = row[5] if row and len(row) > 5 else None
            metadata = json.loads(raw) if raw else {}
            if not isinstance(metadata, dict):
                metadata = {}
            game_text = str(metadata.get("m_GameText") or "").lower()
            if "+[(" in game_text:
                intent = "friendly"
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        self._random_transform_intent_cache[ability_guid] = intent
        return intent

    def effect_metadata_for(self, ability_guid):
        """Return effect GUID/type pairs from the current ability metadata."""
        ability_guid = str(ability_guid).lower()
        if ability_guid not in self._effect_metadata_cache:
            from pvp_db import db_ability_effect_metadata_rows
            rows = db_ability_effect_metadata_rows(
                ability_guid, conn=self._connection())
            self._effect_metadata_cache[ability_guid] = [
                (str(effect_guid).lower(), effect_type)
                for effect_guid, effect_type in rows]
        return self._effect_metadata_cache[ability_guid]

    def _template_value_from_catalog_row(self, row):
        """Value a generated card from typed template fields, as in
        AICardEvaluator.CalculateTemplateValue.
        """
        template_guid = str(row[0]).lower()
        if template_guid in self._template_value_cache:
            return self._template_value_cache[template_guid]
        template_card = CardInfo((
            -1, template_guid, "template", row[2], row[1], row[8],
            row[3], row[4], row[5], row[10], "[]", row[6], row[7],
            0, 0, 0, 0, 0, 0, None, None, 0))
        value = self.calculate_template_value(template_card)
        self._template_value_cache[template_guid] = value
        return value

    def conscript_output_value(self, card, ability_guid, effect_guid):
        """Estimate the value of the random card Conscript adds to hand.

        Candidate templates come from the same typed filter and mode-aware
        pool as effect resolution. The estimate is their mean template value,
        multiplied by the authored count (including the Underworld modifier).
        """
        try:
            from rules_port.bom_fields import effect_field, effect_template
            from rules_port.token_effects import (
                _authored_banned_guids, _matching_template_candidates,
            )
            from pvp_db import db_template_catalog_for_filter
            from types import SimpleNamespace

            template = effect_template(effect_guid) or {}
            card_filter = template.get("m_CardFilter")
            if card_filter is not None and hasattr(card_filter, "to_dict"):
                card_filter = card_filter.to_dict()
            if not isinstance(card_filter, dict):
                return 0.0

            state = dict(self.bstate or {})
            state.update({
                "session_id": self.session.session_id,
                "resolving_ability": str(ability_guid).lower(),
                "resolving_source_uid": int(card.card_uid),
                "resolving_owner_id": self.ai_owner_id,
            })
            # Planning is outside effect resolution; do not inherit temporary
            # inputs from whichever ability happened to resolve previously.
            state.pop("ability_variables", None)
            filter_context = SimpleNamespace(
                db=self._connection(), session=self.session, bstate=state)
            candidates = set(_matching_template_candidates(
                filter_context, card_filter,
                _authored_banned_guids(filter_context),
                source_uid=int(card.card_uid), player=self.ai_owner_id))
            if not candidates:
                return 0.0

            values = [
                self._template_value_from_catalog_row(row)
                for row in db_template_catalog_for_filter(
                    conn=self._connection())
                if str(row[0]).lower() in candidates]
            if not values:
                return 0.0

            amount = int(effect_field(
                self._connection(), state, effect_guid, "m_Amount",
                default=1) or 0)
            faction = template.get("m_Faction", "")
            if isinstance(faction, dict):
                faction = (faction.get("value__") or faction.get("name") or
                           faction.get("_t") or "")
            if str(faction).rsplit(".", 1)[-1].lower() == "underworld":
                try:
                    from rules_port.static_rules import player_int_attributes
                    amount += int(player_int_attributes(
                        self._connection(), self.session.session_id, state,
                        self.ai_owner_id).get(
                            "ConscriptUnderworldBonus", 0) or 0)
                except (ImportError, TypeError, ValueError):
                    pass
            amount = max(0, amount)
            return (sum(values) / len(values)) * amount
        except Exception as exc:
            log_req(f"    AI Conscript valuation error for {card.name}: "
                    f"{exc!r}")
            return 0.0

    # -- hints / value -----------------------------------------------------
    def hints_for(self, card):
        if card.card_uid not in self._hints:
            self._hints[card.card_uid] = Hints(card, self.personality, self)
        return self._hints[card.card_uid]

    def get_card_value(self, card):
        return self.hints_for(card).value

    def get_list_value(self, cards):
        return sum(self.get_card_value(c) for c in cards)

    # -- granted-ability awareness ----------------------------------------
    def granted_ability_guids_for(self, card):
        """Ability template IDs the hand card would grant on resolution.

        Read from the authored ``GrantAbilityEffectTemplate`` rows.  The
        stored ``ability_effects.param`` is either the bare granted GUID or a
        JSON object with ``m_GrantedAbilityTemplateId`` depending on the
        extraction, so both shapes are accepted.
        """
        if card is None:
            return frozenset()
        cache = getattr(self, "_granted_guids_cache", None)
        if cache is None:
            cache = self._granted_guids_cache = {}
        key = getattr(card, "card_uid", None)
        if key in cache:
            return cache[key]
        guids = set()
        try:
            from pvp_db import db_ability_effect_type_params
            connection = getattr(self, "_connection", None)
            conn = connection() if callable(connection) else _db
            for ability_guid in getattr(card, "ability_guids", ()) or ():
                for effect_type, raw in db_ability_effect_type_params(
                        ability_guid, conn=conn):
                    if str(effect_type) != "GrantAbilityEffectTemplate":
                        continue
                    guid = self._grant_param_guid(raw)
                    if guid:
                        guids.add(guid)
        except Exception:
            # Target ranking must never fail because authored effect metadata
            # is unavailable; fall back to no-grant scoring.
            guids = set()
        result = frozenset(guids)
        cache[key] = result
        return result

    @staticmethod
    def _grant_param_guid(raw):
        if raw is None:
            return ""
        text = str(raw).strip()
        if not text:
            return ""
        try:
            data = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            value = (data.get("m_GrantedAbilityTemplateId")
                     or data.get("granted_ability_template_id"))
            text = str(value or "")
        elif isinstance(data, str):
            text = data
        text = text.strip().strip('"').lower()
        return text if len(text) == 36 and "-" in text else ""

    def reapply_penalty(self, card, target):
        """Penalty when ``card`` would re-grant an ability ``target`` has.

        Keeps a debuff/curse from stacking on the same troop when an
        identically-statriced twin is available: the cursed troop scores
        lower, so the same-name clean troop wins the tie.
        """
        grants = self.granted_ability_guids_for(card)
        if not grants:
            return 0.0
        owned = set(getattr(target, "granted_ability_guids", ()) or ())
        if grants & owned:
            return self.REDUNDANT_GRANT_PENALTY
        return 0.0

    def target_score(self, card, target):
        """``get_card_value`` adjusted for abilities the card would re-grant."""
        if card is None:
            return self.get_card_value(target)
        return (self.get_card_value(target)
                - self.reapply_penalty(card, target))

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
            # Conscript adds a random card from its authored candidate pool to
            # hand. Value the expected result using the same template-value
            # model used for generated-card candidates.
            num += hint.conscript_value
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
    def can_pay(self, cost, variable=False, variable_multiplier=1):
        if variable:
            # Require enough to choose at least one X for the AI's useful
            # playability checks. Double-X metadata doubles payment per X.
            cost += max(1, int(variable_multiplier or 1))
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
        if card.has_variable_cost and not card.is_troop():
            cost += max(1, card.variable_cost_multiplier)
        if not self.handler._thresholds_met(card.threshold_json,
                                            self.threshold):
            return "False"
        if card.is_action():
            for ag in card.ability_guids:
                if self._action_needs_target(card, ag) and not self._has_legal_target(card, ag):
                    return "False"
            if (self.has_required_explicit_target(card)
                    and self.choose_action_target(card) is None):
                return "False"
        if self.resources + self._on_board_resources() >= cost:
            return "True"
        return "NeedsResources"

    def preferred_x_cost(self, card):
        """Return the AI's preferred affordable X value for a card play."""
        if not card.has_variable_cost:
            return 0
        multiplier = max(1, card.variable_cost_multiplier)
        affordable = max(0, self.resources - card.cost) // multiplier
        return max(0, min(int(self.personality.minimum_x_value), affordable))

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
        if card.has_variable_cost and not card.is_troop():
            cost += max(1, card.variable_cost_multiplier)
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
            # [] means complete metadata exists but every target is automatic
            # or implicit (or no legal manual target currently exists). Only
            # a nonempty manual-target set needs a target-choice check here;
            # required explicit targets are rejected separately below.
            return bool(metadata_targets)
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
        hints = self.hints_for(card)
        if card.is_action() and hints.conscript:
            if card.has_variable_cost and not self.can_pay(
                    card.cost, variable=True,
                    variable_multiplier=card.variable_cost_multiplier):
                return False
            return True
        if card.is_basic_action():
            # Non-quick actions: removal, lifegain and buffs are worth playing
            # if they have a legal target (or are targetless).
            if card.has_variable_cost and not self.can_pay(
                    card.cost, variable=True,
                    variable_multiplier=card.variable_cost_multiplier):
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
                if card.has_variable_cost and not card.is_troop():
                    cost += max(1, card.variable_cost_multiplier)
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
            if target.has_attribute(ECardAttributes.CantAttack):
                return None, 0, None
        for card in self.hand:
            if self.is_playable(card) not in ("True", "NeedsResources"):
                continue
            if quick and not card.is_quick_action():
                continue
            h = self.hints_for(card)
            if h.removal is None:
                continue
            if h.removal.random_transform:
                if (not target.is_troop()
                        or not self.is_random_transform_priority_target(target)):
                    continue
            elif (target.is_troop()
                  and target.has_attribute(ECardAttributes.Immortal)):
                continue
            # A non-hard "removal" that only re-applies an ability the target
            # already carries is spent: let the threat loop consider the next
            # target (e.g. the clean same-name twin) instead of stacking the
            # same curse on one troop (Spider Nest).
            if (not h.removal.hard
                    and self.reapply_penalty(card, target) > 0):
                continue
            if self._can_target(card, target):
                # Match the C# GetRemovalFor(c) contract: the target being
                # evaluated is also the target passed to the activation. Do
                # not re-run a generic selector here; it could choose one of
                # our own troops when an authored filter allows both sides.
                target_uid = self.choose_action_target(
                    card, preferred_target=target.card_uid)
                if target_uid != target.card_uid:
                    continue
                # Hard removal (destroy/void/bounce) handles any targetable card.
                if h.removal.hard:
                    return card, 0, target_uid
                # Exhaust/lockdown removal (tap a threat, "can't attack"): the
                # strongest opposing troop is the best target.
                if h.removal.exhaust and target.is_troop():
                    return card, 0, target_uid
                threshold = h.removal.threshold
                x_cost = 0
                if card.has_variable_cost and not card.is_troop():
                    # Burn-to-the-ground style: X = defense to kill.
                    affordable_x = max(0, self.resources - card.cost) // max(
                        1, card.variable_cost_multiplier)
                    if affordable_x >= target.effective_defense(
                            in_play=True):
                        x_cost = target.effective_defense(in_play=True)
                        threshold = x_cost
                    else:
                        continue
                if threshold > 0 and threshold >= target.effective_defense(
                        in_play=True):
                    return card, x_cost, target_uid
        return None, 0, None

    def is_random_transform_priority_target(self, target):
        """Only spend a random transform on a serious opposing troop threat."""
        if not target.is_troop():
            return False
        return bool(
            self.is_dangerous(target)
            or self.is_high_value_target(target)
            or (target.rarity or "").lower() == "legendary"
            or target.has_attribute(ECardAttributes.CantBeBlocked))

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
            if card.has_variable_cost and not card.is_troop():
                affordable = max(0, self.resources - card.cost) // max(
                    1, card.variable_cost_multiplier)
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
            if (c.is_troop()
                    and c.has_attribute(ECardAttributes.CantBeBlocked)):
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
            if card.has_variable_cost and not card.is_troop():
                x_cost = max(0, self.resources - card.cost) // max(
                    1, card.variable_cost_multiplier)
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
    def _metadata_action_target_slots(self, card, ag):
        """Return legal choices and authored bounds for each explicit slot.

        ``None`` means the ability has no complete target metadata. An empty
        list means complete metadata exists but has no player-selected slot.
        Each slot keeps its original target-template index because effects
        refer to targets by that index.
        """
        from pvp_db import db_ability_target_template_ids, db_target_template_row
        connection = self._connection()
        payload = db_ability_target_template_ids(ag, conn=connection)
        if not payload:
            return None
        try:
            template_ids = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not template_ids:
            return None
        target_rows = [db_target_template_row(str(template_id), conn=connection)
                       for template_id in template_ids]
        if any(target is None for target in target_rows):
            return None
        from rules_port.targeting import (
            legal_targets, target_uses_both_players,
        )
        champions = []
        ai_champion = getattr(self.handler, "_ai_champ_scid", None)
        try:
            if ai_champion is not None:
                champions.append((int(ai_champion.uid.uid64),
                                  self.ai_owner_id, "AI", self.ai_health))
        except (AttributeError, TypeError, ValueError):
            pass
        if self.player_champ_uid is not None:
            champions.append((int(self.player_champ_uid), self.player_db_id,
                              "Player", self.player_health))
        slots = []
        for target_index, (template_id, target) in enumerate(
                zip(template_ids, target_rows)):
            kind = (target[11] if target else "") or ""
            auto = int(target[2] or 0) if target else 0
            explicit = int(target[5] or 0) if target else 0
            random_target = int(target[3] or 0) if target else 0
            if (auto or not explicit or kind in (
                    "PlayerTargetTemplate", "AbilitySourceCardTargetTemplate",
                    "AbilityCreatedTargetTemplate") or random_target):
                continue
            # ``legal_targets`` takes the database owner id, while the AI
            # turn APIs pass its wire UID (UID(type=3, instance=1000)).
            # Passing that wrapper through makes the filter tree fail when it
            # compares IsControlledBy/IsNotControlledBy ownership.
            candidates = legal_targets(
                connection, self.session.session_id, self.ai_owner_id,
                str(template_id),
                card.card_uid,
                both_players=target_uses_both_players(
                    connection, str(template_id)),
                champions=champions, battle_state=self.bstate)
            slots.append({
                "index": target_index,
                "template_id": str(template_id),
                "candidates": [int(uid) for uid in candidates],
                "minimum": max(0, int(target[8] or 0)),
                "maximum": max(0, int(target[9] or 0)),
            })
        return slots

    def _metadata_action_targets(self, card, ag):
        """Return legal player-selected targets from authored metadata.

        An empty list means complete target metadata exists but has no legal
        manual choice. None means metadata is absent or incomplete and legacy
        effect inference may apply.
        """
        slots = self._metadata_action_target_slots(card, ag)
        if slots is None:
            return None
        for slot in slots:
            if slot["candidates"]:
                return list(slot["candidates"])
        # Complete authored metadata is authoritative even when every target
        # is automatic or implicit. Do not infer a single-card choice from an
        # effect such as damage-to-each-opposing-champion.
        return []

    def has_required_explicit_target(self, card):
        """Whether playing this action requires a selected card target.

        This is deliberately based on target-template metadata, not the
        presence of a target-shaped effect. Auto-targets and optional target
        slots may legally resolve without an AI-selected card.
        """
        from pvp_db import db_ability_target_template_ids, db_target_template_row

        connection = self._connection()
        for ability_guid in card.ability_guids:
            payload = db_ability_target_template_ids(
                ability_guid, conn=connection)
            try:
                template_ids = json.loads(payload or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                template_ids = []
            rows = [db_target_template_row(str(template_id), conn=connection)
                    for template_id in template_ids or []]
            if rows and all(row is not None for row in rows):
                for row in rows:
                    is_auto = int(row[2] or 0)
                    is_random = int(row[3] or 0)
                    is_optional = int(row[4] or 0)
                    is_explicit = int(row[5] or 0)
                    min_count = int(row[8] or 0)
                    kind = str(row[11] or "")
                    if (is_explicit and not is_auto and not is_random
                            and kind not in (
                                "PlayerTargetTemplate",
                                "AbilitySourceCardTargetTemplate",
                                "AbilityCreatedTargetTemplate")
                            and not is_optional
                            and min_count > 0):
                        return True
                # Complete target metadata is authoritative, including when
                # every slot is implicit or optional.
                continue

            # Compatibility for older ability snapshots without target rows.
            if self._action_needs_target(card, ability_guid):
                return True
        return False

    def _action_target_intent(self, card, ability_guid):
        """Classify an explicit target from its authored effect parameters."""
        hostile = False
        beneficial = False
        for effect_type, params in self.effects_for(ability_guid):
            if effect_type in (
                    "DestroyCardAbilityEffectTemplate",
                    "VoidCardAbilityEffectTemplate",
                    "ReturnToHandAbilityEffectTemplate",
                    "TapCardAbilityEffectTemplate",
                    "TransformCardAbilityEffectTemplate"):
                hostile = True
            elif effect_type == "MoveCardToZoneEffectTemplate":
                if (params.get("destination") or "").lower() in (
                        "hand", "deck", "void"):
                    hostile = True
            elif effect_type == "TransformCardAtRandomAbilityEffectTemplate":
                transform_intent = self.random_transform_target_intent(
                    ability_guid)
                hostile = hostile or transform_intent == "opponent"
                beneficial = beneficial or transform_intent == "friendly"
            elif effect_type == "CardModifierAbilityEffectTemplate":
                prop = (params.get("property") or "").lower()
                try:
                    amount = int(params.get("amount", 0) or 0)
                except (TypeError, ValueError):
                    amount = 0
                operation = (params.get("operation") or "").lower()
                if prop in ("damage", "damagehero") or amount < 0 \
                        or operation in ("remove", "subtract"):
                    hostile = True
                elif prop in ("attack", "defense", "attribute", "heal",
                              "healhero") and (
                        amount > 0 or operation in ("add", "set")):
                    beneficial = True
            elif effect_type == "GrantAbilityEffectTemplate":
                beneficial = True

        # If a single ability mixes a debuff/removal and a positive modifier,
        # prefer the opponent. Legal-target metadata still decides what can
        # actually be selected.
        if hostile:
            return "opponent"
        if beneficial:
            return "friendly"

        # Preserve the evaluator's established BOM classification for effects
        # whose parameters do not expose a numeric modifier (e.g. keyword
        # grants represented by a nested effect).
        hints = self.hints_for(card)
        if hints.removal is not None and hints.buff is None:
            return "opponent"
        if hints.buff is not None and hints.removal is None:
            return "friendly"
        return None

    def _target_side_uids(self):
        friendly = {c.card_uid for c in self.ai_warzone}
        opposing = {c.card_uid for c in self.player_warzone}
        ai_champion = getattr(self.handler, "_ai_champ_scid", None)
        try:
            if ai_champion is not None:
                friendly.add(int(ai_champion.uid.uid64))
        except (AttributeError, TypeError, ValueError):
            pass
        if self.player_champ_uid is not None:
            opposing.add(int(self.player_champ_uid))
        return friendly, opposing

    def _best_target(self, candidates, card=None):
        """Apply the C# evaluator's card-value ordering to legal candidates."""
        return max(candidates, key=lambda c: (
            c.is_troop(), self.target_score(card, c), c.effective_attack(),
            c.effective_defense(), c.card_uid))

    def choose_ability_target_map(self, source_uid, ability_guid,
                                  preferred_target=None):
        """Choose fresh explicit targets for a triggered ability instance.

        This is the native equivalent of the C# ``GetActivationFor`` pass:
        enumerate the current legal target pool and apply the same evaluator
        ordering, even when the source card is no longer in hand/Warzone.
        """
        card = self._source_card_for_ability(source_uid)
        if card is None:
            return None
        # Random explicit targets are resolved by the native session RNG, not
        # by the deterministic card-value ordering used for player pickers.
        # Returning ``None`` keeps that resolver path intact.
        from pvp_db import db_ability_target_template_ids, db_target_template_row
        connection = self._connection()
        try:
            payload = db_ability_target_template_ids(
                str(ability_guid).lower(), conn=connection)
            template_ids = json.loads(payload or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        manual_target = False
        for template_id in template_ids or []:
            target = db_target_template_row(
                str(template_id), conn=connection)
            if target is None:
                return None
            kind = str(target[11] or "")
            if (int(target[2] or 0) == 0 and
                    int(target[3] or 0) == 0 and
                    int(target[5] or 0) != 0 and kind not in (
                        "PlayerTargetTemplate",
                        "AbilitySourceCardTargetTemplate",
                        "AbilityCreatedTargetTemplate")):
                manual_target = True
        if not manual_target:
            return None
        return self.choose_action_target_map(
            card, preferred_target=preferred_target,
            ability_guids=(str(ability_guid).lower(),))

    def choose_action_target_map(self, card, preferred_target=None,
                                 ability_guids=None):
        """Choose explicit targets by authored slot and target-count bounds.

        ``None`` means no complete metadata was available. An empty mapping
        means the metadata is complete and no manual target is needed or
        currently legal.
        """
        friendly_uids, opposing_uids = self._target_side_uids()
        preferred = []
        if preferred_target is not None:
            raw = (preferred_target if isinstance(
                preferred_target, (list, tuple, set)) else [preferred_target])
            for value in raw:
                try:
                    preferred.append(int(value))
                except (TypeError, ValueError):
                    continue
        saw_authoritative_targets = False
        cards = {c.card_uid: c for c in
                 self.ai_warzone + self.player_warzone}
        if ability_guids is None:
            ability_ids = card.ability_guids
        elif isinstance(ability_guids, str):
            ability_ids = (ability_guids,)
        else:
            ability_ids = tuple(ability_guids)
        for ag in ability_ids:
            slots = self._metadata_action_target_slots(card, ag)
            if slots is None:
                continue
            saw_authoritative_targets = True
            if not slots:
                continue
            intent = self._action_target_intent(card, ag)
            selected_map = {}
            for slot in slots:
                candidates = list(slot["candidates"])
                if not candidates:
                    if slot["minimum"] > 0:
                        return {}
                    continue
                side_ids = (opposing_uids if intent == "opponent" else
                            friendly_uids if intent == "friendly" else None)
                preferred_candidates = ([uid for uid in candidates
                                         if uid in side_ids]
                                        if side_ids is not None else candidates)
                # Target-filter metadata is authoritative. An effect heuristic
                # must not reject its only legal targets when the authored
                # filter permits the opposite side.
                if preferred_candidates:
                    candidates = preferred_candidates
                available = [cards[uid] for uid in candidates if uid in cards]
                ordered = [item.card_uid for item in sorted(
                    available,
                    key=lambda item: (item.is_troop(),
                                      # Deprioritize a target that already
                                      # carries an ability this card would
                                      # re-grant, so same-name troops spread.
                                      self.target_score(card, item),
                                      item.effective_attack(),
                                      item.effective_defense(), item.card_uid),
                    reverse=True)]
                ordered.extend(uid for uid in candidates if uid not in cards)
                for uid in reversed(preferred):
                    if uid in ordered:
                        ordered.remove(uid)
                        ordered.insert(0, uid)
                maximum = int(slot["maximum"] or 0)
                count = (len(ordered) if maximum <= 0 else
                         min(len(ordered), maximum))
                chosen = ordered[:count]
                if len(chosen) < int(slot["minimum"] or 0):
                    return {}
                if chosen:
                    selected_map[int(slot["index"])] = tuple(chosen)
            if selected_map:
                return selected_map
        return {} if saw_authoritative_targets else None

    def choose_action_targets(self, card, preferred_target=None):
        """Return selected card IDs in authored target-slot order."""
        target_map = self.choose_action_target_map(
            card, preferred_target=preferred_target)
        if target_map is not None:
            return [int(uid) for values in target_map.values()
                    for uid in values]
        target = self.choose_action_target(card, preferred_target)
        return [] if target is None else [int(target)]

    def choose_precombat_block_restriction(self):
        """Choose an action adding CantBlock to legal opposing troops.

        The effect attribute, operation, target index, legal candidates, and
        target maximum all come from authored ability/target metadata.
        """
        opposing = {card.card_uid: card for card in self.player_warzone}
        cant_block = int(ECardAttributes.CantBlock)
        for card in self.hand:
            if not card.is_action() or self.is_playable(card) != "True":
                continue
            for ag in card.ability_guids:
                slots = self._metadata_action_target_slots(card, ag)
                if slots is None:
                    continue
                for effect_type, params in self.effects_for(ag):
                    if effect_type != "CardModifierAbilityEffectTemplate":
                        continue
                    if (str(params.get("property") or "").lower()
                            != "attribute"
                            or str(params.get("operation") or "").lower()
                            != "add"):
                        continue
                    raw_flags = params.get("attribute_flags") or ""
                    if isinstance(raw_flags, (list, tuple, set)):
                        names = {str(value).replace("_", "").lower()
                                 for value in raw_flags}
                    else:
                        names = {
                            part.strip().replace("_", "").lower()
                            for part in str(raw_flags).replace(",", "|")
                            .split("|") if part.strip()
                        }
                    matches = "cantblock" in names
                    if not matches:
                        try:
                            matches = bool(int(raw_flags) & cant_block)
                        except (TypeError, ValueError):
                            pass
                    if not matches:
                        continue
                    try:
                        target_index = int(params.get("target_index", 0) or 0)
                    except (TypeError, ValueError):
                        target_index = 0
                    slot = next((value for value in slots
                                 if int(value["index"]) == target_index), None)
                    if slot is None:
                        continue
                    candidates = [opposing[uid] for uid in slot["candidates"]
                                  if uid in opposing and opposing[uid].is_troop()
                                  and not opposing[uid].has_attribute(
                                      ECardAttributes.CantBlock)]
                    if not candidates:
                        continue
                    candidates.sort(key=lambda item: (
                        self.get_card_value(item), item.effective_attack(),
                        item.effective_defense(), item.card_uid), reverse=True)
                    maximum = int(slot["maximum"] or 0)
                    chosen = candidates if maximum <= 0 else candidates[:maximum]
                    if len(chosen) < int(slot["minimum"] or 0):
                        continue
                    return card, {target_index: tuple(
                        int(item.card_uid) for item in chosen)}
        return None

    def choose_action_target(self, card, preferred_target=None):
        """Pick a target for a hand action using gamedata effect params:
        enforce effect-side preference after metadata legality, and retain an
        explicitly selected legal target.  None = no target needed."""
        target_map = self.choose_action_target_map(
            card, preferred_target=preferred_target)
        if target_map is not None:
            return next((int(uid) for values in target_map.values()
                         for uid in values), None)
        friendly_uids, opposing_uids = self._target_side_uids()
        saw_authoritative_targets = False
        for ag in card.ability_guids:
            candidates = self._metadata_action_targets(card, ag)
            if candidates is not None:
                saw_authoritative_targets = True
            if candidates:
                intent = self._action_target_intent(card, ag)
                if preferred_target is not None:
                    preferred_target = int(preferred_target)
                    if preferred_target not in candidates:
                        continue
                    if intent == "opponent" and preferred_target not in opposing_uids:
                        continue
                    if intent == "friendly" and preferred_target not in friendly_uids:
                        continue
                    return preferred_target

                cards = {c.card_uid: c for c in
                         self.ai_warzone + self.player_warzone}
                available = [cards[uid] for uid in candidates if uid in cards]
                if intent == "opponent":
                    available = [c for c in available
                                 if c.card_uid in opposing_uids]
                    if available:
                        return self._best_target(available, card=card).card_uid
                    champions = [uid for uid in candidates
                                 if uid in opposing_uids]
                    if champions:
                        return champions[0]
                    continue
                if intent == "friendly":
                    available = [c for c in available
                                 if c.card_uid in friendly_uids]
                    if available:
                        return self._best_target(available, card=card).card_uid
                    champions = [uid for uid in candidates
                                 if uid in friendly_uids]
                    if champions:
                        return champions[0]
                    continue
                if available:
                    return max(available, key=lambda c: (
                        c.effective_attack(), c.effective_defense(), c.card_uid
                    )).card_uid
                return candidates[0]
        # Complete authored target metadata with no manual candidate is
        # authoritative; don't infer one from the effect as a fallback.
        if saw_authoritative_targets:
            return None
        if preferred_target is not None:
            # Metadata-less legacy removal effects are already checked by
            # find_removal_for; preserve its exact opposing threat rather than
            # selecting a different (possibly friendly) permanent.
            if int(preferred_target) in opposing_uids:
                return int(preferred_target)
            return None

        for ag in card.ability_guids:
            intent = self._action_target_intent(card, ag)
            if intent == "opponent":
                troops = [c for c in self.player_warzone if c.is_troop()]
                if troops:
                    return self._best_target(troops, card=card).card_uid
                if self.player_champ_uid is not None:
                    return self.player_champ_uid
            elif intent == "friendly":
                troops = [c for c in self.ai_warzone if c.is_troop()]
                if troops:
                    return self._best_target(troops, card=card).card_uid
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


def build_evaluator(handler, session, battle_state, ai_uid, player_uid,
                    ai_owner_id=0, player_owner_id=None):
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
                         player_champ_uid=player_champ_uid,
                         ai_owner_id=ai_owner_id,
                         player_owner_id=player_owner_id)

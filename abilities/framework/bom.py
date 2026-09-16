"""BOM (Bill of Materials) walking and leaf executors for champion abilities.

A champion ability is a bill-of-materials: the ``ability_effects`` table expands
it into an ordered list of leaf effect templates.  Each leaf's ``effect_type``
maps to an executor registered via ``@leaf_register``.
"""

import json
import struct
import random

import game_engine

from ._shared import (_log, next_game_card_uid, owner_uid, pvp_champion_uid,
                      pvp_opponent_pid, state_after_zone_exit)
from .effects.damage import deal_damage
from .context import EffectContext
from .effects.registry import _LEAFS, effect, leaf_register
from .effects import combat as _combat  # register combat effects
from .effects import choices as _choices  # register card-choice effects
from .effects import utility as _utility  # register generic client effects
from gamedata import DEFAULT_RECORD_STORE, ability_graph, runtime_effects

from .fields import (ability_record, effect_field, effect_template,
                     effect_template_value, modifier_metadata,
                     counter_template_name)


_RECORD_STORE = DEFAULT_RECORD_STORE


def _walk_bom(db, ability_guid):
    """Return ordered BOM rows from the current typed Records graph."""
    if not ability_guid:
        return []
    graph = ability_graph(_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        raise RuntimeError(
            f"ability {str(ability_guid).lower()} is missing from current Records")
    return list(runtime_effects(graph))


def _deck_owner_for_target(db, handler, session, bstate, target):
    """Resolve a target champion to the DB owner of that champion's deck."""
    if target is None:
        return None
    from pvp_db import db_card_owner_id
    card_owner = db_card_owner_id(session.session_id, int(target), conn=db)
    if card_owner is not None:
        return int(card_owner)
    if (bstate or {}).get("pvp"):
        # PvP champion cards are represented by champ_map rather than rows in
        # game_cards.  Both pids are nonzero, so do not collapse the opponent
        # onto the local player's UID.
        for pid, champ_uid in ((bstate or {}).get("champ_map") or {}).items():
            try:
                if int(champ_uid) == int(target):
                    return int(pid)
            except (TypeError, ValueError):
                continue
    p = getattr(handler, "_player_champ_scid", None)
    a = getattr(handler, "_ai_champ_scid", None)
    if p is not None and int(target) == int(p.uid.uid64):
        return (handler.user_profile["id"] if handler.user_profile else 0)
    if a is not None and int(target) == int(a.uid.uid64):
        return 0
    return None


def _reveal_owner_for_target(db, handler, session, bstate, default_owner,
                             target_kind, ability_guid):
    """Resolve the player whose cards a metadata target asks us to reveal."""
    if target_kind != "MatchSecondaryTargetTemplate":
        return default_owner
    stored = ((bstate or {}).get("stored_targets", {})
              .get(ability_guid) or [])
    if not stored:
        return default_owner
    target_owner = _deck_owner_for_target(
        db, handler, session, bstate, stored[-1])
    return default_owner if target_owner is None else int(target_owner)


# ---------------------------------------------------------------------------
#  Leaf executors
# ---------------------------------------------------------------------------

@effect("DrawNCardsAbilityEffectTemplate")
def _leaf_draw(effect):
    """Draw the typed count for the resolved target or caster."""
    return effect.draw_effect()


@effect("ConversationAbilityEffectTemplate")
def _leaf_conversation(effect):
    """Open the authored encounter conversation and suspend the BOM."""
    return effect.conversation()


@effect("PutTopOfDeckIntoHandAbilityEffectTemplate")
def _leaf_put_top_into_hand(effect):
    """Put typed-count deck cards into the caster's hand."""
    return effect.put_top_into_hand()


@effect("DiscardCardAbilityEffectTemplate")
def _leaf_discard(effect):
    """Discard the card selected by the ability target instance.

    ``DiscardCardAbilityEffectTemplate`` is also used for random and
    multi-card effects.  The resolver supplies one metadata-legal target at a
    time, so this leaf must only move that target and must not infer a card
    from the ability's display text.  The same path is used for PvP, PvE, and
    champion/talent abilities.
    """
    return effect.discard()


@effect("RandomizeVariableEffectTemplate")
def _leaf_randomize(effect):
    """Roll the typed random-variable effect into the active builder state."""
    return effect.randomize_variable()


@effect("RandomizeVariableAbilityEffectTemplate")
def _legacy_randomize(effect):
    """Retain the historical custom alias for old direct callers."""
    from abilities.cards.replenish_spell_power import replenish_spell_power

    return replenish_spell_power(
        effect.game, effect.session, effect.db, effect.handler,
        effect.player_uid, effect.ai_uid, effect.bstate,
        effect.effect_guid, None)


def _champion_target_uid(handler, bstate, db, session):
    """When an ability's first target template is a PlayerTargetTemplate
    ('You' — the controller's champion, e.g. Shamed Gladiator's Deploy "This
    deals 2 damage to you"), return that champion's SessionCardId uid — the
    effect hits the champion, not the source card.  Data-driven from the
    template's gamedata kind."""
    ag = (bstate or {}).get("resolving_ability")
    if not ag:
        return None
    from pvp_db import (db_ability_target_template_ids, db_target_template_info,
                        db_card_owner_id)
    target_ids = db_ability_target_template_ids(ag, conn=db)
    if not target_ids:
        return None
    try:
        tids = json.loads(target_ids)
    except Exception:
        return None
    if not tids:
        return None
    trow = db_target_template_info(tids[0], conn=db)
    if not trow or (trow[1] or "") != "PlayerTargetTemplate":
        return None
    owner = (bstate or {}).get("resolving_owner_id")
    if owner is None:
        owner = (bstate or {}).get("resolving_source_uid")
        if owner is not None:
            owner = db_card_owner_id(
                session.session_id, int(owner), conn=db) or 0
    if (bstate or {}).get("pvp"):
        champ_uid = pvp_champion_uid(bstate, owner)
        return int(champ_uid) if champ_uid is not None else None
    champ = (getattr(handler, "_player_champ_scid", None) if owner
             else getattr(handler, "_ai_champ_scid", None))
    if champ is None:
        return None
    return int(champ.uid.uid64)


def _opposing_champion_uid(handler, bstate, db, session):
    """"This deals N damage to each opposing champion" — the effect's gamedata
    AbilityTargetTemplate declares MultiplePlayers / "each opposing champion";
    the champion opposite the ability source's controller is the target."""
    import json as _j
    ag = (bstate or {}).get("resolving_ability", "")
    if not ag:
        return None
    from pvp_db import (db_ability_target_template_ids,
                        db_target_template_targeting_info)
    target_ids = db_ability_target_template_ids(ag, conn=db)
    if not target_ids:
        return None
    try:
        tids = _j.loads(target_ids)
    except Exception:
        return None

    def _has_filter(node, wanted):
        if isinstance(node, dict):
            if str(node.get("_t", "")).rsplit(".", 1)[-1] == wanted:
                return True
            return any(_has_filter(value, wanted)
                       for value in node.values())
        if isinstance(node, list):
            return any(_has_filter(value, wanted) for value in node)
        return False

    for tid in (tids or []):
        trow = db_target_template_targeting_info(tid, conn=db)
        if not trow:
            continue
        try:
            target_filter = _j.loads(trow[0] or "{}")
        except (TypeError, ValueError, _j.JSONDecodeError):
            target_filter = {}
        # MultiplePlayers alone is not enough: it can describe a target pool
        # containing troops and champions.  The typed filter must identify a
        # champion and exclude the source controller.
        opposing_champion = (
            _has_filter(target_filter, "IsHero") and
            (_has_filter(target_filter, "IsNotControlledBy") or
             str(trow[1] or "").lower() in ("opponent", "opposing")))
        if opposing_champion:
            owner = (bstate or {}).get("resolving_owner_id", 0)
            if (bstate or {}).get("pvp"):
                opponent_pid = pvp_opponent_pid(bstate, owner)
                champ_uid = pvp_champion_uid(bstate, opponent_pid)
                return int(champ_uid) if champ_uid is not None else None
            if owner:
                a = getattr(handler, "_ai_champ_scid", None)
                return int(a.uid.uid64) if a else None
            p = getattr(handler, "_player_champ_scid", None)
            return int(p.uid.uid64) if p else None
    return None


def _apply_resource_property(game, session, db, handler, pl_t, ai_t, bstate,
                             pm, target_uid):
    """Apply resource / charge / threshold CardModifier leaves
    ("Each champion gains 10 [DIAMOND]", Demolition's "[L-1][R-1]").  The
    affected side(s) come from the resolved target champion, or both when the
    text says "each champion"."""
    import re as _re
    text = (pm.get("text") or "").lower()
    amount = int(pm.get("amount") or 0)
    # ChargePointsModifier (and other typed modifiers) stores its operand as
    # an EffectInputVariable (usually the ability constant ``A``), so the
    # extracted parent param quite correctly has amount=0.  Resolve that
    # operand from the ability metadata instead of treating zero as a no-op.
    if amount == 0:
        raw = json.dumps(ability_record(
            db, (bstate or {}).get("resolving_ability", "")))
        from .statics import _leaf_numeric_value
        amount = int(_leaf_numeric_value(
            db, session.session_id, bstate, pm, raw,
            (bstate or {}).get("resolving_owner_id", 0),
            int((bstate or {}).get("resolving_source_uid") or 0),
            pm.get("property") or "") or 0)
    owner = None
    if target_uid is not None:
        owner = _controller_id_for_target(
            db, session, handler, bstate, target_uid)
    if owner is None:
        owner = (bstate or {}).get("resolving_owner_id", 0) or \
                (handler.user_profile["id"] if handler.user_profile else 0)
    # A resolved target is authoritative. This handles both
    # EachOpposingChampion (the player's champion when the Fortune is
    # opponent-owned) and EachChampion (one target event per champion).
    # The old text-based both-sides branch applied EachChampion twice and sent
    # opposing-champion effects to both sides.
    sides = ["player" if owner else "ai"]
    if target_uid is None and "each champion" in text:
        sides = ["player", "ai"]
    logs = []
    prop = pm.get("property")
    from rules_port.resources import project_resource_change
    color_flag = 0
    if prop == "threshold":
        shard = str(pm.get("shard") or "").rsplit(".", 1)[-1]
        if shard and shard.lower() not in ("unknown", "none"):
            color_flag = game_engine.SHARD_TO_FLAG.get(shard.lower(), 0)
        elif "random threshold" in text:
            color_flags = list({int(flag) for flag in
                                game_engine.SHARD_TO_FLAG.values() if flag})
            if color_flags:
                color_flag = random.choice(color_flags)
        else:
            # Compatibility with pre-metadata BOM rows.
            m = _re.search(r'\[([A-Za-z]+)\]', text)
            if m:
                color_flag = game_engine.SHARD_TO_FLAG.get(m.group(1).lower(), 0)
    for side in sides:
        if prop == "currentresource":
            change = project_resource_change(
                game, session, bstate, pl_t, ai_t, side, prop, amount)
            logs.append(f"{side} resources {change.old_value}->{change.new_value}")
        elif prop == "chargepoints":
            change = project_resource_change(
                game, session, bstate, pl_t, ai_t, side, prop, amount)
            logs.append(f"{side} charges {change.old_value}->{change.new_value}")
        elif prop == "totalresource":
            change = project_resource_change(
                game, session, bstate, pl_t, ai_t, side, prop, amount)
            logs.append(f"{side} total {change.old_value}->{change.new_value}")
        elif prop == "threshold" and color_flag:
            # PvP state is JSON round-tripped between priority windows, so
            # threshold keys may be strings even though the live view uses
            # integer shard flags.  Normalize the addressed key before
            # incrementing; otherwise a selected Shard of Cunning choice
            # silently creates a second ``8``/``"8"`` entry and the client
            # never sees the additional threshold.
            change = project_resource_change(
                game, session, bstate, pl_t, ai_t,
                side, prop, amount, color=color_flag)
            # ``PlayerUpdated``/main-phase option packets are built from the
            # transient Game projection, while RulesPort persists the same
            # value in battle_state. Keep both views synchronized so an AI
            # choice (for example Shard of Cunning's Blood/Sapphire token)
            # is visible immediately instead of only after the next reload.
            logs.append(f"{side} threshold {color_flag} "
                        f"{change.old_value}->{change.new_value}")
    return "; ".join(logs)


def _card_modifier_legacy(game, session, db, handler, pl_t, ai_t, bstate,
                          effect_guid, param):
    """CardModifier handles heal, damage, stat changes for champion powers.

    Parses the AbilityEffectTemplate name to determine the modifier type
    (e.g. 'Gain5Health' → heal 5 HP, 'M1Atk' → -1 ATK, 'YouLose4Health' → damage 4).
    """
    import re as _re
    effect_ctx = EffectContext.from_legacy(
        game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param)
    from .champions import heal_self, damage_self
    from .effects.counters import (
        remove_card_counters, counter_name_from_text, is_champion_target,
    )
    from ._shared import (
        apply_attribute_grant,
        owner_uid, card_collection_for_location, state_after_zone_exit,
    )

    # Data-driven path: the leaf's parent-level param JSON carries
    # {property, amount, duration} resolved from the top-level ability record.
    pm = _parse_leaf_param(param)
    typed_modifier = modifier_metadata(effect_guid)
    if typed_modifier:
        # The generated adapter payload carries duration and target wiring;
        # the child effect template is authoritative for the operation and
        # modifier-specific fields.
        pm = dict(pm or {})
        if typed_modifier.get("property"):
            pm.setdefault("property", typed_modifier["property"])
        if typed_modifier.get("input_value") and not pm.get("amount"):
            pm["amount"] = typed_modifier["input_value"]
        if "value" in typed_modifier:
            pm["amount"] = typed_modifier["value"]
        if typed_modifier.get("input_variable"):
            pm["input_variable"] = typed_modifier["input_variable"]
        if typed_modifier.get("attributeflags"):
            pm["attribute_flags"] = typed_modifier["attributeflags"]
        if typed_modifier.get("attribute"):
            pm["attribute"] = typed_modifier["attribute"]
        if typed_modifier.get("operation"):
            pm["operation"] = typed_modifier["operation"]
        if typed_modifier.get("counter_template_guid"):
            pm["counter_template_guid"] = typed_modifier[
                "counter_template_guid"]
        for key in ("removeallcounters", "removehalfroundedup",
                    "replaceexistingvalue", "iscombatdamage",
                    "combatdamageonly", "noncombatdamageonly",
                    "onlypreventfromdamagedealer",
                    "damagedealeradditionaltarget", "oneshot",
                    "lastsindefinitely", "cardfilter", "subtype",
                    "copysourcecard", "setthresholds", "shard"):
            if key in typed_modifier:
                pm[key] = typed_modifier[key]
    if pm and pm.get("property") in ("attack", "defense", "healhero",
                                      "attribute", "counter", "damage",
                                      "currentresource", "totalresource",
                                      "threshold", "chargepoints", "cardcost",
                                      "intattr", "loselife", "setherohealth",
                                      "spellpoints", "cardthreshold",
                                      "damagemultiplier", "damageshield",
                                      "damageimmunity", "blockimmunity",
                                      "blockimmunityexception", "blockrestriction",
                                      "targetingimmunity", "attackimmunity",
                                      "subtype"):
        target_uid = ((bstate or {}).get("player_mod_target")
                      or (bstate or {}).get("player_spell_target"))
        # Resolve numeric values from the ability's serialized variables.  In
        # particular, CounterVariable and TriggerTargetPropertyVariable are
        # not literal values even when the effect row carries amount=0 or 1.
        raw = json.dumps(ability_record(
            db, (bstate or {}).get("resolving_ability", "")))
        src_uid = (bstate or {}).get("resolving_source_uid")
        src_owner = (bstate or {}).get("resolving_owner_id", 0)

        def _numeric(prop):
            from .statics import _leaf_numeric_value
            return _leaf_numeric_value(
                db, session.session_id, bstate, pm, raw, src_owner,
                int(src_uid) if src_uid is not None else 0, prop)

        try:
            _raw_vars = json.loads(raw or "{}").get("m_Variables") or []
        except (TypeError, ValueError, json.JSONDecodeError):
            _raw_vars = []
        has_dynamic_numeric = any(
            str(v.get("_t", "")).split(".")[-1] in (
                "CounterVariable", "TriggerTargetPropertyVariable",
                "ExpressionAbilityVariable", "CardSumAbilityVariable",
                "CardCountAbilityVariable", "CountListAttrAbilityVariable",
                "AbilityPropertyVariable")
            for v in _raw_vars if isinstance(v, dict))

        if pm.get("property") == "intattr":
            if target_uid is None:
                return "intattr: no target"
            # IntAttrModifier fields are typed metadata.  The normalized
            # parent param often carries amount=0, so use the child
            # m_Value/operation supplied by modifier_metadata instead of
            # inferring the marker from localized game text.
            attr = str(pm.get("attribute") or "")
            operation = str(pm.get("operation") or "Set").lower()
            amount = pm.get("amount")
            if (amount is None or int(amount or 0) == 0) and typed_modifier:
                amount = typed_modifier.get("value", 0)
            if (amount is None or int(amount or 0) == 0) and pm.get("input_variable"):
                from .statics import ability_variable_value
                resolved = ability_variable_value(
                    db, session.session_id, bstate,
                    (bstate or {}).get("resolving_ability", ""),
                    str(pm["input_variable"]), src_owner,
                    int(src_uid) if src_uid is not None else 0)
                if resolved is not None:
                    amount = int(resolved)
            try:
                amount = int(amount or 0)
            except (TypeError, ValueError):
                amount = 0
            if not attr:
                return "intattr: missing attribute"
            from pvp_db import db_card_modifier_state, db_tame_card
            row = db_card_modifier_state(
                session.session_id, int(target_uid), conn=db)
            if not row:
                # PlayerTargetTemplate modifiers are represented by the
                # target player's champion SessionCardId, not by a
                # game_cards row.  Keep these values in battle state so the
                # client-facing PlayerUpdated projection can expose them.
                target_owner = _controller_id_for_target(
                    db, session, handler, bstate, target_uid)
                if target_owner is None:
                    return f"intattr: target {hex(int(target_uid))} missing"
                player_attrs = (bstate.setdefault("player_int_attrs", {})
                                .setdefault(str(int(target_owner)), {}))
                current = int(player_attrs.get(attr, 0) or 0)
                if operation in ("add", "increment"):
                    value = current + amount
                elif operation in ("remove", "subtract"):
                    value = current - amount
                else:
                    value = amount
                if value:
                    player_attrs[attr] = value
                else:
                    player_attrs.pop(attr, None)
                if attr.lower() == "canseeopponentshand":
                    visibility = bstate.setdefault("player_visibility", {})
                    if value:
                        visibility.setdefault(str(int(target_owner)), {})[
                            "CanSeeOpponentsHand"] = value
                    else:
                        visibility.pop(str(int(target_owner)), None)
                    from .effects.visibility import apply_player_visibility_to_game
                    apply_player_visibility_to_game(game, bstate)
                owner_player_uid = owner_uid(
                    target_owner, pl_t, ai_t, bstate)
                champion = (game.player_champion_card_id
                            if owner_player_uid == game.player_uid
                            else game.ai_champion_card_id)
                if champion and getattr(champion, "uid", None) is not None:
                    game.push_player_updated(
                        owner_player_uid, champ_id=champion)
                return (f"intattr {attr}={value} "
                        f"player={int(target_owner)}")
            try:
                saved = json.loads(row[4] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                saved = {}
            if not isinstance(saved, dict):
                saved = {}
            markers = saved.setdefault("int_attrs", {})
            if not isinstance(markers, dict):
                markers = {}
                saved["int_attrs"] = markers
            current = int(markers.get(attr, 0) or 0)
            # Tunneling's printed value is the initial counter threshold.
            # Instance IntAttrModifier values are persisted as the current
            # threshold, so the first Add must start at the TAC value rather
            # than at zero.  Other IntAttrs retain their ordinary marker
            # semantics.
            if (attr.lower() == "tunneling" and
                    attr not in markers and
                    operation in ("add", "increment")):
                from abilities.framework.effects.counters import tunneling_value
                current = tunneling_value(db, row[0], {})
            if operation in ("add", "increment"):
                value = current + amount
            elif operation in ("remove", "subtract"):
                value = current - amount
            else:
                value = amount
            if value:
                markers[attr] = value
            else:
                markers.pop(attr, None)
            from pvp_db import db_set_card_mutation_field
            db_set_card_mutation_field(
                session.session_id, int(target_uid), "permanent_buffs",
                json.dumps(saved), conn=db)
            db.commit()
            # Tamed and Untamed markers are mutually exclusive.  Keep the
            # original static Untamed ability attached for metadata/aura
            # purposes, but suppress its effective marker once Tamed is set.
            tamed_success = attr.lower() == "tamed" and value > 0
            if tamed_success:
                markers.pop("Untamed", None)
                # A successful Taming Sphere capture exiles the captured
                # troop.  This is deliberately keyed from the typed
                # IntAttrModifier (Tamed=1), rather than a card name or game
                # text, so both the 2-cost chance branch and the 5-cost
                # guaranteed branch behave identically.  Failed random
                # branches never apply this modifier and therefore leave the
                # target in its original zone.
                db_tame_card(
                    session.session_id, int(target_uid), json.dumps(saved),
                    state_after_zone_exit(row[3]), conn=db)
                db.commit()
            scid = game_engine.SessionCardId(game_engine.UID(int(target_uid)))
            _tpl, ct, _name, cost, atk, defense, gem = handler._card_full_data(
                game, scid, row[0])
            cdef = game.card_defs.get(scid)
            owner = owner_uid(row[1], pl_t, ai_t, bstate)
            collection = (game_engine.ECardCollections.Void
                          if tamed_success
                          else card_collection_for_location(row[2]))
            state = (state_after_zone_exit(row[3]) if tamed_success
                     else int(row[3] or 0))
            if tamed_success:
                game.push_card_moved(
                    scid, owner, game_engine.ECardCollections.Void,
                    game_engine.ECardLocations.Top, 0)
            game.push_card_updated(
                scid, owner, collection, ct, template_id=row[0],
                cost=cost, attack=atk, defense=defense,
                state=state,
                int_attrs=(dict(cdef.int_attrs) if cdef else {}),
                gems=gem, nulling=(row[2] == "deck"))
            if tamed_success:
                # Zone exit triggers are part of the normal voiding contract;
                # in particular this keeps capture consistent with other
                # effects that remove a troop from play.
                from .triggers import resolve_triggers
                resolve_triggers(
                    db, handler, game, session, pl_t, ai_t, bstate,
                    "CardExitedZoneEvent", int(target_uid),
                    source_owner_uid=row[1])
            if current <= 0 < value:
                from .triggers import resolve_triggers
                resolve_triggers(
                    db, handler, game, session, pl_t, ai_t, bstate,
                    "CardGainedIntAttrEvent", int(target_uid),
                    source_owner_uid=row[1], event_int_attribute=attr)
            return f"intattr {attr}={value} target={hex(int(target_uid))}"

        if pm.get("property") == "cardcost":
            # Permanent cost reduction while in hand ("Fury of the Mountain
            # God: this gets cost -1 when a troop you control deals damage").
            # Persist on the card so playability and the played cost agree.
            src_uid = (bstate or {}).get("resolving_source_uid")
            cost_target = target_uid or src_uid
            if not cost_target:
                return "cardcost: no target"
            delta = int(pm.get("amount") or 0)
            if delta == 0 and typed_modifier:
                # CardCostModifier's current Records form carries the
                # literal in its typed EffectInputVariable (M1/P2/etc.), not
                # in the deprecated m_Amount field.
                delta = int(_numeric("cardcost") or 0)
            if delta == 0:
                # Dynamic cost reduction (e.g. Pterobot "cost -1 for each Dwarf
                # and/or Robot you control"): the leaf amount is 0, the real
                # value comes from the ability's m_Variables.  Store the
                # parsed formula on the instance and evaluate it on demand.
                from .cost_mod import formula_from_raw
                from pvp_db import db_ability_raw_json
                formula = formula_from_raw(db_ability_raw_json(
                    (bstate or {}).get("resolving_ability", ""), conn=db) or "")
                if formula:
                    from pvp_db import db_card_cost_state, db_set_card_cost_formulas
                    existing = db_card_cost_state(
                        session.session_id, int(cost_target), conn=db)
                    try:
                        entries = json.loads(existing[4] or "[]") if existing else []
                    except Exception:
                        entries = []
                    # CardCreatedEvent can be replayed during setup/reconnect.
                    # Register one metadata formula per source ability; a
                    # duplicate entry would apply the same dynamic reduction
                    # twice when the card is later displayed in the warzone.
                    if formula not in entries:
                        entries.append(formula)
                    db_set_card_cost_formulas(
                        session.session_id, int(cost_target), json.dumps(entries),
                        conn=db)
                    db.commit()
                    return (f"CardModifier cardcost dynamic "
                            f"zones={formula.get('zones')} "
                            f"x{formula.get('multiplier')} "
                            f"target={hex(int(cost_target))}")
            from pvp_db import db_add_card_cost_modifier, db_card_zone_details
            db_add_card_cost_modifier(
                session.session_id, int(cost_target), delta, conn=db)
            db.commit()
            c_scid = game_engine.SessionCardId(game_engine.UID(int(cost_target)))
            c_trow = db_card_zone_details(
                session.session_id, int(cost_target), conn=db)
            c_tpl = c_trow[0] if c_trow else None
            _tpl3, ct3, _n3, cost3, atk3, def3, _g3 = handler._card_full_data(
                game, c_scid, c_tpl, c_trow[1] if c_trow else None)
            card_owner = (c_trow[2] if c_trow
                          else (bstate or {}).get("resolving_owner_id", 0))
            card_location = c_trow[3] if c_trow else "hand"
            collection = {
                "deck": game_engine.ECardCollections.Deck,
                "hand": game_engine.ECardCollections.Hand,
                "discard": game_engine.ECardCollections.Discard,
                "void": game_engine.ECardCollections.Void,
                "warzone": game_engine.ECardCollections.Warzone,
                "CastSpells": game_engine.ECardCollections.CastSpells,
                "underground": game_engine.ECardCollections.Underground,
                "choosing": game_engine.ECardCollections.Choosing,
            }.get(card_location, game_engine.ECardCollections.Hand)
            game.push_card_updated(
                c_scid, owner_uid(card_owner, pl_t, ai_t, bstate), collection, ct3,
                template_id=_tpl3, cost=cost3, attack=atk3, defense=def3,
                nulling=(card_location == "deck"))
            return f"cost {delta:+} on {hex(int(cost_target))} -> {cost3}"
        if pm.get("property") in ("currentresource", "totalresource",
                                  "threshold", "chargepoints"):
            return _apply_resource_property(game, session, db, handler, pl_t,
                                            ai_t, bstate, pm, target_uid)
        if pm.get("property") == "healhero":
            from .triggers import _apply_health_gain
            amount = _numeric("healhero")
            text = pm.get("text") or ""
            # Escalation: "Gain ESC:4 health." — the amount scales with every
            # escalation spell cast this game (data-driven from the text).
            m_esc = _re.search(r'esc:(\d+)', text, _re.IGNORECASE)
            if m_esc:
                base = int(m_esc.group(1))
                uses = int((bstate or {}).get("player_escalation_uses", 0))
                amount = base * (uses + 1)
                (bstate or {})["player_escalation_uses"] = uses + 1
            if amount <= 0 and not has_dynamic_numeric:
                m_gain = _re.search(r'gain\s+(\d+)\s+health', text.lower())
                amount = int(m_gain.group(1)) if m_gain else 1
            # The source card's real owner is authoritative: for a played
            # spell the card is a game_cards row owned by the caster, so a
            # stale resolving_owner_id (e.g. 0 left by an earlier AI-card
            # trigger) must NOT redirect the heal to the opponent.
            source_owner = None
            src_uid = (bstate or {}).get("resolving_source_uid")
            if src_uid is not None:
                from pvp_db import db_card_owner_id
                source_owner = db_card_owner_id(
                    session.session_id, int(src_uid), conn=db)
            if source_owner is None:
                source_owner = (bstate or {}).get("resolving_owner_id",
                                                  handler.user_profile["id"]
                                                  if handler.user_profile else 0)
            # Targeted health belongs to the resolved champion. A Fortune is
            # resolved from the opponent's side, so its
            # EachOpposingChampion target is the player champion rather than
            # the AI source. Untargeted "gain health" effects still belong to
            # the ability controller.
            if target_uid is not None:
                target_owner = _controller_id_for_target(
                    db, session, handler, bstate, target_uid)
                if target_owner is not None:
                    source_owner = target_owner
            return _apply_health_gain(game, bstate, pl_t, ai_t, amount,
                                      source_owner, db=db, handler=handler,
                                      session=session)
        if pm.get("property") == "attribute":
            temp_attr = pm.get("duration") in (
                "EndOfTurn", "BeginningOfOwnersTurn",
                "AfterCardsReadyOnPlayersTurn")
            attribute_text = pm.get("text") or ""
            if pm.get("attribute_flags"):
                attribute_text = str(pm["attribute_flags"]).replace("|", " ")
            # MultiplePlayers/Warzone attribute effects (Nazhk's
            # CantReadyAutomatically) apply to every opposing troop, even
            # when an older target-template snapshot reports max_target_count=1.
            attribute_targets = [target_uid]
            if ("cantreadyautomatically" in attribute_text.lower()
                    and target_uid is not None):
                source_owner = (bstate or {}).get("resolving_owner_id", 0)
                from pvp_db import db_warzone_troop_uids_except_owner
                attribute_targets = [r[0] for r in db_warzone_troop_uids_except_owner(
                    session.session_id, source_owner, conn=db)]
            attribute_owner = (bstate or {}).get("resolving_owner_id", 0)
            # "AfterCardsReadyOnPlayersTurn" expires at the affected troop's
            # controller's next Prep, not at the source champion's Prep (the
            # Nazhk Webguard power targets opposing troops).
            if pm.get("duration") == "AfterCardsReadyOnPlayersTurn" and target_uid:
                from pvp_db import db_card_owner_id
                owner = db_card_owner_id(
                    session.session_id, int(target_uid), conn=db)
                if owner is not None:
                    attribute_owner = owner
            bits = 0
            for attribute_target in attribute_targets:
                bits |= apply_attribute_grant(
                    game, session, db, handler, pl_t, ai_t,
                    attribute_target, attribute_text, temporary=temp_attr,
                    bstate=bstate, duration=pm.get("duration"),
                    source_owner_id=attribute_owner,
                    attribute_flags=pm.get("attribute_flags"))
            return f"attribute grant +{bits:b} target={hex(int(target_uid)) if target_uid else 'none'}"
        if pm.get("property") == "damage":
            return effect_ctx.damage_modifier(pm, typed_modifier)
        if pm.get("property") == "loselife":
            amount = effect_ctx.modifier_value(pm, typed_modifier, "loselife")
            return effect_ctx.lose_life(
                target_uid or effect_ctx.modifier_target(), amount,
                typed_modifier)
        if pm.get("property") == "setherohealth":
            amount = effect_ctx.modifier_value(
                pm, typed_modifier, "setherohealth")
            return effect_ctx.set_hero_health(
                target_uid or effect_ctx.modifier_target(), amount)
        if pm.get("property") == "spellpoints":
            amount = effect_ctx.modifier_value(
                pm, typed_modifier, "spellpoints")
            return effect_ctx.spell_points(
                target_uid or effect_ctx.modifier_target(), amount)
        if pm.get("property") == "cardthreshold":
            return effect_ctx.card_threshold(
                target_uid or effect_ctx.resolved_target(), typed_modifier)
        if pm.get("property") == "subtype":
            return effect_ctx.subtype_modifier(
                target_uid or effect_ctx.resolved_target(), typed_modifier)
        if pm.get("property") == "damageshield":
            amount = effect_ctx.modifier_value(
                pm, typed_modifier, "damageshield")
            return effect_ctx.damage_shield(
                target_uid or effect_ctx.modifier_target(), amount,
                typed_modifier)
        if pm.get("property") in (
                "damagemultiplier", "damageimmunity", "blockimmunity",
                "blockimmunityexception", "blockrestriction",
                "targetingimmunity", "attackimmunity"):
            return effect_ctx.rule_modifier(
                target_uid or effect_ctx.modifier_target(), pm,
                typed_modifier)
        if pm.get("property") == "counter":
            cname = ""
            counter_guid = pm.get("counter_template_guid")
            if counter_guid:
                try:
                    from pvp_db import db_counter_template_name
                    cname = db_counter_template_name(counter_guid, conn=db) or ""
                except Exception:
                    # Minimal unit fixtures predate the extracted counter
                    # catalog.  The typed GUID remains authoritative in live
                    # databases; use the compatibility text only when that
                    # optional lookup table is absent.
                    cname = ""
                cname = cname or counter_template_name(counter_guid)
            cname = cname or counter_name_from_text(pm.get("text")) or "counter"
            amount = int(pm.get("amount") or 0)
            low_text = (pm.get("text") or "").lower()
            operation = str(pm.get("operation") or "").lower()
            is_add_counter = (
                operation == "add" or
                (not operation and "add" in low_text and
                 "counter" in low_text and "remove" not in low_text))
            is_remove_counter = operation in ("remove", "subtract")
            is_set_counter = operation == "set"
            remove_all = bool(pm.get("removeallcounters")) or \
                operation in ("removeall", "clear")
            if amount <= 0 and target_uid and is_add_counter:
                # CountListAttrAbilityVariable values (for example
                # Construction Plans' ExhaustedCards list) are carried in the
                # activation state, while the extracted leaf amount is zero.
                # Resolve that variable instead of falling through to the
                # remove-counters path.
                # The typed CounterModifier names its input variable.  Resolve
                # that exact variable first; choosing the first AbilityConstant
                # would incorrectly turn Squashing Pumpkins' +1 counter into
                # the preceding +6 health constant.
                input_variable = str(pm.get("input_variable") or "")
                if input_variable:
                    from .statics import ability_variable_value
                    resolved = ability_variable_value(
                        db, session.session_id, bstate,
                        (bstate or {}).get("resolving_ability", ""),
                        input_variable, src_owner,
                        int(src_uid) if src_uid is not None else 0)
                    if resolved is not None:
                        amount = int(resolved)
                if amount <= 0:
                    amount = int(_numeric("counter") or 0)
            if target_uid and is_champion_target(handler, bstate, target_uid):
                # Live champions have SessionCardIds but no game_cards row, so
                # their counters live in the persisted battle-state JSON.
                # Keep the same typed GUID identity and event projection as
                # ordinary card counters.
                if is_set_counter:
                    operation_name = "set"
                elif is_remove_counter:
                    operation_name = "remove"
                elif is_add_counter:
                    operation_name = "add"
                elif remove_all:
                    operation_name = "clear"
                else:
                    operation_name = "clear"
                old_n, new_n = effect_ctx.counter(
                    target_uid, cname, counter_guid, amount,
                    operation_name)
                op_text = (" set " if is_set_counter else
                           ("-" if is_remove_counter else "+"))
                return (f"counter {cname}{op_text}{amount} -> {new_n} "
                        f"target={hex(int(target_uid))}")
            if amount > 0 and target_uid and is_remove_counter:
                old_n, _new_n = effect_ctx.counter(
                    target_uid, cname, counter_guid, amount, "remove")
                return f"counter {cname}-{amount} target={hex(int(target_uid))}"
            if is_set_counter and target_uid:
                old_n, _new_n = effect_ctx.counter(
                    target_uid, cname, counter_guid, amount, "set")
                return f"counter {cname} set {amount} target={hex(int(target_uid))}"
            if amount > 0 and target_uid and is_add_counter:
                old_n, n = effect_ctx.counter(
                    target_uid, cname, counter_guid, amount, "add")
                return f"counter {cname}+{amount} -> {n} target={hex(int(target_uid))}"
            if remove_all or (amount <= 0 and target_uid is None) or (
                    "remove all" in low_text and "all your" in low_text):
                # "remove all <counter> counters from all your <cards> in all
                # zones" (e.g. Incantation of Righteousness): clear the named
                # counter from every matching card the controller owns and
                # stage each card for the ability's transform leaf.  The
                # effect's gamedata condition already gated this leaf.
                from .effects.counters import card_counters as _card_counters
                owner_id = (bstate or {}).get("resolving_owner_id", 0)
                from pvp_db import db_owner_card_locations
                rows = db_owner_card_locations(
                    session.session_id, owner_id, conn=db)
                cleared = []
                pending = []
                for cu, loc in rows:
                    old_n = _card_counters(db, session.session_id, cu).get(cname, 0)
                    if old_n > 0:
                        remove_card_counters(db, session.session_id, cu, cname)
                        from .effects.counters import push_card_counters
                        push_card_counters(game, session, db, handler, pl_t,
                                           ai_t, cu, changed_counter=cname,
                                           old_value=old_n)
                        cleared.append(int(cu))
                        pending.append((int(cu), loc))
                if pending:
                    bstate["pending_transform_cards"] = pending
                return (f"removed {cname} counters from {len(cleared)} cards "
                        f"(transform {len(pending)})")
            if target_uid:
                effect_ctx.counter(
                    target_uid, cname, counter_guid, 0, "clear")
                return f"counter {cname} cleared on {hex(int(target_uid))}"
            return f"counter {cname}: no target"
        return effect_ctx.stat_modifier(pm, typed_modifier)

    from pvp_db import (db_effect_type, db_ability_game_text,
                        db_champion_ability_game_text,
                        db_effect_param_for_ability_types,
                        db_ability_raw_json)
    eff_name = db_effect_type(effect_guid, conn=db) or ""
    if eff_name != "CardModifierAbilityEffectTemplate":
        # The row is the parent ability, not the effect — skip
        pass

    # Get the game text for the champion ability
    game_text = ""
    ability_guid = bstate.get("resolving_ability", "")
    if ability_guid:
        game_text = db_champion_ability_game_text(ability_guid, conn=db) or ""
    value = 1
    ability_guid = bstate.get("resolving_ability", "")
    if ability_guid:
        var_param = db_effect_param_for_ability_types(
            ability_guid, ("RandomizeVariableEffectTemplate",
                           "RandomizeVariableAbilityEffectTemplate"), conn=db)
        if var_param:
            val = _parse_constant(var_param)
            if val:
                value = val
        else:
            # No variable — extract value from the effect template name
            m = _re.search(r'(\d+)', game_text)
            if m:
                value = int(m.group(1))

    key = bstate.get("player_health_key", "player_health")

    # Determine modifier type from game text
    lower = game_text.lower()
    if "gain" in lower and "health" in lower:
        return heal_self(game, pl_t, value, bstate, key)
    elif "lose" in lower and "health" in lower or "pay" in lower and "health" in lower:
        return damage_self(game, pl_t, value, bstate, key)
    elif "deal" in lower and "damage" in lower:
        # Deal damage to target — for now treat as self-damage or log
        return f"deal {value} damage"
    elif _re.search(r'[+-]\d+', game_text):
        # Stat modifier — parse target from bstate
        target_uid = (bstate or {}).get("player_mod_target")
        if target_uid:
            atk_d = _parse_stat(game_text, 'ATK')
            def_d = _parse_stat(game_text, 'DEF')
            effect_ctx.stat_mod(int(target_uid), atk_d, def_d)
            return f"mod {hex(int(target_uid))} {atk_d:+}/{def_d:+}"
        return f"stat mod: {game_text}"
    else:
        return f"card modifier: {game_text}"


@effect("CardModifierAbilityEffectTemplate")
def _leaf_card_modifier(effect):
    """Apply the broad typed modifier operation through ``EffectContext``."""
    return effect.card_modifier()


def _parse_constant(param_str):
    """Extract a numeric constant from serialized ability variable data."""
    import struct
    try:
        if not param_str:
            return None
        # Try JSON
        d = json.loads(param_str)
        return d.get("value", d.get("count", None))
    except:
        return None


def _parse_leaf_param(param):
    """Parse an ability_effects.param JSON blob (parent-level child params)."""
    if not param:
        return None
    try:
        d = json.loads(param)
        return d if isinstance(d, dict) else None
    except (ValueError, TypeError):
        return None


def _parse_stat(text, stat):
    """Parse +/-N[STAT] from game text."""
    import re
    m = re.search(r'([+-]\d+)\s*\[' + re.escape(stat) + r'\]', text)
    return int(m.group(1)) if m else 0


def _ability_text(db, bstate):
    """The resolving ability's game text (card or champion ability)."""
    ag = (bstate or {}).get("resolving_ability", "")
    if not ag:
        return ""
    from pvp_db import db_ability_game_text, db_champion_ability_game_text
    text = db_ability_game_text(ag, conn=db)
    if text is not None:
        return text or ""
    text = db_champion_ability_game_text(ag, conn=db)
    if text is not None:
        return text or ""
    return ""


def _linked_template_guids_from_metadata(db, bstate):
    """Return card ResourceIds linked by the serialized ability metadata.

    Some older ability records do not expose transform choices as a dedicated
    field; they only retain the linked card ResourceIds in the serialized
    record.  Extract the IDs and resolve them through card_templates.  This
    deliberately does not interpret names, percentages, or display wording.
    """
    import re as _re
    ag = (bstate or {}).get("resolving_ability", "")
    from pvp_db import db_ability_raw_json
    raw = db_ability_raw_json(ag, conn=db) or ""
    if not raw:
        return []
    links = _re.findall(
        r"data=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", raw)
    out = []
    for guid in links:
        guid = guid.lower()
        from pvp_db import db_template_exists
        if db_template_exists(guid, conn=db):
            if guid not in out:
                out.append(guid)
    return out


def _resolve_leaf_target(bstate):
    """The resolved target for a BOM leaf (spell target, ability target,
    trigger target, or the source card itself)."""
    return ((bstate or {}).get("player_spell_target")
            or (bstate or {}).get("player_mod_target")
            or (bstate or {}).get("resolving_target_uid")
            or (bstate or {}).get("resolving_source_uid"))


def _record_ability_list_target(db, bstate, target_uid):
    """Record a target in any metadata-declared CountListAttr variable."""
    if target_uid is None:
        return
    ability_guid = (bstate or {}).get("resolving_ability")
    if not ability_guid:
        return
    record = ability_record(db, ability_guid)
    for variable in record.get("m_Variables") or []:
        if not isinstance(variable, dict):
            continue
        if str(variable.get("_t", "")).rsplit(".", 1)[-1] != \
                "CountListAttrAbilityVariable":
            continue
        name = str(variable.get("m_Name") or "")
        list_name = str(variable.get("m_ListAttrName") or name)
        if not name or not list_name:
            continue
        lists = (bstate or {}).setdefault("ability_lists", {})
        values = lists.setdefault(list_name, [])
        if int(target_uid) not in {int(value) for value in values}:
            values.append(int(target_uid))
        active_variables = (bstate or {}).get("ability_variables")
        if isinstance(active_variables, dict):
            active_variables[name] = len(values)


def _push_card_state(game, session, db, handler, pl_t, ai_t, uid, new_state,
                     bstate=None):
    """Push a CardUpdated in the card's authoritative current collection."""
    from ._shared import card_collection_for_location
    from pvp_db import db_card_zone_details
    details = db_card_zone_details(session.session_id, int(uid), conn=db)
    trow = (details[0], details[2], details[3]) if details else None
    if not trow:
        return
    scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
    _tpl, ct, _n, cost, atk, def_, _g = handler._card_full_data(game, scid, trow[0])
    # In Practice, user_id=0 is the AI and any non-zero id is the human.  PvP
    # has two real non-zero player ids, so use the PvP-aware owner mapping or
    # a CardUpdated for an opponent's damaged troop is rendered under the
    # caster's controller and appears to change sides.  Damage normally
    # supplies bstate; the UID check covers older leaf callers that do not.
    if ((bstate and bstate.get("pvp")) or
            (int(pl_t.uid64) & 0xff) == 244 and
            (int(ai_t.uid64) & 0xff) == 244):
        owner = game_engine.UID.make(244, int(trow[1]))
    else:
        owner = owner_uid(trow[1], pl_t, ai_t, bstate)
    game.push_card_updated(scid, owner, card_collection_for_location(trow[2]), ct,
                           template_id=trow[0], attack=atk, defense=def_,
                           cost=cost, state=new_state,
                           nulling=(trow[2] == "deck"))


def _state_of(db, session, uid):
    from pvp_db import db_card_state_value
    value = db_card_state_value(session.session_id, int(uid), conn=db)
    return int(value) if value is not None else 0


def _controller_id_for_target(db, session, handler, bstate, target_uid):
    """Return the DB player id that controls a card or champion target."""
    from pvp_db import db_card_owner_id
    card_owner = db_card_owner_id(
        session.session_id, int(target_uid), conn=db)
    if card_owner is not None:
        return int(card_owner)
    if (bstate or {}).get("pvp"):
        for pid, cuid in ((bstate or {}).get("champ_map") or {}).items():
            try:
                if int(cuid) == int(target_uid):
                    return int(pid)
            except (TypeError, ValueError):
                continue
        # Headless PvP fixtures can have the handler's champion SessionCardIds
        # before the live session has populated champ_map.  Resolve ownership
        # from the fixture's player order in that narrow fallback case.
        pids = (bstate or {}).get("pids") or []
        if len(pids) >= 2:
            player_champ = getattr(handler, "_player_champ_scid", None)
            ai_champ = getattr(handler, "_ai_champ_scid", None)
            if player_champ is not None and int(
                    player_champ.uid.uid64) == int(target_uid):
                return int(pids[0])
            if ai_champ is not None and int(ai_champ.uid.uid64) == int(
                    target_uid):
                return int(pids[1])
    for attr, owner in (("_player_champ_scid", handler.user_profile["id"]
                         if handler.user_profile else 0),
                        ("_ai_champ_scid", 0)):
        champ = getattr(handler, attr, None)
        if champ is not None and int(champ.uid.uid64) == int(target_uid):
            return int(owner)
    return None

@effect("SummonTokenTroopAbilityEffectTemplate")
def _leaf_summon(effect):
    """Create authored tokens through the context operation boundary."""
    return effect.summon_token()


@effect("ConscriptAbilityEffectTemplate")
def _leaf_conscript(effect):
    """Create conscripted cards through the context operation boundary."""
    return effect.conscript()


@effect("LoadPlayerDeckAbilityEffectTemplate")
def _leaf_load_player_deck(effect):
    """Load the authored player deck through the context boundary."""
    return effect.load_player_deck()


@effect("ActivateTriggeredAbilityEffectTemplate")
def _leaf_activate_triggered(effect):
    """Activate a typed keyword trigger through the context boundary."""
    return effect.activate_triggered()



def _move_card_to_zone_legacy(game, session, db, handler, pl_t, ai_t, bstate,
                               effect_guid, param):
    """Move a card from one zone to another, data-driven from the effect's
    gamedata param (destination/location) — e.g. Eternal Youth's Escalation
    "PutThisIntoYourDeck" (destination Deck), or hand/void/warzone destinations
    for reveal/crypt chains."""
    import json as _json
    src_uid = (bstate or {}).get("resolving_source_uid")
    try:
        p = _json.loads(param or "{}")
    except Exception:
        p = {}
    dest = (p.get("destination") or "").lower()
    # Newer extracted ability effects store the destination on the typed
    # MoveCardToZoneEffectTemplate and leave the compatibility param empty.
    # Prefer that authoritative field whenever the legacy JSON has no zone.
    if not dest:
        typed_dest = effect_template_value(
            db, bstate, effect_guid, "m_DestinationCollection")
        if typed_dest:
            dest = str(typed_dest).rsplit(".", 1)[-1].lower()
    # Choice-card abilities begin by clearing the previous temporary choices.
    # The client represents this as the typed
    # ``PutAllCardsInTheChoiceZoneIntoThePlayedResourcesZone`` effect.  It is
    # not a normal single-card zone move: leaving the generated cards behind
    # makes an old Choose Wild token a legal candidate for a later Shard of
    # Cunning activation.  Keep this keyed to the authoritative effect
    # metadata, not to a card name.
    typed_name = str((effect_template(effect_guid) or {}).get("m_Name") or
                     p.get("name") or "").lower()
    if (dest in ("playedresources", "playedresource") and
            "choicezone" in typed_name and "allcards" in typed_name):
        from .effects.choices import _clear_choice_zone
        _clear_choice_zone(game, session, db, pl_t, ai_t, handler, bstate)
        return "cleared choice zone"
    forced_target = None
    # Bane's generated move effect deliberately has no fixed destination:
    # "put the top card of your deck into #DESTINATION_ZONE#" means the zone
    # the Bane currently entered (Hand or Discard). Resolve that contract from
    # the source card's authoritative current zone and controller, then move
    # the top card of that controller's deck through the normal zone path.
    if (dest in ("", "none") and
            (p.get("name") or "") ==
            "PutTheTopCardOfYourDeckIntoDestinationZone"):
        if src_uid is None:
            return "bane move: no source"
        from pvp_db import db_card_owner_zone_state, db_deck_top_card
        source_row = db_card_owner_zone_state(
            session.session_id, int(src_uid), conn=db)
        if not source_row or source_row[1] not in ("hand", "discard"):
            return "bane move: source is not in hand or discard"
        top_row = db_deck_top_card(
            session.session_id, int(source_row[0]), conn=db)
        if not top_row:
            return "bane move: deck empty"
        dest = source_row[1]
        forced_target = int(top_row[0])
    # A Deck destination normally means the resolving source card (for
    # effects such as "put this into your deck").  A SourceRevealed target,
    # however, supplies each selected card explicitly and must use the normal
    # target-move path below (Oakhenge's remaining revealed cards).
    resolved_target = _resolve_leaf_target(bstate)
    if (dest == "deck" and resolved_target is not None
            and src_uid is not None and int(resolved_target) != int(src_uid)):
        dest = "deck_target"
    if (p.get("name") or "") == "PutEachCardVoidedByItIntoPlay":
        # "put each card voided by it into play" (Solitary Exile's leave
        # trigger): every card this source voided returns to the warzone.
        return _return_voided_cards(game, session, db, handler, pl_t, ai_t,
                                    bstate, src_uid)
    if dest == "deck":
        if src_uid is None:
            return "move card: no source"
        from pvp_db import (db_card_owner_id, db_set_card_owner,
                            db_move_card_to_deck, db_card_position,
                            db_card_zone_details)
        source_owner = db_card_owner_id(
            session.session_id, int(src_uid), conn=db)
        source_row = (source_owner,) if source_owner is not None else None
        if not source_row:
            return f"put {hex(int(src_uid))} into deck: card not found"
        deck_owner = int(source_row[0])
        # MoveCardToZoneEffectTemplate uses -2 for
        # ControlGivenToTargetIndex when the previous target controls the
        # destination card.  Reginald is the important case: after damaging an
        # opposing champion, its deck destination is that champion's deck,
        # not the source card's original deck.  The first effect in the child
        # ability stores the previous target, so resolve the owner from that
        # stored target without referring to a card name.
        move_name = str(p.get("name") or "")
        if p.get("control_given_to_target_index") == -2 or (
                "PreviousTargetControls" in move_name):
            current_ability = (bstate or {}).get("resolving_ability", "")
            stored = ((bstate or {}).get("stored_targets", {})
                      .get(current_ability) or [])
            previous_target = stored[-1] if stored else None
            if previous_target is not None:
                target_owner = _controller_id_for_target(
                    db, session, handler, bstate, previous_target)
                if target_owner is not None:
                    deck_owner = int(target_owner)
                    db_set_card_owner(
                        session.session_id, int(src_uid), deck_owner, conn=db)
                    db.commit()
        db_move_card_to_deck(
            session.session_id, int(src_uid), state_after_zone_exit(0), conn=db)
        db.commit()
        # Draws leave gaps in the persisted position values.  Choosing a
        # random absolute position therefore biases a returned card toward
        # the top of the deck.  Reinsert against the current ordered deck so
        # every slot is equally likely and only this player's deck is used.
        from pvp_db import db_randomly_insert_deck_cards
        db_randomly_insert_deck_cards(
            session.session_id, deck_owner, [int(src_uid)], connection=db)
        pos_value = db_card_position(
            session.session_id, int(src_uid), conn=db)
        pos = int(pos_value) if pos_value is not None else 0
        scid = game_engine.SessionCardId(game_engine.UID(int(src_uid)))
        details = db_card_zone_details(
            session.session_id, int(src_uid), conn=db)
        tpl = details[0] if details else None
        _tpl, ct, _n, cost, atk, def_, _g = handler._card_full_data(game, scid, tpl)
        deck_player = owner_uid(deck_owner, pl_t, ai_t, bstate)
        game.push_card_moved(scid, deck_player, game_engine.ECardCollections.Deck,
                             game_engine.ECardLocations.Unknown, 0)
        game.push_card_updated(scid, deck_player, game_engine.ECardCollections.Deck, ct,
                               template_id=tpl, cost=cost, attack=atk,
                               defense=def_, state=0, nulling=True)
        return f"put {hex(src_uid)} into deck (pos {pos})"
    # Other destinations: move the resolved target (or the source card).
    target = (forced_target if forced_target is not None
              else _resolve_leaf_target(bstate))
    # Some authored MoveCardToZone effects are source-target operations (for
    # example a self-tunnel trigger) and do not carry an explicit target in
    # the activation payload.  Underground is a real destination, not a
    # synonym for Warzone; bind that source only when no target was supplied.
    if target is None and dest == "underground":
        target = src_uid
    if target is None:
        return "move card: no target/source"
    zone = {"hand": ("hand", game_engine.ECardCollections.Hand),
            "deck_target": ("deck", game_engine.ECardCollections.Deck),
            "warzone": ("warzone", game_engine.ECardCollections.Warzone),
            "underground": ("underground", game_engine.ECardCollections.Underground),
            "discard": ("discard", game_engine.ECardCollections.Discard),
            "void": ("void", game_engine.ECardCollections.Void)}.get(dest)
    if not zone:
        return "move card between zones"
    loc, coll = zone
    from pvp_db import (db_card_owner_zone_state, db_move_card_for_effect,
                        db_card_zone_details, db_card_state_value)
    old_row = db_card_owner_zone_state(
        session.session_id, int(target), conn=db)
    old_loc = old_row[1] if old_row else None
    old_state = int(old_row[2] or 0) if old_row else 0
    if loc == "warzone":
        db_move_card_for_effect(
            session.session_id, int(target), loc, 0, old_state,
            clear_dead=True, clear_bits=game_engine.ECardStates.Dead, conn=db)
    elif loc == "underground":
        # Tunnel is a zone change that preserves the card's controller and
        # clears transient surface/combat flags.  Do not rewrite user_id:
        # Practice AI cards remain user_id=0 and are projected with ai_t.
        db_move_card_for_effect(
            session.session_id, int(target), loc, 0, state_after_zone_exit(0),
            conn=db)
    else:
        db_move_card_for_effect(
            session.session_id, int(target), loc,
            100 if loc == "hand" else 0, state_after_zone_exit(0), conn=db)
    db.commit()
    scid = game_engine.SessionCardId(game_engine.UID(int(target)))
    details = db_card_zone_details(
        session.session_id, int(target), conn=db)
    tpl = details[0] if details else None
    _tpl, ct, _n, cost, atk, def_, _g = handler._card_full_data(game, scid, tpl)
    # The selected/revealed card is now a normal hand card.  Re-materialize
    # its full definition from the authoritative template_guid and publish the
    # hand transition as a draw so the client's CardRepresentation cannot
    # retain the source Oakhenge instance's art/definition.
    card_owner = details[2] if details else 0
    owner = owner_uid(card_owner, pl_t, ai_t, bstate)
    game.push_card_moved(scid, owner, coll,
                         game_engine.ECardLocations.Unknown if loc == "deck"
                         else game_engine.ECardLocations.Top,
                         1 if loc == "hand" else 0)
    if loc == "hand" and old_loc == "deck":
        game.push_card_drawn(scid, owner, 1)
    current_state = db_card_state_value(
        session.session_id, int(target), conn=db)
    game.push_card_updated(
        scid, owner, coll, ct, template_id=tpl, cost=cost,
        attack=atk, defense=def_, state=int(current_state or 0),
        nulling=(loc == "deck"))
    if loc == "warzone" and old_loc != "warzone":
        # Moving a card into play through a BOM (including a one-shot
        # Deathcry) is still an enters-play event.  The normal card-cast path
        # dispatches Deploy/Inspire after its zone move; keep generic zone
        # moves on that same shared, metadata-driven path.
        from .triggers import resolve_enters_play_triggers
        resolve_enters_play_triggers(
            db, handler, game, session, pl_t, ai_t, bstate,
            int(target), int(card_owner or 0), 0)
    if loc == "underground" and old_loc != "underground":
        # Underground has public zone movement but hidden card identity for
        # the opposing viewer.  Keep both trigger halves so authored
        # “when this goes underground” abilities resolve through the same
        # metadata path as the dedicated TunnelCard leaf.
        from .triggers import resolve_triggers
        source_owner = int(card_owner or 0)
        resolve_triggers(
            db, handler, game, session, pl_t, ai_t, bstate,
            "CardExitedZoneEvent", int(target),
            source_owner_uid=source_owner,
            event_source_collection=old_loc,
            event_destination_collection=loc)
        resolve_triggers(
            db, handler, game, session, pl_t, ai_t, bstate,
            "CardEnteredZoneEvent", int(target),
            source_owner_uid=source_owner,
            event_source_collection=old_loc,
            event_destination_collection=loc,
            event_previous_state=old_state)
    # Zone entry is an event in its own right.  Draw helpers emit this for
    # normal draws, while generic BOM moves must emit it here so Hand|Discard
    # triggers (for example a Reginald buried into its controller's discard)
    # fire regardless of which effect moved the card.
    if loc in ("hand", "discard"):
        from .triggers import resolve_triggers
        resolve_triggers(
            db, handler, game, session, pl_t, ai_t, bstate,
            "CardEnteredZoneEvent", int(target),
            source_owner_uid=int(card_owner or 0),
            event_source_collection=old_loc,
            event_destination_collection=loc,
            event_previous_state=old_state)
        if loc == "discard":
            resolve_triggers(
                db, handler, game, session, pl_t, ai_t, bstate,
                "CardDiscardedEvent", int(target),
                source_owner_uid=int(card_owner or 0),
                event_source_collection=old_loc,
                event_destination_collection=loc,
                event_previous_state=old_state)
    if loc == "deck" and dest == "deck_target":
        # A revealed-card choice such as Oakhenge returns the unchosen cards
        # to the deck.  Merely changing their location leaves all of them at
        # position zero, which makes the next draw deterministic and differs
        # from the client's shuffle-into-deck behavior.  Reinsert every
        # revealed card still in the deck into random slots while preserving
        # the rest of the deck order.
        revealed = [int(uid) for uid in
                    (bstate or {}).get("revealed_cards", [])]
        if revealed and card_owner is not None:
            from pvp_db import db_randomly_insert_deck_cards
            db_randomly_insert_deck_cards(
                session.session_id, int(card_owner), revealed,
                connection=db)
    return f"moved {hex(int(target))} to {'deck' if dest == 'deck_target' else dest}"


def _return_voided_cards(game, session, db, handler, pl_t, ai_t, bstate,
                         src_uid):
    """'put each card voided by it into play' — move every card the source
    voided (tracked in bstate.voided_by) back to the warzone.  Data-driven
    from the leaf name, mirroring the client's zone-exit resolution."""
    if src_uid is None:
        return "return voided: no source"
    vby = (bstate or {}).get("voided_by") or {}
    uids = list(vby.get(str(int(src_uid)), []))
    returned = 0
    from pvp_db import (db_card_owner_zone_state, db_restore_card_to_warzone,
                        db_card_zone_details)
    for target_uid in uids:
        row = db_card_owner_zone_state(
            session.session_id, int(target_uid), conn=db)
        if not row:
            continue
        owner = pl_t if row[0] != 0 else ai_t
        db_restore_card_to_warzone(
            session.session_id, int(target_uid),
            game_engine.ECardStates.StartedATurnOnYourSide |
            game_engine.ECardStates.Dead,
            game_engine.ECardStates.CameOutThisTurn, conn=db)
        db.commit()
        scid = game_engine.SessionCardId(game_engine.UID(int(target_uid)))
        details = db_card_zone_details(
            session.session_id, int(target_uid), conn=db)
        tpl_guid = details[0] if details else None
        _tpl, ct, _n, _c, atk, def_, _g = handler._card_full_data(
            game, scid, tpl_guid)
        game.push_card_moved(scid, owner, game_engine.ECardCollections.Warzone,
                             game_engine.ECardLocations.Top, 0)
        game.push_card_updated(scid, owner, game_engine.ECardCollections.Warzone,
                               ct, template_id=tpl_guid, attack=atk,
                               defense=def_)
        # The returned card entered play — fire its enters-play triggers.
        from .triggers import resolve_enters_play_triggers
        resolve_enters_play_triggers(db, handler, game, session, pl_t, ai_t,
                                     bstate, int(target_uid), row[0], 0)
        returned += 1
    if uids:
        vby[str(int(src_uid))] = []
    return f"returned {returned} voided cards"


@effect("MoveCardToZoneEffectTemplate")
def _leaf_move_card(effect):
    """Move a card through the context zone/event contract."""
    return effect.move_card_to_zone()


@effect("BuryCardAbilityEffectTemplate")
def _leaf_bury(effect):
    """Bury the typed number of cards from the resolved deck."""
    return effect.bury()

@effect("CounterSpellAbilityEffectTemplate")
def _leaf_counter_spell(effect):
    """Interrupt a chain card through the shared counter-spell boundary."""
    return effect.counter_spell()

@effect("VoidCardAbilityEffectTemplate")
def _leaf_void(effect):
    """Void the resolved target through the shared zone operation."""
    return effect.void_card()

@effect("UntapCardAbilityEffectTemplate")
def _leaf_untap(effect):
    """Ready (untap) the target troop, or every friendly warzone troop for
    "ready each ... you control" effects."""
    text = _ability_text(effect.db, effect.bstate)
    target = effect.resolved_target()
    if target is not None:
        uids = [int(target)]
    elif "each" in (text or "").lower():
        owner = int((effect.bstate or {}).get("resolving_owner_id", 0))
        from pvp_db import db_warzone_troop_uids_for_owner
        uids = [r[0] for r in db_warzone_troop_uids_for_owner(
            effect.session.session_id, owner, conn=effect.db)]
    else:
        return "untap: no target"
    for u in uids:
        effect.update_card_state(
            u, remove=game_engine.ECardStates.Tapped, commit=False)
    effect.db.commit()
    return f"readied {len(uids)}"

@effect("TapCardAbilityEffectTemplate")
def _leaf_tap(effect):
    """Exhaust (tap) the target troop, or every opposing warzone troop for
    "exhaust each opposing troop" effects."""
    text = _ability_text(effect.db, effect.bstate)
    target = effect.resolved_target()
    if target is not None:
        uids = [int(target)]
    elif "each" in (text or "").lower():
        owner = int((effect.bstate or {}).get("resolving_owner_id", 0))
        from pvp_db import db_warzone_troop_uids_except_owner
        uids = [r[0] for r in db_warzone_troop_uids_except_owner(
            effect.session.session_id, owner, conn=effect.db)]
    else:
        return "tap: no target"
    for u in uids:
        effect.update_card_state(
            u, add=game_engine.ECardStates.Tapped,
            trigger="CardTappedEvent", commit=False)
    effect.db.commit()
    return f"tapped {len(uids)}"

@effect("DestroyCardAbilityEffectTemplate")
def _leaf_destroy(effect):
    """Destroy the resolved target through the shared death boundary."""
    return effect.destroy()

def _reveal_cards_legacy(game, session, db, handler, pl_t, ai_t, bstate,
                         effect_guid, param):
    """Reveal cards described by the effect's target-template metadata."""
    count = 1
    target_kind = ""
    target_template_id = ""
    random_target = False
    reveal_collection = game_engine.ECardCollections.Deck
    ability_guid = (bstate or {}).get("resolving_ability", "")
    effect_target_index = 0
    # ``param`` is the compact adapter payload used by synthetic interpreter
    # fixtures; live Records reveal effects derive these values from the typed
    # target template below.
    try:
        adapter = json.loads(param) if isinstance(param, str) and param else {}
        if isinstance(adapter, dict):
            count = max(0, int(adapter.get("count", count) or count))
            target_template_id = str(
                adapter.get("target_template_id") or "").lower()
            target_kind = str(adapter.get("target_kind") or "")
            random_target = bool(adapter.get("random_target", random_target))
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    graph = ability_graph(_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        return "reveal: ability is missing from current Records"
    target_ids = [target.guid for target in graph.targets]
    for effect in graph.effects:
        if effect.guid == str(effect_guid or "").lower():
            effect_target_index = effect.target_index
            break
    if 0 <= effect_target_index < len(target_ids):
        target_template_id = target_ids[effect_target_index]
    target_meta = None
    if target_template_id:
        from .targeting import target_template
        target_meta = target_template(db, target_template_id)
        if target_meta:
            target_kind = target_meta.get("target_kind") or ""
            random_target = bool(target_meta.get("is_random_target"))
    if 0 <= effect_target_index < len(target_ids):
        tid = target_ids[effect_target_index]
        from pvp_db import db_target_template_resolution_info
        frow = db_target_template_resolution_info(str(tid), conn=db)
        filt = json.loads(frow[0]) if frow and frow[0] else {}
        target_kind = (frow[1] or "") if frow else ""
        random_target = bool(frow[2]) if frow else random_target
        top = next((f for f in filt.get("m_TargetFilters", [])
                    if str(f.get("_t", "")).split(".")[-1]
                    == "TopNOfDeck"), None)
        if top:
            count = max(0, int(top.get("m_Amount", 1) or 1))
    owner = int((bstate or {}).get("resolving_owner_id", 0))
    # The reveal effect's target template specifies the source zone.  Do not
    # assume Deck: Shadowgrove Witch's child ability targets a random card in
    # the opposing champion's Hand, while other reveal effects target Deck.
    reveal_zone = "deck"
    tid = (target_ids[effect_target_index]
           if 0 <= effect_target_index < len(target_ids) else None)
    from pvp_db import db_target_template_filter
    filter_json = (db_target_template_filter(str(tid), conn=db)
                   if tid else None)
    filt = json.loads(filter_json) if filter_json else {}
    stack = [filt]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            typ = str(node.get("_t", "")).rsplit(".", 1)[-1]
            if typ == "InZone" and node.get("m_Collection"):
                reveal_zone = str(node["m_Collection"]).lower()
                break
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    # The reveal event must carry the same collection as the metadata-selected
    # source zone.  Leaving the default Deck collection makes a hand reveal
    # render in the client's deck coverflow even when the correct hand card
    # was selected server-side.
    if target_kind != "AbilitySourceCardTargetTemplate":
        from ._shared import card_collection_for_location
        reveal_collection = card_collection_for_location(reveal_zone)
    if target_kind == "AbilitySourceCardTargetTemplate":
        # A source-card reveal (Argus: "reveal Argus from your hand") is not
        # a deck reveal.  The resolution engine has already resolved the
        # source target from the metadata target template and stores it as
        # the current target UID.  Use that card directly so the following
        # SourceRevealed target applies the modifier to the same instance.
        source_target = _resolve_leaf_target(bstate)
        row = None
        if source_target is not None:
            from pvp_db import db_reveal_owned_card
            row = db_reveal_owned_card(
                session.session_id, int(source_target), owner, conn=db)
        rows = [row[:5]] if row else []
        if row:
            from ._shared import card_collection_for_location
            reveal_collection = card_collection_for_location(row[5])
    else:
        # Subterranean Spy's optional surface ability stores an opposing
        # champion, then targets that champion's deck.  The stored target is
        # the authority for which player's deck is revealed; using the
        # resolving owner here would reveal the Spy controller's own deck.
        owner = _reveal_owner_for_target(
            db, handler, session, bstate, owner, target_kind, ability_guid)
        if random_target and target_template_id:
            # A random reveal is not the top card. Resolve the typed target
            # filter, then choose one instance. The selected card remains in
            # the deck until PlayCard consumes it, so the next repetition
            # cannot reveal the same instance again.
            from .targeting import legal_targets
            candidates = legal_targets(
                db, session.session_id, owner, target_template_id,
                (bstate or {}).get("resolving_source_uid"),
                both_players=False, champions=[], battle_state=bstate)
            selected_uid = random.choice(candidates) if candidates else None
            rows = []
            if selected_uid is not None:
                from pvp_db import db_reveal_card_row
                row = db_reveal_card_row(
                    session.session_id, int(selected_uid), owner, reveal_zone,
                    conn=db)
                rows = [row] if row else []
        else:
            # A hand reveal can carry a typed target filter.  The target
            # metadata determines whether the reveal is random; a filter by
            # itself does not make it random.  In particular, Withering Touch
            # reveals every matching hand instance so the later choice can
            # offer two copies of the same card as two distinct cards.
            if reveal_zone == "hand" and target_template_id:
                from .targeting import legal_targets
                candidates = legal_targets(
                    db, session.session_id, owner, target_template_id,
                    (bstate or {}).get("resolving_source_uid"),
                    both_players=False, champions=[], battle_state=bstate)
                if random_target:
                    candidates = ([random.choice(candidates)]
                                  if candidates else [])
                from pvp_db import db_reveal_cards
                rows = db_reveal_cards(
                    session.session_id, owner, "hand", count,
                    candidates, conn=db)
            else:
                from pvp_db import db_reveal_cards
                rows = db_reveal_cards(
                    session.session_id, owner, reveal_zone, count, conn=db)
                if reveal_zone == "hand" and rows:
                    rows = [random.choice(rows)]
    uids = [int(r[0]) for r in rows]
    bstate["revealed_cards"] = uids
    if uids:
        # CardsRevealed only identifies the instances.  The client resolves
        # those ids through its card cache, so publish the current full card
        # definition first; otherwise a revealed card remains a face-down /
        # partial deck representation in the coverflow UI.  RevealCards also
        # carries an explicit recipient policy in gamedata.  A Self/You
        # "look at" must be delivered only to its controller in PvP; a shared
        # packet would disclose the cards to the opponent even though the
        # event's player_id names the controller.
        from ._shared import owner_uid
        effect_param = param or ""
        # m_PlayerRevealTargets is typed effect metadata. Jank Bot's authored
        # value is Everyone, which must remain a shared event in PvP.
        reveal_targets = str(
            (effect_template(effect_guid) or {}).get(
                "m_PlayerRevealTargets") or "Everyone")
        try:
            meta = json.loads(effect_param) if effect_param else {}
            if isinstance(meta, dict):
                reveal_targets = str(
                    meta.get("player_reveal_targets") or "Everyone")
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        # AI reveals are public game information: the human client must see
        # the same CardsRevealed checkpoint as the AI controller.
        if int(owner or 0) == 0:
            reveal_targets = "Everyone"
        private = False
        if (bstate or {}).get("pvp") and reveal_targets.lower() in (
                "self", "you", "controller"):
            private_sender = getattr(handler, "_push_private_revealed_cards",
                                     None)
            if callable(private_sender):
                private = bool(private_sender(
                    session, bstate, owner, rows, pl_t, ai_t))
        if not private:
            for row in rows:
                scid = game_engine.SessionCardId(game_engine.UID(int(row[0])))
                card_owner = owner_uid(row[3], pl_t, ai_t, bstate)
                _tpl, ct, _name, cost, atk, defense, _gem = \
                    handler._card_full_data(game, scid, row[2])
                game.push_card_updated(
                    scid, card_owner, reveal_collection, ct,
                    state=int(row[4] or 0), template_id=row[2], cost=cost,
                    attack=atk, defense=defense, nulling=False)
            ev = game_engine.CardsRevealedSessionEventArgs()
            ev.player_id = owner_uid(owner, pl_t, ai_t, bstate)
            ev.session_card_ids = [game_engine.SessionCardId(game_engine.UID(u))
                                   for u in uids]
            ev.collections = [reveal_collection] * len(uids)
            ev.owning_players = [ev.player_id] * len(uids)
            ev.positions = [int(r[1] or 0) for r in rows]
            game._push(ev)
    return f"revealed {len(uids)}"


@effect("RevealCardsAbilityEffectTemplate")
def _leaf_reveal(effect):
    """Reveal cards through the context prompt/event contract."""
    return effect.reveal_cards()


@effect("StoreTargetsAbilityEffectTemplate")
def _leaf_store_targets(effect):
    """Remember the resolved target so later effects in the same ability can
    reference it (e.g. "Target that troop. It gets +2/+2")."""
    return effect.store_target()


@effect("StoreListAttrAbilityEffectTemplate")
def _leaf_store_list_attr(effect):
    """Persist one typed TAC list entry for later effects in this ability.

    The Python battle state is the server-side equivalent of the client's
    AbilityInstance TAC.  Keeping the list keyed by ability and honoring Set
    is enough for shard selectors and list-based EffectFields without
    leaking transient choices into the card instance.
    """
    template = effect_template(effect.effect_guid) or {}
    list_name = str(template.get("m_ListAttrName") or "")
    attr_name = str(template.get("m_IntAttrName") or "")
    return effect.store_list_attr(
        list_name, attr_name, int(template.get("m_IntAttrValue") or 0),
        set_list=bool(template.get("m_Set")),
        until_end_of_turn=bool(template.get("m_OnlyUntilEndOfTurn")))


@effect("StoreNameAbilityEffectTemplate")
def _leaf_store_name(effect):
    """Remember the resolved target's card name for later effects."""
    return effect.store_name()


@effect("RememberKeywordPowersEffectTemplate")
def _leaf_remember_keyword_powers(effect):
    """Remember matching current ability GUIDs for a later GrantAbility leaf."""
    return effect.remember_keyword_powers()


def _card_atk(db, session, uid, bstate=None):
    """Return the current combat ATK, including instance/static modifiers."""
    from .statics import effective_stats
    atk, _def, _attrs, _flags, _rage = effective_stats(
        db, session.session_id, bstate or {}, int(uid))
    return atk


def _deal_damage(game, session, db, handler, pl_t, ai_t, bstate, uid, amount):
    """Compatibility entry point for the focused damage effect module."""
    return deal_damage(game, session, db, handler, pl_t, ai_t, bstate, uid,
                       amount)




@effect("RevertPermanentModificationsAbilityEffectTemplate")
def _leaf_revert_mods(effect):
    """Revert the target's permanent modifications: attack/defense/cost mods
    and permanent atk/def buffs (counters are kept)."""
    return effect.revert_modifications()


def _battle_cards_legacy(game, session, db, handler, pl_t, ai_t, bstate,
                         effect_guid, param):
    """Battle2Cards: the source (and/or a remembered target) deals its ATK to
    the resolved target.  "battles" -> both deal; "deals damage equal to its
    [ATK]" -> only the source deals."""
    text = _ability_text(db, bstate) or ""
    low = text.lower()
    source = (bstate or {}).get("resolving_source_uid")
    target = _resolve_leaf_target(bstate)
    stored = ((bstate or {}).get("stored_targets", {})
              .get((bstate or {}).get("resolving_ability", "")) or [])
    uids = []
    if source is not None:
        uids.append(int(source))
    if target is not None and int(target) not in uids:
        uids.append(int(target))
    for s in stored:
        if int(s) not in uids:
            uids.append(int(s))
    if len(uids) < 2:
        return "battle: need two cards"
    a, d = uids[0], uids[-1]
    logs = []
    # Battle's extracted form for Tharg's Warrior talent is not the generic
    # "both cards battle" operation.  It says the remembered troop deals its
    # ATK to *you*.  The source is the champion, so the normal source->target
    # direction would calculate a champion's zero ATK and never damage the
    # player.  Resolve the troop->champion hit explicitly and retain the
    # troop as the transient damage dealer so CardDealtDamage triggers fire.
    # A champion is not a combat card, so a Battle2 effect whose source is the
    # champion and whose remembered target is a troop represents the metadata
    # form "previous target deals damage equal to its ATK to you".  Use the
    # resolved card types rather than display/game text (talent abilities do
    # not always have a card_abilities_meta game_text row).
    from pvp_db import db_card_mutation_info
    source_info = db_card_mutation_info(session.session_id, a, conn=db)
    target_info = db_card_mutation_info(session.session_id, d, conn=db)
    source_row = (source_info[2],) if source_info else None
    target_row = (target_info[2],) if target_info else None
    source_is_champion = source_row is None or str(source_row[0]).lower() == "champion"
    target_is_troop = target_row is not None and "troop" in str(target_row[0]).lower()
    if source_is_champion and target_is_troop:
        datk = _card_atk(db, session, d, bstate)
        previous_dealer = (bstate or {}).get("resolving_source_uid")
        bstate["resolving_source_uid"] = d
        try:
            result = _deal_damage(game, session, db, handler, pl_t, ai_t,
                                  bstate, a, datk)
        finally:
            if previous_dealer is None:
                bstate.pop("resolving_source_uid", None)
            else:
                bstate["resolving_source_uid"] = previous_dealer
        logs.append(f"{hex(d)} deals {datk} to you -> {result}")
        return "; ".join(logs)
    atk = _card_atk(db, session, a, bstate)
    result = _deal_damage(game, session, db, handler, pl_t, ai_t, bstate,
                          d, atk)
    logs.append(f"{hex(a)} deals {atk} to {hex(d)} -> {result}")
    from .triggers import resolve_triggers
    resolve_triggers(
        db, handler, game, session, pl_t, ai_t, bstate,
        "CardBattledEvent", a,
        source_owner_uid=_deck_owner_for_target(
            db, handler, session, bstate, a) or 0,
        extra_target=d)
    if "battles" in low:
        datk = _card_atk(db, session, d, bstate)
        result = _deal_damage(game, session, db, handler, pl_t, ai_t, bstate,
                              a, datk)
        logs.append(f"{hex(d)} deals {datk} to {hex(a)} -> {result}")
        resolve_triggers(
            db, handler, game, session, pl_t, ai_t, bstate,
            "CardBattledEvent", d,
            source_owner_uid=_deck_owner_for_target(
                db, handler, session, bstate, d) or 0,
            extra_target=a)
    return "; ".join(logs)


@effect("Battle2CardsAbilityEffectTemplate")
def _leaf_battle(effect):
    """Resolve a metadata battle through the shared damage contract."""
    return effect.battle_cards()


@effect("GiveBonusTurnAbilityEffectTemplate")
def _leaf_bonus_turn(effect):
    """Take an additional turn after this one."""
    return effect.queue_bonus_turn()


@effect("SacrificeCardAbilityEffectTemplate")
def _leaf_sacrifice(effect):
    """Sacrifice the resolved target (or the source card)."""
    return effect.sacrifice()


@effect("TransformSelfAbilityEffectTemplate")
def _leaf_transform_self(effect):
    """Transform the source through the metadata context operation."""
    return effect.transform_self()


def _create_token_copy_legacy(game, session, db, handler, pl_t, ai_t, bstate,
                              effect_guid, param):
    """Create a replica of the resolved target troop (into hand per the text
    "put it into your hand", else the warzone)."""
    import re as _re
    text = _ability_text(db, bstate) or ""
    target = _resolve_leaf_target(bstate)
    if target is None:
        return "copy: no target"
    from pvp_db import (db_card_zone_details, db_copy_template_payload,
                        db_next_game_card_row_id, db_insert_generated_card)
    trow = db_card_zone_details(session.session_id, int(target), conn=db)
    if not trow:
        return "copy: target template missing"
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    count = 1
    m = _re.search(r'(one|two|three|four|five|\d+)', text.lower())
    if m:
        count = int(m.group(1)) if m.group(1).isdigit() else words.get(m.group(1), 1)
    into_hand = "into your hand" in text.lower()
    owner = int((bstate or {}).get("resolving_owner_id", 0))
    tpl = trow[0]
    tpl_row = db_copy_template_payload(tpl, conn=db)
    created = 0
    created_uids = []
    for i in range(count):
        next_id = db_next_game_card_row_id(session.session_id, conn=db)
        card_uid = next_game_card_uid(db, session.session_id)
        created_uids.append(card_uid)
        loc = "hand" if into_hand else "warzone"
        db_insert_generated_card(
            session.session_id, owner, card_uid, tpl, loc, tpl_row[0],
            tpl_row[1], tpl_row[2], next_id, conn=db)
        scid = game_engine.SessionCardId(game_engine.UID(card_uid))
        _tpl2, ct2, _n2, cost2, atk2, def2, _g2 = handler._card_full_data(
            game, scid, tpl)
        coll = game_engine.ECardCollections.Hand if into_hand else game_engine.ECardCollections.Warzone
        game.push_card_moved(scid, pl_t if owner else ai_t, coll,
                             game_engine.ECardLocations.Top, 1)
        game.push_card_updated(scid, pl_t if owner else ai_t, coll, ct2,
                               template_id=tpl, cost=cost2, attack=atk2,
                               defense=def2, nulling=False)
        created += 1
    db.commit()
    # Replica cards fire their own CardCreatedEvent abilities too.
    if created:
        from .triggers import resolve_triggers
        for card_uid in created_uids:
            resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                             "OtherCardCreatedEvent", int(card_uid), owner)
            resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                             "CardCreatedEvent", int(card_uid), owner,
                             zones=())
    return f"copied {created}x {tpl[:8]} {'to hand' if into_hand else 'to warzone'}"


@effect("CreateTokenCopyAbilityEffectTemplate")
def _leaf_create_token_copy(effect):
    """Create a metadata-defined token copy through the context boundary."""
    return effect.create_token_copy()


@effect("RevokeAbilityEffectTemplate")
def _leaf_revoke(effect):
    """Remove a granted ability from the resolved target card."""
    return effect.revoke_ability()


def _create_and_cast_spell_legacy(game, session, db, handler, pl_t, ai_t,
                                  bstate, effect_guid, param):
    """Copy the resolved spell and cast it (e.g. Chimes of the Zodiac
    "When you play an action, copy it.")."""
    import json as _json
    target = ((bstate or {}).get("card_cast_copy_target")
              or _resolve_leaf_target(bstate))
    if target is None:
        return "copy spell: no target"
    from pvp_db import (db_card_zone_details, db_copy_template_payload,
                        db_next_game_card_row_id, db_insert_generated_card)
    trow = db_card_zone_details(session.session_id, int(target), conn=db)
    if not trow:
        return "copy spell: target template missing"
    tpl = trow[0]
    tpl_row = db_copy_template_payload(tpl, conn=db)
    if not tpl_row:
        return "copy spell: template not found"
    owner = int((bstate or {}).get("resolving_owner_id", 0))
    next_id = db_next_game_card_row_id(session.session_id, conn=db)
    card_uid = next_game_card_uid(db, session.session_id)
    db_insert_generated_card(
        session.session_id, owner, card_uid, tpl, "CastSpells", tpl_row[0],
        tpl_row[1], tpl_row[2], next_id, conn=db)
    db.commit()
    try:
        ags = _json.loads(tpl_row[1] or "[]")
    except Exception:
        ags = []
    scid = game_engine.SessionCardId(game_engine.UID(card_uid))
    _tpl2, ct2, _n2, cost2, atk2, def2, _g2 = handler._card_full_data(
        game, scid, tpl)
    game.push_card_moved(scid, pl_t if owner else ai_t,
                         game_engine.ECardCollections.CastSpells,
                         game_engine.ECardLocations.Top, 0)
    game.push_card_updated(scid, pl_t if owner else ai_t,
                           game_engine.ECardCollections.CastSpells, ct2,
                           template_id=tpl, cost=cost2, attack=atk2, defense=def2)
    from abilities import resolve_played_spell as _resolve_spell
    logs = _resolve_spell(game, session, db, handler, pl_t, ai_t, bstate, ags)
    from pvp_db import db_discard_card
    db_discard_card(session.session_id, card_uid, connection=db)
    return f"copied+cast {tpl[:8]}: {logs}"


@effect("CreateAndCastSpellAbilityEffectTemplate")
def _leaf_create_cast_spell(effect):
    """Create and cast through the named orchestration boundary."""
    return effect.create_and_cast_spell()


@effect("DestroyCardByDefenseAbilityEffectTemplate")
def _leaf_destroy_by_defense(effect):
    """Destroy troops through the shared batch-death operation."""
    return effect.destroy_by_defense()


def _transform_card_at_random_legacy(game, session, db, handler, pl_t, ai_t,
                                     bstate, effect_guid, param):
    """Transform the target into a random card matching its typed filter.

    ``m_Filter`` is the authoritative candidate pool.  Inferring only
    Artifact/Troop from display text misses same-shard, same-cost,
    same-rarity, and "another card" transforms.
    """
    from .transform import transform_card
    from .targeting import evaluate_card_filter, shards_from_threshold
    target = _resolve_leaf_target(bstate)
    if target is None:
        return "transform random: no target"
    typed = effect_template(effect_guid) or {}
    filter_json = typed.get("m_Filter")
    if not isinstance(filter_json, dict):
        return "transform random: no typed filter"
    source_uid = (bstate or {}).get("resolving_source_uid")
    from pvp_db import db_transform_target_info, db_transform_candidate_templates
    target_row = db_transform_target_info(
        session.session_id, int(target), conn=db)

    def card_record(row, uid):
        data = {
            "card_uid": int(uid), "template_guid": row[0],
            "name": row[6] or "",
            "card_type": row[1] or "", "location": row[2] or "",
            "user_id": int(row[3] or 0), "state": int(row[4] or 0),
            "cost": int(row[7] or 0), "rarity": row[8] or "",
            "shards": shards_from_threshold(row[9]),
            "subtype": row[10] or "", "attributes": int(row[11] or 0) |
            int(row[12] or 0),
        }
        try:
            saved = json.loads(row[5] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            saved = {}
        data["counters"] = saved.get("counters") or {}
        data["counter_guids"] = saved.get("counter_guids") or {}
        return data

    # HasSourceCastingCostFilter is evaluated by the client with the card
    # currently being transformed as its sourceCard (TransformCardAtRandom's
    # Apply(targetCard, ...)), not with the card whose ability initiated the
    # transform.  Using the activated Cosmic Transmogrifier here made every
    # target look like a cost-5 card.
    source_card = card_record(target_row, target) if target_row else None
    target_types = {
        part.strip() for part in (source_card.get("card_type", "") if
                                  source_card else "").split("|")
        if part.strip()
    }
    # This authored filter is a category union (Artifact | Constant | Troop)
    # plus the source-cost comparison.  The card's category selects the
    # corresponding branch for each target: a troop remains a troop, a
    # constant remains a constant, and an artifact remains an artifact.
    type_union = None
    if str(filter_json.get("_t", "")).rsplit(".", 1)[-1] == "AndCardFilter":
        for child in filter_json.get("m_TargetFilters", []):
            if str(child.get("_t", "")).rsplit(".", 1)[-1] != "OrCardFilter":
                continue
            categories = set()
            for branch in child.get("m_TargetFilters", []):
                branch_type = str(branch.get("_t", "")).rsplit(".", 1)[-1]
                if branch_type == "IsArtifact":
                    categories.add("Artifact")
                elif branch_type == "IsTroop":
                    categories.add("Troop")
                elif (branch_type == "IsType" and
                      str(branch.get("m_CardType") or "") == "Constant"):
                    categories.add("Constant")
            if categories == {"Artifact", "Constant", "Troop"}:
                type_union = categories
                break
    if source_card:
        ability_guid = (bstate or {}).get("resolving_ability", "")
        try:
            from pvp_db import db_ability_raw_json
            raw = json.loads(db_ability_raw_json(ability_guid, conn=db) or "{}")
            source_card["ability_variables"] = {
                str(v.get("m_Name")): int(v.get("m_DefaultValue", 0) or 0)
                for v in raw.get("m_Variables", [])
                if isinstance(v, dict) and v.get("m_Name")
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            source_card["ability_variables"] = {}
        source_card["cost_delta"] = 0
        # HasSourceCastingCostFilter's AddValue is an EffectField.  Its
        # concrete variable is resolved from the active ability metadata.
        add_value = filter_json.get("m_AddValue")
        if isinstance(add_value, dict):
            variable = add_value.get("m_InputVariableName") or \
                add_value.get("m_VariableName")
            if variable:
                source_card["cost_delta"] = int(
                    source_card["ability_variables"].get(variable, 0))
    rows = db_transform_candidate_templates(conn=db)
    candidates = []
    cant_same = bool(typed.get("m_CantBeSameCard"))
    for row in rows:
        candidate = {
            "card_uid": 0, "template_guid": row[0], "name": row[1] or "",
            "card_type": row[2] or "",
            "cost": int(row[3] or 0), "rarity": row[4] or "",
            "shards": shards_from_threshold(row[5]),
            "subtype": row[6] or "", "attributes": int(row[7] or 0),
            "location": "", "user_id": source_card.get("user_id", 0)
            if source_card else 0,
        }
        if cant_same and source_card and row[0].lower() == \
                source_card["template_guid"].lower():
            continue
        if type_union and target_types and not target_types.intersection(
                {part.strip() for part in (candidate["card_type"] or "").split("|")
                 if part.strip()}):
            continue
        if evaluate_card_filter(candidate, filter_json, source_uid,
                                source_card=source_card):
            candidates.append(row[0])
    if not candidates:
        return "transform random: no candidates"
    new_tpl = random.choice(candidates)
    transform_card(handler, game, session, pl_t, ai_t, int(target), new_tpl,
                   bstate=bstate)
    return f"transformed {hex(int(target))} -> random {new_tpl[:8]}"

@effect("TransformCardAtRandomAbilityEffectTemplate")
def _leaf_transform_random(effect):
    """Transform through the authored random-filter operation."""
    return effect.transform_card_random()


def _transform_card_legacy(game, session, db, handler, pl_t, ai_t, bstate,
                           effect_guid, param):
    """Transform a card into another card."""
    from .transform import transform_card
    import re as _re
    if (bstate or {}).get("_skip_transform"):
        return "transform skipped (gate not met)"
    ability_guid = (bstate or {}).get("resolving_ability", "")
    game_text = ""
    if ability_guid:
        from pvp_db import db_ability_game_text
        game_text = db_ability_game_text(ability_guid, conn=db) or ""
    # The transform effect carries the destination template directly in
    # m_CardTemplateId.  This is what the client applies; use the display link
    # only for old extracted rows that predate that field.
    new_tpl = effect_template_value(
        db, bstate, effect_guid, "m_CardTemplateId", "")
    if new_tpl and str(new_tpl).lower() != "0" * 36:
        new_tpl = str(new_tpl).lower()
    else:
        new_tpl = ""
    # The ability text can link the SOURCE card first ("...remove all counters
    # from all your <a data=<source>>Incantations</a>... Transform them into
    # <a data=<target>>Sentinels</a>") — the transform target is the LAST card
    # link, matching the old trigger-path behaviour.
    if not new_tpl:
        links = _re.findall(r'data=([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})',
                            game_text or "")
        new_tpl = links[-1].lower() if links else None
    if not new_tpl:
        return "transform: no template link in ability text"
    # Incantation-style chains stage every copy (with its zone) when counters
    # were removed — transform them all in place (deck/hand/discard included).
    pending = (bstate or {}).get("pending_transform_cards") or []
    if pending:
        count = 0
        loc = None
        for entry in pending:
            tuid = entry[0] if isinstance(entry, (tuple, list)) else entry
            loc = entry[1] if isinstance(entry, (tuple, list)) and len(entry) > 1 else None
            transform_card(handler, game, session, pl_t, ai_t,
                           int(tuid), new_tpl, keep_zone=True, bstate=bstate)
            count += 1
        bstate.pop("pending_transform_cards", None)
        return f"transform {count} -> {new_tpl[:8]} (keep zone {loc})"
    target_uid = ((bstate or {}).get("player_transform_target")
                  or (bstate or {}).get("player_mod_target")
                  or (bstate or {}).get("player_shift_source")
                  or (bstate or {}).get("resolving_source_uid"))
    if target_uid:
        transform_card(handler, game, session, pl_t, ai_t,
                       int(target_uid), new_tpl, bstate=bstate)
        return f"transformed {hex(int(target_uid))} -> {new_tpl[:8]}"
    return "transform: no target"

@effect("TransformCardAbilityEffectTemplate")
def _leaf_transform(effect):
    """Transform through the authored direct-template operation."""
    return effect.transform_card()


def _verdict_legacy(game, session, db, handler, pl_t, ai_t, bstate,
                    effect_guid, param):
    """Apply a verdict effect."""
    return "verdict effect"


@effect("VerdictAbilityEffectTemplate")
def _leaf_verdict(effect):
    """Apply a verdict through the named orchestration boundary."""
    return effect.verdict()


def _grant_ability_legacy(game, session, db, handler, pl_t, ai_t, bstate,
                          effect_guid, param):
    """Grant an ability to a card — append the ability GUID to the target
    card's game_cards.card_abilities list and push a CardUpdated."""
    import json as _json
    import game_engine as _ge
    from .fields import ability_record

    granted_guids = []
    if param and param.lower() != "00000000-0000-0000-0000-000000000000":
        granted_guids = [param.lower()]
    else:
        granted_guids = list((bstate or {}).get("remembered_powers", {}).get(
            (bstate or {}).get("resolving_ability", ""), []))
    if not granted_guids:
        typed_guid = effect_template_value(
            db, bstate, effect_guid, "m_GrantedAbilityTemplateId", "")
        if typed_guid and typed_guid != "0" * 36:
            granted_guids = [typed_guid]
    if not granted_guids:
        return "grant: no ability GUIDs"
    target_uid = (bstate or {}).get("grant_target")
    if not target_uid:
        return "grant: no target"
    grant_template = effect_template(effect_guid) or {}
    # The client permits repeated copies only when the GrantAbility template
    # explicitly marks the granted ability as non-unique.  Older extracted
    # templates omit that field, so retain the historical unique behavior.
    ability_is_unique = bool(grant_template.get("m_AbilityIsUnique", 1))

    # Append to target card's abilities.
    from pvp_db import (db_card_grant_info, db_ability_metadata_exists,
                        db_set_card_abilities)
    row = db_card_grant_info(
        session.session_id, int(target_uid), conn=db)
    if not row:
        # Champions are not represented by game_cards in the live session.
        # Still retain the granted ability on the handler and immediately
        # resolve a newly-granted GameStarted ability (e.g. Taming Sphere).
        for attr, owner in (("_player_champ_scid", handler.user_profile["id"]
                             if handler.user_profile else 0),
                            ("_ai_champ_scid", 0)):
            champ = getattr(handler, attr, None)
            if champ is None or int(champ.uid.uid64) != int(target_uid):
                continue
            dynamic = getattr(handler, "_champion_granted_ability_guids", None)
            if dynamic is None:
                dynamic = handler._champion_granted_ability_guids = {}
            champ_key = int(target_uid)
            current = dynamic.setdefault(champ_key, [])
            added = []
            for granted_guid in granted_guids:
                if not db_ability_metadata_exists(
                        granted_guid, conn=db) and not ability_record(
                            db, granted_guid):
                    _log(f"    GrantAbility: {granted_guid[:8]} not in metadata")
                    continue
                if ability_is_unique and granted_guid in current:
                    continue
                current.append(granted_guid)
                added.append(granted_guid)

            # A newly granted ability that also listens for GameStarted must
            # resolve once during the current dispatch.  Other grants are
            # retained for their own future event (Ridge Raiders is a death
            # trigger and must not deal damage during setup).
            if added and (bstate or {}).get("event_type") == "GameStartedEvent":
                from .fields import ability_record
                from .triggers import _resolve_ability_bom
                for granted_guid in added:
                    child = ability_record(db, granted_guid)
                    child_event = str((child.get("m_TriggerEventType") or {}).get(
                        "m_InternalType") or "")
                    if child_event.endswith("GameStartedEvent"):
                        _resolve_ability_bom(
                            db, handler, game, session, pl_t, ai_t, bstate,
                            granted_guid, int(target_uid), "", target_uid=None,
                            source_owner_uid=owner)
            return f"grant champion ability ({len(added)})"
        return "grant: target card not found"
    try:
        ab_list = _json.loads(row[0] or "[]")
    except Exception:
        ab_list = []
    added = []
    for granted_guid in granted_guids:
        if not db_ability_metadata_exists(granted_guid, conn=db):
            _log(f"    GrantAbility: {granted_guid[:8]} not in DB — extraction may be stale")
            continue
        if ability_is_unique and granted_guid in ab_list:
            continue
        # AbilityIsUnique is false for Spider's Nest.  Each cast creates a
        # separate ability instance, even though both instances reference the
        # same metadata GUID; retaining duplicate GUIDs lets trigger
        # resolution fire once per grant.
        ab_list.append(granted_guid)
        added.append(granted_guid)
    db_set_card_abilities(
        session.session_id, int(target_uid), _json.dumps(ab_list), conn=db)
    db.commit()

    # Push CardUpdated so the client renders the new ability button.
    scid = _ge.SessionCardId(_ge.UID(int(target_uid)))
    from ._shared import card_collection_for_location, owner_uid
    owner = owner_uid(row[2], pl_t, ai_t, bstate)
    tpl_guid, ct, _n, cost, atk, def_, _gem = handler._card_full_data(
        game, scid, row[1], None)
    game.push_card_updated(scid, owner, card_collection_for_location(row[3]), ct,
                           attack=atk, defense=def_, cost=cost,
                           state=int(row[4] or 0), template_id=tpl_guid,
                           nulling=(row[3] == "deck"))
    return f"granted {len(added)} ability(s) to {hex(int(target_uid))}"


@effect("GrantAbilityEffectTemplate")
def _leaf_grant_ability(effect):
    """Grant an ability through the named orchestration boundary."""
    return effect.grant_ability()


@effect("RegisterTriggerAbilityEffectTemplate")
def _leaf_register_trigger(effect):
    """Register a dynamic trigger on a card for this battle instance.

    Registered ability templates are kept separately from the card's printed
    list so a temporary control-change trigger does not become permanent card
    data.  The trigger dispatcher consumes this list when its metadata is
    available in the extracted seed.
    """
    return effect.register_trigger()


def _copy_ability_legacy(game, session, db, handler, pl_t, ai_t, bstate,
                         effect_guid, param):
    """Put a copy of the CardActivatedEvent ability back on the chain."""
    original = (bstate or {}).get("card_activated_item")
    if not original or not original.get("ability_guid"):
        return "copy ability: no activated ability"
    import battle_engine as _be
    instance_id = int((bstate or {}).get("_next_instance_id", 1))
    bstate["_next_instance_id"] = instance_id + 1
    copied = {
        "kind": "ability", "ability_guid": original["ability_guid"],
        "source_uid": original.get("source_uid"),
        "target_uid": original.get("target_uid"),
        "instance_id": instance_id,
    }
    _be.stack_push(bstate, copied)
    source = original.get("source_uid")
    if source is not None:
        game.push_ability_on_chain(
            game_engine.SessionCardId(game_engine.UID(int(source))),
            game_engine.ResourceId.from_str(str(original["ability_guid"])),
            ability_instance_id=instance_id)
    return f"copied ability {str(original['ability_guid'])[:8]}"


@effect("CopyAbilityEffectTemplate")
def _leaf_copy_ability(effect):
    """Copy an ability through the named chain orchestration boundary."""
    return effect.copy_ability()


def _queue_free_played_card(game, session, db, handler, pl_t, ai_t, bstate,
                            card_uid, owner_id, template_guid, card_type):
    """Put a free-played non-resource card onto the authoritative chain.

    ``PlayCardAbilityEffectTemplate`` is allowed to bypass normal cost and
    threshold checks, but it still uses the ordinary CastSpells/stack path.
    Keeping the card there until the stack resolves is important for attack
    triggers and for the client to render each random card as a separate
    chain item.
    """
    import battle_engine as _be
    from pvp_db import db_set_card_played_to_zone

    card_uid = int(card_uid)
    owner = owner_uid(owner_id, pl_t, ai_t, bstate)
    scid = game_engine.SessionCardId(game_engine.UID(card_uid))
    from pvp_db import db_card_location, db_add_temporary_attributes
    previous_location = db_card_location(
        session.session_id, card_uid, conn=db)
    surfaced_from_underground = bool(
        previous_location and
        str(previous_location).lower() == "underground")
    db_set_card_played_to_zone(session.session_id, card_uid, "CastSpells")
    if surfaced_from_underground:
        # A troop that untunnels has Speed for the turn it surfaces.  Persist
        # this as a temporary instance attribute so targeting, attack-option
        # generation, combat validation, and CardUpdated all agree; the
        # normal end-turn expiry clears it.
        db_add_temporary_attributes(
            session.session_id, card_uid, game_engine.ECardAttributes.Speed,
            conn=db)
        db.commit()
        # Reese's typed replacement is granted by the authored Underground ->
        # CastSpells transition. It must not be active while he is buried.
        from .effects.tokens import activate_creation_replacements_for_card
        activate_creation_replacements_for_card(
            db, session.session_id, card_uid)
    _tpl, card_type_bits, _name, cost, attack, defense, gems = \
        handler._card_full_data(game, scid, template_guid)
    # Production handlers return ECardTypes, while lightweight handlers may
    # return the database's string representation. Normalize before applying
    # bit flags or serializing the free-play event.
    if isinstance(card_type_bits, str):
        card_type_bits = game_engine.card_type_from_db(card_type_bits)
    game.push_card_moved(
        scid, owner, game_engine.ECardCollections.CastSpells,
        game_engine.ECardLocations.Top, 0)
    game.push_card_updated(
        scid, owner, game_engine.ECardCollections.CastSpells,
        card_type_bits, template_id=template_guid, cost=cost,
        attack=attack, defense=defense, gems=gems, nulling=False)

    permanent = bool(card_type_bits & (
        game_engine.ECardTypes.Troop | game_engine.ECardTypes.Artifact |
        game_engine.ECardTypes.Constant))
    if permanent:
        if card_type_bits & game_engine.ECardTypes.Artifact:
            game.push_artifact_card_played(scid, owner)
        else:
            game.push_troop_card_played(scid, owner)
        kind = "troop"
    else:
        # SpellCardCast is the public cast/reveal event for an action waiting
        # on the chain. The free flag tells the client no resource payment was
        # made, while the later SpellCardPlayed event is emitted on resolve.
        game.push_spell_card_cast(scid, owner, free=True)
        kind = "spell"

    from pvp_db import db_card_ability_payload
    abilities_payload = db_card_ability_payload(
        session.session_id, card_uid, conn=db)
    try:
        ability_guids = [str(value).lower() for value in json.loads(
            abilities_payload or "[]") if value] if abilities_payload else []
    except (TypeError, ValueError, json.JSONDecodeError):
        ability_guids = []
    instance_id = int((bstate or {}).get("_next_instance_id", 1))
    bstate["_next_instance_id"] = instance_id + 1
    _be.stack_push(bstate, {
        "kind": kind, "source_uid": card_uid,
        "ability_guids": ability_guids, "target_uid": None,
        "instance_id": instance_id, "x_cost": 0,
        "free": True,
    })
    # PLAY_CARD_ABILITY_TEMPLATE_ID is a built-in client chain renderer. It
    # works even when the random card has no ability of its own.
    chain_guid = (ability_guids[0] if ability_guids else
                  game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID)
    game.push_ability_on_chain(
        scid, game_engine.ResourceId.from_str(chain_guid),
        ability_instance_id=instance_id)
    _be.save_state(session, bstate)
    return f"queued {kind} {card_uid} for free (chain={instance_id})"


def _play_card_legacy(game, session, db, handler, pl_t, ai_t, bstate,
                      effect_guid, param):
    """Play a card for free using the effect's gamedata target.

    The target template for Chlorophyllia/its nested ability is a random
    Wild Shard in the controller's deck.  That is different from the common
    source-card case (for example, a card that plays itself after being
    drawn), so resolve the target template first and use the normal resource
    play events/state changes for a selected resource.
    """
    from pvp_db import (db_effect_parent_ability,
                        db_ability_effect_target_index,
                        db_ability_target_template_ids,
                        db_card_zone_details)
    target_ability_guid = (db_effect_parent_ability(effect_guid, conn=db) or
                           (bstate or {}).get("resolving_ability"))
    target_index = 0
    if target_ability_guid:
        current_effect = db_ability_effect_target_index(
            target_ability_guid, effect_guid, conn=db)
        if current_effect is not None:
            target_index = int(current_effect)
        else:
            record = ability_record(db, target_ability_guid)
            for entry in record.get("m_AbilityEffectList") or []:
                if not isinstance(entry, dict):
                    continue
                guid = str((entry.get("m_EffectTemplateId") or {}).get(
                    "m_Guid") or "").lower()
                if guid == str(effect_guid or "").lower():
                    if entry.get("m_TargetTemplateIndex") is not None:
                        target_index = int(entry["m_TargetTemplateIndex"])
                    break
    if target_ability_guid:
        target_ids_payload = db_ability_target_template_ids(
            target_ability_guid, conn=db)
        try:
            target_ids = json.loads(target_ids_payload or "[]") \
                if target_ids_payload else []
        except (TypeError, ValueError, json.JSONDecodeError):
            target_ids = []
        if not target_ids:
            record = ability_record(db, target_ability_guid)
            target_ids = [str(item.get("m_Guid") or "").lower()
                          for item in (record.get(
                              "m_AbilityTargetTemplateIds") or [])
                          if isinstance(item, dict) and item.get("m_Guid")]
        if target_ids:
            # A PlayCard effect can have a target template for two very
            # different purposes.  Resource abilities use an auto-target
            # describing a random Resource in the controller's deck.  A
            # triggered card such as Angel of Dawn uses
            # AbilityTriggerCardTargetTemplate, where the target is the card
            # that caused the event (Angel itself in this case).  Only the
            # former should enter the deck/resource selection path below.
            # Treating every target template as a deck resource made drawn
            # troops stay in hand with "no matching target in deck".
            from .targeting import legal_targets, target_template

            target_id = (target_ids[target_index]
                         if 0 <= target_index < len(target_ids)
                         else target_ids[0])
            target = target_template(db, target_id)
            target_kind = (target or {}).get("target_kind") or ""

            # ``PlayACardInTheChoiceZoneForFree`` is an authored child
            # ability used by resources such as Shard of Cunning.  Its
            # target is a real temporary Choice card in the Choosing zone,
            # not a random deck card and not the parent source card.  The
            # client plays that selected token immediately, then resolves its
            # automatic threshold/ability against the real parent.  Treat the
            # resolved target as a first-class free-play operation here so the
            # nested RulesPort path cannot fall back to replaying the Shard
            # itself (which leaves the threshold ungranted).
            resolved_uid = _resolve_leaf_target(bstate)
            if resolved_uid is not None:
                choice_details = db_card_zone_details(
                    session.session_id, int(resolved_uid), conn=db)
                choice_row = ((choice_details[0], choice_details[2],
                               choice_details[3])
                              if choice_details else None)
                if choice_row and str(choice_row[2]).lower() == "choosing":
                    from .effects.choices import (
                        play_choice_card, resolve_choice_card_abilities)
                    choice_owner = int((bstate or {}).get(
                        "resolving_owner_id", 0) or 0)
                    if play_choice_card(
                            game, session, db, handler, pl_t, ai_t, bstate,
                            int(resolved_uid), choice_owner):
                        choice_logs = resolve_choice_card_abilities(
                            game, session, db, handler, pl_t, ai_t, bstate,
                            int(resolved_uid),
                            (bstate or {}).get("resolving_source_uid"),
                            choice_owner)
                        suffix = ("; " + "; ".join(str(item)
                                  for item in choice_logs if item)
                                  if choice_logs else "")
                        return (f"played choice card {int(resolved_uid)}"
                                f" for free{suffix}")
            is_random_deck_target = False
            if target_kind == "AbilityTargetTemplate" and target:
                try:
                    filter_json = json.loads(target.get("filter_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    filter_json = {}

                def _has_filter(node, wanted):
                    if isinstance(node, dict):
                        if str(node.get("_t", "")).rsplit(".", 1)[-1] == wanted:
                            return node
                        for child in node.values():
                            found = _has_filter(child, wanted)
                            if found is not None:
                                return found
                    elif isinstance(node, list):
                        for child in node:
                            found = _has_filter(child, wanted)
                            if found is not None:
                                return found
                    return None

                zone_filter = _has_filter(filter_json, "InZone")
                # The selected card may be identified by any metadata filter
                # (for example Wild Shard uses IsCardName rather than
                # IsResource).  The target template, not a card name/type
                # check here, determines that this is a random deck target.
                is_random_deck_target = (
                    bool(target.get("is_random_target")) and
                    zone_filter is not None and
                    str(zone_filter.get("m_Collection", "")).lower() == "deck")

            # SourceRevealedTargetTemplate is the second half of Jank Bot's
            # authored two-effect child ability. Reuse the exact card chosen
            # by RevealCards rather than rolling a second random target (or
            # falling back to Jank Bot itself).
            resolved_uid = _resolve_leaf_target(bstate)
            revealed_uids = {int(uid) for uid in
                             ((bstate or {}).get("revealed_cards") or [])}
            selected_uid = None
            if resolved_uid is not None and int(resolved_uid) in revealed_uids:
                from pvp_db import db_card_owner_location_position
                selected_row = db_card_owner_location_position(
                    session.session_id, int(resolved_uid), conn=db)
                if (selected_row and
                        int(selected_row[0]) == int((bstate or {}).get(
                            "resolving_owner_id", 0) or 0) and
                        str(selected_row[1]).lower() == "deck"):
                    selected_uid = int(resolved_uid)

            if selected_uid is None and is_random_deck_target:

                owner_id = int((bstate or {}).get("resolving_owner_id", 0) or 0)
                source_uid = (bstate or {}).get("resolving_source_uid")
                candidates = legal_targets(
                    db, session.session_id, owner_id, target_id, source_uid,
                    both_players=False)
                if candidates:
                    selected_uid = int(random.choice(candidates))
            if selected_uid is not None:
                owner_id = int((bstate or {}).get("resolving_owner_id", 0) or 0)
                try:
                    from pvp_db import (db_resource_selection_card,
                                        db_move_card_to_played_resources)
                    selected = db_resource_selection_card(
                        session.session_id, selected_uid, conn=db)
                    # The target template chooses the candidate; the card's
                    # authoritative type determines whether this resolver
                    # branch should apply resource-pool bookkeeping.
                    if selected and selected[2] == "Resource":
                        (tpl_guid, instance_id, _card_type, current_grant,
                         max_grant, threshold_json, resource_abilities) = selected
                        current_grant = int(current_grant or 0)
                        max_grant = int(max_grant or 0)
                        # Imported/legacy resource rows predate the explicit
                        # grant columns; keep their behavior identical to a
                        # normal basic shard.
                        if not current_grant and not max_grant:
                            current_grant = max_grant = 1
                        db_move_card_to_played_resources(
                            session.session_id, selected_uid, conn=db)
                        db.commit()

                        pvp = bool((bstate or {}).get("pvp"))
                        side = "player" if pvp else ("player" if owner_id else "ai")
                        resource_key = f"{side}_resources"
                        total_key = f"{side}_total_resources"
                        charge_key = f"{side}_charges"
                        threshold_key = f"{side}_threshold"
                        bstate[resource_key] = int(bstate.get(resource_key, 0)) + current_grant
                        bstate[total_key] = int(bstate.get(total_key, 0)) + max_grant
                        threshold_flags = []
                        charge_grant = 0
                        try:
                            resource_ability_guids = json.loads(
                                resource_abilities or "[]")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            resource_ability_guids = []
                        for resource_ability in resource_ability_guids:
                            from pvp_db import db_ability_effect_type_params
                            for effect in db_ability_effect_type_params(
                                    str(resource_ability).lower(), conn=db):
                                if effect[0] != "CardModifierAbilityEffectTemplate":
                                    continue
                                try:
                                    modifier = json.loads(effect[1] or "{}")
                                except (TypeError, ValueError, json.JSONDecodeError):
                                    modifier = {}
                                prop = modifier.get("property")
                                amount = int(modifier.get("amount") or 0)
                                if prop == "chargepoints":
                                    charge_grant += amount
                                elif prop == "threshold":
                                    import re as _re
                                    match = _re.search(
                                        r"\[([A-Za-z]+)\]", modifier.get("text", ""))
                                    if match:
                                        flag = game_engine.SHARD_TO_FLAG.get(
                                            match.group(1).lower(), 0)
                                        if flag:
                                            threshold_flags.append((flag, amount))
                        if not charge_grant:
                            charge_grant = 1
                        bstate[charge_key] = int(bstate.get(charge_key, 0)) + charge_grant
                        threshold = bstate.setdefault(threshold_key, {})
                        scid = game_engine.SessionCardId(game_engine.UID(selected_uid))
                        card_owner = owner_uid(owner_id, pl_t, ai_t, bstate)
                        _tpl, card_type, _name, _cost, _atk, _def, _gem = \
                            handler._card_full_data(
                                game, scid, tpl_guid, instance_id)
                        game.push_card_updated(
                            scid, card_owner, game_engine.ECardCollections.PlayedResources,
                            game_engine.ECardTypes.Resource, template_id=tpl_guid)
                        game.push_resource_card_played(scid, card_owner, free=True)

                        if side == "player":
                            game.player_resources = bstate[resource_key]
                            game.player_total_resources = bstate[total_key]
                            game.player_charges = bstate[charge_key]
                        else:
                            game.ai_resources = bstate[resource_key]
                            game.ai_total_resources = bstate[total_key]
                            game.ai_charges = bstate[charge_key]

                        ev_cur = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
                        ev_cur.player_id = card_owner
                        ev_cur.operation = 1
                        ev_cur.delta = current_grant
                        ev_cur.new_value = bstate[resource_key]
                        game._push(ev_cur)
                        ev_tot = game_engine.PlayerTotalResourcePoolChangedSessionEventArgs()
                        ev_tot.player_id = card_owner
                        ev_tot.operation = 1
                        ev_tot.delta = max_grant
                        ev_tot.new_value = bstate[total_key]
                        game._push(ev_tot)
                        for flag, amount in threshold_flags:
                            current_threshold = threshold.get(flag)
                            if current_threshold is None:
                                current_threshold = threshold.get(str(flag), 0)
                            threshold[flag] = int(current_threshold or 0) + amount
                            ev_th = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                            ev_th.player_id = card_owner
                            ev_th.color = flag
                            ev_th.operation = 1
                            ev_th.delta = amount
                            ev_th.new_value = threshold[flag]
                            game._push(ev_th)
                            from .triggers import resolve_gain_threshold_triggers
                            resolve_gain_threshold_triggers(
                                db, handler, game, session, pl_t, ai_t,
                                bstate, owner_id, color=flag)
                        if side == "player":
                            game.player_threshold = dict(threshold)
                        else:
                            game.ai_threshold = dict(threshold)
                        ev_chg = game_engine.ChampionChargePointsChangedSessionEventArgs()
                        ev_chg.player_id = card_owner
                        ev_chg.operation = 1
                        ev_chg.delta = 1
                        ev_chg.new_value = bstate[charge_key]
                        game._push(ev_chg)
                        from .triggers import resolve_gain_charge_triggers
                        resolve_gain_charge_triggers(
                            db, handler, game, session, pl_t, ai_t, bstate,
                            owner_id)
                        from .resources import (
                            resolve_granted_resource_abilities)
                        resource_logs = resolve_granted_resource_abilities(
                            game, session, db, handler, pl_t, ai_t, bstate,
                            selected_uid, owner_id)
                        if resource_logs:
                            _log("    Resource granted abilities: " +
                                 "; ".join(resource_logs))
                        return (f"played free resource {selected_uid} "
                                f"(+{current_grant} current/+{max_grant} total, "
                                f"thresholds={threshold_flags}, charge={charge_grant})")

                    if selected:
                        return _queue_free_played_card(
                            game, session, db, handler, pl_t, ai_t, bstate,
                            selected_uid, owner_id, selected[0], selected[2])
                except Exception:
                    raise

                return "play for free: no matching target in deck"

    from pvp_db import db_set_card_played_to_zone
    src_uid = (bstate or {}).get("resolving_source_uid")
    if src_uid is None:
        return "play for free: no source"
    from pvp_db import db_card_zone_details
    row = db_card_zone_details(session.session_id, int(src_uid), conn=db)
    if not row:
        return "play for free: source not found"
    tpl_guid, _instance_id, source_owner, ctype = row
    owner_id = int((bstate or {}).get("resolving_owner_id", 0) or 0)
    if source_owner is not None:
        owner_id = int(source_owner or 0)
    # Source-card PlayCard effects use the same free CastSpells/stack path as
    # random deck cards. This preserves the normal response window and avoids
    # resolving a permanent immediately inside its parent's ability.
    return _queue_free_played_card(
        game, session, db, handler, pl_t, ai_t, bstate,
        int(src_uid), owner_id, tpl_guid, ctype)


@effect("PlayCardAbilityEffectTemplate")
def _leaf_play_card(effect):
    """Play a card through the named orchestration boundary."""
    return effect.play_card()


def _fire_event_legacy(game, session, db, handler, pl_t, ai_t, bstate,
                       effect_guid, param):
    """Fire a game event."""
    return "fire event"


@effect("FireEventEffectTemplate")
def _leaf_fire_event(effect):
    """Fire an event through the named orchestration boundary."""
    return effect.fire_event()


@effect("ActivateAbilityEffectTemplate")
def _leaf_invoke(effect):
    """Activate an ability through the named orchestration boundary."""
    return effect.activate_ability()


def _tac_legacy(game, session, db, handler, pl_t, ai_t, bstate, effect_guid,
                param):
    from .tac import tac_function, tac_guid

    if not param:
        return "tac: no serialized data"
    func = tac_function(param)
    guid = tac_guid(param)
    if func == "ShiftAbility" and guid:
        return _shift_power(game, session, db, handler, pl_t, ai_t, bstate, guid)
    if func == "Escalate":
        # "Escalate your cards with the same name as this in all zones"
        # (e.g. Chronic Madness): each copy's EscalationCount grows, so the
        # next ESC-based amount doubles (4 -> 8 -> ...).  The caller's
        # escalation re-render block (player_escalation_uses) pushes the new
        # multiplier onto every copy the caster owns.
        owner = int((bstate or {}).get("resolving_owner_id", 0))
        side = "ai" if owner == 0 else "player"
        key = f"{side}_escalation_uses"
        if (bstate or {}).get("_esc_counted_this_resolution"):
            # An ESC-based leaf earlier in this same resolution already
            # advanced the counter (Ragefire's "Deal ESC:2 damage"): the
            # Escalate operation is the same event, not a second one.
            return "escalate (already counted by ESC leaf)"
        (bstate or {})[key] = int((bstate or {}).get(key, 0)) + 1
        (bstate or {})["_esc_counted_this_resolution"] = True
        return f"escalate {side} (uses={bstate[key]})"
    return f"tac: {func or '?'}"


@effect("TACAbilityEffectTemplate")
def _leaf_tac(effect):
    """Run a TAC operation through the named orchestration boundary."""
    return effect.tac()


def _shift_power(game, session, db, handler, pl_t, ai_t, bstate, ability_guid):
    source_uid = (bstate or {}).get("player_shift_source")
    target_uid = (bstate or {}).get("player_shift_target")
    if not source_uid or not target_uid:
        return f"shift: missing source/target (source={source_uid} target={target_uid})"
    handler._shift_ability_between(
        session, pl_t, ai_t, int(source_uid), int(target_uid), ability_guid,
        game, bstate=bstate)
    return f"shift {ability_guid[:8]} {hex(int(source_uid))} -> {hex(int(target_uid))}"


# ---------------------------------------------------------------------------
#  BOM helpers
# ---------------------------------------------------------------------------

def bom_has_leaf(db, ability_guid, leaf_type):
    """True if the ability's BOM (recursively through ActivateAbility leaves)
    contains a leaf effect of ``leaf_type``."""
    seen = set()

    def walk(g):
        if g in seen:
            return False
        seen.add(g)
        for row in _walk_bom(db, g):
            et = row["effect_type"]
            if et == leaf_type:
                return True
            if et == "ActivateAbilityEffectTemplate" and row["param"]:
                if walk(row["param"]):
                    return True
        return False

    return walk(ability_guid)


def bom_has_discard(db, ability_guid):
    """Convenience: does the ability's BOM chain end in a discard effect?"""
    return bom_has_leaf(db, ability_guid, "DiscardCardAbilityEffectTemplate")


def bom_leaf_prompt_data(db, ability_guid, leaf_type):
    """Return ``(leaf_ability_guid, target_template_guid)`` for a BOM leaf.

    ``ActivateAbilityEffectTemplate`` stores the invoked ability GUID in the
    BOM ``param`` column.  Follow that metadata recursively, then resolve the
    leaf's target template from its own ``target_template_ids`` row.  This is
    used by follow-up client prompts (such as choose-and-discard) so protocol
    code does not need to know the GUID of a shared child ability.
    """
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    from gamedata.records import reference_guid

    store = getattr(bom_leaf_prompt_data, "_record_store", None)
    if store is None:
        store = DEFAULT_RECORD_STORE
        bom_leaf_prompt_data._record_store = store
    seen = set()

    def walk(guid):
        guid = str(guid or "").lower()
        if not guid or guid in seen:
            return None
        seen.add(guid)
        graph = ability_graph(store, guid)
        if graph is None:
            return None
        for effect in graph.effects:
            if effect.concrete_type == leaf_type:
                index = int(effect.target_index)
                target = (graph.targets[index]
                          if 0 <= index < len(graph.targets) else None)
                return guid, target.guid if target is not None else None
            if effect.concrete_type == "ActivateAbilityEffectTemplate":
                child = reference_guid(
                    effect.template.field("m_AbilityToInvoke")
                    if effect.template is not None else None)
                found = walk(child)
                if found:
                    return found
        return None

    return walk(ability_guid)

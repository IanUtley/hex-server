"""Generic card-trigger resolution (Deploy, Inspire, Deathcry, attacks, blocks).

A card's abilities live in ``game_cards.card_abilities`` (ability GUIDs, synced
from the template) and ``card_abilities_meta.trigger_event_type`` names the
game event that fires them.  When that event happens, every applicable card
(and the triggering card) with a matching trigger fires, and each ability's
BOM is resolved through the leaf executors in ``bom.py``.

Supported trigger events:
    AsEntersPlayEvent     — Inspire: another troop with cost >= this troop's
                            cost enters play under the same controller
    CardEnteredZoneEvent  — Deploy (self enters play) and Deathcry (self dies)
    CardAttackedEvent     — "when this attacks"
    CardBlockedEvent      — "when this blocks"
    CardInspiredEvent     — "when this inspires a troop"
"""

import json
import random
import re

import game_engine

from ._shared import (
    _log,
    _stat_delta,
    apply_attribute_grant,
    number_word_to_int,
    owner_uid,
)
from .effects.counters import (
    card_counters, add_card_counter, remove_card_counters,
    push_card_counters, counter_name_from_text,
)
from .stat_mod import apply_card_stat_mod


def _parse_leaf_param(param):
    """Parse an ability_effects.param JSON blob (parent-level child params)."""
    if not param:
        return None
    try:
        d = json.loads(param)
        return d if isinstance(d, dict) else None
    except (ValueError, TypeError):
        return None


def _warzone_ability_holders(db, session_id, controller_uid, zones=("warzone",)):
    """Return {card_uid: [ability_guid, ...]} for a player's cards in the given
    locations (default warzone)."""
    holders = {}
    # Encounter setup cards live in the non-rendered ``mod`` zone.  They still
    # need to participate in the same start-of-game/static trigger scan as
    # cards in the warzone, so include that companion zone whenever callers
    # request warzone holders.  Keeping it here makes the behavior metadata
    # driven and also covers setup abilities on cards created by encounters.
    zone_values = list(dict.fromkeys(
        list(zones) + (["mod"] if "warzone" in zones else [])))
    placeholders = ",".join("?" * len(zone_values))
    rows = db.execute(
        ("SELECT card_uid, card_abilities FROM game_cards "
         "WHERE session_id=? AND user_id=? AND location IN (%s) "
         "AND card_abilities IS NOT NULL AND card_abilities != ''") % placeholders,
        (session_id, controller_uid) + tuple(zone_values)).fetchall()
    for cu, ab_json in rows:
        try:
            ags = [g.lower() for g in json.loads(ab_json or "[]")]
        except (ValueError, TypeError):
            ags = []
        if ags:
            holders[int(cu)] = ags
    return holders


def _card_ability_guids(db, session_id, card_uid):
    """Ability GUIDs currently on a specific card instance."""
    row = db.execute(
        "SELECT card_abilities FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row or not row[0]:
        return []
    try:
        return [g.lower() for g in json.loads(row[0])]
    except (ValueError, TypeError):
        return []


def ability_matches_keyword(db, ability_guid, keyword):
    """Match a current ability against ActivateTriggered's typed keyword.

    The client stores keyword flags in the ability TAC for Deathcry and uses
    the event type for Momentum powers.  The extracted game text is retained
    as a compatibility fallback for older records whose serialized TAC is
    absent, never as the primary source of the effect configuration.
    """
    key = str(keyword or "").lower().rstrip("s")
    row = db.execute(
        "SELECT trigger_event_type, game_text, raw_json "
        "FROM card_abilities_meta WHERE ability_guid=?",
        (str(ability_guid).lower(),)).fetchone()
    if not row:
        return False
    event_type, game_text, raw = row
    if key == "deathcry":
        try:
            from .tac import _tac_attr_hash, decode_tac
            record = json.loads(raw or "{}")
            tac = record.get("m_SerializedTAC") or {}
            data = tac.get("data") if isinstance(tac, dict) else ""
            if data and _tac_attr_hash("Deathcry") in decode_tac(data):
                return True
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        return bool(re.search(r"(?:^|>)\s*deathcry\b",
                             str(game_text or "").lower()))
    if key == "momentum":
        return "CardInspiredEvent" in str(event_type or "")
    return key in str(game_text or "").lower()


def manually_trigger_abilities(db, handler, game, session, pl_t, ai_t,
                               bstate, target_uid, keyword):
    """Resolve all matching triggered powers currently on one card.

    This is the server counterpart of Session.ManuallyTriggerAbilities.  It
    deliberately invokes only the card's current ability list, so temporary
    grants and transformed-card abilities participate naturally.
    """
    ags = _card_ability_guids(db, session.session_id, target_uid)
    if not ags:
        return ""
    row = db.execute(
        "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target_uid))).fetchone()
    owner_id = int(row[0]) if row else 0
    from .resolution import resolve_ability
    results = []
    for ag in ags:
        if not ability_matches_keyword(db, ag, keyword):
            continue
        results.append(resolve_ability(
            handler, game, session, db, pl_t, ai_t, bstate, ag,
            int(target_uid), owner_id, {}))
    return "; ".join(str(result) for result in results if result)
def _champion_ability_holders(db, handler, controller_uid):
    """Return champion sources and their trigger abilities for one side.

    Champions are not rows in ``game_cards`` during a live battle, so their
    passive abilities cannot be discovered by the normal zone scan.  The
    champion GUID/SessionCardId pair is held by the battle handler; the
    ability metadata and BOM remain shared with the normal data-driven path.
    """
    try:
        controller_uid = int(controller_uid or 0)
    except (TypeError, ValueError):
        return {}
    player_id = int(handler.user_profile["id"]) if (
        controller_uid != 0 and getattr(handler, "user_profile", None)) else 0
    if controller_uid != player_id:
        return {}
    scid = (getattr(handler, "_ai_champ_scid", None)
            if controller_uid == 0 else
            getattr(handler, "_player_champ_scid", None))
    guid = (getattr(handler, "_ai_champ_guid", None)
            if controller_uid == 0 else
            getattr(handler, "_player_champ_guid", None))
    if scid is None:
        return {}
    abilities = []
    if guid:
        rows = db.execute(
            "SELECT ca.ability_guid FROM champion_abilities ca "
            "JOIN card_abilities_meta cam ON cam.ability_guid=ca.ability_guid "
            "WHERE ca.champion_guid=? AND cam.trigger_event_type IS NOT NULL "
            "AND cam.trigger_event_type != '' ORDER BY ca.ability_guid",
            (str(guid),)).fetchall()
        abilities.extend(str(row[0]).lower() for row in rows)
    dynamic = getattr(handler, "_champion_granted_ability_guids", {}) or {}
    abilities.extend(str(ag).lower() for ag in dynamic.get(
        int(scid.uid.uid64), []) if str(ag).lower() not in abilities)
    if not abilities:
        return {}
    return {int(scid.uid.uid64): abilities}


def _entering_card_is_troop(db, session_id, card_uid):
    """True if the CardEnteredZone source card is a Troop instance."""
    if card_uid is None:
        return False
    row = db.execute(
        "SELECT card_type FROM game_cards WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    return bool(row and (row[0] or "") == "Troop")


def _ai_battle_target(db, session, source_uid, ability_guid, candidates):
    """Choose an AI battle target from the data-defined legal candidates.

    The client AI's useful preference for a battle deploy is a low-health
    flier first, then a random troop that can be killed by the source.  Keep
    the candidate set metadata-driven and only apply that tactical ordering
    when the ability actually contains a Battle2Cards effect.
    """
    if not candidates:
        return None
    effect = db.execute(
        "SELECT 1 FROM ability_effects WHERE ability_guid=? "
        "AND effect_type='Battle2CardsAbilityEffectTemplate' LIMIT 1",
        (ability_guid,)).fetchone()
    if not effect:
        return candidates[0]
    source = db.execute(
        "SELECT COALESCE(ct.attack,0) + COALESCE(gc.card_attack_mod,0) "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session.session_id, int(source_uid))).fetchone()
    damage = max(0, int(source[0] or 0)) if source else 0
    if damage <= 0:
        return random.choice(candidates)
    placeholders = ",".join("?" * len(candidates))
    rows = db.execute(
        "SELECT gc.card_uid, (COALESCE(ct.attributes,0) | "
        "COALESCE(gc.card_attributes,0)), "
        "COALESCE(ct.defense,0) + COALESCE(gc.card_defense_mod,0) "
        "- COALESCE(gc.card_damage,0) "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        f"WHERE gc.session_id=? AND gc.card_uid IN ({placeholders})",
        (session.session_id, *[int(uid) for uid in candidates])).fetchall()
    by_uid = {int(uid): (int(attrs or 0), int(health or 0))
              for uid, attrs, health in rows}
    fliers = [uid for uid in candidates
              if (by_uid.get(int(uid), (0, 0))[0]
                  & game_engine.ECardAttributes.Flight)
              and 0 < by_uid.get(int(uid), (0, 0))[1] < damage]
    if fliers:
        return random.choice(fliers)
    killable = [uid for uid in candidates
                if 0 < by_uid.get(int(uid), (0, 0))[1] <= damage]
    return random.choice(killable or candidates)


def _apply_health_gain(game, bstate, pl_t, ai_t, amount, source_owner_uid,
                       db=None, handler=None, session=None):
    """Gain *amount* champion health for the ability source's controller.

    ``source_owner_uid`` is the DB user_id of the ability source (0 = AI,
    non-zero = the human player).  Updates the authoritative battle-state
    health (``bstate``) plus the transient ``Game``, and pushes the
    ChampionHealthChanged event to the correct champion.  Any warzone card
    whose trigger is ChampionHealedEvent ("When you gain health, ...", e.g.
    Righteous Paladin, Incantation of Righteousness) then fires.
    """
    health_key = "player_health" if source_owner_uid else "ai_health"
    if (bstate or {}).get("pvp"):
        health_key = (bstate or {}).get("pvp_health_map", {}).get(
            int(source_owner_uid or 0), health_key)
    cur = bstate.get(health_key, getattr(game, health_key, 20))
    # Emberspire Witch: "Champions can't gain health." — while she is in play,
    # no champion gains health (continuous static flag from the statics layer).
    if db is not None and handler is not None and session is not None:
        try:
            from .statics import global_flags
            if "cant_gain_health" in global_flags(
                    db, session.session_id, bstate):
                return "prevented: champions can't gain health"
        except Exception:
            pass
    new_val = cur + amount
    bstate[health_key] = new_val
    setattr(game, health_key, new_val)
    ev = game_engine.ChampionHealthChangedSessionEventArgs()
    ev.player_id = owner_uid(source_owner_uid, pl_t, ai_t, bstate)
    ev.old_damage_value = cur
    ev.new_damage_value = new_val
    game._push(ev)
    # Fire "when you gain health" triggers for the healed player's cards.
    if db is not None and handler is not None and session is not None:
        if not (bstate or {}).get("_resolving_healed_event"):
            bstate["_resolving_healed_event"] = True
            try:
                resolve_triggers(db, handler, game, session, pl_t, ai_t,
                                 bstate, "ChampionHealedEvent", None,
                                 source_owner_uid=source_owner_uid)
            finally:
                bstate.pop("_resolving_healed_event", None)
    return f"health +{amount} -> {new_val}"


def _resolve_ability_bom(db, handler, game, session, pl_t, ai_t, bstate,
                         ability_guid, source_uid, game_text, target_uid=None,
                         source_owner_uid=None):
    """Resolve one ability's BOM against a source card (and optional target)."""
    from .bom import _walk_bom, _LEAFS
    from .transform import transform_card

    bstate = bstate or {}
    bstate["resolving_ability"] = ability_guid
    bstate["resolving_owner_id"] = source_owner_uid or 0
    bstate["resolving_source_uid"] = source_uid
    bstate["resolving_target_uid"] = target_uid
    # Keep the event target separate from the leaf's current target.  A
    # TriggerTargetPropertyVariable (for example a card's true cast cost)
    # refers to the card that caused the trigger, even after a later effect
    # resolves against another target.
    previous_trigger_target = bstate.get("resolving_trigger_target_uid")
    bstate["resolving_trigger_target_uid"] = target_uid
    bstate["_skip_transform"] = False

    def _resolve_target():
        """Leaf target: explicit > stored target for this ability > source."""
        if target_uid is not None:
            return target_uid
        stored = (bstate or {}).get("stored_targets", {}).get(ability_guid)
        if stored:
            return stored[-1]
        return source_uid

    # GrantAbility leaves need to know which card receives the ability.
    # For Deploy/Inspire, it's the entering troop (target_uid);
    # for self-targeting triggers, it's the source.
    bstate["grant_target"] = target_uid if target_uid is not None else source_uid
    logs = []
    # Brief "shake" on the source card: set Activated state, then clear it after
    if source_uid is not None:
        src_row = db.execute(
            "SELECT card_state, template_guid, location FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(source_uid))).fetchone()
        hidden_setup = bool(db.execute(
            "SELECT 1 FROM card_templates WHERE guid=? AND "
            "LOWER(COALESCE(subtype,''))='battleboard' LIMIT 1",
            (src_row[1],)).fetchone()) if src_row else False
        if src_row and src_row[2] == "warzone" and not hidden_setup:
            orig_state = src_row[0] or 0
            scid_src = game_engine.SessionCardId(game_engine.UID(int(source_uid)))
            owner = owner_uid(source_owner_uid, pl_t, ai_t, bstate)
            _tpl, ct, _n, _c, atk, def_, _g = handler._card_full_data(game, scid_src, src_row[1])
            game.push_card_updated(scid_src, owner, game_engine.ECardCollections.Warzone, ct,
                                   state=orig_state | game_engine.ECardStates.Activated,
                                   template_id=src_row[1], attack=atk, defense=def_)
    # Authoritative path: walk the ability's BOM data-driven (effect groups,
    # gamedata conditions, ability variables, target templates, ActivateAbility
    # recursion).  Only fall back to the legacy flat walk when this ability has
    # NO effect rows at all — trusting the engine's (possibly empty) result
    # avoids double-applying leaves.
    rows_exist = db.execute(
        "SELECT 1 FROM ability_effects WHERE ability_guid=? LIMIT 1",
        (ability_guid,)).fetchone()
    if not rows_exist:
        from .fields import ability_record
        rows_exist = bool(ability_record(db, ability_guid).get(
            "m_AbilityEffectList"))
    if rows_exist:
        from .resolution import resolve_ability
        # Triggers carry their explicit target only (target_uid); the generic
        # player_spell_target / player_mod_target fields may hold a STALE value
        # from an enclosing resolution (e.g. a heal trigger firing inside
        # another ability's leaf) — clear them so the engine can't mis-target.
        saved_targets = {}
        for _k in ("player_spell_target", "player_mod_target"):
            if _k in bstate:
                saved_targets[_k] = bstate.pop(_k)
        # The activation target belongs to the explicit target template, not
        # necessarily target index zero.  Crazed Squirrel Titan, for example,
        # has [This, target opposing troop]; putting the chosen troop at index
        # zero makes the StoreTargets leaf remember the wrong card and leaves
        # the Battle2Cards leaf without its intended target.
        target_map = {}
        if target_uid is not None:
            target_row = db.execute(
                "SELECT target_template_ids FROM card_abilities_meta "
                "WHERE ability_guid=?", (ability_guid,)).fetchone()
            try:
                target_ids = json.loads(target_row[0]) if target_row and target_row[0] else []
            except (TypeError, ValueError, json.JSONDecodeError):
                target_ids = []
            explicit_indexes = []
            for target_index, target_id in enumerate(target_ids):
                explicit_row = db.execute(
                    "SELECT explicit FROM target_templates "
                    "WHERE template_id=?", (str(target_id),)).fetchone()
                if explicit_row and int(explicit_row[0] or 0):
                    explicit_indexes.append(target_index)
            if explicit_indexes:
                target_map = {
                    int(target_index): int(target_uid)
                    for target_index in explicit_indexes
                }
            else:
                # Preserve the legacy shape for focused/minimal metadata
                # fixtures whose single target has no explicit flag.
                target_map = {0: int(target_uid)}
        try:
            out = resolve_ability(handler, game, session, db, pl_t, ai_t,
                                  bstate, ability_guid, source_uid,
                                  source_owner_uid or 0, target_map)
        finally:
            for _k, _v in saved_targets.items():
                bstate[_k] = _v
        # A source can emit additional CardUpdated events while its BOM is
        # resolving (for example Wyldeboar's permanent stat bonuses).  If the
        # final leaf moved that source out of play, reassert the destination
        # after those updates so the client does not leave a deck card face-up
        # or retain an old battle state.
        if source_uid is not None and src_row and src_row[2] == "warzone":
            current = db.execute(
                "SELECT template_guid, location, card_state "
                "FROM game_cards WHERE session_id=? AND card_uid=?",
                (session.session_id, int(source_uid))).fetchone()
            if current and current[1] != "warzone":
                _scid = game_engine.SessionCardId(
                    game_engine.UID(int(source_uid)))
                _tpl3, _ct3, _n3, _c3, _a3, _d3, _g3 = handler._card_full_data(
                    game, _scid, current[0])
                _zone3 = {
                    "hand": game_engine.ECardCollections.Hand,
                    "deck": game_engine.ECardCollections.Deck,
                    "discard": game_engine.ECardCollections.Discard,
                    "void": game_engine.ECardCollections.Void,
                    "CastSpells": game_engine.ECardCollections.CastSpells,
                }.get(current[1], game_engine.ECardCollections.Warzone)
                game.push_card_updated(
                    _scid, owner, _zone3, _ct3, template_id=current[0],
                    attack=_a3, defense=_d3, state=int(current[2] or 0),
                    nulling=current[1] == "deck")
        return out or ""
    rows = _walk_bom(db, ability_guid)
    card_mod_applied = False
    for row in rows:
        etype = row["effect_type"]
        eg = row["effect_guid"]
        param = row["param"]
        if etype == "CardModifierAbilityEffectTemplate":
            # Data-driven path: each leaf's parent-level param JSON carries
            # {property, amount, duration} resolved from the top-level ability
            # record (Guard Dog = one +2 ATK leaf + one +2 DEF leaf, both
            # EndOfTurn). Apply per-leaf so the property/amount/duration are
            # exactly what gamedata specified.
            pm = _parse_leaf_param(param)
            if pm and pm.get("property") in ("attack", "defense", "cardcost",
                                             "healhero", "attribute", "counter",
                                             "damage"):
                if pm.get("property") == "healhero":
                    amount = int(pm.get("amount") or 0)
                    if amount > 0:
                        # Data-driven heal (e.g. Adamanthian Scrivener "gain 1
                        # health." -> property healhero, amount 1).  Rows with
                        # amount 0 are dynamic ("gain 1 health for each ...")
                        # and fall through to the text-derived fallback.
                        logs.append(_apply_health_gain(
                            game, bstate, pl_t, ai_t, amount, source_owner_uid,
                            db=db, handler=handler, session=session))
                    continue
                elif pm.get("property") == "damage":
                    target = _resolve_target()
                    if target is None or target == source_uid:
                        # Deploy "This deals N damage to you" — the 'You'
                        # target template means the controller's champion.
                        from .bom import _champion_target_uid
                        target = _champion_target_uid(
                            handler, bstate, db, session) or target
                    if target is not None:
                        from .statics import _leaf_numeric_value
                        raw_row = db.execute(
                            "SELECT raw_json FROM card_abilities_meta "
                            "WHERE ability_guid=?", (ability_guid,)).fetchone()
                        raw = raw_row[0] if raw_row else ""
                        amount = _leaf_numeric_value(
                            db, session.session_id, bstate, pm, raw,
                            source_owner_uid or 0, source_uid, "damage")
                        if amount <= 0:
                            import re as _re
                            m_dmg = _re.search(
                                r'deal\s+(\d+)\s+damage',
                                (pm.get("text") or "").lower())
                            if m_dmg:
                                amount = int(m_dmg.group(1))
                        if amount > 0:
                            if "esc:" in (pm.get("text") or "").lower() or \
                                    "esc " in (pm.get("text") or "").lower():
                                bstate["player_escalation_uses"] = int(
                                    bstate.get("player_escalation_uses", 0)) + 1
                            from .bom import _deal_damage
                            logs.append(_deal_damage(
                                game, session, db, handler, pl_t, ai_t,
                                bstate, target, amount))
                    continue
                elif pm.get("property") == "attribute":
                    target = _resolve_target()
                    if target is not None:
                        temp_attr = pm.get("duration") in (
                            "EndOfTurn", "BeginningOfOwnersTurn",
                            "AfterCardsReadyOnPlayersTurn")
                        bits = apply_attribute_grant(
                            game, session, db, handler, pl_t, ai_t, target,
                            pm.get("text") or game_text, temporary=temp_attr,
                            bstate=bstate, duration=pm.get("duration"),
                            source_owner_id=source_owner_uid)
                        logs.append(f"CardModifier attribute +{bits:b} target={target}")
                    continue
                elif pm.get("property") == "counter":
                    target = _resolve_target()
                    amount = int(pm.get("amount") or 0)
                    if amount > 0 and target is not None:
                        cname = counter_name_from_text(pm.get("text")) or "counter"
                        old_n = card_counters(db, session.session_id, target).get(cname, 0)
                        n = add_card_counter(db, session.session_id, target, cname, amount)
                        push_card_counters(game, session, db, handler, pl_t, ai_t,
                                           target, changed_counter=cname,
                                           old_value=old_n)
                        logs.append(f"CardModifier counter {cname}+{amount} -> {n} "
                                    f"target={target}")
                    else:
                        # "remove all X counters from all your <cards> in all
                        # zones" — gated by the effect's condition (e.g. the
                        # Incantation of Righteousness five-or-more transform).
                        logs.append(_resolve_remove_all_counters(
                            db, handler, game, session, pl_t, ai_t, bstate,
                            pm, game_text, source_uid))
                    continue
                elif pm.get("property") == "cardcost":
                    target = target_uid if target_uid is not None else (bstate or {}).get("player_spell_target")
                    if target is None:
                        target = source_uid
                    if target is not None:
                        delta = int(pm.get("amount") or 0)
                        if delta == 0:
                            # Dynamic cost reduction (e.g. Pterobot "cost -1 for
                            # each Dwarf and/or Robot you control"): the leaf
                            # amount is 0, the real value comes from the
                            # ability's m_Variables.  Store the parsed formula
                            # on the instance and evaluate it on demand.
                            from .cost_mod import formula_from_raw
                            raw_row = db.execute(
                                "SELECT raw_json FROM card_abilities_meta "
                                "WHERE ability_guid=?", (ability_guid,)).fetchone()
                            formula = formula_from_raw(raw_row[0] if raw_row else "")
                            if formula:
                                existing = db.execute(
                                    "SELECT cost_mod_json FROM game_cards "
                                    "WHERE session_id=? AND card_uid=?",
                                    (session.session_id, int(target))).fetchone()
                                try:
                                    entries = json.loads(
                                        existing[0] or "[]") if existing else []
                                except Exception:
                                    entries = []
                                entries.append(formula)
                                db.execute(
                                    "UPDATE game_cards SET cost_mod_json=? "
                                    "WHERE session_id=? AND card_uid=?",
                                    (json.dumps(entries), session.session_id,
                                     int(target)))
                                db.commit()
                                logs.append(
                                    f"CardModifier cardcost dynamic "
                                    f"zones={formula['zones']} "
                                    f"x{formula['multiplier']} target={target}")
                                continue
                        db.execute(
                            "UPDATE game_cards SET card_cost_mod = COALESCE(card_cost_mod, 0) + ? "
                            "WHERE session_id=? AND card_uid=?",
                            (delta, session.session_id, int(target)))
                        db.commit()
                        logs.append(f"CardModifier cardcost {delta:+} target={target}")
                    continue
                else:
                    this_turn = (pm.get("duration") in ("EndOfTurn", "BeginningOfOwnersTurn",
                                                        "AfterCardsReadyOnPlayersTurn")
                                 or "this turn" in (game_text or "").lower())
                    if pm.get("property") == "attack":
                        atk_d, def_d = int(pm.get("amount") or 0), 0
                    else:
                        atk_d, def_d = 0, int(pm.get("amount") or 0)
                    if atk_d == 0 and def_d == 0 and "equal to this troop's [def]" in (
                            game_text or "").lower():
                        # Dynamic stat: "+[ATK] equal to this troop's [DEF]"
                        # (Chimera Guard Outrider) — amount comes from the
                        # source card's current defense.
                        srow = db.execute(
                            "SELECT ct.defense, gc.card_defense_mod FROM game_cards gc "
                            "JOIN card_templates ct ON ct.guid = gc.template_guid "
                            "WHERE gc.session_id=? AND gc.card_uid=?",
                            (session.session_id, int(source_uid))).fetchone()
                        if srow:
                            src_def = (srow[0] or 0) + (srow[1] or 0)
                            if pm.get("property") == "attack":
                                atk_d, def_d = src_def, 0
                            else:
                                atk_d, def_d = 0, src_def
                    target = _resolve_target()
                    apply_card_stat_mod(game, session, db, handler, pl_t, ai_t,
                                        target, atk_d, def_d, this_turn=this_turn)
                    logs.append(f"CardModifier {pm.get('property')} "
                                f"{atk_d if atk_d else def_d:+} dur={pm.get('duration')} "
                                f"target={target}")
                    continue
            # Fallback (no data-driven param): an ability can carry MULTIPLE
            # CardModifier leaves and the game_text holds the COMBINED delta
            # ("+2[ATK]/+2[DEF]"), so apply it exactly ONCE per ability —
            # applying per-leaf would double the buff.
            if card_mod_applied:
                continue
            card_mod_applied = True
            # Stat buffs/attribute grants/heal parse from the ability text.
            atk_d = _stat_delta(game_text, "ATK")
            def_d = _stat_delta(game_text, "DEF")
            low = (game_text or "").lower()
            if "gain" in low and "health" in low:
                # e.g. Spearcliff Pegasus "Gain 2 health"
                import re as _re
                m = _re.search(r'gain\s+(\d+)\s+health', low)
                amount = int(m.group(1)) if m else 1
                logs.append(_apply_health_gain(game, bstate, pl_t, ai_t,
                                               amount, source_owner_uid,
                                               db=db, handler=handler,
                                               session=session))
            elif any(k in low for k in ("flight", "steadfast", "spellshield",
                                        "lifedrain", "first strike", "swiftstrike",
                                        "immortal", "quick action", "canny block",
                                        "can't attack", "can't block", "speed")):
                target = _resolve_target()
                if target is not None:
                    bits = apply_attribute_grant(game, session, db, handler,
                                                 pl_t, ai_t, target, game_text)
                    logs.append(f"attribute grant +{bits:b}")
            else:
                target = _resolve_target()
                if target is not None:
                    # "this turn" buffs (Guard Dog) wear off at the owner's Prep;
                    # permanent buffs (Inspire/Deploy) persist.
                    this_turn = "this turn" in low
                    apply_card_stat_mod(game, session, db, handler, pl_t, ai_t,
                                        target, atk_d, def_d, this_turn=this_turn)
                logs.append(f"{etype}: {atk_d:+}ATK/{def_d:+}DEF target={target}")
        elif etype == "SummonTokenTroopAbilityEffectTemplate":
            fn = _LEAFS.get(etype)
            if fn:
                logs.append(fn(game, session, db, handler, pl_t, ai_t, bstate, eg, param))
        elif etype == "MoveCardToZoneEffectTemplate":
            logs.append(_resolve_move_zone(db, handler, game, session, pl_t, ai_t,
                                           bstate, eg, param, source_uid, game_text,
                                           target_uid))
        elif etype == "CounterSpellAbilityEffectTemplate":
            logs.append(_resolve_counter_spell(db, handler, game, session, pl_t, ai_t,
                                               bstate, eg, param, game_text))
        elif etype == "TransformCardAbilityEffectTemplate":
            import re as _re
            links = _re.findall(r'data=([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})', game_text or "")
            if links:
                # The transform TARGET is the last card link in the text (e.g.
                # Incantation's "...transform them into <a data=f0e3cf6c>
                # Sentinels of Light</a>").
                new_tpl = links[-1].lower()
                pending = (bstate or {}).get("pending_transform_cards") or []
                if pending:
                    for entry in pending:
                        if isinstance(entry, (tuple, list)):
                            tuid, loc = entry[0], entry[1]
                            transform_card(handler, game, session, pl_t, ai_t,
                                           int(tuid), new_tpl, keep_zone=True,
                                           bstate=bstate)
                        else:
                            transform_card(handler, game, session, pl_t, ai_t,
                                           int(tuid), new_tpl, bstate=bstate)
                    bstate.pop("pending_transform_cards", None)
                    logs.append(f"transform {len(pending)} -> {new_tpl[:8]}")
                elif source_uid is not None and not (bstate or {}).get("_skip_transform"):
                    transform_card(handler, game, session, pl_t, ai_t,
                                   int(source_uid), new_tpl, bstate=bstate)
                    logs.append("transform")
                elif (bstate or {}).get("_skip_transform"):
                    logs.append("transform skipped (gate not met)")
            else:
                logs.append("transform: no template link in text")
        elif etype == "ActivateAbilityEffectTemplate" and param:
            _resolve_ability_bom(db, handler, game, session, pl_t, ai_t,
                                 bstate, param, source_uid, game_text, target_uid,
                                 source_owner_uid)
        else:
            fn = _LEAFS.get(etype)
            if fn:
                logs.append(fn(game, session, db, handler, pl_t, ai_t, bstate, eg, param))
    # Restore the source card's original state (clear Activated shake)
    current_source = None
    if source_uid is not None and src_row and src_row[2] == "warzone":
        current_source = db.execute(
            "SELECT location FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(source_uid))).fetchone()
    if (source_uid is not None and src_row and src_row[2] == "warzone"
            and current_source and current_source[0] == "warzone"):
        # Re-read the card AFTER the BOM so the restore carries any buffs the
        # leaves just applied (a pre-resolution snapshot would visually revert
        # e.g. Righteous Paladin's +1/+1 on the client).
        _tpl2, ct2, _n2, _c2, atk2, def2, _g2 = handler._card_full_data(
            game, scid_src, src_row[1])
        game.push_card_updated(scid_src, owner, game_engine.ECardCollections.Warzone, ct2,
                               state=orig_state,
                               template_id=src_row[1], attack=atk2, defense=def2)
        # Re-apply counter badges AFTER the restore push (it carries none) so a
        # counter gained during this resolution stays visible on the client.
        push_card_counters(game, session, db, handler, pl_t, ai_t, source_uid)
    bstate.pop("resolving_ability", None)
    bstate.pop("resolving_target_uid", None)
    if previous_trigger_target is None:
        bstate.pop("resolving_trigger_target_uid", None)
    else:
        bstate["resolving_trigger_target_uid"] = previous_trigger_target
    bstate.pop("_skip_transform", None)
    return "; ".join(str(l) for l in logs if l)


def _resolve_remove_all_counters(db, handler, game, session, pl_t, ai_t,
                                 bstate, pm, game_text, source_uid):
    """CardModifier "counter" leaf with amount 0: remove all counters of the
    named kind from the controller's matching cards and stage them for the
    ability's transform leaf.

    The gate ("if there are five or more ...") is parsed from the ability text,
    and the counter name from the leaf text — both data-driven, no GUIDs.
    """
    import re as _re
    owner_id = (bstate or {}).get("resolving_owner_id", 0)
    cname = counter_name_from_text(pm.get("text") or game_text) or "counter"
    # Gate: the effect's gamedata condition (e.g. Incantation's "if there are
    # five or more incantation counters on this" = SourceCardHasCounters) from
    # the seeded ability_effect_conditions table — data-driven.  Fall back to
    # the text-derived threshold only when the effect row predates the seed.
    condition_id = (pm or {}).get("condition_id") or ""
    if condition_id:
        from .condition_engine import evaluate_effect_condition, ConditionContext
        cond_ctx = ConditionContext(db, session, bstate,
                                    ability_source_uid=source_uid,
                                    ability_source_owner_id=owner_id)
        if not evaluate_effect_condition(db, condition_id, cond_ctx):
            bstate["_skip_transform"] = True
            return f"condition {condition_id[:8]} not met: skip"
    else:
        gm = _re.search(r'if there are (\w+) or more', (game_text or "").lower())
        threshold = number_word_to_int(gm.group(1)) if gm else None
        if source_uid is not None and threshold is not None:
            have = card_counters(db, session.session_id, source_uid).get(cname, 0)
            if have < threshold:
                bstate["_skip_transform"] = True
                return f"counters {cname}={have} < {threshold}: skip"
        elif threshold is not None:
            bstate["_skip_transform"] = True
            return "remove-all counters: no source"
    # Remove the counter from every matching card the controller owns in ANY
    # zone, and stage each one (with its zone) for the transform leaf — the
    # client transforms copies in place (deck/hand/discard included).
    rows = db.execute(
        "SELECT card_uid, location FROM game_cards WHERE session_id=? AND user_id=?",
        (session.session_id, owner_id)).fetchall()
    cleared = []
    pending = []
    for cu, loc in rows:
        old_n = card_counters(db, session.session_id, cu).get(cname, 0)
        if old_n > 0:
            remove_card_counters(db, session.session_id, cu, cname)
            push_card_counters(game, session, db, handler, pl_t, ai_t, cu,
                               changed_counter=cname, old_value=old_n)
            cleared.append(int(cu))
            pending.append((int(cu), loc))
    if pending:
        bstate["pending_transform_cards"] = pending
    return f"removed {cname} counters from {len(cleared)} cards (transform {len(pending)})"

def _resolve_move_zone(db, handler, game, session, pl_t, ai_t, bstate,
                       eg, param, source_uid, game_text, target_uid=None):
    """MoveCardToZone leaf: bounce a troop to hand (Buccaneer) or raise a troop
    from the crypt into play (Captain of the Dragon Guard)."""
    low = (game_text or "").lower()
    if "each card voided by it into play" in low:
        # Solitary Exile: "When this leaves play, put each card voided by it
        # into play." The voided card UIDs were recorded by the void leaf.
        voided = ((bstate or {}).get("voided_by") or {}).get(str(int(source_uid))) or []
        returned = 0
        for vu in list(voided):
            row = db.execute(
                "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
                (session.session_id, int(vu))).fetchone()
            if not row:
                continue
            owner = pl_t if row[0] != 0 else ai_t
            db.execute(
                "UPDATE game_cards SET location='warzone', position=0, "
                "card_state = card_state & ~? "
                "WHERE session_id=? AND card_uid=?",
                (game_engine.ECardStates.Dead, session.session_id, int(vu)))
            db.commit()
            scid = game_engine.SessionCardId(game_engine.UID(int(vu)))
            tpl_row = db.execute(
                "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
                (session.session_id, int(vu))).fetchone()
            tpl_guid = tpl_row[0] if tpl_row else None
            _tpl, ct, _n, _c, atk, def_, _g = handler._card_full_data(game, scid, tpl_guid)
            game.push_card_moved(scid, owner, game_engine.ECardCollections.Warzone,
                                 game_engine.ECardLocations.Top, 0)
            game.push_card_updated(scid, owner, game_engine.ECardCollections.Warzone,
                                   ct, template_id=tpl_guid, attack=atk, defense=def_)
            returned += 1
        if voided:
            bstate.setdefault("voided_by", {})[str(int(source_uid))] = []
        return f"return {returned} voided card(s) into play"
    if "put target troop into its controller's hand" in low:
        # Bounce — for AI cards, auto-pick an opposing warzone troop
        if target_uid is None:
            src_row = db.execute(
                "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
                (session.session_id, int(source_uid))).fetchone()
            if src_row and src_row[0] == 0:
                # AI-controlled: auto-pick a player warzone troop
                bounce_row = db.execute(
                    "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id!=0 "
                    "AND location='warzone' AND card_type LIKE '%Troop%' "
                    "ORDER BY position LIMIT 1",
                    (session.session_id,)).fetchone()
                if bounce_row:
                    target_uid = bounce_row[0]
            if target_uid is None:
                return "bounce: no target"
        # Store the target so subsequent effects (e.g. cardcost) in the same BOM can find it
        bstate["player_spell_target"] = target_uid
        bstate["player_mod_target"] = target_uid
        row = db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(target_uid))).fetchone()
        if not row:
            return "bounce: target not found"
        owner = pl_t if row[0] != 0 else ai_t
        db.execute(
            "UPDATE game_cards SET location='hand', position=100 WHERE session_id=? AND card_uid=?",
            (session.session_id, int(target_uid)))
        db.commit()
        scid = game_engine.SessionCardId(game_engine.UID(int(target_uid)))
        tpl_row = db.execute(
            "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(target_uid))).fetchone()
        tpl_guid = tpl_row[0] if tpl_row else None
        _tpl, ct, _n, _c, atk, def_, _g = handler._card_full_data(game, scid, tpl_guid)
        game.push_card_moved(scid, owner, game_engine.ECardCollections.Hand,
                             game_engine.ECardLocations.Top, 0)
        game.push_card_updated(scid, owner, game_engine.ECardCollections.Hand, ct,
                               template_id=tpl_guid, attack=atk, defense=def_)
        resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                         "CardExitedZoneEvent", int(target_uid),
                         source_owner_uid=row[0])
        return f"bounce {hex(int(target_uid))}"
    if "crypt into play" in low:
        # Raise from crypt
        if target_uid is None:
            return "raise: no target"
        row = db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(target_uid))).fetchone()
        if not row:
            return "raise: target not found"
        owner = pl_t if row[0] != 0 else ai_t
        db.execute(
            "UPDATE game_cards SET location='warzone', position=0, "
            "card_state = (card_state & ~?) | ? WHERE session_id=? AND card_uid=?",
            (game_engine.ECardStates.StartedATurnOnYourSide |
             game_engine.ECardStates.Dead,
             game_engine.ECardStates.CameOutThisTurn,
             session.session_id, int(target_uid)))
        db.commit()
        scid = game_engine.SessionCardId(game_engine.UID(int(target_uid)))
        tpl_row = db.execute(
            "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(target_uid))).fetchone()
        tpl_guid = tpl_row[0] if tpl_row else None
        _tpl, ct, _n, _c, atk, def_, _g = handler._card_full_data(game, scid, tpl_guid)
        game.push_card_moved(scid, owner, game_engine.ECardCollections.Warzone,
                             game_engine.ECardLocations.Top, 0)
        game.push_card_updated(scid, owner, game_engine.ECardCollections.Warzone, ct,
                               template_id=tpl_guid, attack=atk, defense=def_)
        # The raised troop gets +1/+1 (per card text)
        apply_card_stat_mod(game, session, db, handler, pl_t, ai_t,
                            int(target_uid), 1, 1)
        return f"raise {hex(int(target_uid))} from crypt"
    return "move card zone (unhandled)"


def _resolve_counter_spell(db, handler, game, session, pl_t, ai_t, bstate,
                           eg, param, game_text):
    """CounterSpell leaf — interrupts a target card on the chain."""
    target_uid = (bstate or {}).get("player_spell_target")
    if target_uid is None:
        return "counter: no target on chain"
    row = db.execute(
        "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target_uid))).fetchone()
    owner = row[0] if row else 0
    from ._shared import owner_uid
    owner_sid = owner_uid(owner, pl_t, ai_t, bstate)
    from db import db_discard_card
    db_discard_card(session.session_id, int(target_uid), connection=db)
    scid = game_engine.SessionCardId(game_engine.UID(int(target_uid)))
    # Remove the countered card's own item from underneath Countermagic on the
    # chain, otherwise the next pass can resolve the already-countered card.
    stack = (bstate or {}).get("stack")
    if isinstance(stack, list):
        stack[:] = [item for item in stack
                    if int(item.get("source_uid") or 0) != int(target_uid)]
    # A full discard update keeps the client's cached representation out of the
    # hand/chain after the authoritative zone move.
    tpl_row = db.execute(
        "SELECT template_guid, card_type FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target_uid))).fetchone()
    if tpl_row:
        _tpl, card_type, _name, _cost, _atk, _def, _gems = \
            handler._card_full_data(game, scid, tpl_row[0])
        game.push_card_updated(
            scid, owner_sid, game_engine.ECardCollections.Discard,
            game_engine.card_type_from_db(card_type), template_id=tpl_row[0])
    game.push_card_moved(scid, owner_sid, game_engine.ECardCollections.Discard,
                         game_engine.ECardLocations.Top, 0)
    _log(f"    Countered {hex(int(target_uid))}")
    return f"countered {hex(int(target_uid))}"


def _card_drawn_gate(raw_json, bstate, source_owner_uid):
    """Evaluate the CardDrawnEvent trigger conditions modelled in gamedata:
    TriggerCardIsNthCardDrawnThisTurnByThisPlayer (m_Nth) and
    AbilityControllerHasThresholdAbilityCondition (m_ColorFlags /
    m_RequiredQuantity).  Unknown conditions keep the legacy fire-on-match
    behaviour."""
    import re as _re
    if not raw_json:
        return True
    side = "player" if source_owner_uid else "ai"
    if "TriggerCardIsNthCardDrawnThisTurnByThisPlayer" in raw_json:
        m = _re.search(r'"m_Nth"\s*:\s*(\d+)', raw_json)
        nth = int(m.group(1)) if m else 1
        drawn = int((bstate or {}).get(f"{side}_draws_this_turn", 0))
        if drawn != nth:
            return False
    if "AbilityControllerHasThresholdAbilityCondition" in raw_json:
        m = _re.search(r'"m_ColorFlags"\s*:\s*"([^"]+)"', raw_json)
        q = _re.search(r'"m_RequiredQuantity"\s*:\s*(\d+)', raw_json)
        color = m.group(1).lower() if m else ""
        need = int(q.group(1)) if q else 1
        flag = game_engine.SHARD_TO_FLAG.get(color, 0)
        if flag:
            have = int((bstate or {}).get(f"{side}_threshold", {}).get(flag, 0))
            if have < need:
                return False
    return True


def _explicit_target_templates(db, ability_guid):
    """Target template ids for an ability that REQUIRE player choice
    (m_Explicit=1 in AbilityTargetTemplate.jsonl -> target_templates.explicit)."""
    try:
        row = db.execute(
            "SELECT target_template_ids FROM card_abilities_meta WHERE ability_guid=?",
            (ability_guid,)).fetchone()
    except Exception:
        return []
    if row and row[0]:
        try:
            tids = json.loads(row[0])
        except Exception:
            tids = []
    else:
        from .fields import ability_record
        record = ability_record(db, ability_guid)
        tids = [item.get("m_Guid") for item in
                (record.get("m_AbilityTargetTemplateIds") or [])
                if isinstance(item, dict)]
    out = []
    for tid in tids:
        try:
            trow = db.execute(
                "SELECT explicit FROM target_templates WHERE template_id=?",
                (tid,)).fetchone()
        except Exception:
            continue
        if trow and trow[0]:
            out.append(tid)
    return out


def _trigger_collection_allows(raw_json, card_location):
    """Mirror the client's Card.PassesCollectionFlagRequirements: a trigger
    only fires while its source card is in one of the ability's
    m_TriggerCollectionFlags zones.  Missing/None flags mean unrestricted;
    unknown card locations are allowed (the client allows None/Simulacrum).
    """
    import re as _re
    if not raw_json:
        return True
    m = _re.search(r'"m_TriggerCollectionFlags"\s*:\s*"([^"]*)"', raw_json)
    flags = m.group(1) if m else ""
    if not flags or flags.strip().lower() in ("none", ""):
        return True
    allowed = {s.strip().lower() for s in flags.split("|") if s.strip()}
    loc = str(card_location or "").lower()
    if not loc:
        return True
    return loc in allowed


def resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                     event_type, source_uid, source_owner_uid=None,
                     extra_target=None, zones=None,
                     event_source_collection=None,
                     event_destination_collection=None,
                     event_previous_state=None):
    """Fire every card ability whose trigger_event_type == event_type.

    ``source_uid`` is the card that entered / attacked / blocked / died.
    ``source_owner_uid`` is the DB user_id owning that card (0 = AI).
    Returns a log string.
    """
    from db import log_req
    bstate = bstate or {}
    bstate["event_type"] = event_type

    logs = []

    def _opposing_owner(player_id):
        # Practice uses 0 for AI and one non-zero human id. PvP has two
        # non-zero ids, so find the opponent from the match state.
        if (bstate or {}).get("pvp"):
            for pid in (bstate or {}).get("pids") or []:
                try:
                    if int(pid) != int(player_id):
                        return int(pid)
                except (TypeError, ValueError):
                    continue
            for pid in ((bstate or {}).get("champ_map") or {}).keys():
                try:
                    if int(pid) != int(player_id):
                        return int(pid)
                except (TypeError, ValueError):
                    continue
            return None
        return (0 if (player_id or 0) != 0
                else (handler.user_profile["id"]
                      if handler.user_profile else 0))

    # Gather candidate abilities: the source card's own triggers + any warzone
    # card (same owner) with a matching trigger (Inspire/Deathcry).
    cand = {}
    if source_uid is not None:
        for ag in _card_ability_guids(db, session.session_id, source_uid):
            cand.setdefault(int(source_uid), []).append(ag)
    # The event TARGET card's own triggers also fire (CardDrawnEvent's drawn
    # card — e.g. Angel of Dawn's "when you draw this, play it for free").
    if extra_target is not None and int(extra_target) != int(source_uid or 0):
        for ag in _card_ability_guids(db, session.session_id, extra_target):
            cand.setdefault(int(extra_target), []).append(ag)
    owner_id = source_owner_uid
    if owner_id is None and source_uid is not None:
        row = db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(source_uid))).fetchone()
        owner_id = row[0] if row else 0
    # Champion passives live on the champion source, not in game_cards.  Add
    # the matching side's metadata-defined triggered abilities to the same
    # candidate pool used for warzone/hand cards.
    if owner_id is not None:
        for champion_uid, champion_ags in _champion_ability_holders(
                db, handler, owner_id).items():
            cand.setdefault(champion_uid, []).extend(champion_ags)
    if owner_id is not None:
        sides = [owner_id]
        zone_sets = [zones or ("warzone",)]
        # Turn-boundary triggers may be carried by cards in any persistent
        # card zone (for example Argus's "At the start of your turn, reveal
        # Argus from your hand").  The PvP start-turn path calls this
        # dispatcher with source_uid=None, so these cards cannot be discovered
        # through the triggering-card path above.  Scan all persistent zones;
        # _trigger_collection_allows below still enforces each ability's
        # metadata-defined m_TriggerCollectionFlags.  Explicit zone overrides
        # remain authoritative for callers such as GameStartedEvent.
        if (zones is None
                and event_type in ("TurnStartedEvent", "TurnEndedEvent")):
            zone_sets = [("warzone", "hand", "deck", "discard")]
        if event_type == "CardDrawnEvent":
            # Both sides' cards react to a draw ("when you draw" vs "when an
            # opposing champion draws") — the trigger conditions gate the side.
            other = _opposing_owner(owner_id)
            if other is None:
                other = 0 if (owner_id or 0) != 0 else (
                    handler.user_profile["id"] if handler.user_profile else 0)
            sides.append(other)
            # Hand-card draw triggers (the client's TriggerCollectionFlags Hand)
            # join the pool too.
            zone_sets.append(("hand",))
        if event_type == "CardEnteredZoneEvent":
            # A card entered a zone — both sides' cards react ("when a card
            # enters your/opposing crypt/warzone" e.g. Incantation of Fear);
            # the trigger conditions gate the side.
            other = _opposing_owner(owner_id)
            if other is None:
                other = 0 if (owner_id or 0) != 0 else (
                    handler.user_profile["id"] if handler.user_profile else 0)
            sides.append(other)
        for h in sides:
            for zs in zone_sets:
                for cu, ags in _warzone_ability_holders(
                        db, session.session_id, h, zs).items():
                    if (cu != int(source_uid or 0)
                            and cu != int(extra_target or 0)):
                        cand.setdefault(cu, []).extend(ags)
        # "When a troop you control deals damage, if THIS is in your hand, this
        # gets cost -1" (Fury of the Mountain God) — hand-card triggers fire
        # for combat damage events too; the trigger condition gates on the
        # in-hand zone filter.
        if event_type == "CardDealtDamageEvent":
            for cu, ags in _warzone_ability_holders(
                    db, session.session_id, owner_id, ("hand",)).items():
                if cu != int(source_uid or 0):
                    cand.setdefault(cu, []).extend(ags)

    for cu, ags in cand.items():
        for ag in list(ags):
            mrow = db.execute(
                "SELECT trigger_event_type, game_text FROM card_abilities_meta "
                "WHERE ability_guid=?", (ag,)).fetchone()
            raw_record = None
            if not mrow:
                from .fields import ability_record
                raw_record = ability_record(db, ag)
                trigger = raw_record.get("m_TriggerEventType") or {}
                mrow = (str(trigger.get("m_InternalType") or ""),
                        raw_record.get("m_GameText") or "")
            if not mrow:
                continue
            trigger_type = mrow[0] or ""
            # Encounter setup can add a card whose permanent GrantAbility
            # supplies a start-of-game ability (for example Taming Dire
            # Toad granting the Taming Sphere summon).  Such grants have no
            # trigger of their own, but must resolve once before
            # GameStartedEvent so the granted ability is present in time.
            static_grant = False
            if event_type == "GameStartedEvent" and not trigger_type:
                static_grant = bool(db.execute(
                    "SELECT 1 FROM ability_effects "
                    "WHERE ability_guid=? AND effect_type='GrantAbilityEffectTemplate' "
                    "AND effect_duration='Permanent' LIMIT 1", (ag,)).fetchone())
            if event_type in trigger_type or static_grant:
                gtext = mrow[1] or ""
                ir_row = db.execute(
                    "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?", (ag,)).fetchone()
                raw = ir_row[0] if ir_row else ""
                if not raw:
                    from .fields import ability_record
                    raw = json.dumps(raw_record or ability_record(db, ag))
                try:
                    uses_previous_state = bool(
                        json.loads(raw).get("m_UsesPreviousState", 0))
                except (TypeError, ValueError, json.JSONDecodeError):
                    uses_previous_state = False
                # Data-driven trigger-condition evaluation (port of the client's
                # Triggers/Conditions + Abilities.Conditions): the ability fires
                # only when its m_AbilityCondition + m_TriggerCondition trees
                # hold.  Unknown condition types default to True.
                from .condition_engine import (
                    trigger_condition_met,
                    ConditionContext,
                )
                # Champions join the condition engine's card pool so IsHero
                # filters and "controls target" conditions can evaluate the
                # drawing/damaged champion (not a game_cards row in live play).
                champion_pool = []
                champ_fn = getattr(handler, "_champion_targets", None)
                if callable(champ_fn):
                    try:
                        champion_pool = champ_fn() or []
                    except Exception:
                        champion_pool = []
                src_loc = None
                _src_card = None
                src_card_owner = None
                if cu is not None:
                    _orow = db.execute(
                        "SELECT user_id FROM game_cards "
                        "WHERE session_id=? AND card_uid=?",
                        (session.session_id, int(cu))).fetchone()
                    if _orow:
                        src_card_owner = _orow[0]
                # The event source owner is the player who drew/played/damaged
                # the card.  A warzone trigger can belong to the other side,
                # however (for example, an opponent's Twisted Fate reacting to
                # this player's draw).  Conditions and automatic targets must
                # be evaluated from the trigger card controller's perspective.
                ability_owner_id = (src_card_owner
                                    if src_card_owner is not None else owner_id)
                try:
                    _src_card = ConditionContext(
                        db, session, bstate, event_type=event_type,
                        ability_source_uid=cu,
                        ability_source_owner_id=ability_owner_id,
                        trigger_uid=source_uid,
                        pl_t=pl_t, ai_t=ai_t, extra_target=extra_target,
                        champions=champion_pool,
                        ability_source_card_owner=src_card_owner,
                        trigger_owner_id=owner_id).card(cu)
                except Exception:
                    _src_card = None
                if _src_card is not None:
                    src_loc = _src_card.get("location")
                # Encounter setup cards are stored in the hidden ``mod`` zone
                # so the client never renders the battleboard helper.  For
                # trigger collection flags, however, they behave as cards in
                # the warzone (their metadata is authored with Champions|
                # Warzone flags), so normalize that location before checking
                # the source ability's collection requirements.
                trigger_location = (
                    "Champions" if _src_card is not None and
                    _src_card.get("card_type") == "Champion" else
                    ("Warzone" if src_loc == "mod" else src_loc))
                if not _trigger_collection_allows(raw, trigger_location):
                    log_req(f"    {event_type} {ag[:8]} -> source in "
                            f"{trigger_location} not in trigger collections; skipped")
                    continue
                cond_ctx = ConditionContext(
                    db, session, bstate, event_type=event_type,
                    ability_source_uid=cu,
                    ability_source_owner_id=ability_owner_id,
                    trigger_uid=source_uid,
                    pl_t=pl_t, ai_t=ai_t, extra_target=extra_target,
                    champions=champion_pool,
                    ability_source_card_owner=src_card_owner,
                    trigger_owner_id=owner_id,
                    event_source_collection=event_source_collection,
                    event_destination_collection=event_destination_collection,
                    event_previous_state=event_previous_state,
                    uses_previous_state=uses_previous_state)
                if not trigger_condition_met(raw, cond_ctx):
                    continue
                # For CardCastEvent the event's trigger target is the card
                # being cast (the caller passes it as source_uid).  Preserve
                # that target for TriggerTargetPropertyVariable and
                # AbilityTriggerCardTargetTemplate even when no explicit
                # target was supplied by the event caller.
                event_target = extra_target
                if event_target is None and event_type == "CardCastEvent":
                    event_target = source_uid
                # A triggered ability with EXPLICIT target templates (e.g.
                # Solitary Exile's Deploy "Void another target card") must ask
                # the controller to choose before it can resolve.
                from .targeting import legal_targets as _legal_targets
                explicit_tpls = _explicit_target_templates(db, ag)
                if explicit_tpls:
                    champ_pool = getattr(handler, "_champion_targets",
                                         lambda: None)()
                    candidates = _legal_targets(
                        db, session.session_id, ability_owner_id,
                        explicit_tpls[0],
                        cu, both_players=True, champions=champ_pool)
                    if ability_owner_id == 0 or not hasattr(handler, "_prompt_trigger_targets"):
                        # AI-controlled (or non-interactive): auto-pick the
                        # first legal target and resolve normally.
                        if candidates:
                            extra_target = _ai_battle_target(
                                db, session, cu, ag, candidates)
                        else:
                            logs.append(f"{event_type} {ag[:8]} -> no legal target")
                            continue
                    else:
                        handler._prompt_trigger_targets(
                            game, pl_t, ai_t, session, bstate, cu, ag,
                            explicit_tpls, candidates)
                        logs.append(f"{event_type} {ag[:8]} -> awaiting target")
                        continue
                # Check if this ability ignores the chain (Deploy/Inspire/Deathcry
                # have m_IgnoresChain=1 — execute immediately, no priority window)
                ignores = True
                if raw:
                    ignores = '"m_IgnoresChain" : 1' in raw or '"m_IgnoresChain\": 1' in raw
                src_scid = game_engine.SessionCardId(game_engine.UID(cu))
                hidden_battleboard = bool(db.execute(
                    "SELECT 1 FROM game_cards gc JOIN card_templates ct "
                    "ON ct.guid=gc.template_guid WHERE gc.session_id=? "
                    "AND gc.card_uid=? AND LOWER(COALESCE(ct.subtype,''))='battleboard' "
                    "LIMIT 1", (session.session_id, int(cu))).fetchone())
                if ignores:
                    # Tell the client the ability fired so it plays the card's
                    # activation animation (UIBattle.OnAbilityPushedOnChain,
                    # BattleAnimationPlayCardEvent for IgnoresChain=true).
                    if not hidden_battleboard:
                        game.push_ability_on_chain(
                            src_scid, game_engine.ResourceId.from_str(ag),
                            ignores_chain=True)
                    res = _resolve_ability_bom(db, handler, game, session, pl_t, ai_t,
                                               bstate, ag, cu, gtext,
                                               target_uid=(extra_target
                                                           if extra_target is not None
                                                           else event_target),
                                               source_owner_uid=ability_owner_id)
                    logs.append(f"{event_type} {ag[:8]} -> {res}")
                else:
                    # Push to chain stack for opponent priority window
                    import battle_engine as _be
                    inst_id = int(bstate.get("_next_instance_id", 1))
                    bstate["_next_instance_id"] = inst_id + 1
                    _be.stack_push(bstate, {
                        "kind": "trigger", "ability_guid": ag,
                        "source_uid": cu, "target_uid": (extra_target
                                                           if extra_target is not None
                                                           else event_target),
                        "source_owner_uid": ability_owner_id,
                        "instance_id": inst_id,
                        "activated_ability_guid": (
                            bstate.get("activated_ability_guid")
                            if event_type == "CardActivatedEvent" else None),
                        "activated_source_uid": (
                            bstate.get("activated_source_uid")
                            if event_type == "CardActivatedEvent" else None),
                        "activated_target_uid": (
                            bstate.get("activated_target_uid")
                            if event_type == "CardActivatedEvent" else None),
                    })
                    game.push_ability_on_chain(src_scid, game_engine.ResourceId.from_str(ag),
                                               ability_instance_id=inst_id,
                                               ignores_chain=False)
                    logs.append(f"{event_type} {ag[:8]} -> chain")
    if logs:
        log_req("    Triggers (pushed to chain): " + "; ".join(logs))
    return "; ".join(logs)


def resolve_gain_charge_triggers(db, handler, game, session, pl_t, ai_t,
                                 bstate, owner_id):
    """Dispatch the data-defined event for a newly gained champion charge.

    Charge-point UI updates are not gameplay events by themselves.  Use the
    owning champion as the event source so triggers such as Reactor Bot's
    ``When you gain a charge`` can be discovered by the normal metadata-driven
    trigger dispatcher.
    """
    try:
        owner_id = int(owner_id or 0)
    except (TypeError, ValueError):
        owner_id = 0
    champ = (getattr(handler, "_ai_champ_scid", None)
             if owner_id == 0 else
             getattr(handler, "_player_champ_scid", None))
    if champ is None:
        return ""
    return resolve_triggers(
        db, handler, game, session, pl_t, ai_t, bstate,
        "GainChargeEvent", int(champ.uid.uid64), owner_id)


def resolve_stack_trigger(handler, game, session, db, pl_t, ai_t, bstate, item):
    """Resolve a triggered ability that was pushed onto the chain."""
    ag = item.get("ability_guid", "")
    cu = item.get("source_uid")
    target_uid = item.get("target_uid")
    mrow = db.execute(
        "SELECT game_text FROM card_abilities_meta WHERE ability_guid=?", (ag,)).fetchone()
    gtext = mrow[0] if mrow else ""
    # The trigger may belong to a PLAYER card (e.g. Adamanthian Scrivener):
    # look up the source card's owner so the effect targets the right champion.
    src_owner = item.get("source_owner_uid")
    if src_owner is None:
        src_owner = 0
    if cu is not None:
        orow = db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(cu))).fetchone()
        if orow:
            src_owner = orow[0]
        elif src_owner == 0:
            # Champion sources are represented by handler-held SessionCardIds,
            # not game_cards rows. Preserve their controller for effects such
            # as Ridge Raiders' opposing-champion damage trigger.
            pchamp = getattr(handler, "_player_champ_scid", None)
            achamp = getattr(handler, "_ai_champ_scid", None)
            if pchamp is not None and int(pchamp.uid.uid64) == int(cu):
                src_owner = (handler.user_profile["id"]
                             if handler.user_profile else 0)
            elif achamp is not None and int(achamp.uid.uid64) == int(cu):
                src_owner = 0
    if item.get("activated_ability_guid"):
        bstate["card_activated_item"] = {
            "kind": "ability",
            "ability_guid": item.get("activated_ability_guid"),
            "source_uid": item.get("activated_source_uid"),
            "target_uid": item.get("activated_target_uid"),
        }
    return _resolve_ability_bom(db, handler, game, session, pl_t, ai_t,
                                bstate, ag, cu, gtext,
                                target_uid=target_uid,
                                source_owner_uid=src_owner)


def resolve_enters_play_triggers(db, handler, game, session, pl_t, ai_t,
                                 bstate, entering_uid, entering_owner_id,
                                 entering_cost=None, extra_target=None):
    """Fire Deploy (self CardEnteredZone) + Inspire (other troops' AsEntersPlay)."""
    from db import log_req
    logs = []
    # Callers that move a permanent into play often do not have to carry the
    # cost separately (and tokens may legitimately cost zero).  Resolve it
    # from the entering card's template here so every game mode evaluates the
    # same data-defined Inspire condition.
    if entering_cost is None or int(entering_cost or 0) <= 0:
        crow = db.execute(
            "SELECT ct.cost FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid=gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?",
            (session.session_id, int(entering_uid))).fetchone()
        entering_cost = int(crow[0] or 0) if crow else 0
    else:
        entering_cost = int(entering_cost)
    # The client's SourcePlayerBriarLegionVariable counts how many Briar
    # Legions entered play under your control this game (drives Briar Legion's
    # "+2/+2 for each time a Briar Legion entered play under your control").
    try:
        erow = db.execute(
            "SELECT ct.name FROM game_cards gc JOIN card_templates ct "
            "ON ct.guid=gc.template_guid WHERE gc.session_id=? AND gc.card_uid=?",
            (session.session_id, int(entering_uid))).fetchone()
        if erow and (erow[0] or "").lower() == "briar legion":
            side = "player" if entering_owner_id else "ai"
            key = f"{side}_briar_legions_entered"
            bstate[key] = int(bstate.get(key, 0)) + 1
    except Exception:
        pass
    # Deploy: the entering card's own CardEnteredZone triggers
    logs.append(resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                                 "CardEnteredZoneEvent", entering_uid,
                                 entering_owner_id,
                                 extra_target=extra_target))
    # Deploy and Inspire are both the data-defined AsEntersPlay event.  The
    # old hand-written Inspire loop intentionally skipped the entering card,
    # which meant a self-trigger such as Honeycap's "as this enters play"
    # never ran and a 0/0 Honeycap immediately died to state-based effects.
    # Let the normal trigger-condition evaluator distinguish self triggers
    # from Inspire triggers (including cost/ownership filters) instead.
    logs.append(resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                                 "AsEntersPlayEvent", entering_uid,
                                 entering_owner_id,
                                 extra_target=entering_uid))
    if logs:
        log_req("    Enters-play triggers: " + "; ".join(str(l) for l in logs if l))
    return "; ".join(str(l) for l in logs if l)

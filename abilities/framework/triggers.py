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

import game_engine
from gamedata import RecordStore, ability_graph

from ._shared import (
    _log,
    apply_attribute_grant,
    owner_uid,
)
from .effects.counters import (
    card_counters, add_card_counter, remove_card_counters,
    push_card_counters, counter_name_from_text,
)
from .stat_mod import apply_card_stat_mod


_RECORD_STORE = RecordStore()


def _card_uses_variable(db, session_id, card_uid, variable_type):
    """Whether a live card has a typed ability variable.

    This follows the current card ability list, so transformed cards and
    Mimic-created copies are evaluated from their instance metadata rather
    than from a card-name special case.
    """
    row = db.execute(
        "SELECT card_abilities FROM game_cards "
        "WHERE session_id=? AND card_uid=?",
        (session_id, int(card_uid))).fetchone()
    if not row or not row[0]:
        return False
    try:
        ability_guids = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    for ability_guid in ability_guids or []:
        graph = ability_graph(_RECORD_STORE, str(ability_guid).lower())
        if graph is None:
            continue
        for variable in graph.variables:
            if variable.short_type == variable_type:
                return True
    return False


def _refresh_variable_cards(db, handler, game, session, pl_t, ai_t,
                            bstate, variable_type):
    """Re-send warzone cards whose continuous stats use a changed variable."""
    if not hasattr(handler, "_card_full_data"):
        return
    handler._current_bstate = bstate
    rows = db.execute(
        "SELECT card_uid, template_guid, user_id, card_state, card_type "
        "FROM game_cards WHERE session_id=? AND location='warzone'",
        (session.session_id,)).fetchall()
    for card_uid, template_guid, user_id, card_state, card_type in rows:
        if not _card_uses_variable(db, session.session_id, card_uid,
                                   variable_type):
            continue
        scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
        try:
            # SessionCardId is a wire wrapper without value equality. Remove
            # an older definition for this UID before rebuilding it, otherwise
            # repeated refreshes leave stale CardDefs in the same event batch.
            for old_scid in list(game.card_defs):
                if int(old_scid.uid.uid64) == int(card_uid):
                    del game.card_defs[old_scid]
            handler._card_full_data(game, scid, template_guid)
            cdef = game.card_defs.get(scid)
            game.push_card_updated(
                scid, owner_uid(user_id, pl_t, ai_t, bstate),
                game_engine.ECardCollections.Warzone,
                game_engine.card_type_from_db(card_type),
                template_id=template_guid,
                attributes=cdef.attributes if cdef else 0,
                state=int(card_state or 0))
        except Exception as exc:
            _log(f"    Static refresh failed for {variable_type} card "
                 f"{card_uid}: {exc}")


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
    the event type for Momentum powers.  The current Records contract is the
    only rules-data source.
    """
    key = str(keyword or "").lower()
    if key.endswith("ies"):
        key = key[:-3] + "y"
    elif key.endswith("s"):
        key = key[:-1]
    graph = ability_graph(_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        return False
    if key == "deathcry":
        try:
            from .tac import _tac_attr_hash, decode_tac
            tac = graph.source.field("m_SerializedTAC")
            data = (tac.field("data", "") if hasattr(tac, "field")
                    else tac.get("data", "") if isinstance(tac, dict)
                    else "")
            if data and _tac_attr_hash("Deathcry") in decode_tac(data):
                return True
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return False
    if key == "momentum":
        return "CardInspiredEvent" in graph.trigger_event_type
    return False


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


def _ai_trigger_target(db, session, ability_guid, source_uid, owner_id,
                       bstate, champions):
    """Choose a non-auto trigger target using its typed target metadata."""
    from .targeting import legal_targets

    graph = ability_graph(_RECORD_STORE, str(ability_guid).lower())
    target_ids = [target.guid for target in graph.targets] if graph else []
    effects = db.execute(
        "SELECT target_index, effect_type FROM ability_effects "
        "WHERE ability_guid=? AND target_index>=0 ORDER BY effect_order",
        (ability_guid,)).fetchall()
    for target_index, effect_type in effects:
        if int(target_index) >= len(target_ids):
            continue
        target_id = target_ids[int(target_index)]
        template = db.execute(
            "SELECT is_auto_target, target_kind FROM target_templates "
            "WHERE template_id=?", (target_id,)).fetchone()
        if not template or int(template[0] or 0) or template[1] in (
                "PlayerTargetTemplate", "AbilitySourceCardTargetTemplate",
                "SourceRevealedTargetTemplate", "SourceDrawnTargetTemplate",
                "SourceBuriedTargetTemplate", "SourceStoredTargetTemplate",
                "VoidedTargetTemplate", "AbilityCreatedTargetTemplate",
                "AbilityTriggerCardTargetTemplate"):
            continue
        candidates = legal_targets(
            db, session.session_id, owner_id, target_id, source_uid,
            both_players=True, champions=champions, battle_state=bstate)
        # Some authored sacrifice target filters say only "a troop you
        # control".  For a deploy sacrifice, the source is not an eligible
        # replacement for the optional "another troop" choice.
        if effect_type == "SacrificeCardAbilityEffectTemplate":
            candidates = [uid for uid in candidates
                          if int(uid) != int(source_uid)]
        if candidates:
            return _ai_battle_target(db, session, source_uid, ability_guid,
                                     candidates)
    return None


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
    """Resolve one current-Records ability through the shared interpreter.

    Trigger dispatch and card-play resolution use the same client-style
    AbilityInstance interpreter.  This compatibility-shaped entry point is
    retained for callers that still use its historical name, but it has no
    alternate flat/BOM implementation.
    """
    from gamedata import ability_graph
    from gamedata.play_plan import ActivationData

    ability_guid = str(ability_guid).lower()
    graph = ability_graph(_RECORD_STORE, ability_guid)
    if graph is None:
        raise RuntimeError(
            f"ability {ability_guid} is missing from current Records")

    target_map = {}
    if target_uid is not None:
        explicit_indexes = [
            index for index, target in enumerate(graph.targets)
            if target.requires_input and target.explicit
        ]
        indexes = explicit_indexes or ([0] if graph.targets else [])
        target_map = {
            index: (int(target_uid),) for index in indexes
        }

    previous = {
        key: bstate.get(key)
        for key in ("resolving_target_uid", "resolving_trigger_target_uid",
                    "player_spell_target", "player_mod_target",
                    "grant_target")
    }
    bstate["resolving_target_uid"] = target_uid
    bstate["resolving_trigger_target_uid"] = target_uid
    bstate["resolving_ability"] = ability_guid
    bstate["resolving_owner_id"] = source_owner_uid or 0
    bstate["resolving_source_uid"] = source_uid
    bstate["grant_target"] = (target_uid if target_uid is not None
                               else source_uid)
    bstate.pop("player_spell_target", None)
    bstate.pop("player_mod_target", None)
    try:
        from .resolution import resolve_ability
        return resolve_ability(
            handler, game, session, db, pl_t, ai_t, bstate,
            ability_guid, source_uid, source_owner_uid or 0,
            target_map=target_map,
            activation_data=ActivationData.from_values(target_map=target_map))
    finally:
        for key, value in previous.items():
            if value is None:
                bstate.pop(key, None)
            else:
                bstate[key] = value



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


def _explicit_target_templates(db, ability_guid):
    """Return current Records target templates that require player input."""
    graph = ability_graph(_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        return []
    return [target.guid for target in graph.targets
            if target.explicit and target.requires_input]


def _trigger_collection_allows(trigger_flags, card_location):
    """Mirror the client's Card.PassesCollectionFlagRequirements: a trigger
    only fires while its source card is in one of the ability's
    m_TriggerCollectionFlags zones.  Missing/None flags mean unrestricted;
    unknown card locations are allowed (the client allows None/Simulacrum).
    """
    if not trigger_flags:
        return True
    flags = str(trigger_flags)
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
            graph = ability_graph(_RECORD_STORE, str(ag).lower())
            if graph is None:
                continue
            trigger_type = graph.trigger_event_type or ""
            # Encounter setup can add a card whose permanent GrantAbility
            # supplies a start-of-game ability (for example Taming Dire
            # Toad granting the Taming Sphere summon).  Such grants have no
            # trigger of their own, but must resolve once before
            # GameStartedEvent so the granted ability is present in time.
            static_grant = False
            if event_type == "GameStartedEvent" and not trigger_type:
                static_grant = any(
                    effect.concrete_type == "GrantAbilityEffectTemplate" and
                    str(effect.duration).lower() == "permanent"
                    for effect in graph.effects)
            if event_type in trigger_type or static_grant:
                gtext = graph.game_text or ""
                raw = graph.source.to_dict()
                uses_previous_state = graph.uses_previous_state
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
                if not _trigger_collection_allows(
                        graph.trigger_collection_flags, trigger_location):
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
                elif ability_owner_id == 0:
                    # Non-explicit targets are normally auto-resolved by the
                    # client.  The server still needs to make that choice for
                    # AI triggers; otherwise a missing target reaches a leaf
                    # and can incorrectly fall back to the source card.
                    champ_pool = getattr(handler, "_champion_targets",
                                         lambda: None)()
                    extra_target = _ai_trigger_target(
                        db, session, ag, cu, ability_owner_id, bstate,
                        champ_pool or [])
                # Check if this ability ignores the chain (Deploy/Inspire/Deathcry
                # have m_IgnoresChain=1 — execute immediately, no priority window)
                ignores = graph.ignores_chain
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
    # Card-play stack items are drained by the battle engine.  Lightweight
    # trigger harnesses may pass those items through this historical helper;
    # they do not represent an AbilityTemplate and therefore have no ability
    # to interpret here.
    if not ag:
        return ""
    cu = item.get("source_uid")
    target_uid = item.get("target_uid")
    graph = ability_graph(_RECORD_STORE, str(ag).lower())
    if graph is None:
        raise RuntimeError(f"ability {str(ag).lower()} is missing from current Records")
    gtext = graph.game_text
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
    # SourcePlayerBriarLegionVariable is a typed card variable. The match-wide
    # event count is shared by both controllers, so a Briar Legion played by
    # either controller increases the value seen by every Briar.
    try:
        is_briar_counter_card = _card_uses_variable(
            db, session.session_id, entering_uid,
            "SourcePlayerBriarLegionVariable")
    except Exception as exc:
        is_briar_counter_card = False
        _log(f"    Briar Legion metadata lookup failed for {entering_uid}: "
             f"{exc}")
    if is_briar_counter_card:
        if "briar_legions_entered" not in bstate:
            bstate["briar_legions_entered"] = (
                int(bstate.get("player_briar_legions_entered", 0)) +
                int(bstate.get("ai_briar_legions_entered", 0)))
        bstate["briar_legions_entered"] = int(
            bstate.get("briar_legions_entered", 0)) + 1
        try:
            _refresh_variable_cards(
                db, handler, game, session, pl_t, ai_t, bstate,
                "SourcePlayerBriarLegionVariable")
        except Exception as exc:
            _log(f"    Briar Legion static refresh failed: {exc}")
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

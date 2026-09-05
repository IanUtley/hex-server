"""AI opponent logic (turn driving, playing, attacking).

The AI has no client, so every action is resolved server-side and pushed as
events. All functions take ``handler`` (the hconnect_server connection) first,
mirroring ability.py — they reach back into the handler for card data helpers
(``_card_full_data``, ``_fresh_game``, ``_db``) and push events onto the shared
``game``.

Personality: aggressive. When the AI controls an eligible troop it enters
combat and attacks the player with ALL of its eligible troops.
"""
import json
import random
import time

import game_engine
from db import _db, log_req, db_discard_card


AI_PHASE_DELAY = 1.0  # pause between AI phase pushes so the client renders them


# ---------------------------------------------------------------------------
# Personality (ported from the client's AIPersonality.cs value model)
#
# EAttitudes: Aggressive / Comfortable / Defensive. The MinimumXValue is the
# minimum combat "value" a troop needs before the AI commits it to an attack
# (AIPersonality.cs:32 — Aggressive=3, Comfortable=4, Defensive=5). The
# Aggressive AI alpha-strikes: it attacks with every eligible troop. A
# Comfortable/Defensive AI holds back troops below the threshold.
# ---------------------------------------------------------------------------
PERSONALITIES = {
    "Aggressive": {"min_x_value": 3, "alpha_strike": True, "timidness": 0.75},
    "Comfortable": {"min_x_value": 4, "alpha_strike": False, "timidness": 0.85},
    "Defensive": {"min_x_value": 5, "alpha_strike": False, "timidness": 0.95},
}
DEFAULT_PERSONALITY = "Aggressive"

# EDeckPersonality is a separate client enum from EAttitudes.  Default means
# that no deck-specific value override was authored.  The last three enum
# values exist in the client, but AIPersonality.UpdatePersonality has no value
# overrides for them, so they are intentionally not treated as supported
# strategies here.
DECK_PERSONALITIES = {
    "Aggressive", "BigThreats", "BuildArmy", "Burn", "HandAdvantage",
}
_DECK_PERSONALITY_VALUES = {
    0: None, 1: "Aggressive", 2: "BigThreats", 3: "BuildArmy",
    4: "Burn", 5: "Bury", 6: "Destruction", 7: "HandAdvantage",
    8: "Reanimation",
}


def _normalise_name(value):
    if isinstance(value, int):
        return value
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def normalise_deck_personality(value):
    """Return a supported deck-personality name, or None for Default."""
    value = _normalise_name(value)
    if isinstance(value, int):
        value = _DECK_PERSONALITY_VALUES.get(value)
    if value is None or str(value).lower() == "default":
        return None
    for name in DECK_PERSONALITIES:
        if str(value).lower() == name.lower():
            return name
    return None


def normalise_campaign_personality(value):
    """Return a supported combat attitude, falling back to Aggressive."""
    value = _normalise_name(value)
    for name in PERSONALITIES:
        if value is not None and str(value).lower() == name.lower():
            return name
    return DEFAULT_PERSONALITY


def configure_personality(handler, deck_personality=None,
                          campaign_personality=None):
    """Install both client-style AI personality layers for a battle.

    ``deck_personality`` controls card valuation and may be absent.  The
    campaign personality controls combat attitude and is always normalized.
    Keeping both fields on the handler lets every evaluator built during the
    battle use the same setup without changing its call sites.
    """
    deck_name = normalise_deck_personality(deck_personality)
    campaign_name = normalise_campaign_personality(campaign_personality)
    handler._ai_deck_personality = deck_name
    handler._ai_campaign_personality = campaign_name
    # Existing combat code reads this field directly.
    handler._ai_personality = campaign_name
    return deck_name, campaign_name


def personality(handler):
    """Resolve the AI's attitude for this battle (campaign config, else default)."""
    name = getattr(handler, "_ai_personality", None) or DEFAULT_PERSONALITY
    return PERSONALITIES.get(name, PERSONALITIES[DEFAULT_PERSONALITY])

def ai_pass_declare_defense(handler, session, pl_t, ai_t, bstate, game):
    """The AI is the defender: choose blockers for the player's declared
    attackers and emit BlockersAssigned + an updated CombatListing. Records the
    assignment in bstate['ai_blockers'] and marks the blocking troops so
    _resolve_combat_damage fights the blocked combats (attacker vs blockers,
    deaths firing Deathcry). Blocking heuristic, one blocker per attacker:
      - prefer the cheapest blocker that survives the hit (def > attacker atk);
      - else a blocker that trades (dies but takes the attacker down);
      - else chump-block only a big threat (atk >= 3) to protect the champion.
    When the unblocked attack would be lethal, preserve the champion first and
    use legal chump blocks even when the trade is otherwise unfavorable.
    Returns the (possibly reloaded) battle state."""
    attackers = {int(k): int(v) for k, v in (bstate.get("player_attackers") or {}).items()}
    ai_champ_scid = getattr(handler, "_ai_champ_scid", None) or game_engine.SessionCardId(ai_t)
    bstate["ai_blockers"] = {}
    if not attackers:
        return bstate
    # The AI's eligible blockers: untapped warzone troops it controls (any
    # untapped troop may block, summoning sickness only affects attacking).
    from abilities.framework.statics import can_block, effective_stats
    blockers = _db.execute(
        "SELECT gc.card_uid, gc.template_guid "
        "FROM game_cards gc "
        "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='warzone' "
        "AND gc.card_type LIKE '%Troop%' AND (gc.card_state & ?) = 0",
        (session.session_id, game_engine.ECardStates.Tapped)).fetchall()
    avail = []
    for uid, tpl in blockers:
        b_atk, b_def, b_attrs, _b_flags, _b_rage = effective_stats(
            _db, session.session_id, bstate, uid)
        avail.append({
            "uid": int(uid), "tpl": tpl,
            "atk": b_atk,
            "def": b_def,
            # Flight attackers can only be blocked by Flight/SkyGuard blockers.
            "flyer": bool(b_attrs & (game_engine.ECardAttributes.Flight |
                                     game_engine.ECardAttributes.SkyGuard)),
        })
    if not avail:
        return bstate
    # The player's attackers with effective stats.
    att_stats = {}
    for u in attackers:
        a_atk, a_def, a_attrs, a_flags, _a_rage = effective_stats(
            _db, session.session_id, bstate, u)
        from abilities.framework.statics import controller_flags
        if "double_damage" in a_flags or "double_damage" in controller_flags(
                _db, session.session_id, bstate, 0):
            a_atk *= 2
        att_stats[u] = {
            "atk": a_atk,
            "def": a_def,
            "flyer": bool(a_attrs & game_engine.ECardAttributes.Flight),
        }
    ai_health = max(0, int(bstate.get("ai_health", 20) or 0))
    incoming_damage = sum(max(0, a["atk"]) for a in att_stats.values())
    defending_lethal = ai_health > 0 and incoming_damage >= ai_health
    if defending_lethal:
        log_req(f"    AI defense: unblocked damage {incoming_damage} is "
                f"lethal at {ai_health} health; blocking for survival")
    # Decide blocks, biggest threats first (attack descending). Supports
    # MULTIBLOCK (one blocker facing several attackers it can survive) and
    # DOGPILE (several blockers trading for one big attacker when no single
    # blocker survives).
    # Value gate (client SolveBlock): only trade a blocker for an attacker
    # when the attacker is worth at least twice the blocker (or the attacker
    # is a real threat / we're at low life).  Otherwise chump-blocking gives
    # away card advantage.
    try:
        import ai_eval as _aieval
        ev = _aieval.build_evaluator(handler, session, bstate, ai_t, pl_t)
        ai_cards = {int(c.card_uid): c for c in ev.ai_warzone}
    except Exception:
        ev = None
        ai_cards = {}
    def worth_blocking(a_uid, b_uid):
        if defending_lethal:
            return True
        if ev is None:
            return True
        a_card = next((c for c in ev.player_warzone
                       if int(c.card_uid) == int(a_uid)), None)
        b_card = ai_cards.get(int(b_uid))
        if a_card is None or b_card is None:
            return True
        a_val = ev.get_card_value(a_card)
        b_val = ev.get_card_value(b_card)
        # A valuable attacker trading with a cheap blocker is fine; a cheap
        # attacker eating a valuable blocker is not.
        return a_val >= b_val * 2.0
    assignment = {}                      # attacker_uid -> [blocker_uids]
    used = set()                         # blockers already assigned anywhere
    blocker_dmg = {b["uid"]: 0 for b in avail}  # damage each blocker has taken
    for u in sorted(att_stats, key=lambda k: -att_stats[k]["atk"]):
        a = att_stats[u]

        def can_face(b):
            return (b["def"] - blocker_dmg[b["uid"]]) > a["atk"]

        # A Flight attacker can only be blocked by Flight/SkyGuard blockers;
        # "can't be blocked except..." attackers only by qualifying blockers.
        def flight_ok(b):
            return can_block(_db, session.session_id, bstate, u, b["uid"])

        # 1) A single unused blocker that survives the hit.
        free = [b for b in avail if b["uid"] not in used and flight_ok(b)]
        survivors = [b for b in free if can_face(b)]
        if survivors:
            # Prefer the cheapest survivor that is NOT worth more than the
            # attacker (a 5/5 eating a 2/2 is a bad trade even if it lives).
            good = [b for b in survivors if worth_blocking(u, b["uid"])]
            pool = good or survivors
            pick = min(pool, key=lambda b: b["def"])
            assignment[u] = [pick["uid"]]
            used.add(pick["uid"])
            blocker_dmg[pick["uid"]] += a["atk"]
            continue
        # 2) MULTIBLOCK: a blocker already blocking (and surviving so far) can
        #    also take this attacker.
        reuse = [b for b in avail if b["uid"] in used and can_face(b) and flight_ok(b)]
        if reuse:
            pick = max(reuse, key=lambda b: b["def"])
            assignment[u] = [pick["uid"]]
            blocker_dmg[pick["uid"]] += a["atk"]
            continue
        # 3) DOGPILE: no single survivor. If the attacker is a real threat,
        #    trade enough blockers (combined attack >= attacker defense) to kill
        #    it — they take the hit (most die) but bring down the bigger threat.
        if (a["atk"] >= 3 or defending_lethal) and free and any(
                worth_blocking(u, b["uid"]) for b in free):
            cands = sorted(free, key=lambda b: -b["atk"])
            pile, total = [], 0
            for b in cands:
                if not worth_blocking(u, b["uid"]):
                    continue
                pile.append(b["uid"])
                total += b["atk"]
                if total >= a["def"]:
                    break
            if not pile:
                continue
            assignment[u] = pile
            for bid in pile:
                used.add(bid)
                blocker_dmg[bid] += a["atk"]
            continue
        # If this attack cannot be profitably traded but the overall attack
        # would kill the champion, a chump block is still the correct play.
        # Attackers are processed largest-first, so this protects the most
        # incoming damage with the least valuable available blocker.
        if defending_lethal and free:
            pick = min(free, key=lambda b: (b["def"], b["atk"]))
            assignment[u] = [pick["uid"]]
            used.add(pick["uid"])
            blocker_dmg[pick["uid"]] += a["atk"]
    if assignment:
        bstate["ai_blockers"] = {str(k): [str(b) for b in v]
                                 for k, v in assignment.items()}
        for v in assignment.values():
            for bid in v:
                _db.execute(
                    "UPDATE game_cards SET card_state = (card_state | ?) WHERE session_id=? AND card_uid=?",
                    (game_engine.ECardStates.Blocking, session.session_id, bid))
        _db.commit()
    combats = []
    for i, u in enumerate(attackers):
        scid = game_engine.SessionCardId(game_engine.UID(u))
        combat_id = game_engine.CombatId(pl_t, i + 1)
        blocker_scids = [game_engine.SessionCardId(game_engine.UID(b))
                         for b in assignment.get(u, [])]
        game.push_blockers_assigned(combat_id, scid, ai_champ_scid, blocker_scids)
        cs = game_engine.CombatSessionEventArgs()
        cs.player_id = pl_t
        cs.id = combat_id
        cs.attacker = scid
        cs.blockers = blocker_scids
        combats.append(cs)
    if combats:
        game.push_combat_listing(pl_t, combats)
    log_req(f"    AI declares {len(assignment)} block(s): "
            f"{[(hex(k), [hex(b) for b in v]) for k, v in assignment.items()]}")
    return bstate

def player_can_attack_troops(handler, session, user_id=None):
    """True if the given player (default the human) controls a warzone troop
    that can attack.

    A troop can attack iff it is a troop, is in the warzone, is not tapped,
    and is NOT summoning sick. Summoning sickness is absent when the troop
    has StartedATurnOnYourSide (survived to this turn) OR has the Speed
    attribute (haste — can attack the turn it enters). Mirrors the client's
    Card.HasSummoningSickness() + CanAttack(). This drives whether the turn
    enters the combat phases (DeclareCombatPriorityWindow -> AssignDamage).
    """
    if user_id is None:
        user_id = handler.user_profile["id"]
    rows = _db.execute(
        "SELECT gc.card_uid, gc.card_state, gc.card_attributes, "
        "gc.temporary_attributes, ct.attributes, gc.card_abilities "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='warzone' "
        "AND gc.card_type LIKE '%Troop%'",
        (session.session_id, user_id)).fetchall()
    grant_attributes = getattr(handler, "_granted_attributes", None)
    for uid, state, card_attrs, temp_attrs, template_attrs, abilities_json in rows:
        if int(state or 0) & game_engine.ECardStates.Tapped:
            continue
        attrs = int(card_attrs or 0) | int(temp_attrs or 0) | int(template_attrs or 0)
        # Passive abilities can grant combat keywords without persisting them
        # into card_attributes.  Use the same gamedata-driven grant resolver
        # that supplies the card's displayed icons (e.g. a Speed gem).
        if grant_attributes:
            try:
                abilities = json.loads(abilities_json or "[]")
                attrs |= int(grant_attributes(abilities) or 0)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if attrs & (game_engine.ECardAttributes.CantAttack |
                    game_engine.ECardAttributes.Defensive):
            continue
        if (int(state or 0) & game_engine.ECardStates.StartedATurnOnYourSide
                or attrs & game_engine.ECardAttributes.Speed):
            return True
    return False

def ai_can_attack_troops(handler, session):
    """True if the AI controls a warzone troop eligible to attack."""
    return handler._player_can_attack_troops(session, 0)


def ai_discard_card(handler, game, session, pl_t, ai_t):
    """The AI chooses a hand card to discard (e.g. a Deathcry that forces each
    opposing champion to discard). Strategy: prefer a shard (least valuable),
    otherwise a random hand card. Moves it to the graveyard and pushes events
    onto `game`. Returns the discarded card's UID (or None if the hand is empty).
    """
    rows = _db.execute(
        "SELECT gc.id, gc.card_uid, gc.template_guid, ct.card_type FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='hand' ORDER BY gc.position",
        (session.session_id,)).fetchall()
    if not rows:
        return None
    # Discard the least valuable card (client AIHandleDiscardPhase uses
    # GetTheoriticalValue so future playability discounts value).  A shard is
    # still the preferred discard (it only ramps once per turn anyway).
    try:
        import battle_engine as _be
        import ai_eval as _aieval
        bs = _be.load_state(session)
        ev = _aieval.build_evaluator(handler, session, bs, ai_t, pl_t)
        by_val = sorted(ev.hand, key=lambda c: ev.get_theoretical_value(c))
        if by_val:
            pick_card = by_val[0]
            pick = next(r for r in rows if int(r[1]) == pick_card.card_uid)
        else:
            pick = rows[0]
    except Exception:
        shards = [r for r in rows if r[3] == 'Resource']
        pick = shards[0] if shards else random.choice(rows)
    row_id, card_uid, tpl_guid = pick[0], pick[1], pick[2]
    db_discard_card(session.session_id, card_uid, connection=_db)
    scid = game_engine.SessionCardId(game_engine.UID(card_uid))
    _tpl, ct, name, cost, atk, def_, gem = handler._card_full_data(game, scid, tpl_guid, None)
    game.push_card_discarded(scid, ai_t)
    game.push_card_updated(scid, ai_t, game_engine.ECardCollections.Discard, ct,
                           template_id=tpl_guid)
    game.push_card_moved(scid, ai_t, game_engine.ECardCollections.Discard,
                         game_engine.ECardLocations.Top, 0)
    log_req(f"    AI discards {name} ({hex(card_uid)})")
    return int(card_uid)

def ai_declare_attackers(handler, game, session, ai_t, pl_t, battle_state):
    """Aggressive AI: declare eligible AI warzone troops as attackers against the
    player's champion. Push AttackDeclared / CombatListing / CombatSession
    events, mark the attackers Attacking|HasAttacked (+Tapped unless Steadfast)
    and persist them in battle_state['ai_attackers'].

    Attacker selection ports AICombat.DetermineBestAttack: alpha-strike when
    it wins (or the personality is set to AlphaStrike); otherwise commit
    troops whose expected combat value is positive (damage through + blockers
    destroyed - our own losses), and hold back troops that would feed a
    blocker, unless the personality is aggressive enough to swing anyway.
    """
    import battle_engine as _be
    import ability as _abil
    import ai_eval as _aieval
    pers = personality(handler)
    alpha = pers.get("alpha_strike", True)
    # The dynamic attitude (ConsiderAttitutudeChange) shifts the minimum
    # combat value the AI commits: Aggressive 3 / Comfortable 4 / Defensive 5.
    attitude = battle_state.get("ai_attitude") or "Aggressive"
    min_x = {"Aggressive": 3, "Comfortable": 4, "Defensive": 5}.get(
        attitude, pers.get("min_x_value", 3))
    player_champ_uid = getattr(handler, "_player_champ_scid", None)
    player_champ_uid64 = player_champ_uid.uid.to_uint64() if player_champ_uid else 0
    rows = _db.execute(
        "SELECT gc.card_uid, gc.template_guid, ct.attributes, gc.card_attributes, gc.card_state, ct.attack "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='warzone' "
        "AND gc.card_type LIKE '%Troop%'",
        (session.session_id,)).fetchall()
    ev = None
    try:
        ev = _aieval.build_evaluator(handler, session, battle_state, ai_t, pl_t)
    except Exception as _ev_exc:
        log_req(f"    ai_eval combat init error: {_ev_exc!r}")
    # The player's untapped troops are the AI's potential blockers.
    opp_blockers = []
    if ev is not None:
        opp_blockers = [c for c in ev.player_warzone
                        if c.is_troop() and not (
                            c.card_state is not None
                            and int(c.card_state or 0) & game_engine.ECardStates.Tapped)]
    from abilities.framework.statics import effective_stats
    all_attackers = []
    for card_uid, tpl_guid, t_attrs, c_attrs, cstate, atk in rows:
        cstate = cstate or 0
        if (cstate & game_engine.ECardStates.Tapped):
            continue
        # Include dynamic/static attributes in legality and combat selection;
        # a granted keyword need not be copied into the template attributes.
        _eff_atk, _eff_def, eff_attrs, _eff_flags, _eff_rage = effective_stats(
            _db, session.session_id, battle_state, card_uid)
        attrs = (t_attrs or 0) | (c_attrs or 0) | int(eff_attrs or 0)
        if attrs & (game_engine.ECardAttributes.CantAttack |
                    game_engine.ECardAttributes.Defensive):
            continue
        if not (cstate & game_engine.ECardStates.StartedATurnOnYourSide) and not (
                attrs & game_engine.ECardAttributes.Speed):
            continue
        all_attackers.append((int(card_uid), tpl_guid, attrs))
    # Decide the attack set: alpha-strike wins, or per-troop combat value.
    chosen = []
    if all_attackers and not opp_blockers:
        # Aggressive AI attacks with every eligible troop when the opponent
        # has no blockers. This includes 0-attack troops with Rage: attacking
        # is how they acquire their permanent Rage bonus.
        chosen = all_attackers
        log_req(f"    AI open attack: {len(chosen)} eligible attacker(s)")
    elif ev is not None and all_attackers:
        ai_cards = {int(c.card_uid): c for c in ev.ai_warzone}
        attackers_cards = [ai_cards[u] for u, _, _ in all_attackers
                           if u in ai_cards]
        alpha_wins = _aieval_alpha_wins(ev, player_champ_uid64,
                                        battle_state, attackers_cards,
                                        opp_blockers)
        if alpha_wins:
            log_req(f"    AI alpha-strike: lethal with "
                    f"{len(attackers_cards)} attacker(s)")
            chosen = all_attackers
        elif alpha and _aieval_attack_set_value(ev, attackers_cards,
                                                opp_blockers) > 0:
            # Evaluate the attack as a team.  A blocker can stop only one
            # attacker; evaluating every troop against that same blocker
            # independently incorrectly rejects profitable attacks such as
            # five 1/1s into one 1/4 (four troops still connect).
            log_req(f"    AI aggressive group attack: profitable with "
                    f"{len(attackers_cards)} attacker(s)")
            chosen = all_attackers
        else:
            for uid, tpl, attrs in all_attackers:
                card = ai_cards.get(uid)
                if card is None:
                    continue
                atk = card.effective_attack()
                if atk <= 0:
                    continue
                if attrs & game_engine.ECardAttributes.ForceAttack:
                    chosen.append((uid, tpl, attrs))
                    continue
                # Value vs the best single blocker that can face this troop.
                dmg, value = _aieval_best_attack_value(ev, card, opp_blockers)
                if value > 0 and atk >= min_x:
                    chosen.append((uid, tpl, attrs))
                elif value > 0 and alpha:
                    # Aggressive still swings with value-positive attackers
                    # below the comfort threshold.
                    chosen.append((uid, tpl, attrs))
    else:
        # Fallback (evaluator unavailable): old personality gate.
        for card_uid, tpl_guid, t_attrs, c_attrs, cstate, atk in rows:
            cstate = cstate or 0
            if (cstate & game_engine.ECardStates.Tapped):
                continue
            attrs = (t_attrs or 0) | (c_attrs or 0)
            if attrs & (game_engine.ECardAttributes.CantAttack |
                        game_engine.ECardAttributes.Defensive):
                continue
            if not (cstate & game_engine.ECardStates.StartedATurnOnYourSide) and not (
                    attrs & game_engine.ECardAttributes.Speed):
                continue
            if not alpha and int(atk or 0) < min_x and not (
                    attrs & game_engine.ECardAttributes.ForceAttack):
                continue
            chosen.append((int(card_uid), tpl_guid, attrs))
    attackers = {}
    combats = []
    held = 0
    ai_champ_scid = getattr(handler, "_ai_champ_scid", None) or game_engine.SessionCardId(ai_t)
    for card_uid, tpl_guid, attrs in chosen:
        uid = int(card_uid)
        scid = game_engine.SessionCardId(game_engine.UID(uid))
        combat_id = game_engine.CombatId(ai_t, uid & 0xFFFF)
        game.push_attack_declared(combat_id, ai_t, player_champ_uid or game_engine.SessionCardId(pl_t), scid)
        # Mark attacking (tapped unless Steadfast). Persist the OR'd state.
        state = (game_engine.ECardStates.Attacking |
                 game_engine.ECardStates.HasAttacked)
        if not (attrs & game_engine.ECardAttributes.Steadfast):
            state |= game_engine.ECardStates.Tapped
        _db.execute(
            "UPDATE game_cards SET card_state = (card_state | ?) WHERE session_id=? AND card_uid=?",
            (state, session.session_id, uid))
        handler._card_full_data(game, scid, tpl_guid)
        crow = _db.execute(
            "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, uid)).fetchone()
        pushed_state = int(crow[0]) if crow and crow[0] else state
        game.push_card_updated(scid, ai_t, game_engine.ECardCollections.Warzone,
                               game_engine.ECardTypes.Troop,
                               template_id=tpl_guid, state=pushed_state)
        if state & game_engine.ECardStates.Tapped:
            _abil.resolve_triggers(
                _db, handler, game, session, pl_t, ai_t, battle_state,
                "CardTappedEvent", uid, 0)
        # Fire "when this attacks" triggers (e.g. Chimera Guard Outrider).
        _abil.resolve_triggers(
            _db, handler, game, session, pl_t, ai_t, battle_state,
            "CardAttackedEvent", uid, 0)
        _abil.resolve_triggers(
            _db, handler, game, session, pl_t, ai_t, battle_state,
            "CardAttackedOrBlockedEvent", uid, 0)
        # Rage X: when this attacks it gets +X ATK this turn.
        from abilities.framework.keywords.combat import apply_rage_keyword
        apply_rage_keyword(_db, session, handler, game, pl_t, ai_t,
                           battle_state, uid)
        attackers[str(uid)] = str(player_champ_uid64)
        cs = game_engine.CombatSessionEventArgs()
        cs.player_id = ai_t
        cs.id = combat_id
        cs.attacker = scid
        cs.blockers = []
        combats.append(cs)
    _db.commit()
    battle_state["ai_attackers"] = attackers
    _be.save_state(session, battle_state)
    if combats:
        game.push_combat_listing(ai_t, combats)
    log_req(f"    AI declares {len(attackers)} attacker(s) ({'alpha' if alpha else 'min_x=' + str(min_x)}; {held} held): {[hex(int(u)) for u in attackers]}")
    return battle_state


def _aieval_alpha_wins(ev, player_champ_uid64, battle_state, attackers,
                       blockers):
    """Alpha-strike lethal check (AICardEvaluator.AlphaStrikeWins)."""
    try:
        health = int(battle_state.get("player_health", 20))
        return ev.alpha_strike_wins(health, attackers, blockers)
    except Exception:
        return False


def _aieval_best_attack_value(ev, card, blockers):
    """Best (damage, value) for this attacker vs the opponent's blockers —
    the strongest single blocker counts (the client evaluates the full
    subset; a single-blocker worst case is the safe simplification)."""
    best = (card.effective_attack(), card.effective_attack() * 1.0)
    for b in blockers:
        if not ev._can_block(b, card):
            continue
        dmg, value = ev.value_attack(card, [b])
        if value < best[1]:
            best = (dmg, value)
    return best


def _aieval_attack_set_value(ev, attackers, blockers):
    """Estimate the value of attacking with a whole troop group.

    The defender can assign each blocker to at most one attacker. Start with
    the unblocked value of every attacker, then apply the most damaging legal
    blocker assignment. This preserves the important team-level property
    that excess attackers still connect after the available blockers are
    occupied.
    """
    total = sum(max(0, c.effective_attack()) for c in attackers)
    available = list(blockers)
    assigned = set()
    for blocker in available:
        best_delta = 0.0
        best_index = None
        for index, attacker in enumerate(attackers):
            if index in assigned or not ev._can_block(blocker, attacker):
                continue
            _damage, blocked_value = ev.value_attack(attacker, [blocker])
            delta = blocked_value - max(0, attacker.effective_attack())
            if delta < best_delta:
                best_delta = delta
                best_index = index
        if best_index is not None:
            assigned.add(best_index)
            total += best_delta
    return total

def resolve_combat(handler, session, pl_t, ai_t, bstate, attackers, blockers_map,
                  attacker_uid, defender_uid, attacker_key, order_map=None,
                  send_events=None, first_strike=False):
    """Shared combat-damage resolution used by BOTH the AI-attacks-player path
    (resolve_ai_combat_damage) and the player-attacks-AI path
    (handler._resolve_combat_damage) — the two are the same battle, just with
    the attacking/defending players swapped.

    `attackers` = {attacker_uid: defender_uid} (bstate[attacker_key]),
    `blockers_map` = {attacker_uid: [blocker_uids]} (the defender's troops).
    Blocked attackers fight each of their blockers (attacker deals its attack
    to each; each blocker deals its attack back — troops with defense <= damage
    taken die, firing Deathcry). Unblocked attackers hit the defender's
    champion. Lifelink (SpiritDrain) heals each controller for the damage their
    side dealt. Combat events are wrapped in Begin/EndCombatResolution, and
    combat-death Deathcries are drained. Returns the updated bstate.

    ``first_strike=True`` is the Swiftstrike damage step: only combatants with
    FirstStrike/DualStrike deal damage, and the casualties are removed so the
    normal step (``first_strike=False``) only fights survivors.  In the normal
    step FirstStrike-only troops have already dealt and stay quiet; DualStrike
    deals in BOTH steps (the client's Card.CaresAboutCombatPhase).
    """
    import battle_engine as _be
    import ability as _abil
    if not attackers:
        return bstate

    def health_key(uid):
        return "player_health" if uid == pl_t else "ai_health"

    def _owner_of(uid):
        if bstate.get("pvp"):
            # The pid is encoded in the player UID: (pid << 8) | 244.
            return int(uid.uid64) >> 8
        return 0 if uid == ai_t else (handler.user_profile["id"]
                                      if handler.user_profile else 0)

    def champ_scid(uid):
        if bstate.get("pvp"):
            cm = bstate.get("champ_map") or {}
            pid = int(uid.uid64) >> 8
            cu = int(cm.get(str(pid), 0))
            if cu:
                return game_engine.SessionCardId(game_engine.UID(cu))
            return game_engine.SessionCardId(uid)
        if uid == pl_t:
            return getattr(handler, "_player_champ_scid", None) or game_engine.SessionCardId(pl_t)
        return getattr(handler, "_ai_champ_scid", None) or game_engine.SessionCardId(ai_t)

    att_health = health_key(attacker_uid)
    def_health = health_key(defender_uid)
    defender_champ = champ_scid(defender_uid)

    game = handler._fresh_game(session, pl_t, ai_t, bstate)
    def_health_before = bstate.get(def_health, 20)
    att_health_before = bstate.get(att_health, 20)
    att_lifegain = 0
    def_lifegain = 0

    game.push_begin_combat_resolution()
    combats = []
    deferred_deaths = []  # (card_uid, template_guid, owner_id) — Deathcries
    # resolve only after ALL combat damage has been assigned (Hex: combat
    # damage is simultaneous; a blocker's Deathcry cannot resolve mid-fight).
    for u in attackers:
        uloc = _db.execute(
            "SELECT location FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(u))).fetchone()
        if not uloc or uloc[0] != "warzone":
            continue  # died in the earlier Swiftstrike damage step
        scid = game_engine.SessionCardId(game_engine.UID(int(u)))
        from abilities.framework.statics import controller_flags, effective_stats
        atk, a_def, a_attrs, a_flags, _a_rage = effective_stats(
            _db, session.session_id, bstate, u)
        a_dmg = 0  # effective_stats already nets combat damage out of defense
        a_tpl = _db.execute(
            "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, u)).fetchone()
        a_tpl = a_tpl[0] if a_tpl else ""
        a_att_flags = controller_flags(_db, session.session_id, bstate,
                                       _owner_of(attacker_uid))
        if "double_damage" in a_flags or "double_damage" in a_att_flags:
            atk *= 2
        a_has_fs = bool(a_attrs & (game_engine.ECardAttributes.FirstStrike |
                                   game_engine.ECardAttributes.DualStrike))
        a_deals = a_has_fs if first_strike else (
            (not (a_attrs & game_engine.ECardAttributes.FirstStrike))
            or bool(a_attrs & game_engine.ECardAttributes.DualStrike))
        step_atk = atk if a_deals else 0
        a_prevent = "prevent_combat_damage" in a_flags
        combat_id = game_engine.CombatId(attacker_uid, int(u) & 0xFFFF)
        blockers = [game_engine.SessionCardId(game_engine.UID(b))
                    for b in blockers_map.get(int(u), [])]
        game.push_combat_phase_resolved(combat_id, scid, defender_champ, blockers)
        combats.append((combat_id, scid, defender_champ, blockers))
        # Damage targets for CardDealtDamageEvent ("when this deals damage to
        # an opposing champion/troop"): the client fires one event per damaged
        # card, so the trigger conditions (TriggerTarget IsHero, controls-target
        # checks) evaluate against the actual damaged card.
        dmg_targets = []
        if blockers:
            # Blocked. The ATTACKER chooses the order its damage is assigned
            # among the blockers: the first blocker takes as much as it needs to
            # die, then the leftover damage flows to the next, and so on. The
            # human attacker's order arrives via AssignDamageOrderTransaction
            # (order_map); otherwise default weakest-first.
            b_uids = [b for b in blockers_map.get(int(u), [])]
            if order_map and int(u) in order_map:
                ordered = [b for b in order_map[int(u)] if b in b_uids]
                ordered += [b for b in b_uids if b not in ordered]
            else:
                eff = {}
                for b in b_uids:
                    r0 = _db.execute(
                        "SELECT ct.defense, gc.card_defense_mod, gc.card_damage FROM game_cards gc "
                        "JOIN card_templates ct ON ct.guid = gc.template_guid "
                        "WHERE gc.session_id=? AND gc.card_uid=?",
                        (session.session_id, b)).fetchone()
                    eff[b] = (r0[0] or 0) + (r0[1] or 0) - (r0[2] or 0) if r0 else 0
                ordered = sorted(b_uids, key=lambda b: eff[b])
            remaining = step_atk
            total_block_atk = 0
            for b in ordered:
                bloc = _db.execute(
                    "SELECT location FROM game_cards "
                    "WHERE session_id=? AND card_uid=?",
                    (session.session_id, b)).fetchone()
                if not bloc or bloc[0] != "warzone":
                    continue  # died in the Swiftstrike step
                b_atk, b_def, b_attrs, b_flags, _b_rage = effective_stats(
                    _db, session.session_id, bstate, b)
                b_dmg = 0
                b_tpl = _db.execute(
                    "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
                    (session.session_id, b)).fetchone()
                b_tpl = b_tpl[0] if b_tpl else ""
                b_def_flags = controller_flags(_db, session.session_id, bstate,
                                               _owner_of(defender_uid))
                if "double_damage" in b_flags or "double_damage" in b_def_flags:
                    b_atk *= 2
                b_has_fs = bool(b_attrs & (game_engine.ECardAttributes.FirstStrike |
                                           game_engine.ECardAttributes.DualStrike))
                b_deals = b_has_fs if first_strike else (
                    (not (b_attrs & game_engine.ECardAttributes.FirstStrike))
                    or bool(b_attrs & game_engine.ECardAttributes.DualStrike))
                step_b_atk = b_atk if b_deals else 0
                b_prevent = "prevent_combat_damage" in b_flags
                total_block_atk += step_b_atk
                if b_prevent:
                    log_req(f"    Blocked combat: {hex(b)} prevents combat damage")
                    continue
                # The attacker assigns its remaining damage to this blocker: it
                # needs `b_def - b_dmg` to die; leftover carries to the next.
                b_need = max(0, b_def - b_dmg)
                dealt = min(remaining, b_need) if remaining > 0 else 0
                remaining = max(0, remaining - dealt)
                if b_need > 0 and dealt >= b_need:
                    _abil.kill_troop(game, session, _db, handler, pl_t, ai_t, b,
                                     bstate, cause="damage",
                                     defer_deathcry=True,
                                     deferred=deferred_deaths)
                    dmg_targets.append(int(b))
                    log_req(f"    Blocked combat: {hex(u)} assigns {dealt} -> kills blocker {hex(b)} (def {b_def}-{b_dmg}); {remaining} leftover")
                elif dealt > 0:
                    # The blocker survives: mark the damage dealt so the client
                    # shows the reduced defense in red. PRESERVE its current
                    # combat state (Blocking) and just add Damaged.
                    _db.execute(
                        "UPDATE game_cards SET card_damage = card_damage + ? WHERE session_id=? AND card_uid=?",
                        (dealt, session.session_id, b))
                    crow = _db.execute(
                        "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
                        (session.session_id, b)).fetchone()
                    pstate = int(crow[0]) if crow and crow[0] else 0
                    b_scid = game_engine.SessionCardId(game_engine.UID(b))
                    handler._card_full_data(game, b_scid, b_tpl)
                    game.push_card_updated(
                        b_scid, defender_uid, game_engine.ECardCollections.Warzone,
                        game_engine.ECardTypes.Troop, template_id=b_tpl,
                        state=pstate | game_engine.ECardStates.Damaged)
                    dmg_targets.append(int(b))
                    log_req(f"    Blocked combat: {hex(u)} assigns {dealt} -> vs blocker {hex(b)} (def {b_def}-{b_dmg}); blocker survives, {remaining} leftover")
                # Lifelink (SpiritDrain): the blocker's controller heals for the
                # damage the blocker dealt to the attacker.
                if b_attrs & game_engine.ECardAttributes.SpiritDrain and step_b_atk:
                    def_lifegain += step_b_atk
            # Trample / Crush (Juggernaught): after assigning enough damage to
            # kill the blockers, any remaining damage breaks through to the
            # defender's champion.
            if remaining > 0 and (a_attrs & game_engine.ECardAttributes.Juggernaught):
                old_health = bstate.get(def_health, 20)
                bstate[def_health] = max(0, old_health - remaining)
                log_req(f"    Trample: {hex(u)} deals {remaining} leftover -> defender health {old_health}->{bstate[def_health]}")
            # SpiritDrain heals for actual combat damage dealt, not the
            # attacker's full power.  `remaining` is the damage left after
            # assigning damage to blockers; Juggernaught carries that
            # remainder through to the champion.
            a_dealt_this_step = step_atk - remaining
            if (a_attrs & game_engine.ECardAttributes.Juggernaught
                    and remaining > 0):
                a_dealt_this_step += remaining
            # Each blocker deals its full attack back to the attacker.
            if a_prevent:
                log_req(f"    Blocked combat: {hex(u)} prevents combat damage")
            elif a_def - a_dmg <= total_block_atk:
                _abil.kill_troop(game, session, _db, handler, pl_t, ai_t, u,
                                 bstate, cause="damage",
                                 defer_deathcry=True,
                                 deferred=deferred_deaths)
                log_req(f"    Blocked combat: blockers ({total_block_atk}) kill attacker {hex(u)} (def {a_def}-{a_dmg})")
            elif total_block_atk > 0:
                # The attacker survives: mark temporary damage on it. PRESERVE
                # its current combat state (Attacking|HasAttacked|Tapped) and
                # just add Damaged — state=Damaged alone would untap/un-attack it
                # on the client (the flicker seen mid-combat).
                _db.execute(
                    "UPDATE game_cards SET card_damage = card_damage + ? WHERE session_id=? AND card_uid=?",
                    (total_block_atk, session.session_id, u))
                acrow = _db.execute(
                    "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
                    (session.session_id, u)).fetchone()
                apstate = int(acrow[0]) if acrow and acrow[0] else 0
                handler._card_full_data(game, scid, a_tpl)
                game.push_card_updated(
                    scid, attacker_uid, game_engine.ECardCollections.Warzone,
                    game_engine.ECardTypes.Troop, template_id=a_tpl,
                    state=apstate | game_engine.ECardStates.Damaged)
                log_req(f"    Blocked combat: blockers ({total_block_atk}) vs attacker {hex(u)} (def {a_def}-{a_dmg}); attacker survives")
            elif not a_deals:
                log_req(f"    Blocked combat: {hex(u)} does not deal damage this step")
            # Lifelink (SpiritDrain): heal only for damage the attacker
            # actually dealt to blockers (plus any trample damage).
            if a_attrs & game_engine.ECardAttributes.SpiritDrain and a_dealt_this_step:
                att_lifegain += a_dealt_this_step
        else:
            # Unblocked: the attacker hits the defender's champion.
            if a_deals:
                old_health = bstate.get(def_health, 20)
                bstate[def_health] = max(0, old_health - step_atk)
                if step_atk > 0:
                    dmg_targets.append(int(defender_champ.uid.uid64))
                tnow = int(bstate.get("turn_number", 1))
                if bstate.get("damaged_opponent_turn") != tnow:
                    bstate["damaged_opponent_this_turn"] = []
                    bstate["damaged_opponent_turn"] = tnow
                damaged = bstate.setdefault("damaged_opponent_this_turn", [])
                if int(u) not in damaged:
                    damaged.append(int(u))
                log_req(f"    Combat damage: {hex(u)} deals {step_atk} -> defender health {old_health}->{bstate[def_health]}")
                if a_attrs & game_engine.ECardAttributes.SpiritDrain:
                    att_lifegain += step_atk
            else:
                log_req(f"    Combat damage: {hex(u)} does not deal damage this step")
        # Damage-trigger events: one CardDealtDamageEvent per damaged card
        # (the client fires one per damage event; the conditions gate the side).
        for dmg_target in dmg_targets:
            _abil.resolve_triggers(
                _db, handler, game, session, pl_t, ai_t, bstate,
                "CardDealtDamageEvent", int(u),
                _owner_of(attacker_uid), extra_target=dmg_target)
        for b in blockers_map.get(int(u), []):
            _abil.resolve_triggers(_db, handler, game, session, pl_t, ai_t,
                                   bstate, "CardBlockedEvent", int(b),
                                   _owner_of(defender_uid))
            _abil.resolve_triggers(_db, handler, game, session, pl_t, ai_t,
                                   bstate, "CardAttackedOrBlockedEvent", int(b),
                                   _owner_of(defender_uid))
    _db.commit()
    # Lifelink heals for each controller's damage dealt.  Route through
    # _apply_health_gain so "when you gain health" triggers (e.g. Incantation
    # of Righteousness) fire for the healed player, then apply the max cap.
    if att_lifegain:
        from abilities.framework.triggers import _apply_health_gain
        healed_owner = _owner_of(attacker_uid)
        _apply_health_gain(game, bstate, pl_t, ai_t, att_lifegain,
                           healed_owner, db=_db, handler=handler,
                           session=session)
        max_key = "player_max_health" if attacker_uid == pl_t else "ai_max_health"
        if bstate.get(max_key):
            bstate[att_health] = min(bstate[att_health], bstate[max_key])
        log_req(f"    Lifelink: attacker gains {att_lifegain} life -> {bstate[att_health]}")
        att_health_before = bstate.get(att_health, 20)
    if def_lifegain:
        from abilities.framework.triggers import _apply_health_gain
        healed_owner = _owner_of(defender_uid)
        _apply_health_gain(game, bstate, pl_t, ai_t, def_lifegain,
                           healed_owner, db=_db, handler=handler,
                           session=session)
        max_key = "player_max_health" if defender_uid == pl_t else "ai_max_health"
        if bstate.get(max_key):
            bstate[def_health] = min(bstate[def_health], bstate[max_key])
        log_req(f"    Lifelink: defender gains {def_lifegain} life -> {bstate[def_health]}")
        def_health_before = bstate.get(def_health, 20)
    # Reflect each player's net health change to the client.
    game.player_health = bstate.get("player_health", 20)
    game.ai_health = bstate.get("ai_health", 10)
    for uid, health, before in ((attacker_uid, att_health, att_health_before),
                                (defender_uid, def_health, def_health_before)):
        if bstate.get(health, 20) != before:
            ev = game_engine.ChampionHealthChangedSessionEventArgs()
            ev.player_id = uid
            ev.old_damage_value = before
            ev.new_damage_value = bstate.get(health, 20)
            game._push(ev)
            game.push_player_updated(uid, champ_id=champ_scid(uid))
    for combat_id, scid, champ, blockers in combats:
        game.push_combat_removed(combat_id, scid, champ, blockers)
    game.push_end_combat_resolution()
    if not first_strike:
        # End of combat: clear the combat states (Attacking / HasAttacked /
        # Blocking / HasBlocked) from every warzone troop — a Steadfast attacker
        # never tapped, so without this it would keep its "attacking" visuals
        # through the opponent's turn.  Tapped persists until the next Ready.
        # Only at the FINAL (normal) damage step, so the client still sees the
        # attackers/blockers engaged during the Swiftstrike step.
        combat_bits = (game_engine.ECardStates.Attacking |
                       game_engine.ECardStates.HasAttacked |
                       game_engine.ECardStates.Blocking |
                       game_engine.ECardStates.HasBlocked)
        _db.execute(
            "UPDATE game_cards SET card_state = card_state & ~? "
            "WHERE session_id=? AND location='warzone'",
            (combat_bits, session.session_id))
        _db.commit()
        for wzr in _db.execute(
                "SELECT card_uid, template_guid, user_id, card_state "
                "FROM game_cards WHERE session_id=? AND location='warzone'",
                (session.session_id,)).fetchall():
            cu, tpl, owner, st = wzr
            scid = game_engine.SessionCardId(game_engine.UID(cu))
            _tpl, ct, _n, _c, _a, _d, _g = handler._card_full_data(game, scid, tpl)
            from abilities.framework._shared import owner_uid as _ou
            owner_uid = _ou(owner, pl_t, ai_t, bstate)
            game.push_card_updated(
                scid, owner_uid, game_engine.ECardCollections.Warzone,
                ct, template_id=tpl,
                state=int(st or 0))
        # Combat deaths (a blocker/attacker that died) fired Deathcries onto the
        # chain; drain them now — a player-facing discard prompt persists via
        # pending_discard_* in bstate and resolves when the player answers it.
        if not _be.stack_empty(bstate):
            _be.stack_set_pass(bstate, _be.PLAYER, True)
            _be.stack_set_pass(bstate, _be.AI, True)
            while not _be.stack_empty(bstate):
                item = _be.stack_pop(bstate)
                _be.stack_reset_passes(bstate)
                handler._resolve_stack_item(session, pl_t, ai_t, bstate, item, game)
                if _be.stack_empty(bstate):
                    game.push_chain_empty()
            bstate["player_passed"] = False
            bstate["ai_passed"] = False
    # Deathcries from combat deaths: deferred so the blocker/attacker deaths
    # all happened during the simultaneous damage assignment above.  They are
    # IgnoresChain triggers and execute immediately, pushing their own events
    # (champion damage / deck search) after the combat envelope.
    if deferred_deaths:
        from abilities.framework.deathcry import resolve_deathcry
        for cu, tpl, _owner in deferred_deaths:
            resolve_deathcry(game, session, _db, handler, pl_t, ai_t, cu,
                             tpl, bstate)
    if not first_strike:
        bstate[attacker_key] = {}
        bstate.pop("ai_blockers", None)
    _be.save_state(session, bstate)
    if game.events:
        if send_events is not None:
            send_events(game, pl_t, ai_t, bstate)
        else:
            handler._send_battle_events(session, game, pl_t)
    return bstate


def resolve_ai_combat_damage(handler, session, pl_t, ai_t, bstate,
                             first_strike=False):
    """Resolve AI combat damage (the AI attacks the player). Thin wrapper over
    the shared resolve_combat with the AI as the attacker."""
    attackers = {int(k): int(v) for k, v in (bstate.get("ai_attackers") or {}).items()}
    blockers = {int(k): [int(b) for b in (v or [])]
                for k, v in (bstate.get("ai_blockers") or {}).items()}
    return resolve_combat(handler, session, pl_t, ai_t, bstate,
                          attackers, blockers, ai_t, pl_t, "ai_attackers",
                          first_strike=first_strike)


def combat_has_swiftstrike(db, session, bstate):
    """True when any attacking or blocking troop in the current combat has
    Swiftstrike (FirstStrike) or DualStrike — the client's
    Card.CaresAboutCombatPhase(FirstStrike) treats both as participating in
    the first-strike damage step, so AssignFirstStrikeDamage /
    FirstStrikePriorityWindow only occur then."""
    uids = set()
    for key in ("player_attackers", "ai_attackers"):
        uids.update(int(k) for k in (bstate.get(key) or {}))
    for blockers in (bstate.get("ai_blockers") or {}).values():
        uids.update(int(b) for b in blockers)
    if not uids:
        return False
    # Use the same effective-stat path as combat resolution. A QuickAction
    # grant (Ruby Aura) and a static/gem keyword may live outside the printed
    # template attributes, so checking only raw columns can remove the first-
    # strike phases before the grant is used.
    from abilities.framework.statics import effective_stats
    return any(
        effective_stats(db, session.session_id, bstate, uid)[2]
        & (game_engine.ECardAttributes.FirstStrike |
           game_engine.ECardAttributes.DualStrike)
        for uid in uids)

def run_ai_turn(handler, session, pl_t, ai_t, battle_state, start_idx=0):
    """Drive the AI's turn, one phase at a time.

    The AI has no client, so its actions are server-side. Each phase is
    pushed in its own packet, paused AI_PHASE_DELAY so the client renders
    it. If the current phase is a stop for the human (their opponent-turn
    stop settings), the server grants the human priority and returns; the
    human's pass resumes the AI turn from the next phase. At EndTurn the
    turn returns to the human.

    The AI logs holding priority and passing it as its default action for
    every phase it has nothing to do in.
    """
    import battle_engine as be
    # Resolve any pending chain left over from the player's turn before the AI
    # continues (the AI auto-passes; both sides count as passed).
    if not be.stack_empty(battle_state):
        be.stack_set_pass(battle_state, be.PLAYER, True)
        be.stack_set_pass(battle_state, be.AI, True)
        game = handler._fresh_game(session, pl_t, ai_t, battle_state)
        while not be.stack_empty(battle_state):
            item = be.stack_pop(battle_state)
            be.stack_reset_passes(battle_state)
            handler._resolve_stack_item(session, pl_t, ai_t, battle_state, item, game)
            if be.stack_empty(battle_state):
                game.push_chain_empty()
        handler._send_battle_events(session, game, pl_t)
        battle_state["player_passed"] = False
        battle_state["ai_passed"] = False
    if start_idx == 0:
        log_req("    AI begins its turn — turn=ai priority=ai")
        time.sleep(0.5)
        # ConsiderAttitutudeChange: the AI shifts Aggressive/Comfortable/
        # Defensive with its health relative to the opponent.
        try:
            import ai_eval as _att_ev
            _att = _att_ev.build_evaluator(handler, session, battle_state,
                                           ai_t, pl_t)
            _att.update_attitude()
            battle_state["ai_attitude"] = _att.personality.attitude
        except Exception:
            pass
        # Start with the base (no-combat) phase list; combat is added after Prep
        # when the AI has a ready troop to attack with (aggressive personality).
        battle_state["turn_phases"] = be.BASE_TURN_PHASES
        battle_state["player_has_ready_troop"] = False
        battle_state.pop("ai_attackers", None)
        be.save_state(session, battle_state)
        # Rebuild AI hand cards so the client sees them (face-down hand).
        game = handler._fresh_game(session, pl_t, ai_t, battle_state)
        rows = _db.execute(
            "SELECT card_uid, template_guid FROM game_cards "
            "WHERE session_id=? AND user_id=0 AND location='hand' ORDER BY position",
            (session.session_id,)).fetchall()
        for r in rows:
            scid = game_engine.SessionCardId(game_engine.UID(r[0]))
            handler._card_full_data(game, scid, r[1])
            t = handler._template_by_guid(r[1])
            ct = game_engine.card_type_from_db(t[1]) if t else game_engine.ECardTypes.Troop
            game.push_card_updated(scid, ai_t, game_engine.ECardCollections.Hand,
                                   ct, nulling=True)
        # Re-push both champion cards so the client's State.Cards cache has the
        # opponent champion BEFORE its StartTurn fires (OnTurnPhaseUpdated reads
        # State.Cards[ChampionSessionCardId] -> KeyNotFound if missing). Must
        # re-register the CardDefs on THIS fresh Game first, or the CardUpdated
        # carries zero abilities and UpdateAbilityButtons wipes the champion
        # charge/spell buttons.
        handler._push_champions_warm(session, pl_t, ai_t, battle_state, game)
        handler._send_battle_events(session, game, pl_t)

    idx = start_idx
    while True:
        phases = be.turn_phases(battle_state)
        if idx >= len(phases):
            break
        phase = phases[idx]
        battle_state["phase_idx"] = idx
        be.save_state(session, battle_state)
        # State check: a champion at 0 health ends the game at any phase, not
        # just after combat damage (e.g. the AI's own Fang of the Mountain God
        # damaging itself on its turn).
        if handler._check_champion_health(session, pl_t, ai_t, battle_state):
            return battle_state
        # No attackers declared: skip the remaining combat steps straight to
        # the AI's Second Main Phase (same rule as the human's turn).
        if (phase in be.COMBAT_STEPS and
                be.COMBAT_STEPS.index(phase) >= 2 and
                not (battle_state.get("ai_attackers") or {})):
            try:
                idx = phases.index(game_engine.ETurnPhases.SecondMainPhase, idx)
            except ValueError:
                pass
            battle_state["phase_idx"] = idx
            be.save_state(session, battle_state)
            continue
        # Swiftstrike damage steps only occur when an attacking or blocking
        # troop has Swiftstrike/DualStrike (mirrors the client's
        # DeclareDefensePriorityWindowState.GetNextTurnPhase).
        if phase in (game_engine.ETurnPhases.AssignFirstStrikeDamage,
                     game_engine.ETurnPhases.FirstStrikePriorityWindow):
            if not combat_has_swiftstrike(_db, session, battle_state):
                try:
                    idx = phases.index(game_engine.ETurnPhases.AssignDamage, idx)
                except ValueError:
                    pass
                battle_state["phase_idx"] = idx
                be.save_state(session, battle_state)
                continue
        game = handler._fresh_game(session, pl_t, ai_t, battle_state)
        game.push_turn_phase(phase, ai_t, ai_t)
        # The AI holds priority for its phases: an AI-targeted GreenLight makes
        # the client call LoseGreenLight (its PlayerId != the human's), clearing
        # the human's priority/pass button until the AI passes it over.
        game.push_green_light(ai_t, game_engine.EPriorityContext.Normal)
        # Replace the previous human PlayerOptionList while the AI owns
        # priority.  LoseGreenLight normally disables the controls, but the
        # client can retain an old Activate option until a fresh list arrives;
        # sending an explicit empty list prevents artifacts such as Taming
        # Sphere appearing activatable during the AI's turn.
        game.push_options(pl_t, [])
        if phase == game_engine.ETurnPhases.Draw:
            # The first player skips the first-turn draw.  When the AI won the
            # toss and chose Play, player_draws_first_turn is true because the
            # human is the draw-first player; the AI therefore must skip here.
            ai_draws_first_turn = not battle_state.get(
                "player_draws_first_turn", False)
            if (battle_state.get("turn_number", 1) > 1 or
                    ai_draws_first_turn) and handler._ai_draw_card(
                        game, session, ai_t, battle_state):
                # The AI drew from an empty deck and lost — stop the turn.
                return battle_state
        elif phase == game_engine.ETurnPhases.StartTurn:
            # Fire TurnStartedEvent triggers for AI warzone cards
            import ability as _abil
            _abil.resolve_triggers(
                _db, handler, game, session, pl_t, ai_t, battle_state,
                "TurnStartedEvent", None,
                source_owner_uid=0)
        elif phase == game_engine.ETurnPhases.Prep:
            battle_state["ai_resources"] = battle_state.get("ai_total_resources", 0)
            # Fresh AI turn: the 1-resource-per-turn flag resets so the AI can
            # play another threshold this turn.
            battle_state["ai_resource_played_this_turn"] = False
            from abilities.framework._shared import clear_expired_temporary_attributes
            clear_expired_temporary_attributes(
                _db, session.session_id, 0, "start_turn",
                clear_stat_buffs=True)
            # Clear summoning sickness on AI warzone troops (persist to DB).
            ai_wz = _db.execute(
                "SELECT card_uid, template_guid FROM game_cards "
                "WHERE session_id=? AND user_id=0 AND location='warzone'",
                (session.session_id,)).fetchall()
            for wzr in ai_wz:
                scid = game_engine.SessionCardId(game_engine.UID(wzr[0]))
                # Ready/untap: clear combat states + CameOutThisTurn; set
                # StartedATurnOnYourSide.
                attrs_row = _db.execute(
                    "SELECT temporary_attributes FROM game_cards "
                    "WHERE session_id=? AND card_uid=?",
                    (session.session_id, int(wzr[0]))).fetchone()
                clear_mask = (game_engine.ECardStates.CameOutThisTurn |
                              game_engine.ECardStates.Tapped |
                              game_engine.ECardStates.Attacking |
                              game_engine.ECardStates.HasAttacked |
                              game_engine.ECardStates.Blocking |
                              game_engine.ECardStates.HasBlocked)
                if attrs_row and int(attrs_row[0] or 0) & game_engine.ECardAttributes.CantReadyAutomatically:
                    clear_mask &= ~game_engine.ECardStates.Tapped
                _db.execute(
                    "UPDATE game_cards SET card_state = (card_state | ?) & ~?, card_damage = 0 "
                    "WHERE session_id=? AND card_uid=?",
                    (game_engine.ECardStates.StartedATurnOnYourSide,
                     clear_mask,
                     session.session_id, int(wzr[0])))
                # Populate CardDef so push_card_updated retains cost/atk/def/thresholds
                handler._card_full_data(game, scid, wzr[1])
                tpl = handler._template_by_guid(wzr[1])
                ct = game_engine.card_type_from_db(tpl[1]) if tpl else game_engine.ECardTypes.Troop
                from db import db_card_state_raw
                pstate = db_card_state_raw(session.session_id, int(wzr[0]))
                game.push_card_updated(scid, ai_t, game_engine.ECardCollections.Warzone, ct,
                                      template_id=wzr[1],
                                      state=(pstate if pstate is not None else
                                             game_engine.ECardStates.StartedATurnOnYourSide))
            _db.commit()
            clear_expired_temporary_attributes(
                _db, session.session_id, 0, "prep", clear_stat_buffs=True)
        elif phase in (game_engine.ETurnPhases.FirstMainPhase,
                       game_engine.ETurnPhases.SecondMainPhase):
            # Evaluator-driven main phases: evaluate the whole hand and play
            # the best affordable card (troop/constant/artifact/action),
            # one chain item at a time (the human gets a response window).
            # The resource is part of the same main-phase decision.  Do not
            # stop after playing it: the original AI immediately re-evaluates
            # its hand with the extra resource and can then build its board.
            pre_combat = (phase == game_engine.ETurnPhases.FirstMainPhase)
            guard = 0
            while guard < 8:
                guard += 1
                if not be.stack_empty(battle_state):
                    break
                if not battle_state.get("ai_resource_played_this_turn"):
                    # The client's BuildBoard always tries resources first.
                    handler._ai_play_resource(game, session, ai_t, battle_state)
                if not ai_main_phase_play(handler, game, session, ai_t, pl_t,
                                          battle_state, pre_combat=pre_combat):
                    # Nothing to play: if the AI still holds an unplayed
                    # resource this turn, generate it (client BuildBoard ->
                    # GenerateResource) so the next phase push can afford
                    # troops/actions.
                    if not battle_state.get("ai_resource_played_this_turn"):
                        handler._ai_play_resource(game, session, ai_t,
                                                  battle_state)
                        if battle_state.get("ai_resource_played_this_turn"):
                            break
                    break
                if not be.stack_empty(battle_state):
                    break
            # A troop played during First Main can still make this an attacking
            # turn (for example, a Speed troop), so switch to the combat phase
            # list before the loop advances past First Main. Do not rebuild the
            # list during Second Main: at that point combat has already been
            # passed, and replacing BASE_TURN_PHASES with COMBAT_TURN_PHASES
            # changes the meaning of the current numeric phase_idx (Second Main
            # becomes DeclareCombat), stranding the human's next pass as stale.
            if phase == game_engine.ETurnPhases.FirstMainPhase:
                battle_state["player_has_ready_troop"] = ai_can_attack_troops(handler, session)
                battle_state["turn_phases"] = be.build_turn_phases(battle_state)
                be.save_state(session, battle_state)
        elif phase in (game_engine.ETurnPhases.DeclareCombatPriorityWindow,
                       game_engine.ETurnPhases.DeclareAttackPriorityWindow,
                       game_engine.ETurnPhases.DeclareDefensePriorityWindow,
                       game_engine.ETurnPhases.FirstStrikePriorityWindow):
            # Combat trick (AICombat.GetCardToPlayInCombat): at the priority
            # windows the blockers are already declared — if one of our
            # combatants would lose its combat, play a QuickAction buff that
            # flips the outcome (kills the blocker / saves our troop).
            if ai_play_combat_trick(handler, game, session, ai_t, pl_t,
                                    battle_state):
                continue
        elif phase == game_engine.ETurnPhases.DeclareAttack:
            battle_state = ai_declare_attackers(handler, game, session, ai_t, pl_t, battle_state)
        elif phase == game_engine.ETurnPhases.DeclareDefense:
            # The player (defender) auto-declines to block; emit empty
            # BlockersAssigned so the client renders the AI attacking unblocked.
            battle_state = ai_pass_declare_defense(handler, session, pl_t, ai_t, battle_state, game)
        elif phase == game_engine.ETurnPhases.AssignFirstStrikeDamage:
            # Swiftstrike step: FirstStrike/DualStrike combatants deal damage
            # now; casualties are removed before the normal step.
            battle_state = resolve_ai_combat_damage(
                handler, session, pl_t, ai_t, battle_state, first_strike=True)
        elif phase == game_engine.ETurnPhases.AssignDamage:
            battle_state = resolve_ai_combat_damage(handler, session, pl_t, ai_t, battle_state)
        elif phase == game_engine.ETurnPhases.Discard:
            # Downsize the AI's hand at end of turn (max 7; campaign 10). The
            # Discard phase is otherwise a no-op — without this the AI's hand
            # grows forever once it stops playing cards.
            max_hand = handler._max_hand_size(session)
            from db import db_hand_card_count
            guard = 0
            while (db_hand_card_count(session.session_id, 0) > max_hand
                   and guard < 30):
                ai_discard_card(handler, game, session, pl_t, ai_t)
                guard += 1
            if guard >= 30:
                log_req("    AI discard loop guard tripped (hand still oversized)")
        elif phase == game_engine.ETurnPhases.EndTurn:
            # Switch the turn player back to the human. Reset the cycle so
            # the human's turn starts at StartTurn (phase_idx 0).
            # "At the end of your turn" triggers for the AI's cards.
            import ability as _abil_end
            _abil_end.resolve_triggers(
                _db, handler, game, session, pl_t, ai_t, battle_state,
                "TurnEndedEvent", None, 0)
            # "Until end of turn" attribute grants on the AI's cards expire now.
            from abilities.framework._shared import (
                clear_combat_damage, clear_expired_temporary_attributes)
            # Remove combat damage before expiring the AI's temporary
            # end-of-turn bonuses, matching the PvP cleanup ordering.
            clear_combat_damage(_db, session.session_id)
            clear_expired_temporary_attributes(
                _db, session.session_id, 0, "end_turn",
                clear_stat_buffs=True)
            for wzr in _db.execute(
                    "SELECT card_uid, template_guid, user_id FROM game_cards "
                    "WHERE session_id=? AND location='warzone'",
                    (session.session_id,)).fetchall():
                cu, tpl, card_user_id = wzr
                scid = game_engine.SessionCardId(game_engine.UID(cu))
                _tpl, ct, _n, _c, _a, _d, _g = handler._card_full_data(
                    game, scid, tpl)
                crow = _db.execute(
                    "SELECT card_state FROM game_cards WHERE session_id=? "
                    "AND card_uid=?", (session.session_id, cu)).fetchone()
                game.push_card_updated(
                    scid, ai_t if card_user_id == 0 else pl_t,
                    game_engine.ECardCollections.Warzone,
                    ct, template_id=tpl,
                    state=int(crow[0]) if crow else 0)
            next_player = be.next_turn_player(battle_state)
            battle_state["turn_player"] = next_player
            battle_state["turn_number"] = battle_state.get("turn_number", 1) + 1
            battle_state["player_passed"] = False
            battle_state["ai_passed"] = False
            battle_state["phase_idx"] = 0
            battle_state["turn_phases"] = be.BASE_TURN_PHASES
            battle_state.pop("ai_turn_phase_idx", None)
            if next_player == be.PLAYER:
                # A fresh player turn: reset the 1-resource-per-turn flag so
                # they can play a threshold again.
                battle_state["player_resource_played_this_turn"] = False
            else:
                # A bonus AI turn also gets a fresh resource play.
                battle_state["ai_resource_played_this_turn"] = False
            game.push_player_updated(ai_t, champ_id=getattr(handler, "_ai_champ_scid", None))
            be.save_state(session, battle_state)
            handler._send_battle_events(session, game, pl_t)
            if next_player == be.AI:
                log_req("    AI EndTurn: bonus turn kept AI in control")
                return run_ai_turn(handler, session, pl_t, ai_t,
                                   battle_state, start_idx=0)
            log_req(f"    AI EndTurn: turn handed back to player (turn={be.PLAYER} priority=none)")
            # Drive the human's new turn.
            handler._ai_turn_depth = getattr(handler, "_ai_turn_depth", 0) + 1
            if handler._ai_turn_depth <= 3:
                handler._advance_to_priority(session, pl_t, ai_t, battle_state)
            else:
                # Safety: if the human configured NO stops, both turns would
                # auto-advance forever. Force a stop at the first main phase.
                handler._ai_turn_depth = 0
                game = handler._fresh_game(session, pl_t, ai_t, battle_state)
                game.push_turn_phase(game_engine.ETurnPhases.FirstMainPhase, pl_t, pl_t)
                game.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
                game.push_player_updated(pl_t, champ_id=getattr(handler, "_player_champ_scid", None))
                handler._send_battle_events(session, game, pl_t)
                handler._push_main_phase_options(session, pl_t, ai_t)
                log_req("    SAFETY: forced stop at FirstMainPhase (no stops configured)")
            return
        # Every phase gets its own packet (Draw/FirstMain included — the
        # bug that left the client stuck at the previous phase). Re-sync the
        # Game's AI fields from battle state so the PlayerUpdated carries the
        # values AFTER any AI plays (a fresh game built earlier in this
        # iteration holds stale resources/charges).
        game.ai_resources = battle_state.get("ai_resources", 0)
        game.ai_total_resources = battle_state.get("ai_total_resources", 0)
        game.ai_charges = battle_state.get("ai_charges", 0)
        game.ai_spell_points = battle_state.get("ai_spell_points", 0)
        game.ai_threshold = dict(battle_state.get("ai_threshold", {}))
        game.ai_health = battle_state.get("ai_health", 10)
        game.player_health = battle_state.get("player_health", 20)
        # A combat-death deck-search prompt (Darkspire Priestess) was pushed in
        # the combat packet (resolve_combat sends its own).  PAUSE the AI turn
        # WITHOUT sending this phase packet — its TurnPhaseUpdated would tear
        # the client's target picker down ("priority passed while a non-root
        # state is active") right after the coverflow opens.  The answer
        # (SetAbilityActivationData) resumes the AI turn.
        if battle_state.get("pending_deck_search"):
            battle_state["ai_turn_phase_idx"] = idx + 1
            be.save_state(session, battle_state)
            log_req("    AI turn paused for deck-search answer")
            return battle_state
        game.push_player_updated(ai_t, champ_id=getattr(handler, "_ai_champ_scid", None))
        handler._send_battle_events(session, game, pl_t)
        log_req(f"    AI phase {phase}: AI has priority, passing (default action)")
        # The AI played a card (or a trigger went onto the chain) this phase:
        # the item sits in CastSpells until both players pass.  Hand priority
        # to the player so they can respond (Countermagic / instant actions);
        # the human's pass drains the chain when the AI turn resumes.
        if not be.stack_empty(battle_state):
            battle_state["ai_turn_phase_idx"] = idx + 1
            be.save_state(session, battle_state)
            handler._push_phase_options_empty(session, pl_t, ai_t)
            g2 = game_engine.Game(session.session_id, pl_t, ai_t)
            g2.push_green_light(pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
            handler._send_battle_events(session, g2, pl_t)
            log_req(f"    AI phase {phase}: card on chain — priority to player "
                    f"(waiting for response)")
            return
        # If this phase is a stop for the human, hand priority over and wait.
        if be.is_opp_stop(battle_state, phase):
            # If the human has NO eligible blockers at DeclareDefense (only
            # tapped troops, e.g. a Gemsoul Feeder that attacked), they can't
            # block — the AI's attackers go unblocked and the AI just advances
            # (no blocker UI, no priority handoff).
            if phase == game_engine.ETurnPhases.DeclareDefense and not handler._player_can_block(session):
                log_req("    No player blockers — DeclareDefense auto-passed (attackers unblocked)")
                time.sleep(AI_PHASE_DELAY)
                idx += 1
                continue
            battle_state["ai_turn_phase_idx"] = idx + 1
            be.save_state(session, battle_state)
            # The human gets priority during the AI's turn: push the GreenLight
            # PLUS a PlayerOptionList (QuickActions + champion abilities) so
            # instant-speed cards are playable in this window. Without the
            # options the client shows a GreenLight but nothing is clickable.
            # At DeclareDefense (the AI is attacking) the human instead declares
            # blockers, so push the Defend-usage options instead.
            if phase == game_engine.ETurnPhases.DeclareDefense:
                handler._push_blocker_options(session, pl_t, ai_t)
            else:
                handler._push_phase_options_empty(session, pl_t, ai_t)
            g = game_engine.Game(session.session_id, pl_t, ai_t)
            # At DeclareDefense the client only pushes BattleStateDeclareBlockers
            # when it receives a TurnPhaseUpdated with the PLAYER as priority
            # player (PushStateForPhase fires only for the priority player) — the
            # AI's own phase packets carry priority=ai, so re-announce the phase
            # with priority=player to open the blocker UI, then grant a plain
            # greenlight (ResolveTopOfChain would push InactivePriorityWindow on
            # top and hide the Skip/Block button).
            if phase == game_engine.ETurnPhases.DeclareDefense:
                g.push_turn_phase(phase, ai_t, pl_t)
                g.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
            else:
                # ResolveTopOfChain makes the client's GainGreenLight call
                # CheckForMissingPriorityWindowState, which pushes a
                # BattleStateInactivePriorityWindow — the only way the Pass
                # button renders during the opponent's turn.
                g.push_green_light(pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
            handler._send_battle_events(session, g, pl_t)
            # Option builders load the persisted state independently. Re-save
            # the AI stop marker after they have emitted their packet so a late
            # client resync always sees this exact phase and resume index.
            battle_state["phase_idx"] = idx
            battle_state["ai_turn_phase_idx"] = idx + 1
            be.save_state(session, battle_state)
            log_req(f"    AI phase {phase}: opponent stop — priority to player (waiting for pass)")
            return
        time.sleep(AI_PHASE_DELAY)
        idx += 1

def ai_draw_card(handler, game, session, ai_t, battle_state):
    """AI draws the top card of its deck into hand.  Returns True when the AI
    must draw with an empty deck (deck-out: the AI loses the game)."""
    import ability as _abil
    pl_t = game_engine.UID.make(244, int(handler.client_reck_id))
    turn = battle_state.get("turn_number", 1)
    if battle_state.get("ai_draws_turn") != turn:
        battle_state["ai_draws_this_turn"] = 0
        battle_state["ai_draws_turn"] = turn
    battle_state["ai_draws_this_turn"] = int(battle_state.get("ai_draws_this_turn", 0)) + 1
    rows = _db.execute(
        "SELECT id, card_uid, card_template_id, template_guid FROM game_cards "
        "WHERE session_id=? AND user_id=0 AND location='deck' ORDER BY position LIMIT 1",
        (session.session_id,)).fetchall()
    if not rows:
        # Deck-out: a player who must draw with an empty deck loses the game.
        import commands as _cmd
        _cmd.push_battle_game_end(handler=handler, session=session,
                                  winners=[pl_t], losers=[ai_t])
        if hasattr(handler, "_campaign_gameend"):
            handler._campaign_gameend(session, won=True)
        log_req("    Game over: AI deck empty on draw (player wins)")
        return True
    row = rows[0]
    # Replacement: "If you would draw a card..." / "If this would enter a hand..."
    repl_draw = _abil.resolve_triggers(
        _db, handler, game, session, pl_t, ai_t, battle_state,
        "CardWouldBeDrawnEvent", None, 0)
    repl_zone = _abil.resolve_triggers(
        _db, handler, game, session, pl_t, ai_t, battle_state,
        "CardWouldEnterZoneEvent", row[1], 0)
    if repl_draw or repl_zone:
        return
    card_uid = row[1]
    scid = game_engine.SessionCardId(game_engine.UID(card_uid))
    _db.execute("UPDATE game_cards SET location='hand', position=100 WHERE id=?", (row[0],))
    _db.commit()
    tpl_guid, ct, name, cost, atk, def_, _gem = handler._card_full_data(game, scid, row[3], row[2])
    game.push_card_moved(scid, ai_t, game_engine.ECardCollections.Hand,
                         game_engine.ECardLocations.Top, 1)
    game.push_card_drawn(scid, ai_t, 1)
    game.push_card_updated(scid, ai_t, game_engine.ECardCollections.Hand, ct,
                           nulling=True)
    # A draw also emits the normal zone-entry event.  Reginald's granted
    # ability listens for CardEnteredZoneEvent (Hand|Discard), not merely
    # CardDrawnEvent, so it must resolve before the draw trigger pass.
    _abil.resolve_triggers(
        _db, handler, game, session, pl_t, ai_t, battle_state,
        "CardEnteredZoneEvent", card_uid, 0)
    # Fire "when you draw" triggers — the client's CardDrawnEvent source is the
    # drawing champion, the target is the drawn card.
    ai_champ_scid = getattr(handler, "_ai_champ_scid", None) or game_engine.SessionCardId(ai_t)
    _abil.resolve_triggers(
        _db, handler, game, session, pl_t, ai_t, battle_state,
        "CardDrawnEvent", int(ai_champ_scid.uid.uid64), 0,
        extra_target=card_uid)
    log_req(f"    AI drew card {card_uid} ({name})")

def ai_play_resource(handler, game, session, ai_t, battle_state):
    """AI plays a resource card from hand during FirstMainPhase if it can."""
    if battle_state.get("ai_resource_played_this_turn"):
        return
    rows = _db.execute(
        "SELECT gc.id, gc.card_uid, gc.template_guid, "
        "COALESCE(ct.current_resources_granted, 0), "
        "COALESCE(ct.max_resources_granted, 0) FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='hand' "
        "AND gc.card_type='Resource' "
        "ORDER BY gc.position LIMIT 1",
        (session.session_id,)).fetchall()
    if not rows:
        return
    row = rows[0]
    card_uid = row[1]
    cur_grant = int(row[3] or 0)
    max_grant = int(row[4] or 0)
    scid = game_engine.SessionCardId(game_engine.UID(card_uid))
    # Both resource branches dispatch GainChargeEvent.  The player UID is
    # needed by that shared trigger path even when this is not Shards of Fate.
    pl_t = game_engine.UID.make(
        244, int(getattr(handler, "client_reck_id", 0) or 0))
    # Shards of Fate ("Choose a Standard resource in your deck. Gain the
    # thresholds it provides.") — data-driven detection; the AI picks a random
    # Standard resource from its deck, gains that threshold, and grants the
    # template's resource fields (m_MaxResourcesGranted=1 -> +1 max resources only).
    shard_ability = shard_tpl = None
    ab_row = _db.execute(
        "SELECT abilities_json FROM card_templates WHERE guid=?",
        (row[2],)).fetchone()
    if ab_row and ab_row[0]:
        try:
            _ai_ags = json.loads(ab_row[0])
        except Exception:
            _ai_ags = []
        if hasattr(handler, "_shards_of_fate_template"):
            shard_ability, shard_tpl = handler._shards_of_fate_template(
                _ai_ags)
    _db.execute("UPDATE game_cards SET location='PlayedResources', position=9999 WHERE id=?", (row[0],))
    _db.commit()
    battle_state["ai_resource_played_this_turn"] = True
    if shard_tpl:
        battle_state["ai_total_resources"] = (
            battle_state.get("ai_total_resources", 0) + max_grant)
        battle_state["ai_resources"] = (
            battle_state.get("ai_resources", 0) + cur_grant)
        battle_state["ai_charges"] = battle_state.get("ai_charges", 0) + 1
        game.ai_charges = battle_state["ai_charges"]
        ev_chg = game_engine.ChampionChargePointsChangedSessionEventArgs()
        ev_chg.player_id = ai_t
        ev_chg.operation = 1
        ev_chg.delta = 1
        ev_chg.new_value = battle_state["ai_charges"]
        game._push(ev_chg)
        from abilities.framework.triggers import resolve_gain_charge_triggers
        resolve_gain_charge_triggers(
            _db, handler, game, session, pl_t, ai_t, battle_state, 0)
        handler._resolve_shards_of_fate(
            game, session, pl_t, ai_t, battle_state, card_uid,
            shard_ability, shard_tpl, 0)
        import battle_engine as _be
        _be.save_state(session, battle_state)
        log_req(f"    AI played Shards of Fate {card_uid} "
                f"(+{max_grant} max/+{cur_grant} current, threshold gained)")
        return
    battle_state["ai_total_resources"] = (
        battle_state.get("ai_total_resources", 0) + max_grant)
    battle_state["ai_resources"] = (
        battle_state.get("ai_resources", 0) + cur_grant)
    t = handler._template_by_guid(row[2])
    # Move the card to PlayedResources in the client's cache FIRST (per
    # HOWTO: send CardUpdated with the new collection before the move
    # event), so the shard doesn't linger on the stack/chain.
    game.push_card_updated(scid, ai_t, game_engine.ECardCollections.PlayedResources,
                           game_engine.ECardTypes.Resource,
                           template_id=t[0] if t else None)
    game.push_resource_card_played(scid, ai_t, free=False)
    ev_cur = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
    ev_cur.player_id = ai_t; ev_cur.operation = 1; ev_cur.delta = cur_grant
    ev_cur.new_value = battle_state["ai_resources"]; game._push(ev_cur)
    ev_tot = game_engine.PlayerTotalResourcePoolChangedSessionEventArgs()
    ev_tot.player_id = ai_t; ev_tot.operation = 1; ev_tot.delta = max_grant
    ev_tot.new_value = battle_state["ai_total_resources"]; game._push(ev_tot)
    # Threshold display: give the shard's colour a count of 1.
    color = game_engine.ECardShards.Wild
    col_map = {'Ruby': game_engine.ECardShards.Ruby,
               'Sapphire': game_engine.ECardShards.Sapphire,
               'Blood': game_engine.ECardShards.Blood,
               'Diamond': game_engine.ECardShards.Diamond,
               'Wild': game_engine.ECardShards.Wild}
    color = col_map.get(str(t[2] if t else "").split()[0], game_engine.ECardShards.Wild)
    ev_th = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
    ev_th.player_id = ai_t; ev_th.color = color; ev_th.operation = 1; ev_th.delta = 1
    ev_th.new_value = battle_state["ai_threshold"].get(color, 0) + 1
    battle_state["ai_threshold"][color] = ev_th.new_value
    game._push(ev_th)
    # Playing a basic threshold grants the champion a charge point.
    battle_state["ai_charges"] = battle_state.get("ai_charges", 0) + 1
    game.ai_charges = battle_state["ai_charges"]
    import battle_engine as _be
    _be.save_state(session, battle_state)
    ev_chg = game_engine.ChampionChargePointsChangedSessionEventArgs()
    ev_chg.player_id = ai_t; ev_chg.operation = 1; ev_chg.delta = 1
    ev_chg.new_value = battle_state["ai_charges"]
    game._push(ev_chg)
    from abilities.framework.triggers import resolve_gain_charge_triggers
    resolve_gain_charge_triggers(
        _db, handler, game, session, pl_t, ai_t, battle_state, 0)
    _be.save_state(session, battle_state)
    log_req(f"    AI played resource {card_uid} (charge={battle_state['ai_charges']})")

def ai_play_troop(handler, game, session, ai_t, battle_state):
    """AI plays an affordable troop from hand during FirstMainPhase."""
    import battle_engine as _be
    # One chain item at a time: if a previous play/trigger is still pending,
    # the AI waits for the player's response before playing again.
    if not _be.stack_empty(battle_state):
        return
    resources = battle_state.get("ai_resources", 0)
    threshold = battle_state.get("ai_threshold", {})
    rows = _db.execute(
        "SELECT gc.id, gc.card_uid, gc.template_guid, ct.cost, ct.card_type, ct.threshold_json, ct.attack, ct.defense "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='hand' "
        "AND (ct.card_type LIKE '%Troop%' OR ct.card_type LIKE '%Artifact%' "
        "OR ct.card_type LIKE '%Constant%') "
        "ORDER BY gc.position",
        (session.session_id,)).fetchall()
    for row in rows:
        cost, ct, thresh_json, atk, def_ = row[3], row[4], row[5], row[6], row[7]
        if cost is not None and cost <= resources:
            if handler._thresholds_met(thresh_json, threshold):
                tid = row[1]
                scid = game_engine.SessionCardId(game_engine.UID(tid))
                # Push to CastSpells (the chain).  The card stays there until
                # the player passes; the player gets a priority window to
                # respond (counter, etc.) before the item resolves.
                _db.execute(
                    "UPDATE game_cards SET location='CastSpells' WHERE session_id=? AND card_uid=?",
                    (session.session_id, tid))
                _db.commit()
                battle_state["ai_resources"] = resources - cost
                # _card_full_data fills game.card_defs with thresholds/abilities/gems
                tpl_g, ct_n, nm, cost2, atk2, def2, gem2 = handler._card_full_data(
                    game, scid, row[2], row[0])
                # Push chain events
                game.push_card_updated(scid, ai_t, game_engine.ECardCollections.CastSpells,
                                      game_engine.card_type_from_db(ct),
                                      template_id=row[2], cost=cost2, attack=atk2, defense=def2, gems=gem2)
                game.push_card_moved(scid, ai_t, game_engine.ECardCollections.CastSpells,
                                    game_engine.ECardLocations.Top, 0)
                # Tell the client this is a normal card play. UIBattle only
                # animates AbilityPushedOnChain when AbilityTemplateId exists
                # in TemplateManager.Abilities; a card-template GUID is not an
                # ability and leaves AI plays invisible in the chain view.
                game.push_ability_on_chain(
                    scid, game_engine.ResourceId.from_str(
                        game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID))
                # Hold the troop on the chain: the stack item resolves to the
                # warzone (with Deploy/Inspire triggers) when both pass.
                inst_id = int(battle_state.get("_next_instance_id", 1))
                battle_state["_next_instance_id"] = inst_id + 1
                _be.stack_push(battle_state, {
                    "kind": "troop", "source_uid": int(tid),
                    "instance_id": inst_id,
                })
                # Reflect the spent resources in the AI's pool (the DB changed;
                # push the change to the view). The tail PlayerUpdated reads
                # game.ai_resources, so keep that in sync too.
                game.ai_resources = battle_state["ai_resources"]
                ev_cur = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
                ev_cur.player_id = ai_t; ev_cur.operation = 2; ev_cur.delta = cost
                ev_cur.new_value = battle_state["ai_resources"]; game._push(ev_cur)
                _be.save_state(session, battle_state)
                log_req(f"    AI played troop {row[2][:8]} to chain (cost={cost2}, resources left={battle_state['ai_resources']})")
                return


def ai_play_hand_card(handler, game, session, ai_t, battle_state, card,
                      evaluator=None, x_cost=None, target_uid=None):
    """Play any hand card chosen by the evaluator (troop, constant, artifact,
    basic action) onto the chain.  Mirrors the push pattern of ai_play_troop /
    ai_play_spell: CastSpells -> CardUpdated/CardMoved -> AbilityPushedOnChain
    -> stack item, held until both players pass so the human gets a response
    window (countermagic etc.)."""
    import json as _j
    import battle_engine as _be
    if not _be.stack_empty(battle_state):
        return
    resources = int(battle_state.get("ai_resources", 0))
    cost = int(card.cost or 0)
    # Preserve an X value selected by the evaluator (removal/sweeper paths
    # calculate it before calling this function).  The previous unconditional
    # reset silently converted every X spell into an X=0 cast.
    x_cost = int(x_cost or 0)
    if card.is_action() and not card.is_troop():
        if target_uid is None and evaluator is not None:
            target_uid = evaluator.choose_action_target(card)
        if card.variable_cost and x_cost <= 0:
            # "1X" costs X+1; pay the minimum the AI is willing to commit.
            min_x = 3 if evaluator is None else evaluator.personality.minimum_x_value
            affordable = resources - cost - 1
            x_cost = max(0, min(min_x, affordable))
    x_cost = int(x_cost or 0)
    target_uid = int(target_uid) if target_uid else None
    total = cost + x_cost
    if total > resources:
        log_req(f"    AI cannot afford {card.name} ({total}>{resources})")
        return
    tid = int(card.card_uid)
    scid = game_engine.SessionCardId(game_engine.UID(tid))
    _db.execute("UPDATE game_cards SET location='CastSpells' "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, tid))
    _db.commit()
    battle_state["ai_resources"] = resources - total
    tpl_g, ct_n, nm, cost2, atk2, def2, gem2 = handler._card_full_data(
        game, scid, card.template_guid, None)
    game.push_card_updated(
        scid, ai_t, game_engine.ECardCollections.CastSpells,
        game_engine.card_type_from_db(card.card_type),
        template_id=card.template_guid, cost=cost2, attack=atk2, defense=def2,
        gems=gem2)
    game.push_card_moved(scid, ai_t, game_engine.ECardCollections.CastSpells,
                         game_engine.ECardLocations.Top, 0)
    game.push_ability_on_chain(
        scid, game_engine.ResourceId.from_str(
            game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID))
    inst_id = int(battle_state.get("_next_instance_id", 1))
    battle_state["_next_instance_id"] = inst_id + 1
    # Troops and other permanents resolve to the warzone.  Constants such as
    # Daybreak are not actions: treating them as ``spell`` items sends them to
    # the discard after resolution and silently loses their ongoing trigger.
    if card.is_troop() or card.is_artifact() or card.is_constant():
        _be.stack_push(battle_state, {
            "kind": "troop", "source_uid": tid, "instance_id": inst_id,
        })
    else:
        _be.stack_push(battle_state, {
            "kind": "spell", "source_uid": tid,
            "ability_guids": card.ability_guids,
            "target_uid": target_uid,
            "instance_id": inst_id, "x_cost": x_cost,
        })
    game.ai_resources = battle_state["ai_resources"]
    ev_cur = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
    ev_cur.player_id = ai_t
    ev_cur.operation = 2
    ev_cur.delta = total
    ev_cur.new_value = battle_state["ai_resources"]
    game._push(ev_cur)
    _be.save_state(session, battle_state)
    log_req(f"    AI played {card.name} ({card.template_guid[:8]}) to chain "
            f"(cost={cost2}+{x_cost}, target={hex(int(target_uid)) if target_uid else 'none'}, "
            f"resources left={battle_state['ai_resources']})")


def ai_consider_removal(handler, game, session, ai_t, pl_t, battle_state, ev):
    """Port of AITactical.BuildBoard's removal step + AttemptToRemove: if we
    hold removal and the opponent controls a threatening target (dangerous
    troop, non-troop permanent, high-value/legendary card, or the champion),
    play the best removal that answers it."""
    if not ev.have_removal():
        return False
    for target in ev.threatening_targets():
        removal, x_cost, target_uid = ev.find_removal_for(target)
        if removal is None:
            continue
        if ev.is_playable(removal) == "NeedsResources":
            return False  # GenerateResource happens in the main loop
        # Cache the concrete target so ai_play_hand_card targets correctly.
        card = removal
        if card.variable_cost and not card.is_troop() and x_cost:
            ai_play_hand_card(handler, game, session, ai_t, battle_state, card,
                              evaluator=ev, x_cost=x_cost,
                              target_uid=target_uid)
            return True
        ai_play_hand_card(handler, game, session, ai_t, battle_state, card,
                          evaluator=ev, target_uid=target_uid)
        log_req(f"    AI removal: {card.name} -> "
                f"{target.name if target else 'champion'} "
                f"(x={x_cost})")
        return True
    return False


def ai_main_phase_play(handler, game, session, ai_t, pl_t, battle_state,
                       pre_combat=True):
    """One decision from the client's AIHandleMainPhase:
    BurnToWin -> removal (BuildBoard/AttemptToRemove) -> best board builder.
    Returns True when a card went onto the chain (the caller re-enters on the
    next phase push), False when the AI should pass."""
    import ai_eval as _aieval
    try:
        ev = _aieval.build_evaluator(handler, session, battle_state, ai_t,
                                     pl_t)
    except Exception as exc:
        log_req(f"    ai_eval init error: {exc!r}")
        return False
    # 1) BurnToWin: a damage spell that finishes the opponent.
    burn = ev.burn_to_win()
    if burn is not None:
        ai_play_hand_card(handler, game, session, ai_t, battle_state, burn,
                          evaluator=ev)
        log_req(f"    AI BurnToWin: {burn.name}")
        return True
    # 1a) ConsiderSweeping: a board-wipe that trades up.
    sweeper = ev.best_sweeper()
    if sweeper is not None:
        card, x_cost = sweeper
        ai_play_hand_card(handler, game, session, ai_t, battle_state, card,
                          evaluator=ev, x_cost=x_cost)
        log_req(f"    AI sweep: {card.name} (x={x_cost})")
        return True
    # 1b) Champion ability (UseAbilities): summon/buff/heal/burn powers.
    if ai_use_champion_ability(handler, game, session, ai_t, pl_t,
                               battle_state):
        return True
    # 1c) Manual warzone troop abilities (AIAbilityManager thunks).
    if ai_use_warzone_ability(handler, game, session, ai_t, pl_t,
                            battle_state):
        return True
    # 2) Removal step (BuildBoard): answer a threatening permanent.
    lockdown = ev.lockdown_removal()
    if lockdown is not None:
        card, tgt = lockdown
        ai_play_hand_card(handler, game, session, ai_t, battle_state, card,
                          evaluator=ev, target_uid=tgt)
        log_req(f"    AI lockdown: {card.name} -> threat")
        return True
    if ai_consider_removal(handler, game, session, ai_t, pl_t, battle_state,
                           ev):
        return True
    # 3) Best board builder (troop/constant/artifact/basic action).
    best = ev.get_best_board_builder(pre_combat, include_resources=False)
    if best is None:
        if not pre_combat and ai_use_warzone_ability(
                handler, game, session, ai_t, pl_t, battle_state,
                include_non_troops=True, resource_sink=True):
            return True
        return False
    if ev.is_playable(best) != "True":
        if not pre_combat and ai_use_warzone_ability(
                handler, game, session, ai_t, pl_t, battle_state,
                include_non_troops=True, resource_sink=True):
            return True
        return False
    ai_play_hand_card(handler, game, session, ai_t, battle_state, best,
                      evaluator=ev)
    return True


def ai_use_champion_ability(handler, game, session, ai_t, pl_t, battle_state):
    """Port of AITactical.UseAbilities for the AI champion: scan the AI's
    charge powers (champion_abilities gamedata), decide if one is worth
    activating now (summon / buff / heal / burn / draw), pick a target from
    the BOM, pay the charge cost, and push the ability onto the chain exactly
    like the human path.  Returns True when an ability went on the chain."""
    import json as _j
    import battle_engine as _be
    ags = getattr(handler, "_ai_champ_ability_guids", None) or []
    if not ags:
        return False
    charges = int(battle_state.get("ai_charges", 0))
    from db import db_champion_ability_costs, db_champion_ability_thresholds
    ai_champ_scid = getattr(handler, "_ai_champ_scid", None)
    if ai_champ_scid is None:
        return False
    for ag in ags:
        ag = str(ag)
        cost_row = db_champion_ability_costs(ag)
        if not cost_row:
            continue
        cc = int(cost_row[0] or 0)
        # The champion ability list also contains triggered/passive abilities
        # (including StartOfGame abilities). They are not activatable powers,
        # even when their zero costs make them look affordable. Prefer the
        # explicit metadata flags; the positive-cost fallback protects older
        # champion rows whose trigger metadata was not seeded.
        meta = _db.execute(
            "SELECT is_triggered, is_manual, trigger_event_type "
            "FROM card_abilities_meta WHERE ability_guid=? LIMIT 1",
            (ag,)).fetchone()
        if (meta and (int(meta[0] or 0) or meta[2] or
                      not int(meta[1] or 0))):
            continue
        if cc <= 0 and int(cost_row[1] or 0) <= 0:
            continue
        if charges < cc:
            continue
        try:
            handler._resolving_ai_champion = True
            met = handler._champion_thresholds_met(ag, battle_state)
        finally:
            handler._resolving_ai_champion = False
        if not met:
            continue
        effects = _db.execute(
            "SELECT effect_type, param FROM ability_effects "
            "WHERE ability_guid=? ORDER BY effect_order", (ag,)).fetchall()
        params = []
        for etype, pm in effects:
            try:
                params.append((etype, _j.loads(pm) if pm else {}))
            except Exception:
                params.append((etype, {}))
        # ---- classify the ability from its BOM ----------------------------
        summons = [pm for t, pm in params
                   if t == "SummonTokenTroopAbilityEffectTemplate"]
        grants = [pm for t, pm in params
                  if t == "GrantAbilityEffectTemplate"]
        heals = [pm for t, pm in params
                 if t == "CardModifierAbilityEffectTemplate"
                 and (pm.get("property") or "").lower() in
                 ("healhero", "heal")]
        damages = [pm for t, pm in params
                   if t == "CardModifierAbilityEffectTemplate"
                   and (pm.get("property") or "").lower() in
                   ("damage", "damagehero")]
        attribute_grants = [pm for t, pm in params
                           if t == "CardModifierAbilityEffectTemplate"
                           and (pm.get("property") or "").lower()
                           in ("attribute", "attack", "defense")]
        ready_lock = any(
            (pm.get("property") or "").lower() == "attribute"
            and pm.get("duration") == "AfterCardsReadyOnPlayersTurn"
            for pm in attribute_grants)
        draws = [pm for t, pm in params
                 if t == "DrawNCardsAbilityEffectTemplate"]
        target_uid = None
        worth = False
        ai_health = int(battle_state.get("ai_health", 20))
        player_health = int(battle_state.get("player_health", 20))
        if summons:
            # Summon a token: worth it when we have no troop advantage and
            # aren't about to die (Poca's Blaze Elemental, Bun'jitsu's
            # Abomination, Angel of Dawn).
            ai_troops = _db.execute(
                "SELECT COUNT(*) FROM game_cards WHERE session_id=? "
                "AND user_id=0 AND location='warzone' "
                "AND card_type LIKE '%Troop%'",
                (session.session_id,)).fetchone()[0]
            pl_troops = _db.execute(
                "SELECT COUNT(*) FROM game_cards WHERE session_id=? "
                "AND user_id=? AND location='warzone' "
                "AND card_type LIKE '%Troop%'",
                (session.session_id,
                 (handler.user_profile or {}).get("id", 5))).fetchone()[0]
            worth = ai_troops <= pl_troops + 1 or ai_health <= 8
        if heals and ai_health <= 14:
            worth = True
        if draws and len(_db.execute(
                "SELECT 1 FROM game_cards WHERE session_id=? AND user_id=0 "
                "AND location='hand'", (session.session_id,)).fetchall()) <= 4:
            worth = True
        if damages:
            # Direct-damage power: burn for lethal or kill a threat.
            for pm in damages:
                amount = int(pm.get("amount", 0) or 0)
                text = (pm.get("text") or "").lower()
                m = __import__("re").search(r'deal\s+(\d+)\s+damage', text)
                if m:
                    amount = int(m.group(1))
                if amount >= player_health:
                    worth = True
                    break
            if not worth:
                # Kill the weakest opposing troop the damage reaches.
                troops = _db.execute(
                    "SELECT gc.card_uid, ct.defense, gc.card_defense_mod, "
                    "gc.card_damage FROM game_cards gc "
                    "JOIN card_templates ct ON ct.guid=gc.template_guid "
                    "WHERE gc.session_id=? AND gc.user_id=? "
                    "AND gc.location='warzone' AND gc.card_type LIKE '%Troop%'",
                    (session.session_id,
                     (handler.user_profile or {}).get("id", 5))).fetchall()
                for cu, bdef, dmod, dmg in troops:
                    eff = (bdef or 0) + (dmod or 0) - (dmg or 0)
                    if 0 < eff <= amount:
                        worth = True
                        target_uid = int(cu)
                        break
                if not worth and troops:
                    worth = True  # chip the champion
        if (attribute_grants or grants) and not (
                summons or heals or damages or draws):
            # Use the champion ability's target template when available. In
            # particular, S.P.A.M. Bot's power targets a Robot, not merely
            # any troop. This also covers metadata-only value effects such as
            # Whispering Breeze's GrantAbility + Prophesied marker, whose BOM
            # has no direct stat/heal/damage leaf for the AI classifier to see.
            # Keep the older troop heuristic only for champion rows whose
            # target metadata predates target-template extraction.
            target_row = _db.execute(
                "SELECT target_template_ids FROM champion_abilities "
                "WHERE ability_guid=? LIMIT 1", (ag,)).fetchone()
            target_template_ids = []
            if target_row and target_row[0]:
                try:
                    target_template_ids = [str(t).lower() for t in
                                           (_j.loads(target_row[0]) or []) if t]
                except (TypeError, ValueError):
                    target_template_ids = []
            if target_template_ids:
                from abilities.framework.targeting import legal_targets
                candidates = []
                for target_template_id in target_template_ids:
                    candidates = legal_targets(
                        _db, session.session_id, 0, target_template_id,
                        ai_champ_scid.uid.uid64, both_players=False,
                        champions=[], battle_state=battle_state)
                    if candidates:
                        break
                if ready_lock and not candidates:
                    # Some older target-filter snapshots cannot evaluate the
                    # MultiplePlayers/Warzone filter for AI-owned abilities.
                    # The effect metadata still identifies the opposing troop
                    # collection, so use that authoritative zone as fallback.
                    candidates = [r[0] for r in _db.execute(
                        "SELECT card_uid FROM game_cards WHERE session_id=? "
                        "AND user_id=? AND location='warzone' "
                        "AND card_type LIKE '%Troop%'",
                        (session.session_id,
                         (handler.user_profile or {}).get("id", 5))).fetchall()]
                if candidates:
                    marks = ",".join("?" for _ in candidates)
                    best = _db.execute(
                        "SELECT gc.card_uid, ct.attack FROM game_cards gc "
                        "JOIN card_templates ct ON ct.guid=gc.template_guid "
                        f"WHERE gc.session_id=? AND gc.card_uid IN ({marks}) "
                        "ORDER BY ct.attack DESC, gc.position LIMIT 1",
                        [session.session_id] + [int(uid) for uid in candidates]
                    ).fetchone()
                    if best is not None:
                        worth = True
                        target_uid = int(best[0])
            else:
                best = _db.execute(
                    "SELECT gc.card_uid, ct.attack FROM game_cards gc "
                    "JOIN card_templates ct ON ct.guid=gc.template_guid "
                    "WHERE gc.session_id=? AND gc.user_id=0 "
                    "AND gc.location='warzone' AND gc.card_type LIKE '%Troop%' "
                    "AND (gc.card_state & ?) = 0 "
                    "ORDER BY ct.attack DESC LIMIT 1",
                    (session.session_id,
                     game_engine.ECardStates.Tapped)).fetchone()
                if best is not None:
                    worth = True
                    target_uid = int(best[0])
        if not worth:
            continue
        # Champion powers can have card-payment costs in addition to charge
        # points.  The human activation path resolves these from the raw
        # AbilityTemplate (for example Blood Cauldron Ritualist's
        # ``m_SacrificeTarget``); the AI must pay the same metadata-defined
        # costs before putting the ability on the chain.  Keep payment cards
        # out of the effect target set, matching the client's target-map
        # ordering and the human selector.
        sacrifice_uids = []
        cost_templates = getattr(handler, "_ability_cost_templates", None)
        if cost_templates is not None:
            from abilities.framework.targeting import legal_targets
            used_targets = {int(target_uid)} if target_uid else set()
            for target_template_id, cost_type in cost_templates(ag):
                if int(cost_type) != 2:  # EAbilityCostType.Sacrifice
                    continue
                candidates = [int(uid) for uid in legal_targets(
                    _db, session.session_id, 0, target_template_id,
                    ai_champ_scid.uid.uid64, both_players=False,
                    champions=[], battle_state=battle_state)
                    if int(uid) not in used_targets]
                if not candidates:
                    # The ability cannot be activated if its additional cost
                    # cannot be paid, or if paying it would remove the only
                    # legal effect target.
                    continue
                # Sacrifice the least valuable eligible troop and preserve
                # the strongest legal troop as the effect target where the
                # authored target contracts require separate cards.
                marks = ",".join("?" for _ in candidates)
                row = _db.execute(
                    "SELECT gc.card_uid FROM game_cards gc "
                    "JOIN card_templates ct ON ct.guid=gc.template_guid "
                    f"WHERE gc.session_id=? AND gc.card_uid IN ({marks}) "
                    "ORDER BY ct.attack, ct.defense, gc.position LIMIT 1",
                    [session.session_id] + candidates).fetchone()
                sacrifice_uid = int(row[0]) if row else candidates[0]
                sacrifice_uids.append(sacrifice_uid)
                used_targets.add(sacrifice_uid)
            # A cost template that had no legal candidate is a hard failure,
            # whereas no sacrifice templates simply leaves this list empty.
            sacrifice_cost_count = sum(
                1 for _tid, ctype in cost_templates(ag) if int(ctype) == 2)
            if len(sacrifice_uids) != sacrifice_cost_count:
                continue
        # ---- pay + push (mirror the human ability-activation path) -------
        for sacrifice_uid in sacrifice_uids:
            handler._sacrifice_troop(
                game, session, pl_t, ai_t, sacrifice_uid)
        battle_state["ai_charges"] = charges - cc
        _be.save_state(session, battle_state)
        game.ai_charges = battle_state["ai_charges"]
        ev_chg = game_engine.ChampionChargePointsChangedSessionEventArgs()
        ev_chg.player_id = ai_t
        ev_chg.operation = 2
        ev_chg.delta = cc
        ev_chg.new_value = battle_state["ai_charges"]
        game._push(ev_chg)
        src_uid = ai_champ_scid.uid.to_uint64() if hasattr(
            ai_champ_scid, "uid") else 0
        inst_id = int(battle_state.get("_next_instance_id", 1))
        battle_state["_next_instance_id"] = inst_id + 1
        _be.stack_push(battle_state, {
            "kind": "ability", "ability_guid": ag,
            "source_uid": src_uid, "target_uid": target_uid,
            "instance_id": inst_id,
        })
        game.push_ability_on_chain(
            ai_champ_scid, game_engine.ResourceId.from_str(ag))
        _be.save_state(session, battle_state)
        log_req(f"    AI champion ability {ag[:8]} on chain "
                f"(charges {charges}->{battle_state['ai_charges']}, "
                f"target={hex(target_uid) if target_uid else 'none'}, "
                f"sacrifice={[hex(uid) for uid in sacrifice_uids]})")
        return True
    return False


def ai_use_warzone_ability(handler, game, session, ai_t, pl_t, battle_state,
                            include_non_troops=False, resource_sink=False):
    """Activate a worthwhile manual ability on an AI warzone permanent.

    The normal call scans troops for tactical activations.  The optional
    ``resource_sink`` call also scans non-troop permanents during Second Main so
    variable-resource abilities (for example Soul Marble) can consume the
    resources the AI cannot use from hand.  Both paths use the same metadata
    and BOM resolver.
    """
    import json as _j
    import battle_engine as _be
    if not _be.stack_empty(battle_state):
        return False
    if (resource_sink and
            _be.current_phase(battle_state) != game_engine.ETurnPhases.SecondMainPhase):
        return False
    resources = int(battle_state.get("ai_resources", 0))
    permanent_filter = ("" if include_non_troops else
                        "AND gc.card_type LIKE '%Troop%'\n        ")
    troops = _db.execute(
        "SELECT gc.card_uid, gc.template_guid, gc.card_state, ct.attributes, "
        "       gc.card_attributes, gc.card_abilities "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='warzone' "
        + permanent_filter,
        (session.session_id,)).fetchall()
    from abilities.framework.statics import effective_stats
    for uid, tpl, cstate, t_attrs, c_attrs, card_ab in troops:
        cstate = int(cstate or 0)
        attrs = (t_attrs or 0) | (c_attrs or 0)
        try:
            ags = [str(g).lower() for g in _j.loads(card_ab or '[]')]
        except Exception:
            ags = []
        for ag in ags:
            meta = _db.execute(
                "SELECT activation_cost, uses_per_game, uses_per_turn, "
                "exhausts_on_use, is_manual, raw_json "
                "FROM card_abilities_meta WHERE ability_guid=?",
                (ag,)).fetchone()
            if not meta or not meta[4]:
                continue
            cost = int(meta[0] or 0)
            exh = int(meta[3] or 0)
            variable_x, variable_min = handler._ability_x_cost_metadata(ag)
            x_cost = 0
            if variable_x and resource_sink:
                # A sink spends all resources that remain after its fixed
                # activation cost. The ability metadata supplies the floor.
                x_cost = max(0, resources - cost)
                if x_cost < int(variable_min or 0):
                    continue
            elif resource_sink and cost <= 0:
                # A zero-cost, non-X ability is not a resource sink.
                continue
            if cost + x_cost > resources:
                continue
            if exh and (cstate & game_engine.ECardStates.Tapped
                        or (not (cstate & game_engine.ECardStates.StartedATurnOnYourSide)
                            and not (attrs & game_engine.ECardAttributes.Speed))):
                continue
            uses = handler._card_uses(session, uid)
            used = int(uses.get(ag, 0))
            if int(meta[1] or 0) and used >= int(meta[1]):
                continue
            if int(meta[2] or 0) and used >= int(meta[2]):
                continue
            effects = _db.execute(
                "SELECT effect_type, param FROM ability_effects "
                "WHERE ability_guid=? ORDER BY effect_order", (ag,)).fetchall()
            params = []
            for etype, pm in effects:
                try:
                    params.append((etype, _j.loads(pm) if pm else {}))
                except Exception:
                    params.append((etype, {}))
            text = _j.dumps(params).lower()
            source_stats = effective_stats(_db, session.session_id,
                                           battle_state, int(uid))
            s_atk, s_def = source_stats[0], source_stats[1]
            target_uid = None
            worth = False
            requires_blocking_target = False
            # BlockingFilter(IsAbilitySource) means this ability exists only
            # while this troop is attacking and has a declared blocker.  Use
            # the metadata target template against the live combat assignment
            # instead of the generic self-target fallback below.  This covers
            # Chickatwice and keeps the AI from targeting its own attacker.
            target_row = _db.execute(
                "SELECT target_template_ids FROM card_abilities_meta "
                "WHERE ability_guid=?", (ag,)).fetchone()
            try:
                target_templates = _j.loads(target_row[0]) \
                    if target_row and target_row[0] else []
            except (TypeError, ValueError, _j.JSONDecodeError):
                target_templates = []
            from abilities.framework.targeting import (
                legal_targets, target_uses_both_players,
            )
            if resource_sink:
                worth = True
                # Let the authoritative resolver handle source/player/choice
                # targets, but do not fire a generic sink that still needs a
                # human-selected troop or other explicit target.
                for target_template in target_templates:
                    target_meta = _db.execute(
                        "SELECT target_kind, is_auto_target FROM target_templates "
                        "WHERE template_id=?", (target_template,)).fetchone()
                    kind = (target_meta[0] if target_meta else "") or ""
                    auto = int(target_meta[1] or 0) if target_meta else 0
                    if not (auto or kind in (
                            "PlayerTargetTemplate",
                            "AbilitySourceCardTargetTemplate",
                            "AbilityCreatedTargetTemplate")):
                        worth = False
                        break
                if not worth:
                    continue
            for target_template in target_templates:
                target_meta = _db.execute(
                    "SELECT filter_json FROM target_templates "
                    "WHERE template_id=?", (target_template,)).fetchone()
                if not target_meta or "BlockingFilter" not in (target_meta[0] or ""):
                    continue
                requires_blocking_target = True
                candidates = legal_targets(
                    _db, session.session_id, 0, target_template, int(uid),
                    both_players=target_uses_both_players(
                        _db, target_template),
                    champions=handler._champion_targets(),
                    battle_state=battle_state)
                if candidates:
                    worth = True
                    target_uid = int(candidates[0])
                break
            if requires_blocking_target and not worth:
                # This is a conditional combat ability, not a generic
                # self-buff.  In particular, Chickatwice must not target
                # itself when it is not currently being blocked.
                continue
            # Damage / exhaust / void / destroy an opposing troop.
            dmg = 0
            for etype, pm in params:
                if etype == "CardModifierAbilityEffectTemplate":
                    prop = (pm.get("property") or "").lower()
                    if prop in ("damage", "damagehero"):
                        dmg += int(pm.get("amount", 0) or 0)
                        m = __import__("re").search(
                            r'deal\s+(\d+)\s+damage',
                            (pm.get("text") or "").lower())
                        if m:
                            dmg = max(dmg, int(m.group(1)))
            if dmg > 0 or any(t in ("TapCardAbilityEffectTemplate",
                                    "DestroyCardAbilityEffectTemplate",
                                    "VoidCardAbilityEffectTemplate")
                              for t, _ in params):
                opp = _db.execute(
                    "SELECT gc.card_uid, ct.defense, gc.card_defense_mod, "
                    "gc.card_damage FROM game_cards gc "
                    "JOIN card_templates ct ON ct.guid=gc.template_guid "
                    "WHERE gc.session_id=? AND gc.user_id=? "
                    "AND gc.location='warzone' AND gc.card_type LIKE '%Troop%'",
                    (session.session_id,
                     (handler.user_profile or {}).get("id", 5))).fetchall()
                best_target = None
                for cu, bdef, dmod, dmgd in opp:
                    eff = (bdef or 0) + (dmod or 0) - (dmgd or 0)
                    if dmg and eff <= dmg:
                        if best_target is None or eff < best_target[1]:
                            best_target = (int(cu), eff)
                if best_target is not None:
                    worth = True
                    target_uid = best_target[0]
                elif dmg and opp and "champion" in text:
                    worth = True  # chip the champion
                elif any(t == "TapCardAbilityEffectTemplate"
                         for t, _ in params) and opp:
                    # Pure exhaustion: tap the opponent's biggest threat.
                    strong = _db.execute(
                        "SELECT gc.card_uid, ct.attack FROM game_cards gc "
                        "JOIN card_templates ct ON ct.guid=gc.template_guid "
                        "WHERE gc.session_id=? AND gc.user_id=? "
                        "AND gc.location='warzone' AND gc.card_type LIKE '%Troop%' "
                        "AND (gc.card_state & ?) = 0 "
                        "ORDER BY ct.attack DESC LIMIT 1",
                        (session.session_id,
                         (handler.user_profile or {}).get("id", 5),
                         game_engine.ECardStates.Tapped)).fetchone()
                    if strong is not None:
                        worth = True
                        target_uid = int(strong[0])
            # Buff our own troop (self or friendly target).
            if not worth and any(
                    etype == "CardModifierAbilityEffectTemplate"
                    and (pm.get("property") or "").lower()
                    in ("attack", "defense", "attribute")
                    for etype, pm in params):
                if "target troop" in text and "you control" in text:
                    own = _db.execute(
                        "SELECT gc.card_uid, ct.attack FROM game_cards gc "
                        "JOIN card_templates ct ON ct.guid=gc.template_guid "
                        "WHERE gc.session_id=? AND gc.user_id=0 "
                        "AND gc.location='warzone' AND gc.card_type LIKE '%Troop%' "
                        "ORDER BY ct.attack DESC LIMIT 1",
                        (session.session_id,)).fetchone()
                    if own is not None:
                        worth = True
                        target_uid = int(own[0])
                elif s_atk > 0:
                    worth = True  # self-buff (Living Totem, Hellhound...)
                    target_uid = int(uid)
            # Draw / health / resource / summon / transform-up.
            if not worth and any(
                    etype in ("DrawNCardsAbilityEffectTemplate",
                              "SummonTokenTroopAbilityEffectTemplate",
                              "ReplenishResourcesAbilityEffectTemplate",
                              "CreateTokenCopyAbilityEffectTemplate",
                              "PutTopOfDeckIntoHandAbilityEffectTemplate")
                    for etype, _ in params):
                worth = True
            if not worth and any(
                    etype == "CardModifierAbilityEffectTemplate"
                    and (pm.get("property") or "").lower()
                    in ("healhero", "heal")
                    for etype, pm in params):
                if int(battle_state.get("ai_health", 20)) <= 16:
                    worth = True
            if not worth:
                continue
            discard_required = bool(handler._ability_requires_discard(ag))
            if discard_required and not _db.execute(
                    "SELECT 1 FROM game_cards WHERE session_id=? "
                    "AND user_id=0 AND location='hand' LIMIT 1",
                    (session.session_id,)).fetchone():
                # A discard cost is part of activation legality.  Do not
                # spend resources or consume the ability when the AI cannot
                # pay it.
                continue
            # Pay + resolve via the player path with AI-side state.
            bstate = battle_state
            bstate["ai_resources"] = resources - cost - x_cost
            if variable_x and resource_sink:
                bstate["x_cost"] = x_cost
            bstate["player_mod_target"] = target_uid if target_uid else int(uid)
            bstate["player_transform_target"] = (target_uid if target_uid
                                                 else int(uid))
            bstate["player_spell_target"] = target_uid
            bstate["resolving_ability"] = ag
            bstate["resolving_source_uid"] = int(uid)
            bstate["resolving_owner_id"] = 0
            bstate["player_shift_source"] = int(uid)
            bstate["player_shift_target"] = target_uid
            handler._bump_card_use(session, int(uid), ag)
            _be.save_state(session, bstate)
            import ability as _ability_mod
            game2 = handler._fresh_game(session, pl_t, ai_t, bstate)
            if discard_required:
                ai_discard_card(handler, game2, session, pl_t, ai_t)
            fn = _ability_mod.resolve_effect(ag)
            if fn:
                fn(game2, session, _db, handler, pl_t, ai_t, bstate, ag, None)
            handler._remove_one_shot_ability(
                session, int(uid), ag, game2, pl_t, ai_t, bstate)
            # State-based effects are checked after every resolved ability,
            # not only after a stack item resolves.  This matters for
            # abilities such as Chickatwice's one-shot -1/-1: its effective
            # defense can become zero while the AI activation is resolved
            # directly, so it must move to the crypt before the next priority
            # window is offered.
            _ability_mod.state_based_deaths(
                game2, session, _db, handler, pl_t, ai_t, bstate)
            if exh:
                _db.execute(
                    "UPDATE game_cards SET card_state = card_state | ? "
                    "WHERE session_id=? AND card_uid=?",
                    (game_engine.ECardStates.Tapped, session.session_id,
                     int(uid)))
                _db.commit()
                _ability_mod.resolve_triggers(
                    _db, handler, game2, session, pl_t, ai_t, bstate,
                    "CardTappedEvent", int(uid), 0)
            game2.ai_resources = bstate["ai_resources"]
            ev_cur = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
            ev_cur.player_id = ai_t
            ev_cur.operation = 2
            ev_cur.delta = cost + x_cost
            ev_cur.new_value = bstate["ai_resources"]
            game2._push(ev_cur)
            # Token/deck leaves emit their CardMoved/CardUpdated events on the
            # fresh game used for resolution.  Flush that event batch now;
            # otherwise the DB changes are real but the client never sees the
            # deck mutation (and its deck counter remains stale).
            game2.push_player_updated(
                pl_t, champ_id=getattr(handler, "_player_champ_scid", None))
            game2.push_player_updated(
                ai_t, champ_id=getattr(handler, "_ai_champ_scid", None))
            handler._send_battle_events(session, game2, pl_t)
            bstate.pop("player_mod_target", None)
            bstate.pop("player_transform_target", None)
            bstate.pop("player_spell_target", None)
            bstate.pop("resolving_ability", None)
            bstate.pop("resolving_source_uid", None)
            bstate.pop("resolving_owner_id", None)
            bstate.pop("player_shift_source", None)
            bstate.pop("player_shift_target", None)
            if variable_x and resource_sink:
                bstate.pop("x_cost", None)
            _be.save_state(session, bstate)
            action = "resource sink" if resource_sink else "troop ability"
            log_req(f"    AI {action} {ag[:8]} on {hex(int(uid))} "
                    f"(cost={cost}+{x_cost}, target={hex(target_uid) if target_uid else 'self'})")
            return True
    return False


def ai_play_combat_trick(handler, game, session, ai_t, pl_t, battle_state):
    """Port of AICardEvaluator.GetCardToPlayInCombat: at the combat priority
    windows, blockers are declared.  For each combat involving our troop,
    if it would lose (our ATK can't kill the opposing troop or our DEF is
    lethal to us), find a playable QuickAction buff that flips the outcome —
    attack buff kills the blocker, defense buff saves our troop — and play it.
    Returns True when a trick went on the chain."""
    import json as _j
    import battle_engine as _be
    if not _be.stack_empty(battle_state):
        return False
    # On the AI's turn, ai_attackers -> blocked by ai_blockers.  On the
    # player's turn the AI defends with ai_blockers vs player_attackers.
    my_attackers = {int(k): int(v)
                    for k, v in (battle_state.get("ai_attackers") or {}).items()}
    their_attackers = {int(k): int(v)
                       for k, v in (battle_state.get("player_attackers") or {}).items()}
    my_blocks = {int(k): [int(b) for b in (v or [])]
                 for k, v in (battle_state.get("ai_blockers") or {}).items()}
    combats = []  # (my_uid, their_uid)
    for a_uid, blockers in my_blocks.items():
        if int(a_uid) in my_attackers:
            for b in blockers:
                combats.append((int(a_uid), b))     # our attacker vs their blocker
        else:
            for b in blockers:
                combats.append((b, int(a_uid)))     # our blocker vs their attacker
    if not combats:
        return False
    try:
        import ai_eval as _aieval
        ev = _aieval.build_evaluator(handler, session, battle_state, ai_t,
                                     pl_t)
    except Exception:
        return False
    my_cards = {int(c.card_uid): c for c in ev.ai_warzone}
    their_cards = {int(c.card_uid): c for c in ev.player_warzone}
    for my_uid, their_uid in combats:
        mine = my_cards.get(int(my_uid))
        theirs = their_cards.get(int(their_uid))
        if mine is None or theirs is None:
            continue
        my_atk = mine.effective_attack()
        my_def = mine.effective_defense(in_play=True)
        th_atk = theirs.effective_attack()
        th_def = theirs.effective_defense(in_play=True)
        losing = my_atk < th_def or my_def <= th_atk
        if not losing:
            continue
        for trick in ev.hand:
            h = ev.hints_for(trick)
            if (h.buff is None or not trick.is_quick_action()
                    or ev.is_playable(trick) != "True"):
                continue
            atk_buff = h.buff.attack
            def_buff = h.buff.defense
            if atk_buff == -6211975:
                atk_buff = ev.resources - trick.cost
            if def_buff == -6211975:
                def_buff = ev.resources - trick.cost
            flips = False
            if my_atk + atk_buff >= th_def and my_def + def_buff > th_atk:
                flips = True  # kill the blocker and survive
            elif h.buff.swiftstrike and my_atk + atk_buff >= th_def:
                flips = True
            elif my_atk < th_def and my_atk >= th_def + def_buff:
                flips = True
            elif my_def <= th_atk and my_def > th_atk + atk_buff:
                flips = True
            if flips:
                ai_play_hand_card(handler, game, session, ai_t, battle_state,
                                  trick, evaluator=ev,
                                  target_uid=int(my_uid))
                log_req(f"    AI combat trick: {trick.name} -> "
                        f"{mine.name} vs {theirs.name}")
                return True
        # DumpQuickActions: a lifegain quick action with no buff/removal is
        # played whenever it is affordable (client AIHandleAttackDefense
        # PriorityWindow falls through to DumpQuickActions).
        for trick in ev.hand:
            h = ev.hints_for(trick)
            if (not trick.is_quick_action()
                    or ev.is_playable(trick) != "True"
                    or h.buff is not None or h.removal is not None):
                continue
            lifegain = any(
                etype == "CardModifierAbilityEffectTemplate"
                and (pm.get("property") or "").lower()
                in ("healhero", "heal")
                for ag in trick.ability_guids
                for etype, pm in ev.effects_for(ag))
            if lifegain:
                ai_play_hand_card(handler, game, session, ai_t, battle_state,
                                  trick, evaluator=ev)
                log_req(f"    AI lifegain dump: {trick.name}")
                return True
    return False


def _spell_damage_info(db, tpl_guid):
    """(is_x, esc_base, fixed, text) of a hand action's damage BOM leaf, or None.
    ``is_x`` comes from the gamedata m_VariableCost card field (a "pay X"
    spell); the damage amount type comes from the ability_effects params
    ("Deal X/ESC:N/N damage ...")."""
    import json as _j
    import re as _re
    row = db.execute(
        "SELECT abilities_json, variable_cost FROM card_templates WHERE guid=?",
        (tpl_guid,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        ags = _j.loads(row[0])
    except Exception:
        return None
    for ag in (ags or []):
        for e in db.execute(
                "SELECT param FROM ability_effects WHERE ability_guid=? "
                "AND effect_type='CardModifierAbilityEffectTemplate'",
                (ag,)).fetchall():
            if not e or not e[0]:
                continue
            try:
                pm = _j.loads(e[0])
            except Exception:
                continue
            text = (pm.get("text") or "").lower()
            if "damage" not in text:
                continue
            is_x = bool(row[1] and int(row[1]) > 0)
            m_esc = _re.search(r'esc:(\d+)', text)
            esc_base = int(m_esc.group(1)) if m_esc else 0
            fixed = int(pm.get("amount") or 0)
            if fixed <= 0:
                m = _re.search(r'deal\s+(\d+)\s+damage', text)
                if m:
                    fixed = int(m.group(1))
            return {"is_x": is_x, "esc_base": esc_base, "fixed": fixed,
                    "text": text}
    return None


def ai_play_spell(handler, game, session, ai_t, battle_state):
    """AI casts an affordable damage action from hand (Burn, Ragefire, Burn to
    the Ground).  For variable-X spells X is the LARGEST value that kills the
    target, capped by the AI's remaining resources.  The spell goes onto the
    chain (CastSpells) and stays there until the player passes — they get a
    priority window to respond (e.g. Countermagic)."""
    import json as _j
    import battle_engine as _be
    if not _be.stack_empty(battle_state):
        return
    resources = battle_state.get("ai_resources", 0)
    threshold = battle_state.get("ai_threshold", {})
    rows = _db.execute(
        "SELECT gc.id, gc.card_uid, gc.template_guid, ct.cost, ct.card_type, "
        "ct.threshold_json FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='hand' "
        "AND ct.card_type IN ('BasicAction','QuickAction') ORDER BY gc.position",
        (session.session_id,)).fetchall()
    if not rows:
        return
    pl_t = game_engine.UID.make(244, int(handler.client_reck_id))
    player_pid = (handler.user_profile or {}).get("id", 0)
    player_champ = getattr(handler, "_player_champ_scid", None)
    champ_uid64 = player_champ.uid.to_uint64() if player_champ else 0
    # The player's warzone troops (effective defense = base + mod - damage).
    troops = _db.execute(
        "SELECT gc.card_uid, ct.defense, gc.card_defense_mod, gc.card_damage "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='warzone' "
        "AND gc.card_type LIKE '%Troop%'",
        (session.session_id, player_pid)).fetchall()
    troop_defs = {int(r[0]): (r[1] or 0) + (r[2] or 0) - (r[3] or 0)
                  for r in troops}
    player_health = int(battle_state.get("player_health", 20))
    for row in rows:
        cost = row[3] or 0
        if cost > resources:
            continue
        if not handler._thresholds_met(row[5], threshold):
            continue
        info = _spell_damage_info(_db, row[2])
        if not info:
            continue
        # Choose the target: a troop the spell can kill beats the champion.
        target_uid = None
        needed = None
        if info["is_x"]:
            affordable = resources - cost
            for cu, def_ in sorted(troop_defs.items(),
                                   key=lambda kv: (kv[1], kv[0])):
                if def_ <= affordable and def_ > 0:
                    target_uid = cu
                    needed = def_
                    break
            if target_uid is None:
                target_uid = champ_uid64
                needed = player_health
            x_cost = min(affordable, needed) if needed else affordable
            x_cost = max(0, x_cost)
            amount = x_cost
        else:
            if info["esc_base"]:
                uses = int(battle_state.get("ai_escalation_uses", 0))
                amount = info["esc_base"] * (uses + 1)
            else:
                amount = info["fixed"] or 0
            if amount <= 0:
                continue
            for cu, def_ in sorted(troop_defs.items(),
                                   key=lambda kv: (kv[1], kv[0])):
                if def_ <= amount and def_ > 0:
                    target_uid = cu
                    break
            if target_uid is None:
                target_uid = champ_uid64
            x_cost = 0
        if not target_uid:
            continue
        tid = int(row[1])
        scid = game_engine.SessionCardId(game_engine.UID(tid))
        _db.execute(
            "UPDATE game_cards SET location='CastSpells' "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, tid))
        _db.commit()
        battle_state["ai_resources"] = resources - cost - x_cost
        from db import db_get_card_abilities
        ab_json, _ = db_get_card_abilities(row[2])
        try:
            ability_guids = [g.lower() for g in _j.loads(ab_json or "[]")]
        except Exception:
            ability_guids = []
        tpl_g, ct_n, nm, cost2, atk2, def2, gem2 = handler._card_full_data(
            game, scid, row[2], row[0])
        game.push_card_updated(
            scid, ai_t, game_engine.ECardCollections.CastSpells,
            game_engine.card_type_from_db(row[4]),
            template_id=row[2], cost=cost2, attack=atk2, defense=def2, gems=gem2)
        game.push_card_moved(scid, ai_t, game_engine.ECardCollections.CastSpells,
                             game_engine.ECardLocations.Top, 0)
        # Render the spell on the chain (GoChainView) during the response
        # window — without AbilityPushedOnChain the client shows an empty
        # chain with the Resolve button.
        game.push_ability_on_chain(
            scid, game_engine.ResourceId.from_str(
                game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID))
        # Hold the spell on the chain: the stack item resolves its BOM (and
        # sends it to the graveyard) when both players pass.
        inst_id = int(battle_state.get("_next_instance_id", 1))
        battle_state["_next_instance_id"] = inst_id + 1
        _be.stack_push(battle_state, {
            "kind": "spell", "source_uid": int(tid),
            "ability_guids": ability_guids, "target_uid": int(target_uid),
            "instance_id": inst_id, "x_cost": int(x_cost or 0),
        })
        game.ai_resources = battle_state["ai_resources"]
        ev_cur = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
        ev_cur.player_id = ai_t
        ev_cur.operation = 2
        ev_cur.delta = cost + x_cost
        ev_cur.new_value = battle_state["ai_resources"]
        game._push(ev_cur)
        _be.save_state(session, battle_state)
        log_req(f"    AI cast spell {row[2][:8]} (cost={cost}+{x_cost}, "
                f"dmg={amount}, target={hex(target_uid)}) — resources left "
                f"{battle_state['ai_resources']}")
        return


def resolve_ai_mulligan(handler, session, game, ai_t):
    """Resolve the AI's mulligan during the Mulligan phase.

    Match the client's AITactical.ShouldKeepStartingHand policy: keep at the
    four-card snap-keep threshold, otherwise reject the personality's no-go
    resource counts and require a sufficient theoretical hand value. Each
    mulligan redraws one fewer card (7 -> 6 -> ... -> 0), while the AI keeps
    deciding until it keeps or reaches 0 cards. Pushes
    PlayerMulliganedHand / CardUpdated / AcceptedStartingHand events so the
    client sees the AI's decisions. Returns True if the AI ended up with a
    hand (0 is treated as forced-keep at 0 cards).
    """
    import random as _rnd
    while True:
        hand_row = _db.execute(
            "SELECT COUNT(*), SUM(CASE WHEN COALESCE(ct.card_type, gc.card_type)="
            "'Resource' THEN 1 ELSE 0 END) "
            "FROM game_cards gc LEFT JOIN card_templates ct ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.user_id=0 AND gc.location='hand'",
            (session.session_id,)).fetchone()
        count = hand_row[0] if hand_row else 0
        shard_count = (hand_row[1] or 0) if hand_row else 0
        if count == 0:
            # No cards left to mulligan away: forced keep.
            game.push_accepted_starting_hand(ai_t, mulliganed=False)
            log_req("    AI keeps 0-card hand (deck exhausted)")
            return True
        # Client ShouldKeepStartingHand (AITactical.cs:1033):
        #   keep when hand size >= HandSizeSnapKeep (4), or the resource count
        #   is not in StartingResourceNoGo {0,1,6,7} and the theoretical value
        #   of the non-resource cards >= HandValueSnapKeep (5.0).
        keep = False
        hand_value = 0.0
        # Match AITactical.ShouldKeepStartingHand: once the hand has been
        # reduced to the personality's snap-keep size (four), keep it.  The
        # previous comparison was reversed, so a seven-card no-resource hand
        # was kept immediately and a broken/unknown hand could be reported as
        # zero cards.
        if count <= 4:
            keep = True
        else:
            try:
                import ai_eval as _aieval
                import battle_engine as _be
                bs = _be.load_state(session)
                if bs is None:
                    bs = _be.default_state()
                ev = _aieval.build_evaluator(
                    handler, session, bs, ai_t,
                    game_engine.UID.make(244, int(
                        (handler.user_profile or {}).get("id", 5))))
                if shard_count not in (0, 1, 6, 7):
                    hand_value = sum(ev.get_theoretical_value(c) for c in ev.hand
                                     if not c.is_resource())
                    if hand_value >= 5.0:
                        keep = True
            except Exception:
                keep = bool(shard_count)
        if keep:
            game.push_accepted_starting_hand(ai_t, mulliganed=False)
            log_req(f"    AI keeps hand ({count} cards, {shard_count} shards, "
                    f"value={hand_value:.1f})")
            return True
        # AI mulligans: redraw one fewer card (7->6->...->1).
        game.push_player_mulliganed_hand(ai_t, count)
        game.push_accepted_starting_hand(ai_t, mulliganed=True)
        log_req(f"    AI mulligans ({count} cards, {shard_count} shards, "
                f"value={hand_value:.1f})")
        # Move hand to deck (batch)
        hand_rows = _db.execute(
            "SELECT id, card_uid FROM game_cards WHERE session_id=? AND user_id=0 AND location='hand'",
            (session.session_id,)).fetchall()
        for r in hand_rows:
            scid = game_engine.SessionCardId(game_engine.UID(r[1]))
            game.push_card_updated(scid, ai_t, game_engine.ECardCollections.Deck,
                                   game_engine.ECardTypes.Unknown, nulling=True)
        if hand_rows:
            _db.executemany(
                "UPDATE game_cards SET location='deck', position=9999 WHERE id=?",
                [(r[0],) for r in hand_rows])
        # Shuffle deck (batch position update)
        deck_rows = _db.execute(
            "SELECT id FROM game_cards WHERE session_id=? AND user_id=0 AND location='deck'",
            (session.session_id,)).fetchall()
        ids = [r[0] for r in deck_rows]
        _rnd.shuffle(ids)
        if ids:
            _db.executemany(
                "UPDATE game_cards SET position=? WHERE id=?",
                [(i, cid) for i, cid in enumerate(ids)])
        # Draw back one fewer
        new_hand = _db.execute(
            "SELECT id, card_uid FROM game_cards WHERE session_id=? AND user_id=0 AND location='deck' ORDER BY position LIMIT ?",
            (session.session_id, max(0, count - 1))).fetchall()
        for i, r in enumerate(new_hand):
            scid = game_engine.SessionCardId(game_engine.UID(r[1]))
            game.push_card_updated(scid, ai_t, game_engine.ECardCollections.Hand,
                                   game_engine.ECardTypes.Unknown, nulling=True)
        if new_hand:
            _db.executemany(
                "UPDATE game_cards SET location='hand', position=? WHERE id=?",
                [(i, r[0]) for i, r in enumerate(new_hand)])
        _db.commit()

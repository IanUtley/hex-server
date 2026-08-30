"""Shared utilities used by abilities framework modules."""

import json
import re

import game_engine


# These flags describe the current battle/turn instance, not the card's
# identity.  A card leaving play (especially when it is returned to a deck)
# must not carry them into a later draw.  Keeping this mask here gives every
# zone-move implementation the same reset semantics.
ZONE_EXIT_STATE_FLAGS = (
    game_engine.ECardStates.Tapped |
    game_engine.ECardStates.Blocking |
    game_engine.ECardStates.Attacking |
    game_engine.ECardStates.Damaged |
    game_engine.ECardStates.Healed |
    game_engine.ECardStates.Dead |
    game_engine.ECardStates.HasAttacked |
    game_engine.ECardStates.HasBlocked |
    game_engine.ECardStates.EffectExpired |
    game_engine.ECardStates.ZoneChangeReplacement |
    game_engine.ECardStates.Activated |
    game_engine.ECardStates.CameOutThisTurn |
    game_engine.ECardStates.StartedATurnOnYourSide
)


def state_after_zone_exit(state):
    """Return card state with transient battle/turn flags removed."""
    return int(state or 0) & ~int(ZONE_EXIT_STATE_FLAGS)


def _log(msg):
    try:
        from db import log_req
        log_req(msg)
    except Exception:
        pass


def _card_state_of(db, session, card_uid):
    try:
        from db import db_card_state
        return db_card_state(session.session_id, int(card_uid))
    except Exception:
        return 0


def next_game_card_uid(db, session_id):
    """Allocate a fresh SessionCardId instance for a generated card.

    The database row id and the instance portion of a SessionCardId are
    independent values.  Using MAX(game_cards.id) for both can reuse a UID
    when cards were inserted or restored in a different order, causing the
    client/server card cache to resolve a token as another card.
    """
    row = db.execute(
        "SELECT COALESCE(MAX(card_uid >> 8), 10000) + 1 "
        "FROM game_cards WHERE session_id=?", (session_id,)).fetchone()
    instance = max(10001, int(row[0] or 10001))
    while db.execute(
            "SELECT 1 FROM game_cards WHERE session_id=? AND card_uid=? "
            "LIMIT 1", (session_id, game_engine.UID.make(1, instance).uid64)
            ).fetchone():
        instance += 1
    return game_engine.UID.make(1, instance).uid64


def card_collection_for_location(location):
    """Map the authoritative DB location to the client's card collection."""
    return {
        "deck": game_engine.ECardCollections.Deck,
        "hand": game_engine.ECardCollections.Hand,
        "discard": game_engine.ECardCollections.Discard,
        "void": game_engine.ECardCollections.Void,
        "warzone": game_engine.ECardCollections.Warzone,
        "CastSpells": game_engine.ECardCollections.CastSpells,
        "PlayedResources": game_engine.ECardCollections.PlayedResources,
        "underground": game_engine.ECardCollections.Underground,
        "choosing": game_engine.ECardCollections.Choosing,
    }.get(location, game_engine.ECardCollections.Warzone)


def _stat_delta(game_text, stat):
    """Parse +/-N[STAT] from an ability's game text (e.g. '+3[ATK]/+3[DEF]')."""
    m = re.search(r'([+-]\d+)\s*\[' + re.escape(stat) + r'\]', game_text or "")
    return int(m.group(1)) if m else 0


def owner_uid(owner_id, pl_t, ai_t, bstate=None):
    """Map a card's DB user_id to the UID of its controlling player.

    FRA/AI games: user_id 0 is the AI (ai_t), anything else is the human
    (pl_t).  PvP tournaments have TWO non-zero player ids (pids) — the pid
    maps directly to UID.make(244, pid), which is objective for both clients.
    """
    if bstate is not None and bstate.get("pvp"):
        return game_engine.UID.make(244, int(owner_id or 0))
    return pl_t if (owner_id or 0) != 0 else ai_t


def pvp_champion_uid(bstate, owner_id):
    """Return the authoritative champion SCID for a PvP player id."""
    if not (bstate or {}).get("pvp"):
        return None
    for pid, uid in ((bstate or {}).get("champ_map") or {}).items():
        try:
            if int(pid) == int(owner_id):
                return int(uid)
        except (TypeError, ValueError):
            continue
    return None


def pvp_opponent_pid(bstate, owner_id):
    """Return the other real player id in a PvP ability view."""
    if not (bstate or {}).get("pvp"):
        return None
    for pid in (bstate or {}).get("pids") or []:
        try:
            if int(pid) != int(owner_id):
                return int(pid)
        except (TypeError, ValueError):
            continue
    for pid in ((bstate or {}).get("champ_map") or {}).keys():
        try:
            if int(pid) != int(owner_id):
                return int(pid)
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
#  Card attributes (data-driven from ability text — no per-card GUIDs)
# ---------------------------------------------------------------------------

_ATTRIBUTE_KEYWORDS = {
    "flight": game_engine.ECardAttributes.Flight,
    "spiritdrain": game_engine.ECardAttributes.SpiritDrain,
    "lifedrain": game_engine.ECardAttributes.SpiritDrain,
    "steadfast": game_engine.ECardAttributes.Steadfast,
    "spellshield": game_engine.ECardAttributes.SpellShield,
    "immortal": game_engine.ECardAttributes.Immortal,
    "invincible": game_engine.ECardAttributes.PreventAllDamage,
    "rage": game_engine.ECardAttributes.Rage,
    "first strike": game_engine.ECardAttributes.FirstStrike,
    # Typed AttributeModifier metadata uses the enum spelling without a
    # space (FirstStrike), while localized game text uses "first strike".
    "firststrike": game_engine.ECardAttributes.FirstStrike,
    "swiftstrike": game_engine.ECardAttributes.FirstStrike,
    "quick action": game_engine.ECardAttributes.QuickAction,
    "canny block": game_engine.ECardAttributes.CantBlock,
    "speed": game_engine.ECardAttributes.Speed,
    "skyguard": game_engine.ECardAttributes.SkyGuard,
    "juggernaut": game_engine.ECardAttributes.Juggernaught,
    "defensive": game_engine.ECardAttributes.Defensive,
    "can't attack": game_engine.ECardAttributes.CantAttack,
    "can't block": game_engine.ECardAttributes.CantBlock,
    "must block": game_engine.ECardAttributes.MustBlock,
    "can't be blocked": game_engine.ECardAttributes.CantBeBlocked,
    "cantreadyautomatically": game_engine.ECardAttributes.CantReadyAutomatically,
    "dualstrike": game_engine.ECardAttributes.DualStrike,
}


def attribute_bits_from_text(text):
    """Map attribute keywords found in *text* to ECardAttributes bit flags."""
    low = (text or "").lower()
    bits = 0
    # "can't attack or block" contains both sub-strings; check the compound
    # first so the OR form doesn't get swallowed by a single keyword.
    if "can't attack or block" in low:
        bits |= game_engine.ECardAttributes.CantAttack | game_engine.ECardAttributes.CantBlock
    for keyword, flag in _ATTRIBUTE_KEYWORDS.items():
        if keyword in low:
            bits |= flag
    return bits


def attribute_bits_from_flags(flags):
    """Map typed AttributeModifier flags (for example ``FirstStrike``).

    This is deliberately separate from localized game-text parsing.  The
    extracted ``m_AttributeFlags`` value is authoritative whenever present.
    """
    bits = 0
    for token in str(flags or "").replace("|", " ").split():
        normalized = token.strip().lower().replace("_", "")
        bits |= {
            "firststrike": game_engine.ECardAttributes.FirstStrike,
            "swiftstrike": game_engine.ECardAttributes.FirstStrike,
            "dualstrike": game_engine.ECardAttributes.DualStrike,
            "skyguard": game_engine.ECardAttributes.SkyGuard,
            "cantblock": game_engine.ECardAttributes.CantBlock,
            "cantattack": game_engine.ECardAttributes.CantAttack,
            "cantbeblocked": game_engine.ECardAttributes.CantBeBlocked,
            "cantreadyautomatically": game_engine.ECardAttributes.CantReadyAutomatically,
            "defensive": game_engine.ECardAttributes.Defensive,
            "flight": game_engine.ECardAttributes.Flight,
            "immortal": game_engine.ECardAttributes.Immortal,
            "spellshield": game_engine.ECardAttributes.SpellShield,
            "steadfast": game_engine.ECardAttributes.Steadfast,
            "spiritdrain": game_engine.ECardAttributes.SpiritDrain,
            "lifedrain": game_engine.ECardAttributes.SpiritDrain,
            "speed": game_engine.ECardAttributes.Speed,
            "quickaction": game_engine.ECardAttributes.QuickAction,
            # The client enum is historically spelled ``Juggernaught``
            # (two n's), while some localized/older records use the normal
            # ``Juggernaut`` spelling.  Accept both so typed AttributeModifier
            # metadata and rendered game text produce the same Crush bit.
            "juggernaut": game_engine.ECardAttributes.Juggernaught,
            "juggernaught": game_engine.ECardAttributes.Juggernaught,
        }.get(normalized, 0)
    return bits


_TEMPORARY_ATTRIBUTE_EXPIRATIONS = "__attribute_expirations"


def _temporary_attribute_expiry_boundary(duration):
    """Translate gamedata duration names into turn-boundary names.

    BeginningOfOwnersTurn is keyed to the ability source's controller, not
    the controller of every affected card.  Keeping that source owner with
    the temporary bit is what lets an opposing troop retain Defensive until
    the Butterfly controller's next turn.
    """
    return {
        "EndOfTurn": "end_turn",
        "BeginningOfOwnersTurn": "start_turn",
        "AfterCardsReadyOnPlayersTurn": "prep",
    }.get(duration)


def _temporary_buffs_json(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def clear_combat_damage(db, session_id):
    """Remove marked combat damage during the end-of-turn cleanup step.

    Damage is cleared before temporary end-of-turn stat modifiers expire.
    Otherwise a troop surviving only because of a temporary defense bonus can
    be incorrectly killed when that bonus is removed (for example, a 2/3
    troop with +3 DEF and 3 marked damage).
    """
    db.execute(
        "UPDATE game_cards SET card_damage=0 "
        "WHERE session_id=? AND location='warzone'",
        (session_id,))
    db.commit()


def clear_expired_temporary_attributes(db, session_id, owner_id, boundary,
                                       clear_stat_buffs=False):
    """Expire source-owned temporary attributes at one turn boundary.

    Older rows without expiration metadata retain the previous owner-scoped
    behavior.  The metadata is stored alongside temporary stat buffs so no
    schema change is needed, and unrelated temporary grants can coexist.
    """
    rows = db.execute(
        "SELECT card_uid, user_id, temporary_attributes, temporary_buffs "
        "FROM game_cards WHERE session_id=? AND "
        "(temporary_attributes != 0 OR "
        "(temporary_buffs IS NOT NULL AND temporary_buffs != '{}'))",
        (session_id,)).fetchall()
    changed = []
    for card_uid, target_owner, attrs, raw_buffs in rows:
        buffs = _temporary_buffs_json(raw_buffs)
        metadata = buffs.get(_TEMPORARY_ATTRIBUTE_EXPIRATIONS) or {}
        expired_bits = 0
        remaining = {}
        for bit_text, rule in metadata.items():
            try:
                bit = int(bit_text)
            except (TypeError, ValueError):
                continue
            if (isinstance(rule, dict)
                    and int(rule.get("owner", -1)) == int(owner_id)
                    and rule.get("boundary") == boundary):
                expired_bits |= bit
            else:
                remaining[bit_text] = rule

        # Grants written before source-boundary metadata existed expire at the
        # affected card's own boundary, preserving the old behavior.
        if not metadata and int(target_owner or 0) == int(owner_id):
            expired_bits = int(attrs or 0)

        new_attrs = int(attrs or 0) & ~expired_bits
        if remaining:
            buffs[_TEMPORARY_ATTRIBUTE_EXPIRATIONS] = remaining
        else:
            buffs.pop(_TEMPORARY_ATTRIBUTE_EXPIRATIONS, None)
        if clear_stat_buffs:
            stat_buffs = {
                key: value for key, value in buffs.items()
                if key == _TEMPORARY_ATTRIBUTE_EXPIRATIONS
            }
            buffs = stat_buffs
        new_buffs = json.dumps(buffs, separators=(",", ":"), sort_keys=True)
        if new_attrs == int(attrs or 0) and new_buffs == (raw_buffs or "{}"):
            continue
        db.execute(
            "UPDATE game_cards SET temporary_attributes=?, temporary_buffs=? "
            "WHERE session_id=? AND card_uid=?",
            (new_attrs, new_buffs, session_id, int(card_uid)))
        changed.append(int(card_uid))
    if changed:
        db.commit()
    return changed


def apply_attribute_grant(game, session, db, handler, pl_t, ai_t, target_uid,
                          text, temporary=False, bstate=None, duration=None,
                          source_owner_id=None, attribute_flags=None):
    """OR the attribute bits found in *text* into the target card and push a
    CardUpdated so the client renders the new attribute badge.  ``temporary``
    ("until end of turn", e.g. Dimmid's Lifedrain charge power) writes to
    game_cards.temporary_attributes, which the Ready step clears next turn."""
    bits = (attribute_bits_from_flags(attribute_flags)
            if attribute_flags is not None else attribute_bits_from_text(text))
    if not bits or target_uid is None:
        return bits
    target_uid = int(target_uid)
    # Defensive is a troop keyword.  Champion SessionCardIds are valid event
    # targets in PvP, but they are not game_cards rows and must never receive
    # a troop-only Defensive grant if an upstream target list is stale.
    if bits & game_engine.ECardAttributes.Defensive:
        champion_uids = set()
        if (bstate or {}).get("pvp"):
            champion_uids.update(
                int(uid) for uid in (bstate.get("champ_map") or {}).values()
                if uid)
        for attr in ("_player_champ_scid", "_ai_champ_scid"):
            champ = getattr(handler, attr, None)
            if champ is not None:
                try:
                    champion_uids.add(int(champ.uid.uid64))
                except AttributeError:
                    champion_uids.add(int(champ))
        if target_uid in champion_uids:
            bits &= ~game_engine.ECardAttributes.Defensive
            if not bits:
                return 0
    col = "temporary_attributes" if temporary else "card_attributes"
    row = db.execute(
        f"SELECT {col} FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, target_uid)).fetchone()
    cur = row[0] if row else 0
    db.execute(
        f"UPDATE game_cards SET {col}=? WHERE session_id=? AND card_uid=?",
        (cur | bits, session.session_id, target_uid))
    if temporary:
        boundary = _temporary_attribute_expiry_boundary(duration)
        if boundary:
            if source_owner_id is None:
                source_uid = (bstate or {}).get("resolving_source_uid")
                if source_uid is not None:
                    owner_row = db.execute(
                        "SELECT user_id FROM game_cards "
                        "WHERE session_id=? AND card_uid=?",
                        (session.session_id, int(source_uid))).fetchone()
                    if owner_row:
                        source_owner_id = owner_row[0]
                if source_owner_id is None:
                    source_owner_id = (bstate or {}).get("resolving_owner_id", 0)
            source_owner_id = int(source_owner_id or 0)
            meta_row = db.execute(
                "SELECT temporary_buffs FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, target_uid)).fetchone()
            buffs = _temporary_buffs_json(meta_row[0] if meta_row else "{}")
            expirations = buffs.setdefault(_TEMPORARY_ATTRIBUTE_EXPIRATIONS, {})
            for bit in (1 << n for n in range(bits.bit_length())):
                if bits & bit:
                    expirations[str(bit)] = {
                        "owner": source_owner_id,
                        "boundary": boundary,
                    }
            db.execute(
                "UPDATE game_cards SET temporary_buffs=? "
                "WHERE session_id=? AND card_uid=?",
                (json.dumps(buffs, separators=(",", ":"), sort_keys=True),
                 session.session_id, target_uid))
    db.commit()
    trow = db.execute(
        "SELECT template_guid, location FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, target_uid)).fetchone()
    if trow and trow[0]:
        scid = game_engine.SessionCardId(game_engine.UID(target_uid))
        _tpl, ct, _n, _c, atk, def_, _g = handler._card_full_data(game, scid, trow[0])
        orow = db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, target_uid)).fetchone()
        owner = owner_uid(orow[0] if orow else 0, pl_t, ai_t, bstate)
        game.push_card_updated(scid, owner, card_collection_for_location(trow[1]), ct,
                               template_id=trow[0], attack=atk, defense=def_,
                               attributes=cur | bits, nulling=(trow[1] == "deck"))
    _log(f"    Attribute grant {hex(target_uid)}: +{bits:b}")
    return bits


def number_word_to_int(word):
    """Convert a lowercase number word ('five') to an int (None if not a word)."""
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
             "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
             "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
             "nineteen": 19, "twenty": 20}
    return words.get((word or "").strip().lower())


# Compatibility exports.  Counter state is now implemented in the focused
# effects module, while older callers may continue importing from _shared.
from .effects.counters import (  # noqa: E402,F401
    _counters_payload, counter_guid_for_name, card_counters, card_counters_full,
    add_card_counter, push_card_counters, remove_card_counters,
    counter_name_from_text,
)

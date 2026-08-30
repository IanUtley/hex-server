"""Token and generated-card creation effects."""

import json
import random
import re

import game_engine

from ..fields import effect_field, effect_template, effect_template_value
from ..targeting import (evaluate_card_filter, shards_from_threshold,
                         template_faction)
from .._shared import next_game_card_uid, owner_uid


_DECK_TEMPLATES = None


def _load_deck_templates():
    """Load DeckTemplate resources from the extracted gamedata snapshot.

    LoadPlayerDeck is used by a small number of encounter/PvE abilities.  The
    client instantiates the referenced DeckTemplate, rather than interpreting
    the card's display text, so keep the server on that same typed data path.
    """
    global _DECK_TEMPLATES
    if _DECK_TEMPLATES is not None:
        return _DECK_TEMPLATES
    _DECK_TEMPLATES = {}
    path = __import__("pathlib").Path(__file__).resolve().parents[3] / \
        "Records" / "DeckTemplate.jsonl"
    if not path.exists():
        return _DECK_TEMPLATES
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    value = json.loads(line)
                    if isinstance(value, str):
                        value = json.loads(
                            re.sub(r",\s*([}\]])", r"\1", value))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(value, dict):
                    continue
                guid = ((value.get("m_Id") or {}).get("m_Guid") or "")
                if guid:
                    _DECK_TEMPLATES[str(guid).lower()] = value
    except OSError:
        pass
    return _DECK_TEMPLATES


def load_player_deck(game, session, db, handler, pl_t, ai_t, bstate,
                     effect_guid, param):
    """Instantiate a typed DeckTemplate into the resolving player's deck.

    The original effect excludes champion cards and runs card-creation
    abilities on the generated cards.  The latter are represented by the
    normal card ability list here; later zone entry/play processing will fire
    those abilities through the shared trigger dispatcher.
    """
    template = effect_template(effect_guid) or {}
    deck_guid = effect_template_value(
        db, bstate, effect_guid, "m_DeckTemplateId", "")
    if not deck_guid:
        try:
            data = json.loads(param or "{}")
            deck_guid = str(data.get("deck_template_guid") or "").lower()
        except (TypeError, ValueError, json.JSONDecodeError):
            deck_guid = ""
    deck = _load_deck_templates().get(str(deck_guid).lower())
    if not deck:
        return "load player deck: template not found"

    owner_id = int((bstate or {}).get("resolving_owner_id") or 0)
    columns = {row[1] for row in db.execute(
        "PRAGMA table_info(game_cards)").fetchall()}
    position = db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck'",
        (session.session_id, owner_id)).fetchone()[0]
    created = 0
    for entry in deck.get("m_DeckResources") or []:
        if not isinstance(entry, dict):
            continue
        tpl_guid = str(((entry.get("m_idTemplate") or {}).get("m_Guid")
                        or "")).lower()
        if not tpl_guid:
            continue
        count = max(0, int(entry.get("m_Count") or 0))
        row = db.execute(
            "SELECT card_type, abilities_json, attributes FROM card_templates "
            "WHERE guid=?", (tpl_guid,)).fetchone()
        if not row or str(row[0] or "").split("|")[0] == "Champion":
            continue
        card_type, abilities, attributes = row
        for _ in range(count):
            card_uid = next_game_card_uid(db, session.session_id)
            fields = [
                "session_id", "user_id", "card_uid", "card_template_id",
                "location", "position", "is_champion", "card_type",
                "template_guid", "card_abilities", "owner_user_id",
                "card_attributes",
            ]
            values = [session.session_id, owner_id, card_uid, tpl_guid,
                      "deck", position, 0, card_type, tpl_guid,
                      abilities or "[]", owner_id, int(attributes or 0)]
            if "original_template_guid" in columns:
                fields.append("original_template_guid")
                values.append(tpl_guid)
            db.execute(
                "INSERT INTO game_cards ({}) VALUES ({})".format(
                    ",".join(fields), ",".join("?" for _ in fields)), values)
            position += 1
            created += 1
    db.commit()
    return f"loaded {created} card(s) into player deck"


def _random_template_guids(db, filter_json, source_uid, owner_id,
                           bstate=None):
    """Return card templates matching a typed CardFilter.

    Random-card effects must choose from templates, not from the current
    ``game_cards`` rows: Conscript creates a new card from the pool.  In PvP,
    exclude templates marked PvE-only or ineligible for PvP random templates;
    Practice/PvE keeps the full typed-filter pool.  Keeping this helper here
    also makes SummonToken and Conscript use identical filter semantics.
    """
    if not isinstance(filter_json, dict):
        return []
    try:
        rows = db.execute(
            "SELECT guid, name, card_type, cost, attack, defense, "
            "attributes, subtype, rarity, socket_count, threshold_json, "
            "is_pve, no_pvp "
            "FROM card_templates"
        ).fetchall()
    except Exception:
        return []
    candidates = []
    for row in rows:
        if (bstate or {}).get("pvp") and (row[11] or row[12]):
            continue
        candidate = {
            "card_uid": 0,
            "name": row[1] or "",
            "card_type": row[2] or "",
            "cost": row[3] or 0,
            "attack": row[4] or 0,
            "defense": row[5] or 0,
            "attributes": row[6] or 0,
            "subtype": row[7] or "",
            "rarity": row[8] or "",
            "socket_count": row[9] or 0,
            "shards": shards_from_threshold(row[10]),
            "faction": template_faction(row[0]),
            "location": "",
            "user_id": owner_id or 0,
        }
        if evaluate_card_filter(candidate, filter_json, source_uid):
            candidates.append(row[0])
    return candidates


def conscript_cards(game, session, db, handler, pl_t, ai_t, bstate,
                    effect_guid, param):
    """Create random cards from Conscript's typed CardFilter into hand.

    This mirrors the authoritative CreateNTokensFromResource(..., Hand)
    branch.  It is intentionally non-interactive: Conscript is a random
    effect, so it is legal in both PvP and automatic PVE resolution.
    """
    ability_guid = (bstate or {}).get("resolving_ability", "")
    template = effect_template(effect_guid) or {}
    typed_filter = template.get("m_CardFilter")
    amount = effect_field(db, bstate, effect_guid, "m_Amount", default=0)
    if not amount:
        try:
            p = json.loads(param or "{}")
            amount = int(p.get("amount") or 1)
            typed_filter = typed_filter or p.get("card_filter")
        except (TypeError, ValueError, json.JSONDecodeError):
            amount = 1
    amount = max(0, min(int(amount), 100))
    owner_id = int((bstate or {}).get("resolving_owner_id") or 0)
    source_uid = (bstate or {}).get("resolving_source_uid")
    candidates = _random_template_guids(
        db, typed_filter, source_uid, owner_id, bstate)
    if not candidates or amount <= 0:
        return "conscript: no matching card template"

    existing = {row[1] for row in db.execute(
        "PRAGMA table_info(game_cards)").fetchall()}
    created = []
    for _ in range(amount):
        tpl_guid = random.choice(candidates)
        row = db.execute(
            "SELECT card_type, abilities_json, attributes FROM card_templates "
            "WHERE guid=?", (tpl_guid,)).fetchone()
        card_type, abilities, attributes = row or ("Troop", "[]", 0)
        next_id = db.execute(
            "SELECT COALESCE(MAX(id), 10000) + 1 FROM game_cards "
            "WHERE session_id=?", (session.session_id,)).fetchone()[0]
        card_uid = next_game_card_uid(db, session.session_id)
        columns = [
            "id", "session_id", "user_id", "card_uid", "template_guid",
            "card_template_id", "location", "position", "card_state",
            "card_abilities", "card_type", "card_attributes",
        ]
        values = [
            next_id, session.session_id, owner_id, card_uid, tpl_guid,
            tpl_guid, "hand", 100, 0, abilities, card_type, attributes,
        ]
        for column, value in (("owner_user_id", owner_id),
                              ("original_template_guid", tpl_guid),
                              ("gems", 0)):
            if column in existing:
                columns.append(column)
                values.append(value)
        db.execute(
            "INSERT INTO game_cards ({}) VALUES ({})".format(
                ",".join(columns), ",".join("?" for _ in columns)), values)
        created.append((card_uid, tpl_guid))
    db.commit()

    recipient = owner_uid(owner_id, pl_t, ai_t, bstate)
    for card_uid, tpl_guid in created:
        scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
        full_tpl, ct, name, cost, atk, defense, gems = handler._card_full_data(
            game, scid, tpl_guid)
        game.push_card_moved(scid, recipient, game_engine.ECardCollections.Hand,
                             game_engine.ECardLocations.Top, 1)
        game.push_card_updated(
            scid, recipient, game_engine.ECardCollections.Hand, ct,
            attack=atk, defense=defense, cost=cost, template_id=full_tpl,
            gems=gems, card_name=name)
        # Entering hand is distinct from drawing.  This is important for
        # cards whose trigger specifically says "when you draw".
        from ..triggers import resolve_triggers
        resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                         "CardEnteredZoneEvent", int(card_uid), owner_id)
        resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                         "ConscriptEvent", int(card_uid), owner_id)
    bstate["created_conscript_uids"] = [int(uid) for uid, _ in created]
    return f"conscript {len(created)} random card(s) to hand"


def summon_token(game, session, db, handler, pl_t, ai_t, bstate, effect_guid,
                 param):
    """Create token cards from structured effect parameters.

    The legacy text link remains only as a fallback for older extracted
    records; current records should provide ``token_guid`` and amount/location
    fields in ``param``.
    """
    ability_guid = (bstate or {}).get("resolving_ability", "")
    game_text = ""
    if ability_guid:
        g_row = db.execute(
            "SELECT game_text FROM card_abilities_meta WHERE ability_guid=?",
            (ability_guid,)).fetchone()
        if g_row:
            game_text = g_row[0] or ""
        else:
            c_row = db.execute(
                "SELECT game_text FROM champion_abilities WHERE ability_guid=?",
                (ability_guid,)).fetchone()
            if c_row:
                game_text = c_row[0] or ""

    def token_guid_from_text(text):
        match = re.search(
            r'data=([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})', text or "")
        return match.group(1).lower() if match else None

    def count_from_text(text):
        words = {"one": 1, "two": 2, "three": 3, "four": 4,
                 "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10}
        low = (text or "").lower()
        for pattern in (r'create\s+(?:a\s+)?(?:copy\s+of\s+)?([a-z]+)\b',
                        r'summon\s+(?:a\s+)?(?:copy\s+of\s+)?([a-z]+)\b'):
            match = re.search(pattern, low)
            if match and match.group(1) in words:
                return words[match.group(1)]
        return 1

    def token_name_from_text(text):
        match = re.search(r'<b>(.+?)</b>', text or "")
        if match:
            return match.group(1)
        match = re.search(
            r'[Ss]ummon\s+an?\s+([A-Za-z][\w\s]*?)(?:\s*\.|$)',
            text or "")
        return match.group(1).strip() if match else None

    token_guid = token_guid_from_text(game_text)
    count = count_from_text(game_text)
    into_deck = ("into your deck" in game_text.lower() or
                 "into their deck" in game_text.lower())
    token_name = "token troop"
    enters_exhausted = 0
    deck_location = "Unknown"
    into_hand = False
    param_has_dynamic_amount = False
    amount_var = ""
    resolved_removed_amount = None
    param_filter = None
    if param:
        try:
            p = json.loads(param) if isinstance(param, str) else param
            if p.get("token_guid"):
                token_guid = p["token_guid"]
            if isinstance(p.get("card_filter"), dict):
                param_filter = p["card_filter"]
            if p.get("collection") == "Deck":
                into_deck = True
            if str(p.get("collection") or "").lower() == "hand":
                into_hand = True
            deck_location = p.get("location", "Unknown")
            amount_var = p.get("amount_variable", "")
            param_has_dynamic_amount = bool(amount_var)
            if amount_var and ability_guid:
                match = re.search(r'ForEach(\w+?)RemovedThisWay', amount_var)
                if match:
                    counter_name = re.sub(r'Counter$', '', match.group(1)).lower()
                    source_uid = (bstate or {}).get("resolving_source_uid")
                    if source_uid is not None:
                        from .counters import (card_counters,
                                               remove_card_counters,
                                               push_card_counters)
                        have = card_counters(
                            db, session.session_id, source_uid).get(counter_name, 0)
                        if have > 0:
                            remove_card_counters(
                                db, session.session_id, source_uid, counter_name)
                            push_card_counters(
                                game, session, db, handler, pl_t, ai_t, source_uid,
                                changed_counter=counter_name, old_value=have)
                            from ..kill_troop import kill_troop
                            kill_troop(game, session, db, handler, pl_t, ai_t,
                                       int(source_uid), bstate, cause="sacrifice")
                        count = int(have)
                        # This variable means "the number removed by this
                        # effect", not the counter value at the end of the
                        # effect.  The later typed-variable refresh must not
                        # read the now-cleared counter and replace the saved
                        # count with zero.
                        resolved_removed_amount = int(have)
                    else:
                        count = 0
                else:
                    raw = db.execute(
                        "SELECT raw_json FROM card_abilities_meta "
                        "WHERE ability_guid=?", (ability_guid,)).fetchone()
                    if raw and raw[0]:
                        match = re.search(
                            r'"m_Name"\s*:\s*"' + re.escape(amount_var) +
                            r'"\s*,\s*"m_DefaultValue"\s*:\s*(\d+)', raw[0])
                        if match:
                            count = int(match.group(1))
            if not amount_var and p.get("amount"):
                count = int(p["amount"])
            enters_exhausted = int(p.get("exhausted", 0) or 0)
        except (ValueError, TypeError, json.JSONDecodeError):
            pass

    # Prefer the typed AbilityEffectTemplate fields used by the client.  The
    # extracted param remains a compatibility fallback for older databases.
    typed_guid = effect_template_value(
        db, bstate, effect_guid, "m_CardTemplateId")
    if typed_guid and str(typed_guid).lower() != \
            "00000000-0000-0000-0000-000000000000":
        token_guid = typed_guid
    typed_collection = effect_template_value(
        db, bstate, effect_guid, "m_CardCollection")
    if typed_collection:
        into_deck = str(typed_collection).lower() == "deck"
        into_hand = str(typed_collection).lower() == "hand"
    typed_location = effect_template_value(
        db, bstate, effect_guid, "m_CardLocation")
    if typed_location:
        deck_location = str(typed_location)
    if not param_has_dynamic_amount:
        typed_amount = effect_field(
            db, bstate, effect_guid, "m_Amount", default=0)
        if typed_amount > 0:
            count = typed_amount
        typed_amount_field = effect_field(
            db, bstate, effect_guid, "m_AmountField", default=0)
        if typed_amount_field > 0:
            count = typed_amount_field

    # An EffectInputVariable is not a literal value.  In particular, the
    # Briarpatch Conjuror's amount is a CounterVariable whose value must be
    # recalculated after the preceding effect group adds the seed counter.
    # Resolve it against the current DB state rather than using its default
    # value (zero) or the text fallback (one).
    amount_field = (effect_template(effect_guid) or {}).get("m_AmountField")
    typed_amount_var = ""
    if isinstance(amount_field, dict):
        typed_amount_var = (amount_field.get("m_InputVariableName") or
                            amount_field.get("m_VariableName") or "")
    dynamic_amount_var = amount_var or typed_amount_var
    if dynamic_amount_var:
        from ..statics import ability_variable_value
        owner_id = (bstate or {}).get("resolving_owner_id")
        source_uid = (bstate or {}).get("resolving_source_uid")
        resolved_amount = resolved_removed_amount
        if resolved_amount is None:
            resolved_amount = ability_variable_value(
                db, session.session_id, bstate, ability_guid,
                dynamic_amount_var, owner_id if owner_id is not None else 0,
                source_uid)
        if resolved_amount is not None:
            count = max(0, int(resolved_amount))
    typed_exhausted = effect_template_value(
        db, bstate, effect_guid, "m_EntersPlayExhausted")
    if typed_exhausted is not None:
        enters_exhausted = int(typed_exhausted or 0)

    # Some summon effects name a concrete token through m_CardTemplateId;
    # others, such as Moqui's power, leave that GUID empty and provide a
    # typed m_CardFilter instead. Resolve the latter from card-template
    # metadata so the selected card remains random and data-driven.
    if not token_guid:
        typed_filter = ((effect_template(effect_guid) or {}).get("m_CardFilter")
                        or param_filter)
        if isinstance(typed_filter, dict):
            source_uid = (bstate or {}).get("resolving_source_uid")
            source_owner = (bstate or {}).get("resolving_owner_id")
            candidates = _random_template_guids(
                db, typed_filter, source_uid, source_owner, bstate)
            if candidates:
                token_guid = random.choice(candidates)

    tpl_row = None
    if token_guid:
        tpl_row = db.execute(
            "SELECT guid FROM card_templates WHERE guid=?", (token_guid,)
        ).fetchone()
    if not tpl_row:
        token_name = token_name_from_text(game_text)
        if token_name:
            tpl_row = db.execute(
                "SELECT guid FROM card_templates WHERE LOWER(name)=LOWER(?) "
                "LIMIT 1", (token_name,)).fetchone()
    if not tpl_row:
        token_name = token_name_from_text(game_text)
        if token_name:
            tpl_row = db.execute(
                "SELECT guid FROM card_templates WHERE LOWER(name) LIKE ? "
                "LIMIT 1", ("%" + token_name.lower() + "%",)).fetchone()
    if not tpl_row:
        return f"summon {token_name}: no card template found"

    tpl_guid = tpl_row[0]
    # A granted start-of-game ability can be visited more than once while
    # setup dispatches both players' trigger passes.  Keep encounter token
    # creation idempotent for the same owner/template in that event.
    if (bstate or {}).get("event_type") == "GameStartedEvent":
        owner_key = (bstate or {}).get("resolving_owner_id")
        token_key = (int(owner_key or 0), str(tpl_guid).lower())
        created_keys = (bstate or {}).setdefault("created_start_tokens", [])
        if token_key in created_keys:
            return f"summon {token_name}: already created"
        created_keys.append(token_key)
    player_uid = (bstate or {}).get("resolving_owner_id")
    if player_uid is None:
        player_uid = handler.user_profile["id"] if handler.user_profile else 0
    # Some token effects target a champion other than the ability's
    # controller. Incubate is the deck-bound example, while Spiderling Egg's
    # trigger is the battlefield example: "a random opposing champion
    # summons" the token. In both cases the resolved target's controller is
    # authoritative; resolving_owner_id is only the caster/trigger source.
    target_uid = ((bstate or {}).get("player_spell_target")
                  or (bstate or {}).get("player_mod_target")
                  or (bstate or {}).get("resolving_target_uid"))
    # Some automatic AI activations do not carry a client TargetInstance.
    # For deck-bound token effects, the target template still identifies the
    # opposing champion whose deck receives the token. Resolve that target
    # from the typed metadata before falling back to the caster below.
    if target_uid is None and into_deck:
        try:
            from ..bom import _opposing_champion_uid
            target_uid = _opposing_champion_uid(
                handler, bstate, db, session)
        except (AttributeError, TypeError, ValueError):
            target_uid = None
    if target_uid is not None:
        try:
            from ..bom import (_controller_id_for_target,
                               _deck_owner_for_target)
            target_owner = _controller_id_for_target(
                db, session, handler, bstate, target_uid)
            target_is_champion = any(
                int(row[0]) == int(target_uid)
                for row in (handler._champion_targets()
                            if callable(getattr(handler, "_champion_targets", None))
                            else []))
            if target_is_champion and target_owner is not None:
                player_uid = target_owner
            elif into_deck:
                # Fallback for PvE/PvP target representations where the
                # target is a champion but the handler does not expose a
                # target list (the deck-owner helper also knows champ_map).
                target_owner = _deck_owner_for_target(
                    db, handler, session, bstate, target_uid)
                if target_owner is not None:
                    player_uid = target_owner
        except (AttributeError, TypeError, ValueError):
            pass
    # A token put into hand has not entered the warzone, so it must not carry
    # the warzone-only CameOutThisTurn state.  That state would otherwise
    # leak into the hand and make the created card look like a summoned troop.
    token_state = (0 if into_hand else game_engine.ECardStates.CameOutThisTurn)
    if enters_exhausted:
        token_state |= game_engine.ECardStates.Tapped

    try:
        subtype = db.execute(
            "SELECT subtype FROM card_templates WHERE guid=?", (tpl_guid,)
        ).fetchone()
        if subtype and "shin'hare" in (subtype[0] or "").lower():
            from ..statics import controller_flags
            if "shinhare_plus_one" in controller_flags(
                    db, session.session_id, bstate, player_uid):
                count *= 2
    except Exception:
        pass

    created_cards = []
    for index in range(count):
        next_id = db.execute(
            "SELECT COALESCE(MAX(id), 10000) + 1 FROM game_cards "
            "WHERE session_id=?", (session.session_id,)).fetchone()[0]
        card_uid = next_game_card_uid(db, session.session_id)
        location = ("hand" if into_hand else
                    ("deck" if into_deck else "warzone"))
        # Unknown deck location means the card is shuffled into the deck.
        # Start it at a temporary position; after all cards are created the
        # deck-relative insertion helper assigns an unbiased permutation.
        position = (100 if into_hand else
                    (0 if into_deck and deck_location == "Unknown" else
                     (9999 if into_deck else 0)))
        template = db.execute(
            "SELECT card_type, abilities_json, attributes FROM card_templates "
            "WHERE guid=?", (tpl_guid,)).fetchone()
        card_type = template[0] if template else "Troop"
        abilities = template[1] if template else "[]"
        attributes = template[2] if template else 0
        columns = [
            "id", "session_id", "user_id", "card_uid", "template_guid",
            "card_template_id", "location", "position", "card_state",
            "card_abilities", "card_type", "card_attributes",
        ]
        values = [
            next_id, session.session_id, player_uid, card_uid, tpl_guid,
            tpl_guid, location, position, token_state, abilities, card_type,
            attributes,
        ]
        existing = {row[1] for row in db.execute(
            "PRAGMA table_info(game_cards)").fetchall()}
        for column, value in (("owner_user_id", player_uid),
                              ("original_template_guid", tpl_guid),
                              ("gems", 0)):
            if column in existing:
                columns.append(column)
                values.append(value)
        db.execute(
            "INSERT INTO game_cards ({}) VALUES ({})".format(
                ",".join(columns), ",".join("?" for _ in columns)), values)
        created_cards.append(card_uid)
    db.commit()
    if created_cards and into_deck and deck_location == "Unknown":
        from db import db_randomly_insert_deck_cards
        db_randomly_insert_deck_cards(
            session.session_id, int(player_uid), created_cards, connection=db)
    if created_cards:
        bstate["created_token_uids"] = [int(uid) for uid in created_cards]

    if created_cards:
        from ..triggers import resolve_triggers
    for card_uid in created_cards:
        resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                         "CardCreatedEvent", card_uid, player_uid,
                         zones=())

    owner = pl_t if player_uid != 0 else ai_t
    for card_uid in created_cards:
        scid = game_engine.SessionCardId(game_engine.UID(card_uid))
        tpl_guid2, ct, token_name, cost, atk, defense, gem = (
            handler._card_full_data(game, scid, tpl_guid))
        if into_hand:
            game.push_card_moved(scid, owner, game_engine.ECardCollections.Hand,
                                 game_engine.ECardLocations.Top, 1)
            game.push_card_updated(scid, owner, game_engine.ECardCollections.Hand,
                                   ct, template_id=tpl_guid2, attack=atk,
                                   defense=defense, cost=cost, gems=gem,
                                   card_name=token_name, state=0)
        elif into_deck:
            game.push_card_moved(scid, owner, game_engine.ECardCollections.Deck,
                                 game_engine.ECardLocations.Top, 1)
            game.push_card_updated(scid, owner, game_engine.ECardCollections.Deck,
                                   ct, template_id=tpl_guid2, nulling=True)
        else:
            game.push_card_moved(
                scid, owner, game_engine.ECardCollections.Warzone,
                game_engine.ECardLocations.Top, 1)
            game.push_card_updated(
                scid, owner, game_engine.ECardCollections.Warzone, ct,
                attack=atk, defense=defense, cost=cost, template_id=tpl_guid2,
                gems=gem, card_name=token_name, state=token_state)

    if into_hand and created_cards:
        from ..triggers import resolve_triggers
        for card_uid in created_cards:
            resolve_triggers(
                db, handler, game, session, pl_t, ai_t, bstate,
                "CardEnteredZoneEvent", card_uid, player_uid)
    elif not into_deck and created_cards:
        from ..triggers import resolve_enters_play_triggers
        for card_uid in created_cards:
            resolve_enters_play_triggers(
                db, handler, game, session, pl_t, ai_t, bstate, card_uid,
                player_uid, 0)
    destination = "hand" if into_hand else ("into deck" if into_deck
                                             else "to warzone")
    return f"summon {count}x {token_name} {destination}"

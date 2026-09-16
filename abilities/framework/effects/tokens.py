"""Token and generated-card creation effects."""

import json
import random
import re
import sqlite3

import game_engine

from ..fields import effect_field, effect_template, effect_template_value
from ..targeting import (evaluate_card_filter, shards_from_threshold,
                         template_faction)
from .._shared import next_game_card_uid, owner_uid


_DECK_TEMPLATES = None


def _creation_replacement_abilities(db, ability_guids):
    """Return typed creation-replacement metadata on an ability list.

    The client encodes Reese's replacement as an IntAttrModifier. Keep the
    server keyed to that typed attribute and the linked card template in the
    ability record; do not use a card name or localized game text as a rule.
    """
    out = []
    from ..fields import modifier_metadata
    from pvp_db import (db_ability_effect_metadata_rows,
                        db_ability_raw_json, db_template_exists)
    for ability_guid in ability_guids or []:
        for effect_guid, effect_type in db_ability_effect_metadata_rows(
                ability_guid, conn=db):
            if effect_type != "CardModifierAbilityEffectTemplate":
                continue
            metadata = modifier_metadata(effect_guid)
            attribute = str(metadata.get("attribute") or "")
            if (metadata.get("property") != "intattr" or
                    "create" not in attribute.lower() or
                    "instead" not in attribute.lower()):
                continue
            raw = db_ability_raw_json(ability_guid, conn=db)
            links = []
            for guid in re.findall(
                    r"data=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
                    raw[0] if raw else ""):
                guid = guid.lower()
                if db_template_exists(guid, conn=db):
                    links.append(guid)
            out.append((attribute, tuple(dict.fromkeys(links))))
    return out


def activate_creation_replacements_for_card(db, session_id, card_uid):
    """Activate authored creation replacements when a source surfaces.

    Reese's current card ability list is retained as the client-facing source
    of the replacement. The typed IntAttr is also persisted on the instance,
    because replacement effects are consulted when a later token is created
    after the source has returned to play.
    """
    from pvp_db import (db_card_grant_info, db_card_mutation_field,
                        db_set_card_abilities, db_set_card_mutation_field)
    row = db_card_grant_info(session_id, int(card_uid), conn=db)
    if not row:
        return False
    try:
        current = [str(g).lower() for g in json.loads(row[1] or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        current = []
    try:
        from pvp_db import db_card_template_ability_payload
        template_abilities = json.loads(
            db_card_template_ability_payload(row[1], conn=db) or "[]")
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        template_abilities = []
    combined = list(dict.fromkeys(current + [str(g).lower()
                                               for g in template_abilities]))
    try:
        replacements = _creation_replacement_abilities(db, combined)
    except (AttributeError, TypeError, sqlite3.Error):
        # Minimal headless adapters may not expose the full static metadata
        # projection; tunneling itself must still remain valid there.
        return False
    if not replacements:
        return False
    try:
        saved = json.loads(db_card_mutation_field(
            session_id, int(card_uid), "permanent_buffs", conn=db) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    markers = saved.setdefault("int_attrs", {})
    changed = False
    for attribute, _links in replacements:
        if int(markers.get(attribute, 0) or 0) != 1:
            markers[attribute] = 1
            changed = True
    if combined != current:
        changed = True
    if not changed:
        return False
    db_set_card_abilities(session_id, int(card_uid), json.dumps(combined),
                          conn=db)
    db_set_card_mutation_field(session_id, int(card_uid), "permanent_buffs",
                               json.dumps(saved), conn=db)
    db.commit()
    return True


def _replacement_token_guid(db, session_id, owner_id, token_guid, bstate,
                            source_uid):
    """Resolve an authored Worker Bot creation replacement, if active."""
    if not token_guid:
        return token_guid
    from pvp_db import (db_cards_in_zones_with_abilities,
                        db_card_mutation_field)
    rows = db_cards_in_zones_with_abilities(
        session_id, int(owner_id or 0), ("warzone", "underground"), conn=db)
    for card_uid, abilities_json in rows:
        try:
            abilities = json.loads(abilities_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            abilities = []
        try:
            buffs = json.loads(db_card_mutation_field(
                session_id, int(card_uid), "permanent_buffs", conn=db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        attrs = buffs.get("int_attrs", {}) if isinstance(buffs, dict) else {}
        for attribute, linked_templates in _creation_replacement_abilities(
                db, abilities):
            if not int(attrs.get(attribute, 0) or 0) or \
                    str(token_guid).lower() not in linked_templates:
                continue
            robot_filter = {
                "_t": "Game.Shared.Mechanics.Cards.Filters.IsSubType",
                "m_SubType": "Robot",
            }
            candidates = _random_template_guids(
                db, robot_filter, source_uid, owner_id, bstate)
            if candidates:
                return random.choice(candidates)
    return token_guid


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
    from pvp_db import (db_deck_next_position, db_copy_template_payload,
                        db_next_game_card_row_id, db_insert_generated_card)
    position = db_deck_next_position(session.session_id, owner_id, conn=db)
    created = 0
    for entry in deck.get("m_DeckResources") or []:
        if not isinstance(entry, dict):
            continue
        tpl_guid = str(((entry.get("m_idTemplate") or {}).get("m_Guid")
                        or "")).lower()
        if not tpl_guid:
            continue
        count = max(0, int(entry.get("m_Count") or 0))
        row = db_copy_template_payload(tpl_guid, conn=db)
        if not row or str(row[0] or "").split("|")[0] == "Champion":
            continue
        card_type, abilities, attributes = row
        for _ in range(count):
            card_uid = next_game_card_uid(db, session.session_id)
            db_insert_generated_card(
                session.session_id, owner_id, card_uid, tpl_guid, "deck",
                card_type, abilities, attributes,
                db_next_game_card_row_id(session.session_id, conn=db),
                conn=db, position=position, card_state=0,
                owner_user_id=owner_id, original_template_guid=tpl_guid)
            position += 1
            created += 1
    db.commit()
    return f"loaded {created} card(s) into player deck"


def _random_template_guids(db, filter_json, source_uid, owner_id,
                           bstate=None, session=None):
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
        from pvp_db import db_template_catalog_for_filter
        rows = db_template_catalog_for_filter(conn=db)
    except Exception:
        return []
    candidates = []
    banned_guids = set()
    if (bstate or {}).get("pvp"):
        # Tournament modes carry their type in persisted encounter data.
        # Apply bans at the shared typed template-pool boundary so generated
        # card effects, including Corinth's champion choice, agree.
        try:
            tournament_type_id = int(
                (getattr(session, "encounter_data", {}) or {}).get(
                    "tournament_type_id", 0) or 0)
            if tournament_type_id:
                from tournament_db import db_tournament_banned_card_guids
                banned_guids = db_tournament_banned_card_guids(
                    tournament_type_id, conn=db)
        except (TypeError, ValueError, ImportError):
            banned_guids = set()
    for row in rows:
        if str(row[0]).lower() in banned_guids:
            continue
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
        db, typed_filter, source_uid, owner_id, bstate, session)
    if not candidates or amount <= 0:
        return "conscript: no matching card template"

    created = []
    for _ in range(amount):
        tpl_guid = random.choice(candidates)
        from pvp_db import (db_copy_template_payload, db_next_game_card_row_id,
                            db_insert_generated_card)
        row = db_copy_template_payload(tpl_guid, conn=db)
        card_type, abilities, attributes = row or ("Troop", "[]", 0)
        card_uid = next_game_card_uid(db, session.session_id)
        db_insert_generated_card(
            session.session_id, owner_id, card_uid, tpl_guid, "hand",
            card_type, abilities, attributes,
            db_next_game_card_row_id(session.session_id, conn=db), conn=db,
            position=100, card_state=0, owner_user_id=owner_id,
            original_template_guid=tpl_guid, gems=0)
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

    The parameter is an adapter generated from the current typed
    AbilityEffectTemplate. Localized game text is not a rules input.
    """
    ability_guid = (bstate or {}).get("resolving_ability", "")
    token_guid = None
    count = 1
    into_deck = False
    token_name = "token troop"
    enters_exhausted = 0
    deck_location = "unknown"
    into_hand = False
    into_choosing = False
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
            collection = str(p.get("collection") or "").rsplit(
                ".", 1)[-1].lower()
            if collection == "deck":
                into_deck = True
            if collection == "hand":
                into_hand = True
            if collection == "choosing":
                into_choosing = True
            deck_location = p.get("location", "unknown")
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
                    # A named variable is resolved below from the typed
                    # AbilityTemplate. Do not recover its default from a
                    # materialized JSON row.
                    pass
            if not amount_var and p.get("amount"):
                count = int(p["amount"])
            enters_exhausted = int(p.get("exhausted", 0) or 0)
        except (ValueError, TypeError, json.JSONDecodeError):
            pass

    # Confirm the typed AbilityEffectTemplate fields used by the client.
    typed_guid = effect_template_value(
        db, bstate, effect_guid, "m_CardTemplateId")
    if typed_guid and str(typed_guid).lower() != \
            "00000000-0000-0000-0000-000000000000":
        token_guid = typed_guid
    typed_collection = effect_template_value(
        db, bstate, effect_guid, "m_CardCollection")
    if typed_collection:
        collection = str(typed_collection).rsplit(".", 1)[-1].lower()
        into_deck = collection == "deck"
        into_hand = collection == "hand"
        into_choosing = collection == "choosing"
    typed_location = effect_template_value(
        db, bstate, effect_guid, "m_CardLocation")
    if typed_location:
        deck_location = str(typed_location)
    deck_location = str(deck_location or "unknown").rsplit(".", 1)[-1].lower()
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
    typed_attacking = effect_template_value(
        db, bstate, effect_guid, "m_EntersPlayAttacking")
    copy_gems = bool(effect_template_value(
        db, bstate, effect_guid, "m_CopyGems") or 0)

    # Some summon effects name a concrete token through m_CardTemplateId;
    # others, such as Moqui's power, leave that GUID empty and provide a
    # typed m_CardFilter instead. Resolve the latter from card-template
    # metadata so the selected card remains random and data-driven.
    # A filter-driven summon deliberately serializes an all-zero
    # m_CardTemplateId. Treat that sentinel as unset so the typed CardFilter
    # can choose the random template. Keeping the zero GUID truthy here made
    # Primordial Caves skip its Dinosaur filter and produce no token.
    if str(token_guid or "").lower() == \
            "00000000-0000-0000-0000-000000000000":
        token_guid = None
    if not token_guid:
        typed_filter = ((effect_template(effect_guid) or {}).get("m_CardFilter")
                        or param_filter)
        if isinstance(typed_filter, dict):
            source_uid = (bstate or {}).get("resolving_source_uid")
            source_owner = (bstate or {}).get("resolving_owner_id")
            candidates = _random_template_guids(
                db, typed_filter, source_uid, source_owner, bstate, session)
            if candidates:
                token_guid = random.choice(candidates)

    tpl_row = None
    if token_guid:
        from pvp_db import db_template_exists
        if db_template_exists(token_guid, conn=db):
            tpl_row = (token_guid,)
    if not tpl_row:
        return "summon token: no typed card template found"

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
    source_uid = (bstate or {}).get("resolving_source_uid")
    replacement = _replacement_token_guid(
        db, session.session_id, player_uid, token_guid, bstate, source_uid)
    if replacement and str(replacement).lower() != str(token_guid).lower():
        token_guid = replacement
        tpl_guid = str(replacement).lower()
        tpl_row = (tpl_guid,)
    # A token put into hand has not entered the warzone, so it must not carry
    # the warzone-only CameOutThisTurn state.  That state would otherwise
    # leak into the hand and make the created card look like a summoned troop.
    token_state = (0 if into_hand or into_choosing
                   else game_engine.ECardStates.CameOutThisTurn)
    if enters_exhausted:
        token_state |= game_engine.ECardStates.Tapped
    if typed_attacking and not into_hand and not into_deck and not into_choosing:
        token_state |= game_engine.ECardStates.Attacking

    copied_gems = 0
    if copy_gems:
        source_uid = (bstate or {}).get("resolving_source_uid")
        if source_uid is not None:
            from pvp_db import db_card_gem_type
            copied_gems = int(db_card_gem_type(
                session.session_id, int(source_uid), conn=db) or 0)

    try:
        from pvp_db import db_template_subtype
        subtype = db_template_subtype(tpl_guid, conn=db)
        if subtype and "shin'hare" in subtype.lower():
            from ..statics import controller_flags
            if "shinhare_plus_one" in controller_flags(
                    db, session.session_id, bstate, player_uid):
                count *= 2
    except Exception:
        pass

    created_cards = []
    from pvp_db import db_resolve_talent_modified_template
    resolving_owner = int((bstate or {}).get("resolving_owner_id", 0) or 0)
    profile = getattr(handler, "user_profile", None)
    player_id = int((profile.get("id", 0) if isinstance(profile, dict)
                     else getattr(profile, "id", 0)) or 0)
    active_talents = (getattr(handler, "_player_talent_guids", ())
                      if resolving_owner == player_id else
                      getattr(handler, "_ai_talent_guids", ()))
    tpl_guid = db_resolve_talent_modified_template(
        tpl_guid, active_talents, conn=db)
    for index in range(count):
        card_uid = next_game_card_uid(db, session.session_id)
        location = ("hand" if into_hand else
                    ("deck" if into_deck else
                     ("choosing" if into_choosing else "warzone")))
        # Unknown deck location means the card is shuffled into the deck.
        # Start it at a temporary position; after all cards are created the
        # deck-relative insertion helper assigns an unbiased permutation.
        if into_deck and deck_location == "bottom":
            from pvp_db import db_deck_next_position
            position = db_deck_next_position(session.session_id, player_uid,
                                             conn=db)
        else:
            position = (100 if into_hand else
                        (9999 if into_deck and deck_location in
                         ("", "unknown", "random") else
                         (0 if into_deck else 0)))
        from pvp_db import (db_copy_template_payload, db_next_game_card_row_id,
                            db_insert_generated_card)
        template = db_copy_template_payload(tpl_guid, conn=db)
        card_type = template[0] if template else "Troop"
        abilities = template[1] if template else "[]"
        attributes = template[2] if template else 0
        parent_data = {}
        if (bstate or {}).get("resolving_source_uid") is not None:
            parent_data["parent_uid"] = int(
                bstate["resolving_source_uid"])
        db_insert_generated_card(
            session.session_id, player_uid, card_uid, tpl_guid, location,
            card_type, abilities, attributes,
            db_next_game_card_row_id(session.session_id, conn=db), conn=db,
            position=position, card_state=token_state,
            owner_user_id=player_uid, original_template_guid=tpl_guid,
            gems=copied_gems, permanent_buffs=json.dumps(parent_data))
        created_cards.append(card_uid)
    db.commit()
    if created_cards and into_deck and deck_location in ("", "unknown", "random"):
        from pvp_db import db_randomly_insert_deck_cards
        db_randomly_insert_deck_cards(
            session.session_id, int(player_uid), created_cards, connection=db)
    if created_cards:
        bstate["created_token_uids"] = [int(uid) for uid in created_cards]

    from ..triggers import resolve_triggers
    for card_uid in created_cards:
        # The original client distinguishes the created card's own
        # CardCreatedEvent from the event seen by other cards.  Replica/
        # creation triggers (for example Restless Fabricator) listen to the
        # latter, so publish both from the shared token factory.
        resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                         "OtherCardCreatedEvent", card_uid, player_uid)
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
        elif into_choosing:
            game.push_card_moved(
                scid, owner, game_engine.ECardCollections.Choosing,
                game_engine.ECardLocations.Top, 1)
            game.push_card_updated(
                scid, owner, game_engine.ECardCollections.Choosing, ct,
                attack=atk, defense=defense, cost=cost, template_id=tpl_guid2,
                gems=gem, card_name=token_name, state=0)
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
    elif not into_deck and not into_choosing and created_cards:
        from ..triggers import resolve_enters_play_triggers
        for card_uid in created_cards:
            resolve_enters_play_triggers(
                db, handler, game, session, pl_t, ai_t, bstate, card_uid,
                player_uid, 0)
    destination = ("hand" if into_hand else
                   ("into deck" if into_deck else
                    ("to choosing" if into_choosing else "to warzone")))
    return f"summon {count}x {token_name} {destination}"

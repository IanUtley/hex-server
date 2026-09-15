"""RulesPort-owned generated-card effect operations."""

from __future__ import annotations

import random
import json
import game_engine


def _random_socket_gems(template_guid, connection):
    """Build the client's positional gem bitfield for a generated card.

    CardTemplate.m_SocketCount is only the total.  The authoritative minor /
    major split is the authored ``SOCKETABLE MINOR/MAJOR`` text, which the
    client turns into GetMinorSocketCount/GetMajorSocketCount.  Major slots
    accept either gem class; minor slots accept only minor gems.
    """
    from gamedata import DEFAULT_RECORD_STORE

    card = DEFAULT_RECORD_STORE.get("CardTemplate", str(template_guid))
    game_text = str(card.field("m_GameText", "") or "").lower() \
        if card is not None else ""
    major_count = game_text.count("socketable major")
    minor_count = game_text.count("socketable minor")
    columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(card_templates)").fetchall()}
    socket_expr = "socket_count" if "socket_count" in columns else "0"
    row = connection.execute(
        "SELECT " + socket_expr + " FROM card_templates WHERE guid=?",
        (str(template_guid).lower(),)).fetchone()
    socket_count = int(row[0] or 0) if row else 0
    if socket_count <= 0:
        return 0
    # A few old/equipment records have only the total count. Treat those as
    # minor-only, which is the safe compatibility direction.
    if major_count + minor_count == 0:
        minor_count = socket_count
    total = min(socket_count, major_count + minor_count)
    gem_rows = connection.execute(
        "SELECT gem_type, gem_type_name FROM gem_templates WHERE gem_type > 0"
    ).fetchall()
    major = [int(r[0]) for r in gem_rows
             if "_major" in str(r[1] or "").lower()]
    minor = [int(r[0]) for r in gem_rows
             if "_minor" in str(r[1] or "").lower()]
    if not major or not minor:
        return 0
    packed = 1 << 62  # EGemTypesNew.Unknown / GemFormatBit
    position = 0
    # Fill major positions first, but choose from both pools.  This allows a
    # minor gem in a major slot while preserving the client's positional
    # layout; only the subsequent minor-only positions are restricted.
    available = major + minor
    for gem_type in random.sample(available, min(major_count, len(available))):
        packed |= int(gem_type) << (position * 10)
        position += 1
        available.remove(gem_type)
    remaining_minor = [gem_type for gem_type in minor
                       if gem_type in available]
    for gem_type in random.sample(remaining_minor,
                                  min(minor_count, len(remaining_minor))):
        if position >= total:
            break
        packed |= int(gem_type) << (position * 10)
        position += 1
    return packed


def create_matching_target(context, target, count, collection):
    """Create typed copies of a resolved target in the requested collection."""
    from pvp_db import (db_card_source_info, db_copy_template_payload,
                        db_next_game_card_row_id, db_insert_generated_card)
    source = db_card_source_info(context.session.session_id, int(target),
                                 conn=context.db)
    if not source:
        return 0
    template_guid, owner_id = source[0], int(source[3] or 0)
    payload = db_copy_template_payload(template_guid, conn=context.db)
    if not payload:
        return 0
    location = {"hand": "hand", "deck": "deck", "underground": "underground",
                "void": "void", "warzone": "warzone"}.get(
                    str(collection or "warzone").lower(), "warzone")
    from .runtime_helpers import card_collection_for_location, next_game_card_uid, owner_uid
    recipient = owner_uid(owner_id, context.player_uid, context.ai_uid,
                          context.bstate)
    created = []
    for _ in range(max(0, int(count))):
        uid = next_game_card_uid(context.db, context.session.session_id)
        db_insert_generated_card(
            context.session.session_id, owner_id, uid, template_guid, location,
            payload[0], payload[1], payload[2],
            db_next_game_card_row_id(context.session.session_id, conn=context.db),
            conn=context.db, owner_user_id=owner_id,
            original_template_guid=template_guid, gems=0)
        created.append(int(uid))
    context.db.commit()
    for uid in created:
        scid = game_engine.SessionCardId(game_engine.UID(uid))
        _tpl, card_type, _name, cost, attack, defense, gems = \
            context.handler._card_full_data(context.game, scid, template_guid)
        context.game.push_card_moved(
            scid, recipient, card_collection_for_location(location),
            game_engine.ECardLocations.Top, 0)
        context.game.push_card_updated(
            scid, recipient, card_collection_for_location(location), card_type,
            template_id=template_guid, cost=cost, attack=attack,
            defense=defense, gems=gems, nulling=location == "deck")
    return len(created)


def summon_token(context, payload=None):
    """Create typed token cards and dispatch their authored entry events."""
    from pvp_db import (db_copy_template_payload, db_insert_generated_card,
                        db_next_game_card_row_id, db_template_exists)
    payload = payload if isinstance(payload, dict) else {}
    guid = str(context.template_value("m_CardTemplateId", "") or
               payload.get("token_guid") or "").lower()
    if guid.replace("-", "") == "0" * 32:
        guid = ""
    count = payload.get("amount")
    if count is None:
        # AmountField is the typed Records representation for dynamic counts
        # such as the charge power's AbilityConstant "Three".
        count = context.value("m_AmountField", default=None)
    if count is None:
        count = context.template_value("m_Amount", 1)
    try:
        count = max(0, int(count or 0))
    except (TypeError, ValueError):
        count = 0
    collection = str(context.template_value(
        "m_CardCollection", payload.get("collection", "Warzone")) or
        "Warzone").rsplit(".", 1)[-1].lower()
    location = {"deck": "deck", "hand": "hand", "choosing": "choosing",
                "underground": "underground", "void": "void"}.get(
                    collection, "warzone")
    card_filter = payload.get("card_filter")
    selected_guids = None
    candidate_count = None
    banned_guids = set()
    if not guid and card_filter is not None:
        # Event restrictions are authoritative data, just like the typed
        # card filter.  In Iconoclast, do not offer the client-authored bans.
        from gamemodes.tournament_engine import tournament_id_from_session_name
        from tournament_db import (db_tournament_banned_card_guids,
                                   db_tournament_room_for_game)
        tournament_id = tournament_id_from_session_name(
            getattr(context.session, "session_name", ""))
        room = (db_tournament_room_for_game(tournament_id, conn=context.db)
                if tournament_id else None)
        if room and int(room.get("type_id", 0) or 0) == 4:
            banned_guids = db_tournament_banned_card_guids(
                4, conn=context.db)
    if card_filter is None:
        card_filter = context.template_value("m_CardFilter")
    if not guid and card_filter is not None:
        from pvp_db import db_transform_candidate_templates, db_card_zone_details
        from rules_port.filters import records_filter_matches
        candidates = []
        owner = int(context.bstate.get("resolving_owner_id", 0) or 0)
        active_thresholds = (context.bstate.get(f"thresh_{owner}")
                            or context.bstate.get("player_threshold") or {})
        source_row = db_card_zone_details(
            context.session.session_id,
            int(context.bstate.get("resolving_source_uid", 0) or 0),
            conn=context.db)
        source = {"user_id": int(source_row[2]),
                  "owner_id": int(source_row[2]),
                  "controller_id": int(source_row[2])} if source_row else None
        for row in db_transform_candidate_templates(conn=context.db):
            if str(row[0]).lower() in banned_guids:
                continue
            threshold_json = row[5] or ""
            try:
                threshold_data = json.loads(threshold_json) if threshold_json else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                threshold_data = {}
            # Records' TAC filters consume the same normalized threshold
            # entries as runtime CardRepresentations.  The SQL candidate
            # projection is intentionally lightweight, so add that derived
            # view here rather than making the filter depend on SQLite rows.
            threshold_list = (threshold_data.get("list", [])
                              if isinstance(threshold_data, dict) else [])
            candidate_thresholds = []
            for color in threshold_list:
                try:
                    candidate_thresholds.append({
                        "color_flags": {0: 0, 1: 4, 2: 8, 3: 16,
                                         4: 32, 5: 64}.get(
                                             int(color), int(color)),
                        "quantity": 1})
                except (TypeError, ValueError):
                    continue
            candidate = {"card_uid": 0, "template_guid": row[0],
                         "name": row[1] or "", "card_type": row[2] or "",
                         "cost": int(row[3] or 0), "rarity": row[4] or "",
                         "shards": [], "thresholds": candidate_thresholds,
                         "subtype": row[6] or "",
                         "attributes": int(row[7] or 0), "user_id": owner}
            if records_filter_matches(candidate, card_filter,
                                      source=source, context=context,
                                      player={"resource_thresholds":
                                              active_thresholds}):
                candidates.append(row[0])
        if candidates:
            # Choice-zone effects such as the champion charge power present
            # separate options.  Select without replacement so one activation
            # cannot show the same card three times.
            selected_guids = random.sample(candidates, min(count, len(candidates)))
            guid = selected_guids[0] if selected_guids else ""
        candidate_count = len(candidates)
    if not guid or not db_template_exists(guid, conn=context.db):
        return ("summon token: no typed card template found "
                f"(guid={guid!r}, candidates={candidate_count}, "
                f"count={count}, filter={card_filter is not None})")
    owner = int(context.bstate.get("resolving_owner_id", 0) or 0)
    target = context.resolved_target()
    target_owner = context.target_owner(target, default=owner)
    if location in {"deck", "hand"} and target_owner is not None:
        owner = int(target_owner)
    template = db_copy_template_payload(guid, conn=context.db)
    if not template:
        return "summon token: template payload missing"
    from .runtime_helpers import card_collection_for_location, next_game_card_uid, owner_uid
    recipient = owner_uid(owner, context.player_uid, context.ai_uid,
                          context.bstate)
    made = []
    guids_to_create = selected_guids if selected_guids is not None else [guid] * count
    for create_guid in guids_to_create:
        create_template = (template if create_guid == guid else
                           db_copy_template_payload(create_guid, conn=context.db))
        if not create_template:
            continue
        uid = next_game_card_uid(context.db, context.session.session_id)
        gem_type = _random_socket_gems(create_guid, context.db)
        db_insert_generated_card(
            context.session.session_id, owner, uid, create_guid, location,
            create_template[0], create_template[1], create_template[2],
            db_next_game_card_row_id(context.session.session_id, conn=context.db),
            conn=context.db, owner_user_id=owner,
            original_template_guid=create_guid, gems=gem_type)
        made.append((int(uid), create_guid))
    context.db.commit()
    for uid, created_guid in made:
        scid = game_engine.SessionCardId(game_engine.UID(uid))
        _tpl, card_type, name, cost, attack, defense, gems = \
            context.handler._card_full_data(context.game, scid, created_guid)
        collection_value = card_collection_for_location(location)
        context.game.push_card_moved(scid, recipient, collection_value,
                                     game_engine.ECardLocations.Top, 0)
        context.game.push_card_updated(
            scid, recipient, collection_value, card_type,
            template_id=created_guid, card_name=name, cost=cost, attack=attack,
            defense=defense, gems=gems, nulling=location == "deck")
        from .triggers import dispatch_trigger
        dispatch_trigger(context, "CardEnteredZoneEvent", uid, owner,
                         data={"event_destination_collection": location})
    # A Choosing summon only materializes an authored option.  The following
    # ActivateAbility/PlayCard effect owns the actual target boundary.  Do not
    # pause here: when an ability creates several options, pausing each summon
    # would turn one picker into one sequential picker per option.
    return f"summoned {len(made)} token(s)"


def create_token_copy(context):
    """Create authored replicas from the resolved target."""
    target = context.resolved_target()
    if target is None:
        return "copy: no target"
    from pvp_db import (db_card_zone_details, db_copy_template_payload,
                        db_next_game_card_row_id, db_insert_generated_card)
    from .runtime_helpers import next_game_card_uid, owner_uid
    row = db_card_zone_details(context.session.session_id, int(target),
                               conn=context.db)
    if not row:
        return "copy: target template missing"
    # Count and destination are typed effect semantics. Never infer either
    # from display text: that would make native RulesPort behavior depend on a
    # second, legacy card-text rules source.
    count = context.template_value("m_Count", None)
    if count is None:
        count = context.template_value("m_NumberOfCards", None)
    if count is None:
        # The authored single-copy form omits a count field; the typed
        # CreateTokenCopy contract defaults it to one.  Counted variants
        # still provide m_Count/m_NumberOfCards and remain data-driven.
        count = 1
    try:
        count = max(1, int(count))
    except (TypeError, ValueError):
        raise RuntimeError(
            "RulesPort CreateTokenCopy effect has invalid typed count")
    destination = str(context.template_value(
        "m_CardCollection", "") or "").rsplit(".", 1)[-1].lower()
    if not destination:
        raise RuntimeError(
            "RulesPort CreateTokenCopy effect is missing typed destination metadata")
    if context.bstate.get("choice_copy_to_hand"):
        destination = "hand"
    location = "hand" if destination == "hand" else "warzone"
    payload = db_copy_template_payload(row[0], conn=context.db)
    if not payload:
        return "copy: target template payload missing"
    owner_id = int(context.bstate.get("resolving_owner_id", 0) or 0)
    recipient = owner_uid(owner_id, context.player_uid, context.ai_uid,
                          context.bstate)
    import game_engine
    created = []
    from pvp_db import db_card_gem_type
    source_gem = int(db_card_gem_type(
        context.session.session_id, int(target), conn=context.db) or 0)
    for _ in range(count):
        uid = next_game_card_uid(context.db, context.session.session_id)
        db_insert_generated_card(
            context.session.session_id, owner_id, uid, row[0], location,
            payload[0], payload[1], payload[2],
            db_next_game_card_row_id(context.session.session_id, conn=context.db),
            conn=context.db, gems=source_gem)
        scid = game_engine.SessionCardId(game_engine.UID(uid))
        _tpl, card_type, _name, cost, attack, defense, _gems = \
            context.handler._card_full_data(context.game, scid, row[0])
        collection = (game_engine.ECardCollections.Hand if location == "hand"
                      else game_engine.ECardCollections.Warzone)
        context.game.push_card_moved(scid, recipient, collection,
                                     game_engine.ECardLocations.Top, 1)
        context.game.push_card_updated(
            scid, recipient, collection, card_type, template_id=row[0],
            cost=cost, attack=attack, defense=defense, gems=source_gem,
            nulling=False)
        created.append(uid)
    context.db.commit()
    for uid in created:
        from .triggers import dispatch_trigger
        dispatch_trigger(context, "OtherCardCreatedEvent", uid, owner_id)
        dispatch_trigger(context, "CardCreatedEvent", uid, owner_id,
                         data={"zones": ()})
    return f"copied {len(created)}x {row[0][:8]} {'to hand' if location == 'hand' else 'to warzone'}"

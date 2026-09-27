"""RulesPort-owned generated-card effect operations."""

from __future__ import annotations

import random
import json
from collections.abc import Iterable
from typing import Any
import game_engine


def _publish_created_card(context, card_uid, owner_id, card_type):
    """Publish C# creation events and the resolving ability's CreatedCards."""
    if str(card_type or "").lower() != "choice":
        from .statistics import record_ability_card_list
        record_ability_card_list(
            context.bstate, "CreatedCards", int(card_uid))
    from .triggers import dispatch_trigger
    # CreateCard queues OtherCardCreatedEvent, then calls
    # ActivateCardCreationAbilities synchronously before moving the new card.
    dispatch_trigger(
        context, "OtherCardCreatedEvent", int(card_uid), int(owner_id or 0))
    dispatch_trigger(
        context, "CardCreatedEvent", int(card_uid), int(owner_id or 0))


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


def _tac_flag(context, name):
    """Return a boolean Template Attribute Collection flag on the effect."""
    raw = context.template_value("m_SerializedTAC", None)
    if hasattr(raw, "field"):
        raw = raw.field("data", "") or ""
    if isinstance(raw, dict):
        raw = raw.get("data") or raw.get("Data") or ""
    if not raw:
        return False
    from .tac import tac_int
    return bool(tac_int(str(raw), name, 0))


def create_matching_target(context, target, count, collection,
                           deck_location=""):
    """Port of ``CreateTokenMatchingTargetAbilityEffectTemplate.Apply``.

    The effect creates its typed count of random cards from the authored
    ``m_CardFilter`` pool, evaluated against the resolved target card for the
    responsible player (the C# operand order).  The effect TAC ``SameName``
    narrows the pool to the target's own template, which is the authored
    "copy of that card" form; TAC ``SameOwner`` creates the cards under the
    target's controller.
    """
    from pvp_db import (db_card_zone_details, db_copy_template_payload,
                        db_next_game_card_row_id, db_insert_generated_card,
                        db_template_exists)
    responsible = int(context.bstate.get("resolving_owner_id", 0) or 0)
    if not responsible:
        # A test/legacy activation without a resolving owner creates the cards
        # under the target card's controller, which is the only player the
        # effect context identifies.
        responsible = int(context.target_owner(target, default=0) or 0)
    same_owner = _tac_flag(context, "SameOwner")
    owner_id = responsible
    if same_owner:
        owner_id = int(context.target_owner(
            target, default=responsible) or responsible)
    filter_spec = context.template_value("m_CardFilter", None)
    if hasattr(filter_spec, "to_dict"):
        filter_spec = filter_spec.to_dict()
    same_name = _tac_flag(context, "SameName")
    if same_name or not filter_spec:
        details = db_card_zone_details(
            context.session.session_id, int(target), conn=context.db)
        pool = [str(details[0]).lower()] if details else []
    else:
        pool = _matching_template_candidates(
            context, filter_spec, _authored_banned_guids(context),
            source_uid=int(target), player=responsible)
    if not pool:
        return 0
    location = {"hand": "hand", "deck": "deck", "underground": "underground",
                "void": "void", "warzone": "warzone"}.get(
                    str(collection or "warzone").lower(), "warzone")
    from .runtime_helpers import (card_collection_for_location,
                                  next_game_card_uid, owner_uid)
    recipient = owner_uid(owner_id, context.player_uid, context.ai_uid,
                          context.bstate)
    made = []
    for _ in range(max(0, int(count))):
        template_guid = str(random.choice(pool)).lower()
        if not db_template_exists(template_guid, conn=context.db):
            continue
        payload = db_copy_template_payload(template_guid, conn=context.db)
        if not payload:
            continue
        # C# creates one random card per CreateNTokensFromResource call, so
        # the per-template creation bonus applies to each created card.
        for _copy in range(1 + _creation_count_bonus(
                context, template_guid)):
            uid = next_game_card_uid(context.db, context.session.session_id)
            db_insert_generated_card(
                context.session.session_id, owner_id, uid, template_guid,
                location, payload[0], payload[1], payload[2],
                db_next_game_card_row_id(
                    context.session.session_id, conn=context.db),
                conn=context.db, owner_user_id=owner_id,
                original_template_guid=template_guid, gems=0)
            made.append((int(uid), template_guid, payload[0]))
    # C# MoveCardWithDispatch -> FinishMovingCard places a card moved into the
    # deck at ``RNG.Next(deck_count + 1)`` when its location is Unknown: the
    # cards are shuffled in and every other deck card keeps its relative
    # order.  Inserting them without a slot left all copies tied at position
    # 0, i.e. stacked on top of the deck, so the next draws were the copies.
    if (location == "deck" and made and
            str(deck_location or "").lower() in ("", "unknown", "random")):
        from pvp_db import db_randomly_insert_deck_cards
        db_randomly_insert_deck_cards(
            context.session.session_id, owner_id,
            [uid for uid, _guid, _card_type in made], connection=context.db)
    context.db.commit()
    for uid, template_guid, card_type_name in made:
        scid = game_engine.SessionCardId(game_engine.UID(uid))
        _tpl, card_type, _name, cost, attack, defense, gems = \
            context.handler._card_full_data(context.game, scid, template_guid)
        collection_value = card_collection_for_location(location)
        context.game.push_card_moved(
            scid, recipient, collection_value,
            game_engine.ECardLocations.Top, 0)
        context.game.push_card_updated(
            scid, recipient, collection_value, card_type,
            template_id=template_guid, cost=cost, attack=attack,
            defense=defense, gems=gems, nulling=location == "deck")
        _publish_created_card(context, uid, owner_id, card_type_name)
        from .triggers import dispatch_trigger
        dispatch_trigger(context, "CardEnteredZoneEvent", uid, owner_id,
                         data={"event_destination_collection": location})
    return len(made)


# ECardShards list index (Records ``m_Threshold`` order) -> shard flag.
_SHARD_FLAGS = {0: 0, 1: 4, 2: 8, 3: 16, 4: 32, 5: 64}


def _candidate_thresholds(threshold_data):
    """Shard requirements for one template, one entry per colour.

    Records stores the authored requirement twice: ``values`` is the per-shard
    count array and ``list`` is the same requirement flattened with one entry
    per shard ("Wild Wild Wild" -> [4, 4, 4]).  The flattened form must be
    aggregated back into counts before the TAC filter compares it with the
    player's thresholds; comparing entry-by-entry let a card needing three
    Wild through a player holding one.
    """
    if not isinstance(threshold_data, dict):
        return []
    values = threshold_data.get("values")
    if isinstance(values, (list, tuple)) and values:
        requirements = []
        for index, count in enumerate(values):
            try:
                count = int(count or 0)
            except (TypeError, ValueError):
                continue
            if count > 0:
                requirements.append({
                    "color_flags": _SHARD_FLAGS.get(index, index),
                    "quantity": count})
        return requirements
    counts = {}
    for color in threshold_data.get("list", []) or []:
        try:
            flag = _SHARD_FLAGS.get(int(color), int(color))
        except (TypeError, ValueError):
            continue
        counts[flag] = counts.get(flag, 0) + 1
    return [{"color_flags": flag, "quantity": quantity}
            for flag, quantity in counts.items()]


def _authored_banned_guids(context):
    """Return the authored banned-card set for a random creation.

    Event restrictions are authoritative data, just like the typed card
    filter.  In Iconoclast, do not offer the client-authored bans.
    """
    from gamemodes.tournament_engine import tournament_id_from_session_name
    from tournament_db import (db_tournament_banned_card_guids,
                               db_tournament_room_for_game)
    tournament_id = tournament_id_from_session_name(
        getattr(context.session, "session_name", ""))
    room = (db_tournament_room_for_game(tournament_id, conn=context.db)
            if tournament_id else None)
    if room and int(room.get("type_id", 0) or 0) == 4:
        return {str(guid).lower() for guid in
                db_tournament_banned_card_guids(4, conn=context.db)}
    return set()


def _matching_template_candidates(context, card_filter, banned_guids=(),
                                  source_uid=None, player=None):
    """Return the typed templates matching one Records ``CardFilter``.

    Every random generated-card effect (Conscript, the champion Choosing
    summons, CreateTokenMatchingTarget, and the authored Worker Bot
    replacement) draws from this pool, so the tournament bans and the filter
    semantics cannot diverge between them.  ``source_uid``/``player`` let
    CreateTokenMatchingTarget evaluate the filter against its resolved target
    card for the responsible player, which is the C# operand order
    (``GetPotentialRandomCardTemplates(filter, targetCard, responsiblePlayer)``).
    """
    from pvp_db import db_transform_candidate_templates, db_card_zone_details
    from rules_port.filters import records_filter_matches
    banned_guids = {str(guid).lower() for guid in banned_guids or ()}
    candidates = []
    owner = (int(player) if player is not None else
             int(context.bstate.get("resolving_owner_id", 0) or 0))
    active_thresholds = (context.bstate.get(f"thresh_{owner}")
                         or context.bstate.get("player_threshold") or {})
    if source_uid is None:
        source_uid = context.bstate.get("resolving_source_uid")
    source_row = db_card_zone_details(
        context.session.session_id, int(source_uid or 0), conn=context.db)
    rows = db_transform_candidate_templates(conn=context.db)
    by_guid = {str(row[0]).lower(): row for row in rows}
    source: dict[str, Any] | None = None
    if source_row:
        source = {"user_id": int(source_row[2]),
                  "owner_id": int(source_row[2]),
                  "controller_id": int(source_row[2])}
        source_template = by_guid.get(str(source_row[0]).lower())
        if source_template:
            source["template_guid"] = str(source_template[0]).lower()
            source["card_type"] = source_template[2] or ""
            source["cost"] = int(source_template[3] or 0)
            source["rarity"] = source_template[4] or ""
            source["subtype"] = source_template[6] or ""
            source["attributes"] = int(source_template[7] or 0)
            try:
                threshold_data = json.loads(source_template[5] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                threshold_data = {}
            if not isinstance(threshold_data, dict):
                threshold_data = {}
            source["shards"] = [
                _SHARD_FLAGS.get(int(item), int(item))
                for item in (threshold_data.get("list")
                             or threshold_data.get("values") or [])]
    for row in rows:
        if str(row[0]).lower() in banned_guids:
            continue
        threshold_json = row[5] or ""
        try:
            threshold_data = json.loads(threshold_json) if threshold_json else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            threshold_data = {}
        if not isinstance(threshold_data, dict):
            threshold_data = {}
        # Records' TAC filters consume the same normalized threshold entries
        # as runtime CardRepresentations.  The SQL candidate projection is
        # intentionally lightweight, so add that derived view here rather than
        # making the filter depend on SQLite rows.
        candidate_thresholds = _candidate_thresholds(threshold_data)
        candidate = {"card_uid": 0, "template_guid": row[0],
                     "name": row[1] or "", "card_type": row[2] or "",
                     "cost": int(row[3] or 0), "rarity": row[4] or "",
                     "shards": [
                         _SHARD_FLAGS.get(int(item), int(item))
                         for item in (threshold_data.get("list")
                                      or threshold_data.get("values") or [])],
                     "thresholds": candidate_thresholds,
                     "subtype": row[6] or "",
                     "attributes": int(row[7] or 0), "user_id": owner}
        if records_filter_matches(candidate, card_filter,
                                  source=source, context=context,
                                  player={"resource_thresholds":
                                          active_thresholds}):
            candidates.append(row[0])
    return candidates


_FUSE_GUIDS = None
_FUSE_LOCK = __import__("threading").RLock()


def _fuse_template_candidates(context, card_filter):
    """Return the fused-card templates matching one Records filter.

    ``SummonTokenTroopAbilityEffectTemplate`` with ``m_Terminus`` draws from
    ``Session.GetPotentialRandomFuseTemplates``: every card whose TAC carries
    a ``FusedComponents`` list (the fused Terminus results).
    """
    global _FUSE_GUIDS
    from gamedata import DEFAULT_RECORD_STORE
    from rules_port.filters import records_filter_matches
    from rules_port.tac import _tac_attr_hash, decode_tac_tree
    fused = _tac_attr_hash("FusedComponents")
    with _FUSE_LOCK:
        if _FUSE_GUIDS is None:
            _FUSE_GUIDS = []
            for card in DEFAULT_RECORD_STORE.load("CardTemplate"):
                tac = card.field("m_SerializedTAC")
                data = tac.get("data") if isinstance(tac, dict) else tac
                if not data:
                    continue
                if fused in decode_tac_tree(data):
                    _FUSE_GUIDS.append(str(card.guid).lower())
    owner = int(context.bstate.get("resolving_owner_id", 0) or 0)
    source_uid = context.bstate.get("resolving_source_uid")
    from pvp_db import db_card_zone_details
    source = None
    if source_uid is not None:
        row = db_card_zone_details(
            context.session.session_id, int(source_uid), conn=context.db)
        if row:
            source = {"user_id": int(row[2]), "owner_id": int(row[2]),
                      "controller_id": int(row[2])}
    out = []
    for guid in _FUSE_GUIDS:
        card = DEFAULT_RECORD_STORE.get("CardTemplate", guid)
        if card is None:
            continue
        cost = int(card.field("m_ResourceCost", 0) or 0)
        candidate = {
            "card_uid": 0, "template_guid": guid,
            "name": card.field("m_Name", ""),
            "card_type": card.field("m_CardType", ""),
            "cost": cost,
            "rarity": card.field("m_CardRarity", ""),
            "subtype": card.field("m_CardSubtype", ""),
            "attributes": 0, "user_id": owner, "shards": [],
        }
        if records_filter_matches(candidate, card_filter, source=source,
                                  context=context,
                                  player={"resource_thresholds": {}}):
            out.append(guid)
    return out


def _candidate_cost_map(context):
    """Return {template_guid: cost} for the generated-card pool."""
    from pvp_db import db_transform_candidate_templates
    return {str(row[0]).lower(): int(row[3] or 0)
            for row in db_transform_candidate_templates(conn=context.db)}


def _combined_cost_choices(context, candidates, count, total):
    """Port of the CombinedCost selection loop in SummonTokenTroop.

    C# draws candidates until the running total can be met exactly by the
    final card, decrementing the requested total after 1000 failed draws so a
    pool without an exact combination still yields a summon.
    """
    cost_by_guid = _candidate_cost_map(context)
    pool = [guid for guid in candidates
            if int(cost_by_guid.get(guid, 0) or 0) > 0]
    if not pool:
        return []
    rng = context.bstate.get("_rules_rng")

    def pick():
        if rng is not None and hasattr(rng, "next"):
            return pool[int(rng.next(len(pool))) % len(pool)]
        return random.choice(pool)

    remaining = int(total)
    chosen = []
    for index in range(max(0, int(count))):
        if remaining <= 0:
            break
        candidate = pick()
        tries = 0
        while (int(cost_by_guid.get(candidate, 0) or 0) <= 0 or
               (index != count - 1 and
                int(cost_by_guid[candidate]) > remaining) or
               (index == count - 1 and
                int(cost_by_guid[candidate]) != remaining)):
            candidate = pick()
            tries += 1
            if tries > 1000:
                tries = 0
                remaining -= 1
                if remaining <= 0:
                    return chosen
        remaining -= int(cost_by_guid[candidate])
        chosen.append(candidate)
    return chosen


def _opposing_owners(context):
    """Return the controller ids on the other side of the resolving player."""
    bstate = context.bstate or {}
    owner = int(bstate.get("resolving_owner_id", 0) or 0)
    if bstate.get("pvp"):
        out = [int(pid) for pid in (bstate.get("pids") or ())
               if int(pid) != owner]
        if not out:
            out = [int(pid) for pid in (bstate.get("champ_map") or {})
                   if int(pid) != owner]
        return out
    profile = getattr(context.handler, "user_profile", None) or {}
    player = int(profile.get("id", 0) or 0)
    return [0 if owner else player]


def _creation_markers(context, owners):
    """Return authored creation markers on the given owners' cards.

    Each entry is ``(attribute, value, card_uid, linked_templates)``: the
    typed permanent-data marker plus the templates the ability's card links
    name.  ``activate_creation_replacements`` writes these on entry, so this
    is the engine's equivalent of the client reading the champion/source card
    context.
    """
    from pvp_db import (db_card_mutation_field,
                        db_cards_in_zones_with_abilities)
    from .creation_effects import replacement_abilities
    found = []
    for side in owners:
        for card_uid, abilities_json in db_cards_in_zones_with_abilities(
                context.session.session_id, int(side),
                ("warzone", "underground"), conn=context.db):
            try:
                abilities = json.loads(abilities_json or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                abilities = []
            try:
                buffs = json.loads(db_card_mutation_field(
                    context.session.session_id, int(card_uid),
                    "permanent_buffs", conn=context.db) or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                buffs = {}
            markers = buffs.get("int_attrs") if isinstance(buffs, dict) else {}
            if not isinstance(markers, dict) or not markers:
                continue
            for attribute, linked in replacement_abilities(
                    context.db, abilities):
                try:
                    value = int(markers.get(attribute, 0) or 0)
                except (TypeError, ValueError):
                    value = 0
                if value:
                    found.append((str(attribute), value, int(card_uid),
                                  linked))
    return found


def _creation_count_bonus(context, template_guid):
    """Port of ``AuthoritativeSessionBase.GetCreationBonuses``.

    The bonus applies once per creation call and depends on the created
    template's cost/subtype: cost-1 cards use ``Cost1CreationBonus``,
    Shin'hare use ``ShinhareCreationBonus``, Settlers use the source card's
    ``SettlersCreationBonus``, and Eggs use every opponent's
    ``EggCreationBonus``.
    """
    from pvp_db import db_card_template_creation_profile
    profile = db_card_template_creation_profile(
        template_guid, conn=context.db)
    if not profile:
        return 0
    cost, subtype, card_type = profile
    subtype_l = subtype.lower()
    card_type_l = card_type.lower()
    source_uid = context.bstate.get("resolving_source_uid")
    owner = int(context.bstate.get("resolving_owner_id", 0) or 0)
    bonus = 0
    for attribute, value, uid, _linked in _creation_markers(context, (owner,)):
        key = attribute.lower()
        if key == "shinharecreationbonus" and "shin'hare" in subtype_l:
            bonus += value
        elif (key == "cost1creationbonus" and cost == 1 and
              "resource" not in card_type_l and "bane" not in card_type_l):
            bonus += value
        elif (key == "settlerscreationbonus" and "settlers" in subtype_l and
              source_uid is not None and uid == int(source_uid)):
            bonus += value
    for attribute, value, _uid, _linked in _creation_markers(
            context, _opposing_owners(context)):
        if attribute.lower() == "eggcreationbonus" and "egg" in subtype_l:
            bonus += value
    return bonus


def _creation_replacement_guid(context, token_guid):
    """Return the authored template that replaces a would-be token.

    The creating player's cards carry the typed IntAttr marker a replacement
    grant writes (Reese the Crustcrawler's Surface grant is the current one),
    and the replaced template is named in that card's Records ability graph.
    Both halves are authored data, so the substitute comes from the same typed
    random-creation pool every other native summon uses.
    """
    if not token_guid:
        return None
    from pvp_db import db_cards_in_zones_with_abilities, db_card_mutation_field
    from .creation_effects import (replacement_abilities, replacement_filter,
                                   replacement_substitute)
    owner = int(context.bstate.get("resolving_owner_id", 0) or 0)
    for card_uid, abilities_json in db_cards_in_zones_with_abilities(
            context.session.session_id, owner,
            ("warzone", "underground"), conn=context.db):
        try:
            abilities = json.loads(abilities_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            abilities = []
        try:
            buffs = json.loads(db_card_mutation_field(
                context.session.session_id, int(card_uid),
                "permanent_buffs", conn=context.db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        markers = buffs.get("int_attrs", {}) if isinstance(buffs, dict) else {}
        if not markers:
            continue
        for attribute, linked_templates in replacement_abilities(
                context.db, abilities):
            if not int(markers.get(attribute, 0) or 0):
                continue
            if str(token_guid).lower() not in linked_templates:
                continue
            substitute_filter = replacement_filter(attribute)
            if substitute_filter is not None:
                candidates = _matching_template_candidates(
                    context, substitute_filter,
                    _authored_banned_guids(context))
                if candidates:
                    return random.choice(candidates)
                continue
            substitute = replacement_substitute(
                attribute, linked_templates, token_guid)
            if substitute:
                return substitute
    return None


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
        # ``m_Amount`` is a typed EffectField (EffectInputVariable/Constant);
        # resolve it rather than reading the raw template value, which is a
        # dict for Conscript and made the count collapse to 0.
        try:
            count = context.value("m_Amount", 1)
        except (TypeError, ValueError):
            count = 1
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
    deck_location = str(context.template_value(
        "m_CardLocation", "") or "").rsplit(".", 1)[-1].lower()
    card_filter = payload.get("card_filter")
    terminus = bool(context.template_value("m_Terminus", False))
    combined_cost = 0
    if context.template_value("m_CombinedCost", None) is not None:
        try:
            combined_cost = int(context.value("m_CombinedCost", 0) or 0)
        except (TypeError, ValueError):
            combined_cost = 0
    selected_guids = None
    candidate_count = None
    banned_guids = (_authored_banned_guids(context)
                    if not guid and card_filter is not None else set())
    if card_filter is None:
        card_filter = context.template_value("m_CardFilter")
    if not guid and (card_filter is not None or terminus):
        if terminus:
            candidates = _fuse_template_candidates(context, card_filter)
        else:
            candidates = _matching_template_candidates(
                context, card_filter, banned_guids)
        if candidates:
            if combined_cost > 0:
                selected_guids = _combined_cost_choices(
                    context, candidates, count, combined_cost)
            elif terminus:
                # C# draws one random fused card per amount, with replacement.
                selected_guids = [random.choice(candidates)
                                  for _ in range(max(0, count))]
            elif payload.get("random_with_replacement"):
                # Conscript calls CreateNTokensFromResource(1, randomCard)
                # once per requested card. The same authored candidate may
                # therefore be selected more than once.
                selected_guids = [random.choice(candidates)
                                  for _ in range(max(0, count))]
            else:
                # Choice-zone effects such as the champion charge power present
                # separate options.  Select without replacement so one
                # activation cannot show the same card three times.
                selected_guids = random.sample(
                    candidates, min(count, len(candidates)))
            guid = selected_guids[0] if selected_guids else ""
        candidate_count = len(candidates)
    if not guid or not db_template_exists(guid, conn=context.db):
        return ("summon token: no typed card template found "
                f"(guid={guid!r}, candidates={candidate_count}, "
                f"count={count}, filter={card_filter is not None})")
    if selected_guids is None:
        replacement = _creation_replacement_guid(context, guid)
        if replacement:
            # An authored replacement substitutes the created template, so it
            # must happen before the talent-modified lookup and the payload
            # copy: the substitute's own type/abilities/stats are created.
            guid = str(replacement).lower()
    owner = int(context.bstate.get("resolving_owner_id", 0) or 0)
    target = context.resolved_target()
    target_owner = context.target_owner(target, default=owner)
    # Some token effects target a champion other than the ability's
    # controller ("a random opposing champion summons ..."), so the resolved
    # champion target's controller is authoritative for any destination, not
    # just deck/hand.  resolving_owner_id is only the caster/trigger source.
    target_is_champion = False
    if target is not None:
        champion_targets = getattr(context.handler, "_champion_targets", None)
        if callable(champion_targets):
            try:
                target_rows = champion_targets()
                if isinstance(target_rows, Iterable):
                    target_is_champion = any(
                        int(row[0]) == int(target) for row in target_rows)
            except (TypeError, ValueError):
                target_is_champion = False
        if not target_is_champion:
            champ_map = context.bstate.get("champ_map")
            champion_uids = (champ_map.values()
                             if isinstance(champ_map, dict) else ())
            target_is_champion = any(
                int(uid) == int(target)
                for uid in champion_uids)
    if target_owner is not None and (target_is_champion or
                                     location in {"deck", "hand"}):
        owner = int(target_owner)
    from pvp_db import db_resolve_talent_modified_template
    guid = db_resolve_talent_modified_template(
        guid, context.active_talent_guids(), conn=context.db)
    template = db_copy_template_payload(guid, conn=context.db)
    if not template:
        return "summon token: template payload missing"
    from .runtime_helpers import card_collection_for_location, next_game_card_uid, owner_uid
    from .triggers import dispatch_trigger
    recipient = owner_uid(owner, context.player_uid, context.ai_uid,
                          context.bstate)
    conscript_thresholds = None
    conscript_gained_attributes = {}
    if payload.get("conscript_event"):
        source_uid = context.bstate.get("resolving_source_uid")
        if source_uid is not None:
            conscript_thresholds = context._card_thresholds(int(source_uid))
    made = []
    if selected_guids is None:
        # An explicit template amount gets its creation bonus once, exactly
        # like the single CreateNTokensFromResource call the client makes.
        guids_to_create = [guid] * (count + _creation_count_bonus(
            context, guid))
    else:
        # A random pool calls CreateNTokensFromResource once per card, so
        # each created card carries its own replacement and bonus.
        guids_to_create = []
        for create_guid in selected_guids:
            replacement = _creation_replacement_guid(context, create_guid)
            if replacement:
                create_guid = str(replacement).lower()
            guids_to_create.append(create_guid)
            guids_to_create.extend(
                [create_guid] * _creation_count_bonus(context, create_guid))
    for create_guid in guids_to_create:
        create_template = (template if create_guid == guid else
                           db_copy_template_payload(create_guid, conn=context.db))
        if not create_template:
            continue
        uid = next_game_card_uid(context.db, context.session.session_id)
        gem_type = _random_socket_gems(create_guid, context.db)
        if location == "deck":
            if deck_location in ("bottom",):
                position = __import__("pvp_db").db_deck_next_position(
                    context.session.session_id, owner, conn=context.db)
            elif deck_location in ("top",):
                position = 0
            else:
                # Unknown/omitted is a random deck insertion.  A temporary
                # position keeps the card in the deck until the shared
                # insertion helper assigns a uniformly random slot.
                position = 9999
        else:
            position = 0
        db_insert_generated_card(
            context.session.session_id, owner, uid, create_guid, location,
            create_template[0], create_template[1], create_template[2],
            db_next_game_card_row_id(context.session.session_id, conn=context.db),
            conn=context.db, position=position, owner_user_id=owner,
            original_template_guid=create_guid, gems=gem_type)
        if conscript_thresholds is not None:
            # ConscriptModifierAbility is a client built-in CardThreshold
            # effect with CopySourceCard=true. Persist its eventual result in
            # the existing card mutation payload before the first projection.
            from pvp_db import (db_card_mutation_field,
                               db_set_card_mutation_field)
            try:
                buffs = json.loads(db_card_mutation_field(
                    context.session.session_id, uid, "permanent_buffs",
                    conn=context.db) or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                buffs = {}
            if not isinstance(buffs, dict):
                buffs = {}
            previous_thresholds = context._card_thresholds(int(uid))
            buffs["thresholds"] = [int(value)
                                   for value in conscript_thresholds]
            db_set_card_mutation_field(
                context.session.session_id, uid, "permanent_buffs",
                json.dumps(buffs), conn=context.db)
            shard_names = {4: "Blood", 8: "Ruby", 16: "Sapphire",
                           32: "Wild", 64: "Diamond"}
            previous_counts = {}
            current_counts = {}
            for value in previous_thresholds or ():
                previous_counts[int(value)] = previous_counts.get(
                    int(value), 0) + 1
            for value in conscript_thresholds:
                current_counts[int(value)] = current_counts.get(
                    int(value), 0) + 1
            conscript_gained_attributes[int(uid)] = [
                (name, previous_counts.get(flag, 0),
                 current_counts.get(flag, 0))
                for flag, name in shard_names.items()
                if previous_counts.get(flag, 0) == 0 and
                current_counts.get(flag, 0) > 0]
        made.append((int(uid), create_guid, create_template[0]))
    if made:
        # ``AbilityCreatedTargetTemplate`` and "the created card" stat/void
        # effects read the batch this activation materialized.
        context.bstate["created_token_uids"] = [
            int(uid) for uid, _create_guid, _card_type in made]
    context.db.commit()
    if location == "deck" and deck_location in ("", "unknown", "random"):
        from pvp_db import db_randomly_insert_deck_cards
        db_randomly_insert_deck_cards(
            context.session.session_id, owner,
            [uid for uid, _create_guid, _card_type in made], connection=context.db)
    for uid, created_guid, created_card_type in made:
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
        if conscript_thresholds is not None:
            context._push_modifier_card(
                int(uid), thresholds=[int(value)
                                     for value in conscript_thresholds])
        _publish_created_card(context, uid, owner, created_card_type)
        dispatch_trigger(context, "CardEnteredZoneEvent", uid, owner,
                         data={"event_destination_collection": location})
        for attribute, previous, current in conscript_gained_attributes.get(
                int(uid), ()):
            context.emit_int_attribute_gained(
                int(uid), attribute, previous, current)
    if payload.get("conscript_event"):
        faction = str(payload.get("conscript_faction") or "None")
        for uid, _created_guid, _card_type in made:
            # The C# ConscriptEvent has the conscripted card as its source and
            # carries the authored faction as event data. It has no target id.
            dispatch_trigger(
                context, "ConscriptEvent", uid, owner,
                data={"event_faction": faction})
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
        # Records stores the typed count in m_InputValue (EffectConstant /
        # EffectInputVariable, e.g. "Two"/"Four").  It was never read, so every
        # "create two/four copies" created one.
        try:
            count = context.value("m_InputValue", None)
        except (TypeError, ValueError):
            count = None
    if count is None:
        # The authored single-copy form omits a count field; the typed
        # CreateTokenCopy contract defaults it to one.
        count = 1
    try:
        count = max(1, int(count))
    except (TypeError, ValueError):
        raise RuntimeError(
            "RulesPort CreateTokenCopy effect has invalid typed count")
    # C# CreateTokenCopies adds GetCreationBonuses once for the copied
    # template before creating the batch.
    count += _creation_count_bonus(context, row[0])
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
    # C# places the copies under the responsible player unless the authored
    # m_SameOwner asks for the target card's controller.
    if context.template_value("m_SameOwner", False):
        owner_id = int(context.target_owner(
            target, default=owner_id) or owner_id)
    recipient = owner_uid(owner_id, context.player_uid, context.ai_uid,
                          context.bstate)
    import game_engine
    created = []
    is_replica = bool(context.template_value("m_IsReplica", False))
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
        if is_replica:
            from .replica import apply_replica_mods
            apply_replica_mods(context, uid, row[0])
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
        _publish_created_card(context, uid, owner_id, payload[0])
        from .triggers import dispatch_trigger
        dispatch_trigger(context, "CardEnteredZoneEvent", uid, owner_id,
                         data={"event_destination_collection": location})
    return f"copied {len(created)}x {row[0][:8]} {'to hand' if location == 'hand' else 'to warzone'}"

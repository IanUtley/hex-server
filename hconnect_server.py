"""
HConnect protocol server for Hex TCG.
Wire format: [~HCP~][payload_len:u32be][header_len:u32be][header_json:utf8][body_len:u32be][body]
Default port: 9933
"""

import socket
import struct
import json
import time
import threading
import sys
import gzip
import io
import random
import re
import sqlite3
import os
from collections import namedtuple
from collections.abc import Mapping
from functools import lru_cache
import uuid
import hashlib
import signal
from typing import TYPE_CHECKING, Any, cast
from dataclasses import replace
from binascii import hexlify, unhexlify
from datetime import datetime, timezone
from application.protocol_wire import (
    IDENT, make_packet, nested_field, numeric_target_map, parse_packet,
    tournament_deck_card_ids,
)


_numeric_target_map = numeric_target_map
_nested_field = nested_field
_tournament_deck_card_ids = tournament_deck_card_ids

# The live server is launched as ``__main__``, while service modules import
# ``hconnect_server`` for shared handler state.  Alias the running module so
# those imports do not create a second module with a separate client registry.
if __name__ == "__main__":
    sys.modules.setdefault("hconnect_server", sys.modules[__name__])
import game_session
import game_engine
import encoder
import campaign
from domain.constants import (
    LEGACY_AI_PLAYER_UID_TYPE, PLAYER_TRANSACTION_DATA_TYPE, PLAYER_UID_TYPE,
    SERVICE_AI_UID_TYPE,
    SERVICE_GAME_SESSION_UID_TYPE, SERVICE_PLAYER_UID_TYPE,
    PROFILE_BANNED_CARD_LIST_EVENT, ICONOCLAST_SET_FORMAT,
    ICONOCLAST_PLAY_FORMAT,
)
from domain.enums import ETournamentFormats, EGemTypesNew
from application import ApplicationCommandDispatcher
from application.commands import (JoinSessionCommand, RemoveSessionCommand,
                                  ServiceRequestCommand,
                                  SetSessionStateCommand,
                                  StartEncounterCommand, StartSessionCommand)
from application.player_transactions import (classify_player_transaction,
                                              extract_ability_guid,
                                              typed_payload_from_decoded,
                                              extract_resource_guid)
import gamemodes.tournament_server as tournament_server
from application.service_dispatch import dispatch_service
from application.profile_stream import ProfileStreamMixin
from gamemodes.tournament_engine import (
    _encode_enter_tournament_error, _make_deck_data,
    _tournament_format_bitmask, _tournament_session_flags,
    _tournament_style_bitmask,
    build_tournament_desc_json, uid_instance,
    start_waiting_room_game,
    _is_corinth_room, _push_corinth_deck_construction,
    resume_corinth_deck_construction,
    try_start_corinth_match,
    CORINTH_SHARD_GUIDS,
    tournament_id_from_session_name,
    push_tournament_session_start,
    build_waiting_room_data, build_tournament_info_data,
    push_tournament_room_data, record_tournament_forfeit,
    db_tournament_discard_match,
    player_handlers, player_handler_lock, player_decks,
)

from application.encoding_helpers import encode_inventory_item, encode_card_instance, encode_deck_bits, encode_champion_bits_minimal
from application.practice_flow import _push_pve_mulligan_start, _push_pve_game_started, _same_uid, _uid_in, _is_practice_chain_follow_up, _target_count_from_text
from application.records_projection import _records_ability_graph, _records_card_projection, _records_target_spec
from application.profile_flags import _profile_feature_flags
from services.reconnect import _reconnect_declined_for_pid, _record_reconnect_decline
from gamemodes.fra_arena import _fra_arena_setup_values, _fra_arena_session_state_data

from application.champion_defaults import _talent_ability_guid, _RACE_DECK_MAP, _CHAMPION_CLASS_MAP, _CHAMPION_GENDER_MAP, _default_talents_for_champion
from services.chests import CLIENT_CHEST_UID_BASE, _CHEST_KEEP_COUNTS, ClientChest, _resolve_client_chest, _generate_chest_rewards, _grant_pack_inventory_rewards, _open_client_chests
from application.rules_port_timing import _rules_port_timing

from services.legacy_handlers import LEGACY_HANDLERS

DEFAULT_PORT = 9933
_reload_requested = False
DB_PATH = os.environ.get(
    "HEX_DB_PATH",
    os.path.join(os.path.dirname(__file__), "hconnect.db"),
)
_NO_PERSISTED_GEM = object()


PROFILE_FEATURE_FLAGS = _profile_feature_flags()

# How long the AI "thinks" (seconds) before acting on its turn. Each client
# connection runs in its own thread, so this only delays that player's battle
# events; incoming packets just queue in the socket buffer meanwhile.
AI_PHASE_DELAY = 1.0  # pause between AI phase pushes so the client renders them

# All talent data (names, abilities, descriptions) is in the talent_data DB table,
# populated from gamedata during build.


from db import _db, log, log_req, hexdump, set_log_context, card_label
from debug_runtime import enable_debugpy, trace_rules_port
from db import (player_id_from_name, player_id_from_steam, display_name_from_identity,
                STARDUST_TEMPLATES, CHEST_TEMPLATE)
from profile_db import (db_find_deck_owner, db_deck_champion_name,
                        db_deck_sleeve,
                        db_get_or_create_user, db_get_stardust, db_update_resources,
                        db_get_user, db_get_user_by_client_auth_id,
                        db_add_card, db_record_purchase, db_add_inventory,
                        db_get_inventory, db_get_store_item, db_send_email, db_save_deck,
                        db_update_deck, db_get_decks, db_redeem_code,
                        db_get_store_items,
                        db_get_unopened_chests, db_get_unread_mail_count,
                        db_get_user_champions, db_get_mail_list,
                        db_get_champion_deck_match, db_mark_all_mail_read,
                        db_delete_all_mail, db_primal_pack_for,
                        db_get_chest_by_id, db_next_card_instance_id,
                        db_create_card_instance, db_open_chest,
                        db_consume_inventory_row,
                        db_set_champion_last_deck,
                        db_champion_last_deck, db_card_instance_template,
                        db_inventory_item, db_consume_inventory,
                        db_next_inventory_client_uid, db_upsert_inventory_item,
                        db_next_card_instance_for_user, db_insert_card_instance,
                        db_set_inventory_client_uid, db_add_collection,
                        db_record_purchase, db_add_stardust,
                        db_claim_mail_for_user,
                        db_pack_set_info, db_chest_probabilities,
                        db_create_treasure_chest, db_get_unopened_chests_full,
                        db_store_item_name_for_template,
                        db_campaign_deck_for_champion, db_insert_champion,
                        db_purchase_exists,
                             db_update_champion_talents, db_champion_profile,
                             db_delete_champion,
                        db_profile_card_instances, db_card_instance_art,
                        db_set_card_instance_extended_art,
                        db_collection_card_list, db_card_instance_display)
from tournament_db import (db_tournament_room_for_game,
                           db_tournament_by_id, db_tournament_signup_by_player,
                           db_tournament_signups_by_tournament,
                           db_tournament_match_session,
                           db_tournament_status, db_tournament_pool,
                           db_tournament_pool_replace,
                           db_seed_tournament_pool,
                           db_tournament_player_run_live,
                           db_tournament_signup_set_status,
                           db_tournament_signup_set_async_state,
                           db_tournament_clear_player_searching,
                           db_tournament_pool_delete,
                           db_tournament_retire_player_run)
from replay_db import (_record_session_events, db_session_state_snapshot,
                       db_rules_port_state_snapshot,
                       db_record_session_transaction,
                       db_complete_session_transaction,
                       db_mark_session_packet_sent,
                       session_transaction_trace,
                       session_transaction_trace_state,
                       set_session_transaction_trace_ids)
from pvp_db import (db_clear_session_cards, db_game_session_pids,
                    db_delete_game_session, db_get_charge_power,
                    db_apply_tournament_main_deck,
                    db_game_champion, db_game_cards_at_location,
                    db_warzone_troop_count,
                    db_game_draw_cards, db_game_get_hand,
                    db_deck_cards_json,
                    db_deck_active_gems, db_template_exists,
                    db_first_template_guid, db_gem_templates,
                    db_encounter_deck_cards, db_scene_mods,
                    db_template_ability_guids, db_ability_metadata,
                    db_ability_activation_metadata,
                    db_ability_effect_rows, db_ability_has_effect,
                    db_ability_target_template_ids, db_target_template_filter,
                    db_template_card_info, db_card_catalog, db_pvp_set_guids,
                    db_template_subtype, db_gem_ability_guids,
                    EMPTY_SOCKET_GEMS,
                    db_set_card_abilities, db_cleanup_game_sessions,
                    db_set_card_abilities_and_attributes,
                    db_ability_option_cards, db_card_uids_in_zone,
                    db_card_zone_details, db_card_play_info,
                    db_card_chain_info,
                    db_cards_by_template_owner,
                    db_card_zone_projection, db_card_owner_zone_state,
                    db_card_owner_id, db_card_sacrifice_info,
                    db_card_location, db_set_card_location,
                    db_ordered_zone_rows, db_card_combat_identity,
                    db_card_gem_type, db_template_name,
                    db_update_card_state, db_move_card_to_location,
                    db_set_card_state_exact,
                    db_card_mutation_snapshot,
                    db_ability_effect_rows,
                    db_card_positions, db_move_card_to_deck_top,
                    db_move_card_to_deck, db_latest_deck_id_for_user,
                    db_target_template_definition, db_zone_card_count,
                    db_warzone_defender_rows, db_hand_card_uids,
                    db_warzone_blocker_uids,
                    db_warzone_troops_with_state, db_ability_is_triggered,
                    db_card_owner_id, db_ability_raw_json, db_ability_game_text,
                    db_template_attributes, db_card_mutation_info,
                    db_card_state_value, db_warzone_display_rows,
                    db_card_set_attacking_state,
                    db_game_cards_at_location_scalar, db_hand_card_count,
                    db_warzone_troops_basic,
                    db_bulk_blocker_state, db_card_discard_spell,
                    db_card_set_sacrifice_state, db_card_set_warzone_arrival,
                    db_set_card_resolved_at,
                    db_hand_cards_raw, db_hand_cards_full,
                    db_card_template_attrs_joined,
                    db_get_card_abilities, db_card_template_thresholds,
                    db_is_champion_template,
                    db_card_uses, db_bump_card_use,
                    db_card_basic, db_card_with_template,
                    db_card_instance_full,
                    db_card_instance_dynamic_projection,
                    db_card_instance_dynamic_projections,
                    db_card_ability_list,
                    db_template_by_guid,
                    db_champion_template_health, db_set_card_state_or,
                    db_card_template_lethal,
                    db_card_sync_abilities,
                    db_warzone_card_state_attributes,
                    db_card_activation_info, db_deck_top_card_details,
                    db_move_card_row_to_hand, db_reset_warzone_troop,
                    db_session_turn_order, db_insert_game_cards,
                    db_update_game_card_abilities)
from pve_db import (db_campaign_user_id, db_campaign_identity_state,
                    db_get_arena_state, db_get_fra_challengers,
                    db_record_arena_fight, db_get_player_champion_guid,
                    db_campaign_owner_user_id, db_default_talents,
                    db_champion_template_data_exists,
                    db_extended_champion_health, db_champion_template_name,
                    db_champion_talents_for_deck,
                    db_talent_ability_rows,
                    db_encounter_deck_sleeve,
                    db_talent_data_ability_guid, db_campaign_runtime_row,
                    db_campaign_champion_race, db_campaign_state,
                    db_quest_start_rows, db_quest_node_rows,
                    db_campaign_summary_row)
from static import CRAYBURN_PACK_CARD_SEEDS
from az1_pack import (CAMPAIGN_PACK_CONFIGS, STARDUST_REPLACEMENT_RATE,
                      generate_campaign_pack)

if TYPE_CHECKING:
    from gamedata import AbilityGraph


import domain.constants as _dc
_dc.event_logger = _record_session_events
# Game imported ``event_logger`` by value, so updating domain.constants alone
# does not update the callback used by Game.make_network_packet().  Bind it
# explicitly to the actual module containing Game.
import domain.game as _domain_game
_domain_game.event_logger = _record_session_events
game_engine.event_logger = _record_session_events


# === DataWrapper ObjFmt parser (simplified, field-by-field extraction) ===

from application.objfmt_wire import parse_datawrapper
# === ObjFmt encoder ===

from application.response_encoding import (
    client_session_guid, compress_gzip, decompress_gzip,
    encode_challenger_list, encode_datawrapper,
    encode_get_challengers_response, encode_get_store_items_response,
    encode_get_unread_mail_count_response, encode_login_stream_done,
    encode_objfmt_response, encode_objfmt_string, encode_ping_mail_server_response,
    encode_profile_response, encode_session_state, encode_store_item_set1_booster,
    encode_store_response, encode_sync_event, make_uid,
)
from services.card_pools import (
    load_card_templates as _load_card_templates,
    get_pvp_sets as _get_pvp_sets,
    generate_booster as _generate_booster,
    generate_starter_deck as _generate_starter_deck,
    generate_crayburn_chest as _generate_crayburn_chest,
    full_set_pool as _full_set_pool,
    roll_primal_upgrade as _roll_primal_upgrade,
)
UID_TYPE = {
    "ServicePlayer": SERVICE_PLAYER_UID_TYPE,
    "ServiceAI": SERVICE_AI_UID_TYPE,
    "ServiceMail": 252,
    "ServiceProfile": 245,
    "ServiceGameSession": SERVICE_GAME_SESSION_UID_TYPE,
    "ServiceMatchmaking": 247,
    "ServiceEscrow": 249,
    "ServiceCampaign": 253,
}

SERVICE_MAIL_UID = make_uid(UID_TYPE["ServiceMail"], 0)
SERVICE_PROFILE_UID = make_uid(UID_TYPE["ServiceProfile"], 0)
SERVICE_GAME_SESSION_UID = make_uid(UID_TYPE["ServiceGameSession"], 0)
SERVICE_MATCHMAKING_UID = make_uid(UID_TYPE["ServiceMatchmaking"], 0)
SERVICE_PLAYER_UID = make_uid(UID_TYPE["ServicePlayer"], 67890)

# Connected client registry: {user_id: [(HCPHandler, last_active_time), ...]}
_active_clients = {}
_active_clients_lock = threading.Lock()


def get_active_clients_snapshot():
    """Return a thread-safe snapshot copy of active clients."""
    with _active_clients_lock:
        return {uid: list(entries) for uid, entries in _active_clients.items()}
_pvp_ready = {}  # session_id → ready_count for tournament PvP
_pvp_events_ready = {}  # session_id → ready_count for tournament PvP events (22029)
_pending_tournament_starts = set()
_pending_tournament_starts_lock = threading.Lock()
_waiting_rooms = {}
_waiting_room_lock = threading.Lock()
_waiting_room_next_id = 1


def _next_waiting_room_id():
    global _waiting_room_next_id
    rid = _waiting_room_next_id
    _waiting_room_next_id += 1
    return rid
SESSION_TIMEOUT = 900  # 15 minutes

def cleanup_stale_sessions():
    """Remove sessions inactive for more than SESSION_TIMEOUT seconds."""
    now = time.time()
    with _active_clients_lock:
        for uid in list(_active_clients.keys()):
            _active_clients[uid] = [(h, t) for h, t in _active_clients[uid] if now - t < SESSION_TIMEOUT]
            if not _active_clients[uid]:
                del _active_clients[uid]
    # Also clean up ended game sessions from DB
    try:
        db_cleanup_game_sessions(conn=_db)
        _db.commit()
    except Exception:
        pass

def touch_session(handler):
    """Update the last-active time for a handler."""
    with _active_clients_lock:
        for uid, entries in list(_active_clients.items()):
            for i, (h, t) in enumerate(entries):
                if h is handler:
                    entries[i] = (h, time.time())
                    return


# Starter deck cards loaded from gamedata
_STARTER_DECKS = {}
_starter_decks_path = os.path.join(
    os.path.dirname(__file__), "generated", "starter_decks.json")
if os.path.exists(_starter_decks_path):
    with open(_starter_decks_path, "r") as f:
        _STARTER_DECKS = json.load(f)
    log(f"Loaded {len(_STARTER_DECKS)} starter decks from {_starter_decks_path}")


_PVE_CHAMPION_GUIDS = {
    "Human":    "752ef2e4-8eb3-4e24-93f2-6bf707c0aaff",
    "Elf":      "15eb25d9-359e-4dc2-ba54-a76462303723",
    "Coyotle":  "3c1a8175-d137-40f4-86ee-3d39e0b1158d",
    "Orc":      "04a4dd5b-453f-468f-84ff-c9207c637525",
    "Dwarf":    "a0484173-ad18-42d2-a4b8-08cc21088c69",
    "ShinHare": "cdfaf80e-4564-4690-9bfe-9486e0a9dbc1",
    "Vennen":   "9672371b-00aa-49b0-b16a-aaa8aab45c73",
    "Necrotic": "1351fed5-a0d5-44be-892c-78f4b40f7eb1",
}


def _dispatch_service(handler, data_type, target, instance, reqid, comp,
                      session_id, conh, inner_obj, inner_bytes):
    return dispatch_service(
        handler, data_type, target, instance, reqid, comp, session_id, conh,
        inner_obj, inner_bytes,
        service_uids={"mail": SERVICE_MAIL_UID, "profile": SERVICE_PROFILE_UID},
        log_req=log_req)


class HCPHandler(ProfileStreamMixin):
    def __init__(self, conn, addr):
        self.conn = conn
        self.addr = addr
        self._send_lock = threading.Lock()
        self.buf = b""
        self.sid = None
        self.scnt = 0
        self.ccnt = 0
        self._svc_scnt = {}
        self._game_scnt = 0
        self._event_q = []  # (scnt, issuer, target, instance, headers, body) tuples
        self._svc_scnt = {}  # per-service scnt tracking
        self.authenticated = False
        self.client_uid = None
        self.client_auth_id = "12345"
        self.client_reck_id = "67890"
        # An empty mapping represents an unauthenticated connection. Once
        # login succeeds the profile is always a populated user row, so game
        # handlers never need to index through a nullable profile reference.
        self.user_profile: dict[str, Any] = {}
        self._inventory_pending = False
        self._tutorial_game = None
        self._log_session_id = None
        self._log_player_id = None
        self._log_session_players = ()
        self._active_game_session = None
        self._application = ApplicationCommandDispatcher(
            event_publisher=self._publish_application_events)

    def _log_participant_tokens(self, session):
        """Return filename-safe game player IDs for a session log.

        Logging must never break gameplay: a partially constructed handler or
        a session double without a ``players`` iterable yields no tokens.
        """
        tokens = []
        try:
            participants = list(getattr(session, "players", ()) or ())
        except TypeError:
            participants = []
        for entry in participants:
            try:
                participant, _position = entry
            except (TypeError, ValueError):
                continue
            try:
                value = int(participant)
            except (TypeError, ValueError):
                continue
            # GameSession stores typed ServicePlayer UIDs.  The lower 8 bits
            # are the type; filenames use the client-visible player ID so a
            # later !issue can find the file from its handler identity.
            if (value & 0xff) == UID_TYPE["ServicePlayer"]:
                value >>= 8
            tokens.append(value)
        reck_id = getattr(self, "client_reck_id", None)
        if not tokens and reck_id:
            try:
                tokens.append(int(reck_id))
            except (TypeError, ValueError):
                pass
        return tuple(tokens)

    def _bind_log_session(self, session):
        """Route output to this player and, when present, their game."""
        session_id = getattr(session, "session_id", None) if session else None
        profile = getattr(self, "user_profile", None)
        player_id = (profile or {}).get("id") if profile else None
        participant_ids = self._log_participant_tokens(session) if session else ()
        self._log_session_id = session_id
        self._log_player_id = player_id
        self._log_session_players = participant_ids
        self._active_game_session = session
        set_log_context(player_id=player_id, session_id=session_id,
                        participant_ids=participant_ids)
        return session_id

    def _refresh_log_session_context(self):
        """Bind the caller's current persisted game session before dispatch."""
        if not self.user_profile or not self.client_reck_id:
            self._bind_log_session(None)
            return None
        try:
            player_uid = make_uid(
                UID_TYPE["ServicePlayer"], int(self.client_reck_id))
            session = game_session.find_session_by_player(player_uid)
        except Exception:
            session = None
        self._bind_log_session(session)
        return session

    def _checkpoint_engine(self, session):
        """Select the checkpoint adapter for this session.

        Native RulesPort sessions and compatibility sessions share the same
        projection handlers, so those handlers must not unconditionally import
        the legacy battle engine.  Keeping the selection here makes the
        boundary explicit while preserving the rollback path for old sessions.
        """
        self._bind_log_session(session)
        if getattr(session, "_rules_port_session", None) is not None:
            from rules_port import lifecycle
            return lifecycle
        import battle_engine
        return battle_engine

    def _maybe_attach_rules_port(self, session, game, battle_state):
        """Attach the authoritative Python rules host for a live session."""
        if session is None:
            return None
        self._bind_log_session(session)
        # The attached port callbacks close over this value.  Preserve the
        # validated session type across those nested callbacks as well as in
        # the enclosing method.
        session = cast(game_session.GameSession, session)
        # Tournament PvP is currently hosted by services.tournament_game,
        # which owns a two-human state schema and packet projection.  A
        # generic AuthoritativeSession cannot be attached alongside it: its
        # namespaced checkpoint would replace the tournament turn_order before
        # the tournament transaction handler runs.  Keep this explicit until
        # tournament state is migrated into the same RulesPort session model.
        if session is not None and str(
                getattr(session, "session_name", "") or "").startswith(
                    "tourney-"):
            from services.tournament_game import (
                attach_pvp_rules_port, pvp_session_lock)
            # Serialize native port creation/sync with the mulligan
            # completion's first-turn drive (which holds this same lock).
            # Otherwise the other client can observe ``_rules_port_session``
            # as None mid-drive and materialize a second port from a stale
            # checkpoint, rolling the phase back to Ready/Prep.
            with pvp_session_lock(session):
                return attach_pvp_rules_port(self, session, game, battle_state)
        # Practice/PvE checkpoints always carry both champion health values.
        # A few re-entrant projections used to save a partial compatibility
        # dictionary (the observed row had ``player_health=0`` and no
        # ``ai_health``).  The next fresh Game then serialized that malformed
        # value in PlayerUpdated, making an untouched hero appear dead.  Treat
        # a one-sided health checkpoint as corrupt and restore the pair from
        # the champion values selected for this battle.  A legitimate game
        # over has both keys, including a zero, so it is left intact.
        def repair_health_pair(state):
            if not isinstance(state, dict):
                return False
            if "player_health" in state and "ai_health" in state:
                return False
            player_default = getattr(self, "_player_starting_health", None)
            ai_default = getattr(self, "_ai_starting_health", None)
            if player_default is None:
                player_default = getattr(game, "player_health", 20)
            if ai_default is None:
                ai_default = getattr(game, "ai_health", 20)
            state["player_health"] = int(player_default or 0)
            state["ai_health"] = int(ai_default or 0)
            log_req(
                "    Repaired partial RulesPort health checkpoint: "
                f"player={state['player_health']} ai={state['ai_health']}")
            return True

        repaired_battle_state = repair_health_pair(battle_state)
        shared_state = getattr(session, "_rules_port_battle_state", None)
        repaired_shared_state = repair_health_pair(shared_state)
        if repaired_battle_state or repaired_shared_state:
            from rules_port.persistence import save_state
            save_state(session, shared_state if isinstance(
                shared_state, dict) and repaired_shared_state else battle_state)
        try:
            from rules_port import enable_rules_port

            def native_turn_start(_session=session, _game=game,
                                  _state=battle_state):
                """Run StartTurn against the checkpoint the port currently owns.

                A reconnect can rehydrate ``session._rules_port_battle_state``
                after this handler has been built.  The old callback closed
                over the attach-time dictionary, so a TurnStarted resource
                grant could land in a stale object and disappear before Prep
                refilled the active player's pool.  Keep the port's original
                dictionary identity while refreshing its contents from the
                authoritative session checkpoint.
                """
                from rules_port.persistence import load_state
                current = load_state(
                    _session, default=lambda: dict(_state))
                if isinstance(current, dict) and current is not _state:
                    _state.clear()
                    _state.update(current)
                current = _state if isinstance(_state, dict) else current
                if isinstance(current, dict):
                    setattr(_session, "_rules_port_battle_state", current)
                return self._apply_rules_port_start_turn(
                    _session, _game, current)

            port = enable_rules_port(
                session, game, battle_state,
                turn_start_resolver=native_turn_start)
            trace_rules_port(log_req, "attached-before-sync", port, battle_state)
            # A double-clicked ability can persist a ResolveTopOfChainAction
            # after the legacy stack has already emptied.  That orphaned port
            # action makes every subsequent card appear unplayable.  The
            # shared battle checkpoint is authoritative. Discard only a stale
            # RulesPort action when it has no matching chain item or pending
            # input; a live manual/triggered ability response window must be
            # preserved (otherwise the AI's pass is rejected and the chain is
            # stranded).
            if port.prune_orphan_actions(battle_state.get("stack")):
                try:
                    port.persist()
                except Exception:
                    pass
            # Fresh Game objects are event projections; keep the cached port
            # scheduler pointed at the current projection for this checkpoint.
            event_sink = getattr(port, "event_sink", None)
            if event_sink is not None:
                event_sink.game = game
            def native_state_based(_port=port, _game=game,
                                   _session=session, _state=battle_state):
                """Run lethal state checks before native priority is shown."""
                from rules_port.context import EffectContext
                from rules_port.death_effects import state_based_deaths
                from rules_port.persistence import load_state, save_state
                current_state = load_state(
                    _session, default=lambda: dict(_state))
                projection = (getattr(getattr(_port, "event_sink", None),
                                      "game", None) or _game)
                player_uid = getattr(projection, "player_uid", None)
                ai_uid = getattr(projection, "ai_uid", None)
                if player_uid is None or ai_uid is None:
                    return False
                current_state["_rules_port_attached"] = True
                previous = getattr(self, "_current_bstate", None)
                self._current_bstate = current_state
                try:
                    dead = state_based_deaths(EffectContext.from_rules_port(
                        projection, _session, _db, self, player_uid, ai_uid,
                        current_state, "state_based_death", ability=None))
                finally:
                    if previous is None:
                        try:
                            del self._current_bstate
                        except AttributeError:
                            pass
                    else:
                        self._current_bstate = previous
                # ``state_based_deaths`` consumes lethal marks even when no
                # troop dies; persist that transition at the same boundary.
                save_state(_session, current_state)
                if dead:
                    _session._rules_port_mutation_emitted = True
                return bool(dead)
            port.set_state_based_handler(native_state_based)
            def native_phase_priority(_port=port, _game=game,
                                      _session=session, _state=battle_state):
                """Translate Practice/PvE stop preferences into native queues."""
                from rules_port import lifecycle as _lifecycle
                from rules_port.persistence import load_state as _load_state
                state = _load_state(_session, default=lambda: dict(_state))
                phase = _port.current_turn_phase
                active = _port.active_player_id
                human = getattr(_game, "player_uid", None)
                if human is None:
                    # Some projection-only Game instances are constructed
                    # without participant fields. Practice still has one
                    # client participant; recover it from the native session
                    # rather than classifying every active phase as AI-owned.
                    ai = getattr(_game, "ai_uid", None)
                    human = next(
                        (participant for participant in _port.player_ids
                         if not _same_uid(participant, ai)),
                        None)
                human = _port.coerce_transaction_player_id(human)
                # RulesPort owns the stop matrix for both participants.  The
                # compatibility checkpoint only supplies the persisted stop
                # preferences; it must not collapse a self+opponent stop into
                # an ACTIVE-only queue (which strands the client after its
                # first pass).
                policy = _lifecycle.practice_phase_priority(
                    state, phase, active_player_id=active,
                    player_id=human)
                if os.environ.get("HEX_RULES_PORT_TRACE"):
                    log_req(
                        f"[rules-trace] phase-policy phase={phase!r} "
                        f"active={active!r} human={human!r} "
                        f"policy={policy!r}")
                return policy
            port.set_phase_priority_resolver(native_phase_priority)
            # Rehydrate the scheduler's live phase/priority from the existing
            # battle state.  The port snapshot may legitimately be empty on
            # first attach, but transaction validation must see the same
            # checkpoint the legacy engine just sent to the client.
            active = (game.player_uid if battle_state.get("turn_player") == "player"
                      else game.ai_uid)
            checkpoint_phases = list(battle_state.get("turn_phases") or ())
            native_phase = getattr(port, "current_turn_phase", None)
            native_active = getattr(port, "active_player_id", None)
            fresh_battle_checkpoint = (
                bool(checkpoint_phases) and
                int(battle_state.get("turn_number", 1) or 1) == 1 and
                int(battle_state.get("phase_idx", 0) or 0) == 0 and
                int(getattr(port, "total_turns_taken", 0) or 0) == 0)
            # Once RulesPort has a live phase/active-owner checkpoint, it is
            # authoritative. The compatibility cursor can lag while the AI
            # is being driven internally; re-syncing from it here would move
            # an already-started AI turn back to the human's previous
            # SecondMain phase (the exact source of the phase/owner mismatch).
            native_checkpoint_live = (
                native_phase is not None and
                native_phase != game_engine.ETurnPhases.NotPlaying and
                _uid_in(native_active, getattr(port, "player_ids", ())) and
                not fresh_battle_checkpoint)
            if native_checkpoint_live and native_phase is not None:
                active = native_active
                try:
                    checkpoint_phase_idx = checkpoint_phases.index(native_phase)
                except ValueError:
                    checkpoint_phase_idx = battle_state.get("phase_idx", 0)
            else:
                checkpoint_phase_idx = battle_state.get("phase_idx", 0)
            # A card played by the AI can resolve from the human response
            # window without re-entering the AI driver.  Refresh the native
            # combat branch fact from the authoritative warzone before the
            # next phase transition; otherwise FirstMain's stale
            # ``active_player_skips_attack`` skips DeclareAttack entirely.
            native_players = tuple(getattr(port, "player_ids", ()) or ())
            ai_participants = tuple(
                participant for participant in native_players
                if (int(cast(Any, getattr(participant, "uid64", participant)))
                    & 0xFF) in (LEGACY_AI_PLAYER_UID_TYPE,
                                SERVICE_AI_UID_TYPE))
            active_is_ai = (native_active is not None and
                            _uid_in(native_active, ai_participants))
            from rules_port.persistence import current_phase as _checkpoint_phase
            active_phase = (native_phase if native_checkpoint_live
                            else _checkpoint_phase(battle_state))
            if self._refresh_native_combat_branch(
                    session, battle_state, active_is_ai=active_is_ai,
                    active_phase=active_phase):
                # The scheduler's phase cursor maps through ``turn_phases``;
                # keep the reconciliation below aligned with the rebuilt plan.
                checkpoint_phases = list(battle_state["turn_phases"])
            from rules_port.lifecycle import should_draw_for_turn
            port.sync_checkpoint(
                phases=checkpoint_phases,
                phase_idx=checkpoint_phase_idx,
                active_player_id=active,
                client_player_id=game.player_uid,
                phase_facts={
                    # These are checkpoint facts, not a second phase graph.
                    # The native state objects consume them when selecting
                    # their permitted next state.
                    "skip_setup": bool(battle_state.get("skip_setup")),
                    "skip_mulligan": bool(battle_state.get("skip_mulligan")),
                    "all_players_ready_to_start": bool(
                        battle_state.get("all_players_ready_to_start")),
                    # ``player_draws_first_turn`` identifies which side gets
                    # the exceptional first-turn draw; it is not itself the
                    # native ``Prep -> Draw`` branch fact.  Feeding it
                    # directly here made every reattached turn skip Draw for
                    # the AI.  Derive the branch from the active participant
                    # and the current turn instead.
                    "active_player_skips_draw": not should_draw_for_turn(
                        battle_state,
                        "player" if _same_uid(active, game.player_uid)
                        else "ai"),
                    "active_player_skips_attack": not bool(
                        battle_state.get("player_has_ready_troop")),
                    "has_legal_attackers": bool(
                        battle_state.get("player_has_ready_troop")),
                    "has_forced_attackers": bool(
                        battle_state.get("forced_attackers")),
                    "has_extra_combats": bool(
                        (battle_state.get("extra_combats_this_turn") or {}).get(
                            "player", ())),
                },
                # Rehydrate every native phase window, not only First/Second
                # Main. Without this, a client pass in Ready/Prep/Draw or a
                # combat window arrived with no RulesPort action and had to
                # fall back to the legacy phase driver.
                ensure_current_priority=True)
            trace_rules_port(log_req, "attached-after-sync", port, battle_state)
            def native_phase_entry(phase, _session=session,
                                   _state=battle_state, _port=port):
                # StartTurn mutations already have a complete RulesPort
                # lifecycle adapter.  Invoke it from the native state entry,
                # not from the legacy phase cursor.  Other phase projections
                # remain queued for the next migration slice.
                # Reconnect/hot-reload can replace the mutable checkpoint
                # dictionary captured during the original attach. Always
                # rehydrate the current session state before deriving owner,
                # phase, resources, or trigger facts.
                from rules_port.persistence import load_state
                current_state = load_state(
                    _session, default=lambda: dict(_state))
                projection = getattr(
                    getattr(_port, "event_sink", None), "game", None) or game
                active_id = getattr(_port, "active_player_id", None)
                player_id = getattr(projection, "player_uid", None)
                current_state["turn_player"] = (
                    "player" if _same_uid(active_id, player_id) else "ai")
                try:
                    current_state["phase_idx"] = list(
                        current_state.get("turn_phases") or ()).index(phase)
                except ValueError:
                    pass
                if (phase == game_engine.ETurnPhases.DeclareDefense and
                        _same_uid(active_id, getattr(projection, "ai_uid", None)) and
                        current_state.get("ai_attackers")):
                    # Native phase states own the transition, but the host
                    # still owns the client PlayerOptionList projection.
                    # Without this bridge the defender receives the phase but
                    # no legal blocker targets (including ordinary Buffalo).
                    self._push_blocker_options(
                        _session, projection.player_uid, projection.ai_uid)
                if (phase == game_engine.ETurnPhases.DeclareAttack and
                        _same_uid(active_id, getattr(projection, "ai_uid", None)) and
                        not current_state.get("ai_attackers")):
                    # DeclareAttack is a no-input native phase.  If the AI
                    # waits for the ordinary driver loop, RulesPort advances
                    # immediately to AssignDamage before that loop can choose
                    # attackers.  Declare them at phase entry so the native
                    # combat manager is populated before the next-phase test.
                    log_req("    Native DeclareAttack entry: invoking AI attacker selection")
                    current_state = self._ai_declare_attackers(
                        projection, _session, projection.ai_uid,
                        projection.player_uid, current_state)
                    from rules_port.persistence import save_state
                    save_state(_session, current_state)
                    log_req(
                        "    Native DeclareAttack entry: AI selected "
                        f"{len(current_state.get('ai_attackers') or {})} attacker(s)")
                if phase == game_engine.ETurnPhases.StartTurn:
                    # The native EndTurn state rotates the active player
                    # before entering StartTurn. Keep the compatibility-shaped
                    # checkpoint aligned before the turn-start projection
                    # reads it; otherwise resources/tunneling/draws would be
                    # applied to the outgoing player even though RulesPort
                    # has already selected the incoming owner.
                    current_state["turn_player"] = (
                        "player" if _same_uid(active_id, player_id) else "ai")
                    current_state["phase_idx"] = 0
                    current_state["turn_number"] = int(
                        getattr(_port, "total_turns_taken", 0) or
                        current_state.get("turn_number", 1))
                    # StartTurnState invokes the dedicated turn-start
                    # resolver after this hook; returning here prevents the
                    # card-aging/Tunneling lifecycle from running twice.
                    return None
                owner_id = (self.user_profile.get("id", 0)
                            if current_state.get("turn_player") == "player"
                            and isinstance(self.user_profile, dict) else 0)
                from rules_port.triggers import dispatch_native_trigger
                dispatch_native_trigger(
                    db=_db, handler=self, game=projection,
                    session=_session, player_uid=projection.player_uid,
                    ai_uid=projection.ai_uid, battle_state=current_state,
                    event_type="TurnPhaseEvent", source_card_id=None,
                    source_player_id=owner_id,
                    data={"phase": int(phase)})
                if phase == game_engine.ETurnPhases.EndPhase:
                    # EndPhaseState.OnEntry in the client emits TurnEnded
                    # before the Discard phase. Keep this event on the native
                    # phase lifecycle so end-of-turn triggers do not depend
                    # on the compatibility phase cursor.
                    champion = (getattr(self, "_player_champ_scid", None)
                                if owner_id else
                                getattr(self, "_ai_champ_scid", None))
                    champion_uid = (None if champion is None else
                                    int(champion.uid.uid64))
                    dispatch_native_trigger(
                        db=_db, handler=self, game=projection,
                        session=_session, player_uid=projection.player_uid,
                        ai_uid=projection.ai_uid, battle_state=current_state,
                        event_type="TurnEndedEvent",
                        source_card_id=champion_uid,
                        source_player_id=owner_id)
                if phase == game_engine.ETurnPhases.Prep:
                    return self._apply_rules_port_prep(
                        _session, projection, current_state)
                if phase == game_engine.ETurnPhases.Draw:
                    return self._apply_rules_port_draw(
                        _session, projection, current_state)
                return None
            port.set_turn_phase_entry_resolver(native_phase_entry)
            def native_phase_exit(phase, _session=session, _state=battle_state,
                                  _port=port):
                """Publish C# combat completion at its native OnExit point."""
                if phase == game_engine.ETurnPhases.FirstStrikePriorityWindow:
                    if _port.combat_has_standard_damage:
                        return None
                elif phase != game_engine.ETurnPhases.AssignDamage:
                    return None
                from rules_port.persistence import load_state, save_state
                current_state = load_state(
                    _session, default=lambda: dict(_state))
                projection = getattr(
                    getattr(_port, "event_sink", None), "game", None) or game
                active_id = getattr(_port, "active_player_id", None)
                owner_id = (self.user_profile.get("id", 0)
                            if _same_uid(active_id, projection.player_uid)
                            and isinstance(self.user_profile, dict) else 0)
                from rules_port.triggers import dispatch_native_trigger
                dispatch_native_trigger(
                    db=_db, handler=self, game=projection,
                    session=_session, player_uid=projection.player_uid,
                    ai_uid=projection.ai_uid, battle_state=current_state,
                    event_type="CombatEndedEvent", source_card_id=None,
                    source_player_id=owner_id)
                save_state(_session, current_state)
                return None
            port.set_turn_phase_exit_resolver(native_phase_exit)
            def native_turn_boundary(_tentative_active,
                                     _session=session, _port=port):
                """Persist the Practice turn owner at the native boundary.

                RulesPort rotates its in-memory active participant before
                entering StartTurn.  Practice also has a compatibility-shaped
                checkpoint used when the next request reattaches the port; if
                that checkpoint still says ``player``, reattachment restores
                the outgoing owner and the human receives consecutive turns.
                Complete the shared boundary once here, including bonus-turn
                selection, and return its canonical typed participant to the
                scheduler.
                """
                from rules_port import lifecycle as _lifecycle
                from rules_port.persistence import (
                    load_state as _load_state,
                    save_state as _save_state,
                )
                current_state = _load_state(
                    _session, default=lambda: dict(battle_state))
                # Native RulesPort EndTurn transitions bypass the legacy
                # phase driver, which owns the equivalent end-of-turn
                # cleanup. Match the client EndTurnState.OnExit lifecycle
                # before rotating the checkpoint: marked damage clears first
                # so expiring temporary defense cannot kill a troop that
                # survived on that bonus.
                outgoing_owner = (
                    self.user_profile.get("id", 0)
                    if current_state.get("turn_player") == "player"
                    and isinstance(self.user_profile, dict) else 0)
                self._clear_combat_damage(_session, current_state)
                self._clear_expired_temporary_attributes(
                    _session, current_state, outgoing_owner, "end_turn",
                    clear_stat_buffs=True)
                next_side = _lifecycle.complete_turn(current_state)
                if next_side == _lifecycle.PLAYER:
                    selected = game.player_uid
                else:
                    selected = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
                selected = _port.coerce_transaction_player_id(selected)
                _save_state(_session, current_state)
                log_req(
                    "    Practice RulesPort turn boundary: next turn "
                    f"{next_side} ({selected!r})")
                return selected
            port.set_turn_boundary_resolver(native_turn_boundary)
            facts = getattr(port, "runtime_facts", None)
            if facts is not None:
                # The battle engine reloads a fresh mutable state dict for
                # each client checkpoint.  Cached RulesPort sessions must not
                # validate against the resource/threshold snapshot captured
                # when they were first attached.
                facts.battle_state = battle_state
                profile_id = (self.user_profile.get("id")
                              if isinstance(self.user_profile, dict) else None)
                facts.player_owner_id = profile_id
                facts.ai_owner_id = 0
                # Reuse the metadata-driven option legality calculation for
                # RulesPort activation validation. This keeps collection,
                # condition, target, exhaustion, cost, and usage gates in one
                # authority while RulesPort owns transaction ordering.
                def _port_ability_validator(card, player_id, ability_guid):
                    from rules_port.persistence import load_state
                    live_state = load_state(
                        session, default=lambda: dict(battle_state))
                    facts.battle_state = live_state
                    # Champion SessionCardIds are represented in the native
                    # runtime facts, but they are not ordinary game_cards
                    # ability-option rows.  Keep their authored catalog as
                    # the source of truth; cost, threshold, phase, and
                    # payment checks remain RulesPort-owned requirements.
                    if db_is_champion_template(
                            str(getattr(card, "template_guid", "")),
                            conn=_db):
                        from pvp_db import db_champion_ability_guids
                        from gamedata import DEFAULT_RECORD_STORE, ability_graph
                        guid = str(ability_guid).lower()
                        champion_guids = {
                            str(value).lower() for value in
                            db_champion_ability_guids(
                                str(card.template_guid), conn=_db)
                        }
                        graph = ability_graph(DEFAULT_RECORD_STORE, guid)
                        return (guid in champion_guids and graph is not None
                                and bool(getattr(graph, "manual", False)))
                    affordable = self._affordable_troop_abilities(
                        session, live_state)
                    key = (int(card.session_card_id), str(card.template_guid))
                    allowed = affordable.get(key, ())
                    if not allowed:
                        for (card_uid, template_guid), values in affordable.items():
                            if (int(card_uid) == key[0] and
                                    str(template_guid).lower() == key[1].lower()):
                                allowed = values
                                break
                    return str(ability_guid).lower() in {
                        str(value).lower() for value in allowed}
                facts.ability_validator = _port_ability_validator
                # Reconnect restores native combat identity only after the
                # runtime card facts are available for attacker/blocker
                # materialization.
                port.rehydrate_combats()
            # Wire the port's chain to the native RulesPort effect lifecycle.
            # There is no legacy effect backend in a live attached session.
            from rules_port.resolution import PortAbilityResolver
            from rules_port.session import ProjectedChainAbility
            from rules_port.actions import AbilityResolutionState
            from rules_port.card_transactions import MetadataCardTransactionExecutor
            port_ability_resolver = PortAbilityResolver(
                self, game, session, _db, game.player_uid, game.ai_uid,
                battle_state)
            attached_state = battle_state
            def pay_port_ability_cost(ability):
                from rules_port.persistence import load_state
                live_state = load_state(
                    session, default=lambda: dict(attached_state))
                # Payment is a state mutation; never debit an attach-time
                # snapshot after reconnect or hot reload.
                battle_state = live_state
                costs = getattr(getattr(ability, "metadata", None), "costs", None)
                if costs is None:
                    return True
                # AbilityCost.activation is the authored generic resource
                # payment.  The old legacy activation path did this before
                # resolving the BOM; RulesPort must do the same before its
                # chain action is allowed to run (Stargazer is cost 1).
                activation = int(getattr(costs, "activation", 0) or 0)
                variable = int(getattr(costs, "variable_activation", 0) or 0)
                ability_activation = getattr(ability, "activation", None)
                x_cost = int(getattr(ability_activation, "x_cost", 0) or 0)
                amount = activation + (x_cost if variable else 0)
                charge_amount = int(getattr(costs, "charge_points", 0) or 0)
                spell_amount = int(getattr(costs, "spell_points", 0) or 0)
                life_amount = int(getattr(costs, "life", 0) or 0)
                source_uid = getattr(ability, "source_uid", None)
                if getattr(costs, "exhausts_card_on_use", False) and source_uid is None:
                    log_req(f"    RulesPort cost rejected: ability={getattr(ability, 'ability_template_id', '')} has no exhaust source")
                    return False
                owner_id = int(getattr(getattr(ability, "metadata", None),
                                       "owner_id", 0) or 0)
                # Missing RulesPort ownership must not silently become the AI
                # owner. Resolve it from the source card, then from the
                # player submitting this client activation.
                if owner_id == 0 and source_uid is not None:
                    source_owner = db_card_owner_id(
                        session.session_id, int(source_uid), conn=_db)
                    if source_owner is not None:
                        owner_id = int(source_owner)
                if owner_id == 0 and isinstance(self.user_profile, dict):
                    owner_id = int(self.user_profile.get("id", 0) or 0)
                if owner_id == int(self.user_profile.get("id", 0)
                                   if isinstance(self.user_profile, dict) else 0):
                    current_key = "player_resources"
                    total_key = "player_total_resources"
                    event_owner = game.player_uid
                else:
                    current_key = "ai_resources"
                    total_key = "ai_total_resources"
                    event_owner = game.ai_uid
                prefix = "player" if current_key.startswith("player") else "ai"
                current = int(battle_state.get(current_key, 0) or 0)
                charges_key = f"{prefix}_charges"
                spell_key = f"{prefix}_spell_points"
                health_key = f"{prefix}_health"
                charges = int(battle_state.get(charges_key, 0) or 0)
                spell_points = int(battle_state.get(spell_key, 0) or 0)
                health = int(battle_state.get(health_key, 25) or 0)
                spell_uses = battle_state.get(f"{prefix}_sp_uses", {}) or {}
                from rules_port.costs import plan_ability_cost
                from rules_port.static_rules import \
                    charge_point_cost_modifier
                cost_plan = plan_ability_cost(
                    costs, getattr(ability, "activation", None),
                    current_resource=current, charges=charges,
                    spell_points=spell_points, health=health,
                    spell_uses=spell_uses,
                    ability_key=getattr(ability, "ability_template_id", ""),
                    charge_modifier=charge_point_cost_modifier(
                        _db, session.session_id, battle_state, owner_id,
                        source_uid))
                if cost_plan is None:
                    log_req(f"    RulesPort cost rejected: ability={getattr(ability, 'ability_template_id', '')} "
                            f"need={amount}r/{charge_amount}c/{spell_amount}sp/{life_amount}life "
                            f"have={current}r/{charges}c/{spell_points}sp/{health}life")
                    return False
                amount = cost_plan.resource
                charge_amount = cost_plan.charge_points
                effective_spell = cost_plan.spell_points
                # Additional card costs are part of the same client ability
                # activation (for example Hideous Conversion's authored
                # ``m_SacrificeTarget``).  The Mono payload commonly flattens
                # that selection into TargetMap; AbilityInstance.bind_activation
                # preserves it in cost_target_map, but numeric cost payment
                # alone would otherwise let the ability resolve without
                # sacrificing/exhausting the selected card.
                additional_selections = []
                graph = getattr(getattr(ability, "metadata", None), "graph", None)
                activation_data = getattr(ability, "activation", None)
                if graph is not None and activation_data is not None:
                    from rules_port.costs import (
                        ability_cost_targets,
                        validate_cost_target_selection,
                    )
                    cost_map = getattr(activation_data, "cost_target_map", {}) or {}
                    for cost in ability_cost_targets(
                            graph, _db, session.session_id, owner_id,
                            getattr(ability, "source_uid", None),
                            champions=self._champion_targets(),
                            battle_state=battle_state):
                        selected = cost_map.get(cost.index,
                                                cost_map.get(str(cost.index), ()))
                        if cost.is_source_auto_target and not selected:
                            source = getattr(ability, "source_uid", None)
                            selected = (() if source is None else (int(source),))
                        selected = validate_cost_target_selection(
                            _db, session.session_id, owner_id,
                            getattr(ability, "source_uid", None), cost,
                            selected, champions=self._champion_targets(),
                            battle_state=battle_state)
                        if selected is None:
                            log_req(
                                f"    RulesPort cost rejected: invalid {cost.kind} "
                                f"target(s)={selected} index={cost.index}")
                            return False
                        if selected:
                            additional_selections.append((
                                {"kind": cost.kind,
                                 "minimum": cost.minimum,
                                 "maximum": cost.maximum,
                                 "auto": cost.is_source_auto_target},
                                selected))
                if not self._card_costs_valid(
                        session, additional_selections, owner_id):
                    log_req(
                        f"    RulesPort cost rejected: invalid card cost owner={owner_id}")
                    return False
                from rules_port.costs import (
                    apply_ability_cost_plan,
                    plan_source_counter_costs,
                )
                counter_cost_plan = plan_source_counter_costs(
                    _db, session.session_id, source_uid, costs,
                    battle_state)
                if counter_cost_plan is None:
                    log_req(
                        f"    RulesPort cost rejected: unaffordable counter "
                        f"cost ability={getattr(ability, 'ability_template_id', '')}")
                    return False
                payment = apply_ability_cost_plan(
                    battle_state, prefix, cost_plan)
                paid_resource = payment["resource"]
                paid_charge = payment["charge_points"]
                paid_spell = payment["spell_points"]
                paid_life = payment["life"]
                if paid_charge["amount"]:
                    from rules_port.statistics import add_ability_stat
                    add_ability_stat(
                        battle_state, "ChargePointsSpent",
                        paid_charge["amount"],
                        instance_id=getattr(ability, "instance_id", None))
                if paid_resource["amount"]:
                    game.__setattr__(current_key, paid_resource["new"])
                    event = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
                    event.player_id = event_owner
                    event.operation = 2
                    event.delta = paid_resource["amount"]
                    event.new_value = paid_resource["new"]
                    game._push(event)
                    log_req(f"    RulesPort paid ability cost={paid_resource['amount']} owner={owner_id} resources={paid_resource['old']}->{paid_resource['new']}")
                if paid_charge["amount"]:
                    game.__setattr__(f"{prefix}_charges", paid_charge["new"])
                    event = game_engine.ChampionChargePointsChangedSessionEventArgs()
                    event.player_id = event_owner
                    event.operation = 2
                    event.delta = paid_charge["amount"]
                    event.new_value = paid_charge["new"]
                    game._push(event)
                if paid_spell["amount"]:
                    game.__setattr__(f"{prefix}_spell_points", paid_spell["new"])
                    event = game_engine.ChampionSpellPointsChangedSessionEventArgs()
                    event.player_id = event_owner
                    event.operation = 2
                    event.delta = paid_spell["amount"]
                    event.new_value = paid_spell["new"]
                    game._push(event)
                if paid_life["amount"]:
                    game.__setattr__(f"{prefix}_health", paid_life["new"])
                    event = game_engine.ChampionHealthChangedSessionEventArgs()
                    event.player_id = event_owner
                    event.operation = 2
                    event.delta = paid_life["amount"]
                    event.new_value = paid_life["new"]
                    game._push(event)
                if additional_selections:
                    payment_game = getattr(getattr(port, "event_sink", None),
                                           "game", None) or game
                    self._apply_card_play_costs(
                        payment_game, session, battle_state,
                        payment_game.player_uid, payment_game.ai_uid,
                        additional_selections,
                        source_uid=getattr(ability, "source_uid", None),
                        expected_owner_id=owner_id)
                counter_changes = ()
                if counter_cost_plan:
                    from rules_port.costs import \
                        apply_source_counter_cost_plan
                    counter_changes = apply_source_counter_cost_plan(
                        _db, session.session_id, int(source_uid),
                        counter_cost_plan, battle_state,
                        ability_instance_id=getattr(
                            ability, "instance_id", None))
                    from rules_port.context import EffectContext
                    from rules_port.counter_effects import \
                        project_counter_cost_change
                    counter_game = getattr(
                        getattr(port, "event_sink", None), "game", None) or game
                    counter_context = EffectContext.from_rules_port(
                        counter_game, session, _db, self,
                        counter_game.player_uid, counter_game.ai_uid,
                        battle_state,
                        str(getattr(ability, "ability_template_id", "")),
                        ability=ability)
                    for change in counter_changes:
                        project_counter_cost_change(
                            counter_context, int(source_uid),
                            change["counter_guid"], change["old"],
                            change["new"])
                if (counter_cost_plan or
                        any(item["amount"] for item in payment.values())):
                    from rules_port.persistence import save_state
                    save_state(session, battle_state)
                if getattr(costs, "exhausts_card_on_use", False):
                    if source_uid is None:
                        return False
                    source_uid = int(source_uid)
                    db_set_card_state_or(
                        session.session_id, source_uid,
                        int(game_engine.ECardStates.Tapped), conn=_db)
                    _db.commit()
                    row = db_card_zone_projection(
                        session.session_id, source_uid, conn=_db)
                    if row:
                        scid = game_engine.SessionCardId(game_engine.UID(source_uid))
                        # CardUpdated must carry the complete troop definition;
                        # a state-only event leaves Unity with an undrawn
                        # rectangle after the RulesPort activation.
                        self._card_full_data(game, scid, row[0])
                        cdef = game.card_defs.get(scid)
                        game.push_card_updated(
                            scid, event_owner, game_engine.ECardCollections.Warzone,
                            game_engine.card_type_from_db(row[1]),
                            template_id=row[0], state=int(row[2] or 0),
                            cost=cdef.cost if cdef else 0,
                            attack=cdef.attack if cdef else 0,
                            defense=cdef.defense if cdef else 0)
                return True
            port.set_ability_cost_payer(pay_port_ability_cost)
            def resolve_port_chain_item(ability):
                """Resolve a native card chain through the host projection.

                Card effects and zone events remain mode-specific projections,
                but the chain item is now owned by RulesPort.  The legacy
                ``state['stack']`` is used only as the durable descriptor and
                is never allowed to drive priority or resolution directly.

                The lifecycle itself lives in ``rules_port.chain_items`` so
                Practice/PvE and tournament PvP cannot drift apart (the PvP
                copy never produced the picker-continuation marker, which made
                a Deathcry deck search re-run once per remaining candidate).
                """
                if not isinstance(ability, ProjectedChainAbility):
                    return port_ability_resolver(ability)
                timing_started = time.perf_counter()
                from rules_port.chain_items import resolve_chain_item
                state = resolve_chain_item(
                    self, port, session, _db, ability,
                    game.player_uid, game.ai_uid)
                _rules_port_timing(
                    "chain-item", timing_started,
                    kind=ability.descriptor.get("kind"),
                    source_uid=ability.descriptor.get("source_uid"),
                    instance_id=ability.instance_id)
                if state is AbilityResolutionState.COMPLETED:
                    session._rules_port_mutation_emitted = True
                return state

            port.set_ability_resolver(resolve_port_chain_item)
            # The scheduler snapshot is restored before this resolver exists;
            # rebuild any active projected card chain only after both the
            # native resolver and the host projection are available.
            port.rehydrate_projected_chain()
            def card_projection(kind, transaction):
                """Dispatch only the explicit typed card-play mutations."""
                if kind in ("play_troop", "play_artifact", "play_spell",
                            "play_champion"):
                    handled = self._handle_rules_port_card_play(
                        session, transaction)
                    if handled:
                        session._rules_port_mutation_emitted = True
                    return handled
                return False
            def resource_mutation(transaction):
                """Apply every resource through the port-owned seam."""
                handled = self._handle_rules_port_resource(
                    session, transaction)
                if handled:
                    session._rules_port_mutation_emitted = True
                return handled
            def discard_projection(transaction):
                """Consume a validated DiscardTransaction through the port."""
                original = getattr(session, "_rules_port_dispatch_command", None)
                if original is None:
                    return False
                try:
                    card_uid = (getattr(transaction, "payload", {}) or {}).get(
                        "card_id")
                    handled = bool(self._handle_discard_transaction(
                        session, original, card_uid=card_uid))
                    if handled:
                        session._rules_port_mutation_emitted = True
                    return handled
                except Exception as exc:
                    log_req(f"    RulesPort discard mutation failed: {exc}")
                    return False
            def hand_projection(kind, transaction):
                """Consume opening-hand mutations through the port boundary."""
                original = getattr(session, "_rules_port_dispatch_command", None)
                if original is None:
                    return False
                try:
                    if kind == "accept_starting_hand":
                        handled = bool(self._handle_mulligan_keep_transaction(
                            session, original))
                    elif kind == "mulligan":
                        handled = bool(self._handle_mulligan_redraw_transaction(
                            session, original))
                    else:
                        handled = False
                    if handled:
                        session._rules_port_mutation_emitted = True
                    return handled
                except Exception as exc:
                    log_req(f"    RulesPort hand mutation failed: {exc}")
                    return False
            def attack_projection(transaction):
                original = getattr(session, "_rules_port_dispatch_command", None)
                if original is None:
                    return False
                try:
                    handled = bool(self._handle_commit_attack_transaction(
                        session, original,
                        declarations=(getattr(transaction, "payload", {}) or
                                      {}).get("declarations"),
                        authoritative_port=port))
                    if handled:
                        session._rules_port_mutation_emitted = True
                    return handled
                except Exception as exc:
                    log_req(f"    RulesPort attack mutation failed: {exc}")
                    return False
            def defense_projection(transaction):
                original = getattr(session, "_rules_port_dispatch_command", None)
                if original is None:
                    return False
                try:
                    handled = bool(self._handle_commit_defense_transaction(
                        session, original,
                        declarations=(getattr(transaction, "payload", {}) or
                                      {}).get("declarations"),
                        authoritative_port=port))
                    if handled:
                        session._rules_port_mutation_emitted = True
                    return handled
                except Exception as exc:
                    log_req(f"    RulesPort defense mutation failed: {exc}")
                    return False
            def damage_projection(transaction):
                original = getattr(session, "_rules_port_dispatch_command", None)
                if original is None:
                    return False
                try:
                    handled = bool(self._handle_assign_damage_transaction(
                        session, original,
                        assignments=(getattr(transaction, "payload", {}) or
                                     {}).get("assignments"),
                        native_combat=True))
                    if handled:
                        session._rules_port_mutation_emitted = True
                    return handled
                except Exception as exc:
                    log_req(f"    RulesPort damage mutation failed: {exc}")
                    return False
            def choice_projection(transaction):
                """Resume a metadata choice prompt through the port boundary."""
                original = getattr(session, "_rules_port_dispatch_command", None)
                if original is None:
                    return False
                try:
                    pl_t = game_engine.UID.make(244, int(self.client_reck_id))
                    ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
                    handled = bool(self._resolve_pending_choice(
                        session, pl_t, ai_t, original.inner_bytes,
                        typed_payload=getattr(original, "typed_payload", None)))
                    if handled:
                        session._rules_port_mutation_emitted = True
                    return handled
                except Exception as exc:
                    log_req(f"    RulesPort choice continuation failed: {exc}")
                    return False
            def refresh_player_options(transaction):
                """RulesPort request checkpoint; projection stays host-owned."""
                # Tournament PvP has raw participant IDs and a separate
                # persisted HUD state.  The ``game`` captured when the port
                # was attached may be a setup-time Game with the default
                # 20-health/zero-charge values.  Never use that object for a
                # post-transaction refresh: it emits a PlayerUpdated that
                # rolls the client's live charge/health back.
                if (session.session_name or "").startswith("tourney-"):
                    from services.tournament_game import (
                        pvp_load_state, pvp_push_main_phase_options)
                    pvp_state = pvp_load_state(session) or {}
                    pvp_push_main_phase_options(session, pvp_state)
                else:
                    self._push_main_phase_options(
                        session, game.player_uid, game.ai_uid)
                session._rules_port_mutation_emitted = True
                return True
            def ready_cards(transaction):
                """Apply the validated Ready-phase state transition."""
                from pvp_db import (db_rules_port_card_state_projection,
                                    db_update_card_state)
                ids = tuple(transaction.payload.get("card_ids", ()) or ())
                clear = (game_engine.ECardStates.Tapped |
                         game_engine.ECardStates.Attacking |
                         game_engine.ECardStates.Blocking)
                for card_id in ids:
                    raw_card_uid = getattr(card_id, "uid64", card_id)
                    if raw_card_uid is None:
                        return False
                    try:
                        card_uid = int(cast(Any, raw_card_uid))
                    except (TypeError, ValueError):
                        return False
                    row = db_rules_port_card_state_projection(
                        session.session_id, card_uid)
                    if not row or (isinstance(self.user_profile, dict) and
                                   int(row[2]) != int(self.user_profile.get("id", 0))):
                        return False
                    db_update_card_state(
                        session.session_id, card_uid, clear_bits=clear, conn=_db)
                    _db.commit()
                    scid = game_engine.SessionCardId(game_engine.UID(card_uid))
                    self._card_full_data(game, scid, row[0])
                    cdef = game.card_defs.get(scid)
                    game.push_card_updated(
                        scid, game.player_uid, game_engine.ECardCollections.Warzone,
                        game_engine.card_type_from_db(row[1]),
                        template_id=row[0], state=0,
                        cost=cdef.cost if cdef else 0,
                        attack=cdef.attack if cdef else 0,
                        defense=cdef.defense if cdef else 0)
                self._send_battle_events(session, game, transaction.player_id)
                session._rules_port_mutation_emitted = True
                return True
            def resolve_quit_game(transaction):
                """RulesPort owns quit validation; host emits GameEnded."""
                if (session.session_name or "").startswith("tourney-"):
                    from services.tournament_game import pvp_concede
                    handled = bool(pvp_concede(self, session))
                else:
                    # The persisted practice player IDs are integers, but
                    # GameEnded's wire event requires domain UID objects.
                    # _fresh_game() may be built from persisted IDs, so do not
                    # forward game.ai_uid/game.player_uid directly here.
                    player_wire_uid = game_engine.UID.make(
                        244, int(self.client_reck_id))
                    ai_wire_uid = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
                    self._finish_battle_gameend(
                        session, False,
                        winners=[ai_wire_uid], losers=[player_wire_uid])
                    handled = True
                if handled:
                    session._rules_port_mutation_emitted = True
                return handled
            def resolve_encounter_mod_dialog(transaction):
                """Route the typed conversation acknowledgement through port."""
                original = getattr(session, "_rules_port_dispatch_command", None)
                if original is None:
                    return False
                try:
                    handled = bool(self._handle_encounter_mod_dialog_transaction(
                        session, original))
                    if handled:
                        session._rules_port_mutation_emitted = True
                    return handled
                except Exception as exc:
                    log_req(f"    RulesPort encounter dialog failed: {exc}")
                    return False
            # Keep all typed card-play ingress behind one RulesPort-owned
            # boundary.  The host mutation callback is temporary migration
            # plumbing; validation and ordering no longer fall through from
            # the RulesPort scheduler to the dispatcher implicitly.
            def publish_card_activated(ability, transaction):
                sink = getattr(port, "event_sink", None)
                if sink is None:
                    return False
                source_uid = int(getattr(ability, "source_uid", 0) or 0)
                profile = (self.user_profile if
                           isinstance(self.user_profile, dict) else {})
                owner_id = int(profile.get("id", 0) or 0)
                try:
                    from rules_port import lifecycle as _be
                    from rules_port.triggers import dispatch_card_activated
                    state = _be.load_state(session)
                    result = dispatch_card_activated(
                        db=_db, handler=self, game=sink.game, session=session,
                        player_uid=sink.game.player_uid,
                        ai_uid=sink.game.ai_uid, battle_state=state,
                        ability_guid=getattr(ability,
                                             "ability_template_id", ""),
                        source_card_id=source_uid,
                        source_player_id=owner_id,
                        ability_instance_id=ability.instance_id,
                        activation_data=ability.activation.as_dict())
                    _be.save_state(session, state)
                    return result is not False
                except Exception as exc:
                    log_req(f"    RulesPort CardActivatedEvent failed: {exc}")
                    return False

            card_executor = MetadataCardTransactionExecutor(
                    port,
                    graph_loader=lambda guid: _records_ability_graph(self, guid),
                    owner_id=(self.user_profile.get("id", 0)
                              if isinstance(self.user_profile, dict) else 0),
                    store=getattr(self, "_play_plan_store", None),
                    projection=card_projection,
                    activation_event=publish_card_activated,
                    resource_compatibility=resource_mutation)
            port.set_card_transaction_resolver(card_executor)
            port.set_resource_transaction_resolver(
                lambda tx: card_executor("play_resource", tx))
            port.set_hand_transaction_resolver(hand_projection)
            def setup_mutation(transaction):
                """Apply the typed play/draw choice through the setup seam."""
                original = getattr(session, "_rules_port_dispatch_command", None)
                if original is None:
                    return False
                try:
                    handled = bool(self._handle_choose_pick_transaction(
                        session, original))
                    if handled:
                        session._rules_port_mutation_emitted = True
                    return handled
                except Exception as exc:
                    log_req(f"    RulesPort setup mutation failed: {exc}")
                    return False
            port.set_setup_transaction_resolver(setup_mutation)
            def priority_mutation(transaction):
                """Advance the live phase/AI driver after RulesPort pass."""
                # Card chains are native RulesPort actions. Let the native
                # stack consume this pass; otherwise the compatibility phase
                # driver would pop the durable descriptor first and create a
                # second resolution authority.
                from rules_port.kernel import PriorityWindowAction
                # A reconnect can restore the durable chain descriptor before
                # its action objects. Repair that boundary before interpreting
                # this pass; otherwise a stale ordinary phase window wins and
                # the tunneled card remains on the chain indefinitely.
                if getattr(port, "chain", None) is not None:
                    port.ensure_projected_chain_action()
                action = port.action_stack.peek()
                if isinstance(action, PriorityWindowAction):
                    # The queue may contain the persisted raw form while the
                    # normalized transaction contains a typed UID (or vice
                    # versa).  Pass the queue's exact identity after the
                    # UID-equivalence check so PriorityWindowAction's strict
                    # equality cannot strand the window.
                    pass_player = transaction.player_id
                    if (_same_uid(action.priority_player_id, pass_player) and
                            action.priority_player_id != pass_player):
                        pass_player = action.priority_player_id
                    if port.pass_player_priority(pass_player):
                        next_player = port.action_stack.priority_player_id
                        phase_window = (getattr(action,
                                               "ability_responding_to", None)
                                        is None)
                        practice_window = not (session.session_name or "").startswith(
                            "tourney-")
                        log_req("    RulesPort pass DEBUG: "
                                f"passed={pass_player!r} next={next_player!r} "
                                f"phase_window={phase_window} "
                                f"practice={practice_window} "
                                f"responding_to="
                                f"{getattr(action, 'ability_responding_to', None)!r}",
                                level="DEBUG")
                        # Practice/PvE has one client and one server-driven
                        # participant. The AI must pass through the same
                        # native action both for ordinary ALL-player phase
                        # windows and for the response window opened by a
                        # manual ability. Otherwise a hand Tunnel ability
                        # remains on the chain after the human's pass, with
                        # the card still in Hand and no resolver checkpoint.
                        if phase_window:
                            client_id = transaction.player_id
                            ai_id = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
                            response_phases = {
                                game_engine.ETurnPhases.DeclareAttackPriorityWindow,
                                game_engine.ETurnPhases.DeclareDefensePriorityWindow,
                                game_engine.ETurnPhases.FirstStrikePriorityWindow,
                                game_engine.ETurnPhases.EndPhase,
                            }
                            phase = port.current_turn_phase
                            while (next_player is not None and
                                   not _same_uid(next_player, client_id)):
                                # The phase driver already made decisions on
                                # the AI's own turn. When the human is active,
                                # let the Practice AI use the same metadata-
                                # based quick response path before passing a
                                # combat window or EndPhase.
                                if (practice_window and phase in response_phases
                                        and not _same_uid(
                                            port.active_player_id, ai_id)
                                        and _same_uid(next_player, ai_id)):
                                    ai_acted = False
                                    try:
                                        from ai import ai_respond_to_priority
                                        from rules_port.persistence import (
                                            load_state as _load_response_state)
                                        response_state = _load_response_state(
                                            session)
                                        # ``game`` is the event builder for the
                                        # pass transaction already being
                                        # handled.  _fresh_game() reattaches
                                        # RulesPort and syncs the checkpoint;
                                        # that rebuilds an ALL-player queue
                                        # from the AI's priority and puts the
                                        # already-passed human back in it.
                                        # Reuse this builder so the AI decision
                                        # cannot reopen the same phase window.
                                        response_game = game
                                        ai_acted = bool(ai_respond_to_priority(
                                            self, response_game, session,
                                            ai_id, game.player_uid,
                                            response_state))
                                        if ai_acted:
                                            event_sink = getattr(
                                                port, "event_sink", None)
                                            if event_sink is not None:
                                                event_sink.game = response_game
                                            self._send_battle_events(
                                                session, response_game,
                                                game.player_uid)
                                            port.persist()
                                            next_player = (
                                                port.action_stack
                                                .priority_player_id)
                                            break
                                    except Exception as exc:
                                        log_req(
                                            "    RulesPort phase AI response "
                                            f"failed; using pass fallback: {exc!r}")
                                if not port.pass_player_priority(next_player):
                                    log_req(
                                        "    RulesPort phase AI pass DEBUG: "
                                        f"pass_player_priority({next_player!r}) "
                                        "returned False; priority="
                                        f"{port.action_stack.priority_player_id!r}")
                                    break
                                next_player = port.action_stack.priority_player_id
                        elif practice_window:
                            # A manual/triggered ability response is different
                            # from an ordinary phase window. The Practice AI
                            # has no Unity client to submit its own response,
                            # but it must still get the same decision point:
                            # play a useful metadata-defined quick action or
                            # pass. Do not unconditionally pass the AI here,
                            # or quick removal/combat tricks can never fire.
                            client_id = transaction.player_id
                            log_req("    RulesPort AI response loop DEBUG: "
                                    f"client={client_id!r} "
                                    f"next={next_player!r}",
                                    level="DEBUG")
                            while (next_player is not None and
                                   not _same_uid(next_player, client_id)):
                                ai_acted = False
                                try:
                                    from ai import ai_respond_to_priority
                                    from rules_port.persistence import (
                                        load_state as _load_response_state)
                                    response_state = _load_response_state(session)
                                    # Reuse the current transaction's Game.
                                    # Constructing a fresh one re-syncs the
                                    # native checkpoint and can repopulate
                                    # participants who already passed this
                                    # response window.
                                    response_game = game
                                    ai_acted = bool(ai_respond_to_priority(
                                        self, response_game, session,
                                        game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000),
                                        game.player_uid, response_state))
                                    if ai_acted:
                                        event_sink = getattr(
                                            port, "event_sink", None)
                                        if event_sink is not None:
                                            # Keep native resolver events from
                                            # the AI response in the same event
                                            # batch that contains its card
                                            # movement and chain projection.
                                            event_sink.game = response_game
                                        self._send_battle_events(
                                            session, response_game,
                                            game.player_uid)
                                        port.persist()
                                        next_player = port.action_stack.priority_player_id
                                        break
                                except Exception as exc:
                                    log_req(
                                        "    RulesPort AI response failed; "
                                        f"using pass fallback: {exc!r}")
                                if not port.pass_player_priority(next_player):
                                    log_req("    RulesPort AI pass DEBUG: "
                                            f"pass_player_priority({next_player!r}) "
                                            "returned False; priority="
                                            f"{port.action_stack.priority_player_id!r}",
                                            level="DEBUG")
                                    break
                                next_player = port.action_stack.priority_player_id
                                log_req("    RulesPort AI pass DEBUG: "
                                        f"next now {next_player!r}",
                                        level="DEBUG")
                        # A first pass only hands APNAP priority to the next
                        # player. Do not tick an as-yet-unentered action: its
                        # on_enter() may rebuild the queue, undoing that pass.
                        # Once all required players passed, drive the native
                        # action stack through chain resolution/phase entry.
                        if next_player is None:
                            # One pass may require an action cleanup tick,
                            # several NONE phase transitions, and finally the
                            # next real input window. Let the native scheduler
                            # own that entire boundary.
                            drive_started = time.perf_counter()
                            port.drive_until_input(max_steps=64)
                            _rules_port_timing(
                                "post-pass-drive", drive_started,
                                phase=port.current_turn_phase,
                                priority=port.action_stack.priority_player_id,
                                chain=tuple(getattr(
                                    port.chain, "_instance_ids", ())))
                            # A projected card chain can finish its resolver
                            # and immediately expose the server actor's
                            # follow-up priority in the same scheduler tick.
                            # Practice has no second client transaction for
                            # that actor; consume the native passes and
                            # continue to the next real input boundary.  A
                            # resolver may queue several server-owned
                            # responses in a row (a troop's Deploy trigger
                            # after its cast trigger), so keep draining until
                            # the client owns the window or the chain ends.
                            for _follow_up in range(8):
                                follow_up = port.action_stack.peek()
                                follow_priority = getattr(
                                    follow_up, "priority_player_id", None)
                                if not _is_practice_chain_follow_up(
                                        follow_up, transaction.player_id,
                                        practice_window):
                                    break
                                if not port.pass_player_priority(
                                        follow_priority):
                                    break
                                port.drive_until_input(max_steps=64)
                        next_action = port.action_stack.peek()
                        # The compatibility host used to invoke the AI from
                        # its own pass handler. Native phase actions bypass
                        # that handler, so explicitly resume the server actor
                        # when this boundary leaves it with priority.
                        # The closure's Game can be a stale projection from
                        # the just-finished human transaction.  In Practice
                        # the server AI has one canonical typed identity;
                        # never infer it from that transient Game object.
                        ai_id = (None if (session.session_name or "").startswith(
                            "tourney-") else game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000))
                        from rules_port.persistence import load_state as _load_rules_state
                        native_state = _load_rules_state(session)
                        log_req(
                            "    RulesPort post-pass: "
                            f"phase={port.current_turn_phase!r} "
                            f"active={port.active_player_id!r} "
                            f"priority={getattr(next_action, 'priority_player_id', None)!r} "
                            f"players={port.player_ids!r} "
                            f"state_turn={native_state.get('turn_player')!r}")
                        if any(native_state.get(key) for key in (
                                "pending_choice", "pending_trigger",
                                "pending_deck_search", "pending_conversation",
                                "pending_discard_ability",
                                "resolution_paused")):
                            # The resolver has already sent the prompt packet.
                            # Do not let this pass boundary immediately call
                            # the AI driver or rebuild ordinary phase options;
                            # either would replace the class-39/choice UI
                            # before the client can answer it.
                            log_req(
                                "    RulesPort post-pass held for pending "
                                "client input")
                            return True
                        if (isinstance(next_action, PriorityWindowAction) and
                                _same_uid(next_action.priority_player_id, ai_id) and
                                getattr(next_action, "ability_responding_to", None)
                                is None):
                            native_phases: list[int] = [
                                int(phase) for phase in
                                (native_state.get("turn_phases") or ())]
                            try:
                                native_idx = native_phases.index(
                                    port.current_turn_phase)
                            except ValueError:
                                native_idx = int(native_state.get("phase_idx", 0) or 0)
                            # ``run_ai_turn`` normally enters a phase itself.
                            # This one was entered by drive_until_input, so
                            # tell it to consume the already-materialized
                            # native action instead of firing entry hooks twice.
                            setattr(
                                port, "_native_phase_already_entered",
                                port.current_turn_phase)
                            self._run_ai_turn(
                                session, game.player_uid, ai_id,
                                native_state, start_idx=native_idx)
                        elif (isinstance(next_action, PriorityWindowAction) and
                              _same_uid(next_action.priority_player_id,
                                        game.player_uid)):
                            # A completed native pass may have entered the
                            # next phase for the human. Rebuild only the
                            # client projection; RulesPort has already chosen
                            # and materialized the phase/window.
                            self._project_rules_port_priority(
                                session, game.player_uid, ai_id, native_state)
                        return True
                original = getattr(session, "_rules_port_dispatch_command", None)
                if original is None:
                    return False
                try:
                    handled = bool(self._handle_pass_priority_transaction(
                        session, original))
                    if handled:
                        session._rules_port_mutation_emitted = True
                    return handled
                except Exception as exc:
                    log_req(f"    RulesPort priority transition failed: {exc}")
                    return False
            port.set_priority_transaction_resolver(priority_mutation)
            def _rules_port_host_projection(method_name, transaction):
                """Run a host state/event projection for a native intent."""
                original = getattr(session, "_rules_port_dispatch_command", None)
                if original is None:
                    return False
                try:
                    handled = bool(getattr(self, method_name)(session, original))
                    if handled:
                        session._rules_port_mutation_emitted = True
                    return handled
                except Exception as exc:
                    log_req(f"    RulesPort {method_name} projection failed: {exc}")
                    return False
            port.set_auto_pass_transaction_resolver(
                lambda tx: _rules_port_host_projection(
                    "_handle_set_auto_pass_transaction", tx))
            port.set_cancel_auto_pass_transaction_resolver(
                lambda tx: _rules_port_host_projection(
                    "_handle_cancel_auto_pass_transaction", tx))
            port.set_priority_sync_resolver(
                lambda tx: _rules_port_host_projection(
                    "_handle_priority_sync_transaction", tx))
            port.set_turn_phase_resolver(
                lambda tx: _rules_port_host_projection(
                    "_handle_set_turn_phases_transaction", tx))
            port.set_discard_transaction_resolver(discard_projection)
            port.set_discard_continuation_resolver(
                lambda tx, _session=session:
                self._resolve_rules_port_discard_continuation(_session, tx))
            port.set_triggered_ability_transaction_resolver(
                lambda tx, _session=session:
                self._resolve_rules_port_trigger_continuation(_session, tx))
            port.set_attack_transaction_resolver(attack_projection)
            port.set_defense_transaction_resolver(defense_projection)
            from rules_port.combat import CombatDamageBackend
            port.set_combat_damage_backend(
                CombatDamageBackend(lambda _port, transaction:
                                    damage_projection(transaction)))
            port.set_choice_transaction_resolver(choice_projection)
            port.set_player_options_resolver(refresh_player_options)
            port.set_ready_card_transaction_resolver(ready_cards)
            port.set_quit_game_resolver(resolve_quit_game)
            port.set_encounter_mod_resolver(resolve_encounter_mod_dialog)
            # If the checkpoint is already in a non-stop phase, finish that
            # native action and walk the phase graph now. Otherwise the first
            # reconnect packet leaves the client looking at Ready/Prep/Draw
            # and waiting for a pass that should never have been required.
            from rules_port.kernel import PriorityWindowAction
            current_action = port.action_stack.peek()
            if (active == game.player_uid and
                    isinstance(current_action, PriorityWindowAction) and
                    getattr(current_action, "ability_responding_to", None) is None and
                    current_action.priority_player_id is None):
                port.drive_until_input(max_steps=64)
            # Fail the attach loudly if a newly added typed transaction has no
            # host projection.  Without this gate the kernel can validate an
            # intent, then leave SQLite/client state unchanged while the
            # compatibility dispatcher appears to have handled it.
            port.assert_projection_wiring()
            setattr(session, "_rules_port_session", port)
            log_req(f"    RulesPort attached session={session.session_id}",
                    level="DEBUG")
            return port
        except Exception as exc:
            log_req(f"    RulesPort attach failed session={getattr(session, 'session_id', '?')}: {exc}")
            # The native host is the live authority. Returning None here would
            # let setup or a later event continue through the legacy engine,
            # producing a hybrid session whose behavior depends on which
            # initialization step failed.
            raise RuntimeError(
                "RulesPort attachment failed; refusing legacy fallback") from exc

    def _sync_rules_port_setup_checkpoint(self, session, phase, player_id):
        """Keep native setup validation aligned with the phase packet.

        PVE still builds its PickGoesFirst and Mulligan packets through the
        host projection. Mirror that phase and its priority window into the
        native RulesPort snapshot before sending the packet, so the next
        typed transaction is validated against the state the client sees.
        """
        port = getattr(session, "_rules_port_session", None)
        if port is None:
            raise RuntimeError(
                "RulesPort setup phase has no attached native session")
        participant = port.coerce_transaction_player_id(player_id)
        port.sync_checkpoint(
            phases=(phase,), phase_idx=0,
            active_player_id=participant,
            client_player_id=participant,
            ensure_current_priority=True,
            ensure_main_priority=False,
        )
        port.persist()
        log_req("    RulesPort setup checkpoint: "
                f"phase={phase!r} player={participant!r}")

    def _dispatch_native_trigger(self, game, session, pl_t, ai_t, bstate,
                               event_type, source_card_id=None,
                               source_player_id=None, target_card_id=None,
                               target_player_id=None, **data):
        """Emit a gameplay event through the attached rules authority."""
        from rules_port.triggers import dispatch_native_trigger
        return dispatch_native_trigger(
            db=_db, handler=self, game=game, session=session,
            player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
            event_type=event_type, source_card_id=source_card_id,
            source_player_id=source_player_id,
            target_card_id=target_card_id,
            target_player_id=target_player_id, data=data)

    def _clear_combat_damage(self, session, bstate):
        from rules_port.lifecycle import clear_combat_damage
        return clear_combat_damage(_db, session.session_id)

    def _clear_expired_temporary_attributes(self, session, bstate, owner_id,
                                             boundary, clear_stat_buffs=False):
        from rules_port.lifecycle import clear_expired_temporary_attributes
        return clear_expired_temporary_attributes(
            _db, session.session_id, owner_id, boundary,
            clear_stat_buffs=clear_stat_buffs,
            battle_state=bstate, handler=self)

    def _dispatch_rules_port_transaction(self, session, command, player_uid):
        """Serialize tournament PvP dispatch under the per-session lock.

        Both clients run on their own connection thread and mutate the same
        native RulesPort object.  Without this lock a mulligan completion's
        first-turn phase drive (which holds the lock) can interleave with the
        other client's transaction, which then reads a mid-drive checkpoint and
        rolls the durable phase back — the first shard/card play is rejected by
        ``MainPhaseRequirement``/``PlayerHasPriorityRequirement``.
        """
        if session is not None and str(
                getattr(session, "session_name", "") or "").startswith(
                    "tourney-"):
            from services.tournament_game import pvp_session_lock
            with pvp_session_lock(session):
                return self._dispatch_rules_port_transaction_locked(
                    session, command, player_uid)
        return self._dispatch_rules_port_transaction_locked(
            session, command, player_uid)

    def _dispatch_rules_port_transaction_locked(self, session, command, player_uid):
        """Resolve one explicitly typed command through the native host."""
        if session is None:
            return False
        session = cast(game_session.GameSession, session)
        port = getattr(session, "_rules_port_session", None)
        payload = getattr(command, "typed_payload", None)
        # Setup, mulligan, pass-priority, and priority-sync commands now enter
        # the same typed RulesPort normalization path as gameplay commands.
        # If a legacy client omits required typed fields, normalization still
        # safely returns None and the outer dispatcher can report that decode
        # gap without mutating state.
        if port is None:
            log_req("    RulesPort skipped: no attached session host")
            return False
        # The port is shared by both connections.  Pin the dispatch identity
        # for the whole locked transaction so every projection (resource,
        # threshold, charge, cost) is attributed to THIS player.
        if session is not None and str(
                getattr(session, "session_name", "") or "").startswith(
                    "tourney-"):
            try:
                port._pvp_current_handler = self
                port._pvp_current_session = session
            except (AttributeError, TypeError):
                pass
        # The shared battle checkpoint keeps ``phase_idx`` at the beginning
        # of the turn while the client is in the pre-game dialogs.  Mulligan
        # and play/draw requests are nevertheless authoritative phase
        # transitions, so hydrate the port from the transaction family before
        # applying its phase requirements.  Without this, the first Mulligan
        # request is rejected as StartTurn and the client remains dialog-less.
        setup_phase = None
        if (getattr(command, "is_mulligan_keep", False) or
                getattr(command, "is_mulligan_redraw", False)):
            setup_phase = game_engine.ETurnPhases.Mulligan
        elif getattr(command, "is_choose_pick", False):
            setup_phase = game_engine.ETurnPhases.PickGoesFirst
        # Older in-flight PVE sessions could already have shown Mulligan while
        # their native checkpoint still said NotPlaying. Repair only that
        # pre-turn checkpoint when native history confirms the preceding
        # Play/Draw choice and the same player still owns active and priority.
        # New setup packets save phases before transmission, so this only
        # bridges sessions created before that fix.
        if (setup_phase == game_engine.ETurnPhases.Mulligan and
                port.current_turn_phase == game_engine.ETurnPhases.NotPlaying):
            try:
                from rules_port.persistence import load_state as _load_setup_state
                setup_state = _load_setup_state(session)
                setup_player = port.coerce_transaction_player_id(player_uid)
                native_state = (setup_state.get("rules_port", {})
                                if isinstance(setup_state, dict) else {})
                history = (native_state.get("transaction_history", ())
                           if isinstance(native_state, dict) else ())
                chose_first = any(
                    isinstance(entry, dict) and entry.get("kind") in (
                        "choose_play_first", "choose_draw_first")
                    for entry in history)
                if (isinstance(setup_state, dict) and
                        not setup_state.get("pvp") and
                        not setup_state.get("turn_phases") and
                        chose_first and
                        _same_uid(port.active_player_id, setup_player) and
                        _same_uid(port.action_stack.priority_player_id,
                                  setup_player)):
                    self._sync_rules_port_setup_checkpoint(
                        session, setup_phase, setup_player)
            except Exception as exc:
                log_req(f"    RulesPort setup checkpoint recovery failed: {exc}")
        if setup_phase is not None:
            port.current_turn_phase = setup_phase
        # Clear orphaned continuation markers before any fresh transaction,
        # not only ability activations.  A prior AI discard must never consume
        # the player's next shard/card transaction.
        try:
            from rules_port import lifecycle as _be_clean
            clean_state = _be_clean.load_state(session)
            if (not clean_state.get("pending_discard_ability") and
                    not getattr(port, "pending_activation", None)):
                for key in ("rules_port_resume_effect_order", "resolution_paused",
                            "pending_discard_target_template", "pending_discard_scid"):
                    clean_state.pop(key, None)
                _be_clean.save_state(session, clean_state)
        except Exception:
            pass
        # Class-23 discard replies are serialized by Mono as a fresh
        # ActivateAbilityTransaction (child GUID) even though they continue
        # the waiting parent.  The port's in-memory pending marker may be
        # absent after a reconnect, so consult the authoritative battle
        # checkpoint before normalization and force the continuation shape.
        if getattr(command, "is_ability_activate", False):
            try:
                from rules_port import lifecycle as _be_wire
                wire_state = _be_wire.load_state(session)
                if wire_state.get("pending_discard_ability"):
                    # Mono emits the discard selection as a child
                    # ActivateAbilityTransaction.  The child GUID is only a
                    # UI helper; RulesPort must resume the waiting parent
                    # instance with the selected hand card.
                    pending = getattr(port, "pending_activation", None)
                    if not isinstance(pending, dict):
                        pending = {}
                    continuation_payload = dict(payload or {})
                    if pending.get("ability_instance_id") is not None:
                        continuation_payload["ability_instance_id"] = int(
                            pending["ability_instance_id"])
                    continuation_payload.setdefault("activation_data", {})
                    command = replace(command, is_ability_activate=False,
                                      is_set_ability_data=True,
                                      typed_payload=continuation_payload)
                    payload = continuation_payload
                    log_req("    RulesPort discard continuation: forcing SetAbilityActivationData "
                            f"instance={continuation_payload.get('ability_instance_id')}")
                elif not getattr(port, "pending_activation", None):
                    # A completed AI discard continuation must not poison the
                    # next player's fresh activation with its resume offset
                    # or paused marker (otherwise Stargazer draws nothing and
                    # appears to jump zones).
                    for key in ("rules_port_resume_effect_order",
                                "resolution_paused", "pending_discard_target_template",
                                "pending_discard_scid"):
                        wire_state.pop(key, None)
                    _be_wire.save_state(session, wire_state)
            except Exception:
                pass
        # Choice-card selections can also arrive as a child
        # ActivateAbilityTransaction. Treat them as continuations while the
        # metadata choice checkpoint is pending.
        if getattr(command, "is_ability_activate", False):
            try:
                from rules_port import lifecycle as _be_choice_wire
                if _be_choice_wire.load_state(session).get("pending_choice"):
                    command = replace(command, is_ability_activate=False,
                                      is_set_ability_data=True)
                    log_req("    RulesPort choice continuation: forcing SetAbilityActivationData")
            except Exception:
                pass
        # Class-23 discard continuations are consumed by the legacy hand
        # discard bridge below. They do not represent a live RulesPort
        # AbilityInstance, so do not reject them as missing port state.
        try:
            from rules_port import lifecycle as _be_discard_bridge
            if (getattr(command, "is_set_ability_data", False) and
                    _be_discard_bridge.load_state(session).get(
                        "pending_discard_ability")):
                payload = dict(payload or {})
                payload["_discard_continuation"] = True
                command = replace(command, typed_payload=payload)
        except Exception:
            pass
        # Trigger target selections emitted by the legacy metadata trigger
        # bridge are likewise persisted as a battle-state checkpoint, not as
        # a hydrated RulesPort AbilityInstance. Let the existing triggered
        # ability resolver consume these selections instead of rejecting them
        # on the port queue requirements after a reconnect/reattach.
        try:
            from rules_port import lifecycle as _be_trigger_bridge
            if getattr(command, "is_activate_triggered_abilities", False):
                _bridge_state = _be_trigger_bridge.load_state(session)
                # The "choose a card in your deck" picker is a class-39 prompt
                # too, so its answer arrives in this envelope (Scheme).  Mark
                # both pending prompts before normalization: otherwise the
                # non-triggered ability is reclassified as a fresh manual
                # activation and rejected by AbilityCanBeActivated.
                if (_bridge_state.get("pending_trigger") or
                        _bridge_state.get("pending_deck_search")):
                    payload = dict(payload or {})
                    payload["_triggered_continuation"] = True
                    command = replace(command, typed_payload=payload)
            elif getattr(command, "is_set_ability_data", False):
                _bridge_state = _be_trigger_bridge.load_state(session)
                if (_bridge_state.get("pending_trigger") or
                        _bridge_state.get("pending_deck_search")):
                    # Class-23 target/search answers are persisted host
                    # continuations rather than hydrated RulesPort ability
                    # instances. Route them through the continuation
                    # projection instead of trying to resume a missing native
                    # activation instance.
                    payload = dict(payload or {})
                    payload["_triggered_continuation"] = True
                    command = replace(command, typed_payload=payload)
        except Exception:
            pass
        facts = getattr(port, "runtime_facts", None)
        projected_game = None
        if facts is not None:
            # The PlayerId nested in PlayerTransactionRequestArgs is the
            # client/session namespace, while Game.player_uid is the service
            # projection namespace.  Preserve the request identity for
            # owner-side validation instead of treating the human as AI.
            facts.client_player_uid = player_uid
            # The port event sink owns the current Game projection; this
            # dispatch helper itself does not have a local ``game`` variable.
            projected_game = getattr(getattr(port, "event_sink", None),
                                     "game", None)
            def _valid_champion(value, fallback):
                try:
                    uid = getattr(value, "uid", value)
                    if int(getattr(uid, "uid64", uid) or 0) > 0:
                        return value
                except (TypeError, ValueError):
                    pass
                return fallback
            facts.player_champion_card_id = _valid_champion(
                getattr(projected_game, "player_champion_card_id", None),
                getattr(self, "_player_champ_scid", None))
            facts.ai_champion_card_id = _valid_champion(
                getattr(projected_game, "ai_champion_card_id", None),
                getattr(self, "_ai_champ_scid", None))
            # Synthetic champion cards are not in game_cards. Preserve the
            # exact ability catalog projected on each champion so a talent
            # charge power can pass the same source-card gate as a signature
            # champion ability.
            def _champion_ability_guids(card_id, fallback):
                card_def = getattr(projected_game, "card_defs", {}).get(card_id)
                values = tuple(str(value.guid).lower() for value in
                               (getattr(card_def, "abilities", ()) or ()))
                if values:
                    return values
                return tuple(str(getattr(value, "guid", value)).lower()
                             for value in (fallback or ()))
            facts.player_champion_ability_guids = _champion_ability_guids(
                facts.player_champion_card_id,
                getattr(self, "_player_champ_abilities", ()))
            facts.ai_champion_ability_guids = _champion_ability_guids(
                facts.ai_champion_card_id,
                getattr(self, "_ai_champ_ability_guids", ()))
        payload_required = any(getattr(command, name, False) for name in (
            "is_discard", "is_ready_card", "is_play_resource",
            "is_play_troop", "is_play_artifact", "is_play_spell",
            "is_play_champion", "is_ability_activate",
            "is_activate_triggered_abilities", "is_set_ability_data",
            "is_commit_attack", "is_commit_defense", "is_assign_damage",
            "is_set_stops", "is_set_auto_pass", "is_quit_game",
            "is_encounter_mod_dialog"))
        if payload is None and payload_required:
            log_req("    RulesPort skipped: decoder supplied no typed payload")
            # These transaction classes are inside the RulesPort migration
            # boundary.  Do not let a malformed/unsupported nested ObjFmt
            # envelope fall through to the legacy rules handler, where it
            # could mutate state without the port's phase/priority checks.
            self._push_transaction_ack(session)
            setattr(session, "_rules_port_dispatch_command", None)
            return True
        from rules_port.wire import (submit_classified_transaction,
                                     normalize_player_transaction)
        # Choice-card prompts are metadata continuations and therefore do not
        # have a live RulesPort AbilityInstance. Mark them for the dedicated
        # continuation resolver instead of applying SetAbility requirements.
        if (getattr(command, "is_set_ability_data", False) and
                isinstance(payload, dict)):
            try:
                from rules_port import lifecycle as _be_choice
                if _be_choice.load_state(session).get("pending_choice"):
                    payload = dict(payload)
                    payload["_choice_continuation"] = True
                    command = replace(command, typed_payload=payload)
            except Exception:
                pass
        # Mono's class-23 response commonly carries only the selected target
        # and omits AbilityInstanceId. Recover it only from an authoritative
        # pending continuation; never guess for a fresh activation.
        if (getattr(command, "is_set_ability_data", False) and
                isinstance(payload, dict) and
                payload.get("ability_instance_id") in (None, "")):
            continuation_instance = None
            pending = getattr(port, "pending_activation", None)
            if isinstance(pending, dict):
                continuation_instance = pending.get("ability_instance_id")
            try:
                if continuation_instance is None:
                    from rules_port import lifecycle as _be_cont
                    cont_state = _be_cont.load_state(session)
                    for key in ("pending_trigger", "pending_deck_search"):
                        marker = cont_state.get(key)
                        if (isinstance(marker, dict) and
                                marker.get("instance_id") is not None):
                            continuation_instance = marker.get("instance_id")
                            break
                    if (continuation_instance is None and
                            cont_state.get("pending_discard_ability")):
                        continuation_instance = cont_state.get(
                            "pending_discard_ability_instance_id", 1)
            except Exception:
                continuation_instance = None
            if continuation_instance is not None:
                payload = dict(payload)
                payload["ability_instance_id"] = int(continuation_instance)
                command = replace(command, typed_payload=payload)
                log_req("    RulesPort continuation instance recovered="
                        f"{payload['ability_instance_id']}")
        # Some client builds serialize a manual/quick ability activation using
        # the (misspelled) ActivateTriggeredAbiliesTransaction envelope.  The
        # envelope name is not authoritative: metadata marks whether the
        # ability is actually triggered.  Reclassify a single non-triggered
        # ability before normalization so QuickActions (for example Scheme)
        # are validated as ordinary activations rather than requiring a
        # pending triggered-ability queue.
        if (getattr(command, "is_activate_triggered_abilities", False) and
                isinstance(payload, dict)):
            try:
                ability_guid = str(payload.get("ability_template_id") or "").lower()
                activation = payload.get("activation_data")
                triggered = (db_ability_is_triggered(
                    ability_guid, conn=_db) if ability_guid else None)
                if (triggered is False and
                        payload.get("source_card_id") is not None and
                        isinstance(activation, (dict, tuple, list))):
                    command = replace(command,
                                      is_ability_activate=True,
                                      is_activate_triggered_abilities=False,
                                      typed_payload=payload)
                    log_req("    RulesPort reclassified non-triggered ability "
                            f"{ability_guid} from triggered envelope")
            except Exception as exc:
                log_req(f"    RulesPort ability envelope check failed: {exc}")
        setattr(session, "_rules_port_dispatch_command", command)
        session._rules_port_mutation_emitted = False
        # Mono can submit the same ability activation twice on a rapid
        # double-click.  Once the first request has been accepted, the second
        # must be an idempotent acknowledgement; letting it fall through to
        # the legacy handler pays the cost/opens a second discard continuation
        # and leaves the RulesPort scheduler stranded.
        if getattr(command, "is_ability_activate", False):
            try:
                history = list(getattr(port, "transaction_history", ()) or ())
                last = history[-1] if history else None
                lp = (last or {}).get("payload", {})
                if (last and (last or {}).get("kind") == "activate_ability" and
                        (getattr(port, "pending_activation", None) or
                         bool(getattr(port, "action_stack", None) and port.action_stack.count)) and
                        int(lp.get("source_card_id", -1)) == int((payload or {}).get("source_card_id", -2)) and
                        str(lp.get("ability_template_id", "")).lower() ==
                        str((payload or {}).get("ability_template_id", "")).lower()):
                    log_req("    RulesPort ignored duplicate ability activation")
                    self._push_transaction_ack(session)
                    setattr(session, "_rules_port_dispatch_command", None)
                    return True
            except (TypeError, ValueError, AttributeError):
                pass
        if not submit_classified_transaction(
                port, command, player_uid, payload=payload):
            rejected = normalize_player_transaction(
                command, player_uid, current_phase=port.current_turn_phase,
                payload=payload)
            coerce_player = getattr(port, "coerce_transaction_player_id", None)
            intent_player = (coerce_player(player_uid)
                             if callable(coerce_player) else player_uid)
            if rejected is None:
                # The typed decoder could not build an intent (for example a
                # play transaction whose nested payload never decoded).  The
                # port has nothing to explain, so name the boundary directly.
                log_req("    RulesPort reject: unclassified transaction"
                        f" phase={port.current_turn_phase!r}"
                        f" player={player_uid!r}")
            log_req("    RulesPort boundary diagnostic:"
                    f" intent_phase="
                    f"{getattr(rejected, 'phase', None)!r}"
                    f" native_phase={port.current_turn_phase!r}"
                    f" player={player_uid!r}"
                    f" coerced={intent_player!r}"
                    f" in_session="
                    f"{_uid_in(intent_player, port.player_ids)}"
                    f" terminated={port.terminated}"
                    f" native_priority="
                    f"{port.action_stack.priority_player_id!r}"
                    f" active={port.active_player_id!r}")
            if getattr(rejected, "kind", "") == "pass_priority":
                try:
                    action = port.action_stack.peek()
                    log_req(
                        "    RulesPort priority diagnostic: "
                        f"phase={port.current_turn_phase!r} "
                        f"stack_priority={port.action_stack.priority_player_id!r} "
                        f"action_priority={getattr(action, 'priority_player_id', None)!r} "
                        f"queue={list(getattr(action, '_priority_queue', ()) or ())!r} "
                        f"chain={list(getattr(port.chain, '_instance_ids', ()) or ())!r}")
                except Exception as diag_exc:
                    log_req(f"    RulesPort priority diagnostic failed: {diag_exc}")
            if (getattr(command, "is_ability_activate", False) and
                    projected_game is not None):
                try:
                    source = (payload or {}).get("source_card_id")
                    guid = (payload or {}).get("ability_template_id")
                    card = facts.get_card(source) if facts is not None else None
                    log_req(f"    RulesPort ability validation: source={source} guid={guid} "
                            f"card={card} owner={getattr(card, 'owner_id', None)} "
                            f"location={getattr(card, 'location', None)} abilities={getattr(card, 'abilities', None)}")
                except Exception as diag_exc:
                    log_req(f"    RulesPort ability diagnostic failed: {diag_exc}")
            setattr(session, "_rules_port_dispatch_command", None)
            # A decoder-owned typed card/ability intent is already inside the
            # migration boundary.  Do not let its RulesPort validation or
            # mutation rejection fall through to the legacy dispatcher: doing
            # so executes the same client request twice under different
            # phase/priority rules.
            # The empty acknowledgement also releases Mono's one-request
            # client gate while leaving authoritative state unchanged.
            rules_port_intent = any(
                getattr(command, name, False) for name in (
                    "is_play_resource", "is_play_troop", "is_play_artifact",
                    "is_play_spell", "is_play_champion",
                    "is_ability_activate", "is_activate_triggered_abilities",
                    "is_set_ability_data", "is_discard", "is_ready_card",
                    "is_commit_attack", "is_commit_defense", "is_assign_damage",
                    "is_request_player_options", "is_state_checksum",
                    "is_encounter_mod_dialog", "is_quit_game",
                    "is_mulligan_keep", "is_mulligan_redraw",
                    "is_set_stops", "is_set_auto_pass",
                    "is_pass_priority", "is_choose_pick",
                    "is_cancel_auto_pass", "is_priority_sync",
                    "is_tip_window_closed"))
            if rules_port_intent:
                options_resynced = (
                    getattr(command, "is_ability_activate", False) and
                    self._refresh_options_after_rejected_ability(
                        session, port, projected_game, player_uid))
                if not options_resynced:
                    self._push_transaction_ack(session)
                return True
            return False
        handled = port.handle_transaction()
        # ``handle_transaction`` only enqueues/mutates the port state.  Mirror
        # the C# session tick so a newly-created ability enters its action
        # stack immediately and can emit its activation checkpoint/continuation
        # in this same 3055 response.
        if handled:
            # A live HConnect session still owns the persisted battle phase.
            # On reconnect/reattach the port may have an empty action stack
            # even though the legacy state is sitting at a priority stop.  If
            # we call tick() first, AuthoritativeSession interprets that empty
            # stack as permission to advance FirstMain -> DeclareCombat before
            # it consumes the just-submitted activation.  Consume the queued
            # transaction first, then tick only the action stack created by
            # that transaction; never let the migration scheduler advance the
            # legacy phase on its own.
            for _ in range(16):
                if port.action_stack.count == 0:
                    break
                if not port.tick():
                    break
            # ``ResolveTopOfChainAction`` persists when it removes the chain
            # item, but the action stack itself is popped by the surrounding
            # ``GameActionStack.update`` immediately afterwards. Persist the
            # post-tick snapshot as well; otherwise a reconnect (or the next
            # transaction's reattach) sees a completed resolver still on the
            # stack and the client can remain unable to play another card.
            try:
                port.persist()
            except Exception as exc:
                log_req(f"    RulesPort post-tick persistence failed: {exc}")
        log_req(
            f"    RulesPort handled={handled} transaction={command.__class__.__name__}",
            level="DEBUG")
        if handled:
            game = getattr(getattr(port, "event_sink", None), "game", None)
            emitted = bool(getattr(session, "_rules_port_mutation_emitted", False))
            session._rules_port_mutation_emitted = False
            # While an interactive prompt is open (class-23 picker, triggered
            # target, deck search, ...) the prompt owns the client's UI.  The
            # scheduler's post-resolution events (GreenLight/TurnPhaseUpdated/
            # AbilityPushedOnChain) would replace it, so suppress them until
            # the prompt is answered.
            prompt_pending = False
            if (session.session_name or "").startswith("tourney-"):
                try:
                    from rules_port.persistence import load_state
                    _live = load_state(session) or {}
                    prompt_pending = any(_live.get(key) for key in (
                        "pending_choice", "pending_trigger",
                        "pending_deck_search", "pending_conversation",
                        "pending_discard_ability", "resolution_paused"))
                except Exception:
                    prompt_pending = False
            if game is not None and game.events:
                log_req(
                    "    RulesPort post-tick event sink: "
                    f"emitted={emitted} prompt_pending={prompt_pending} "
                    f"classes="
                    f"{[getattr(type(ev), 'CLASS_ID', 0) for ev in game.events]}")
            if game is not None and not emitted and not prompt_pending:
                    self._send_battle_events(session, game, player_uid)
            # A manual champion activation leaves a chain item pending.  The
            # native resolver emits the mutation packet, but the PvP adapter
            # still owns the client-facing response window.  Project the
            # current ResolveTopOfChain GreenLight to both clients or the
            # charge is paid while neither client receives a pass window.
            if (getattr(command, "is_ability_activate", False) and
                    (session.session_name or "").startswith("tourney-")):
                try:
                    from services.tournament_game import (
                        _pvp_populate_game_state, _send_pvp_packet,
                        player_handlers, pvp_load_state,
                    )
                    live_pvp = pvp_load_state(session) or {}
                    if live_pvp.get("stack") or not port.chain.is_empty:
                        pids = [int(value) for value in
                                (live_pvp.get("pids") or ())]
                        priority_pid = int(live_pvp.get("priority_pid") or 0)
                        if len(pids) == 2 and priority_pid in pids:
                            phase = port.current_turn_phase
                            turn_pid = int(live_pvp.get("turn_pid") or
                                            priority_pid)
                            for target_pid in pids:
                                target_h = player_handlers.get(target_pid)
                                if not target_h:
                                    continue
                                other_pid = next(pid for pid in pids
                                                 if pid != target_pid)
                                target_uid = game_engine.UID.make(244, target_pid)
                                other_uid = game_engine.UID.make(244, other_pid)
                                window = game_engine.Game(
                                    int(session.session_id), target_uid,
                                    other_uid)
                                _pvp_populate_game_state(
                                    window, live_pvp, target_pid, other_pid)
                                # UIBattle checks its local HasPriority bit
                                # when TurnPhaseUpdated arrives. Send the
                                # matching GreenLight first so a phase update
                                # cannot trigger a stale priority resync.
                                window.push_green_light(
                                    game_engine.UID.make(244, priority_pid),
                                    game_engine.EPriorityContext.ResolveTopOfChain)
                                window.push_turn_phase(
                                    phase,
                                    game_engine.UID.make(244, turn_pid),
                                    game_engine.UID.make(244, priority_pid))
                                _send_pvp_packet(
                                    target_h, session, window, target_uid,
                                    "rules-port-ability-priority")
                            log_req(
                                f"    PvP RulesPort ability priority window: "
                                f"pid={priority_pid} phase={phase}")
                except Exception as exc:
                    log_req(
                        f"    PvP RulesPort ability priority projection failed: {exc}")
            # Practice/PvE has the same native chain semantics as tournament
            # PvP, but only one client projection.  The activation tick emits
            # the charge/card updates; replace the stale main-phase options
            # with a chain-only response window while the ability is pending.
            if (getattr(command, "is_ability_activate", False) and
                    not (session.session_name or "").startswith("tourney-") and
                    getattr(port, "chain", None) is not None and
                    not port.chain.is_empty):
                try:
                    self._push_practice_ability_chain_window(session, port, game)
                except Exception as exc:
                    log_req(
                        f"    RulesPort Practice ability priority projection failed: {exc}")
            # A completed manual ability returns to the same main-phase
            # priority window.  The legacy ability handler rebuilt the
            # PlayerOptionList at that boundary, but the RulesPort action
            # resolver only emits its mutation/GreenLight events.  Without a
            # fresh option list Unity keeps the pre-activation list (or an
            # empty chain list), making every card appear unplayable until a
            # later phase packet happens to refresh it.
            #
            # Keep this generic and conservative: a pending continuation,
            # port chain item, or legacy trigger stack means the client is
            # still in a response/chain checkpoint and must not receive main
            # phase card options yet.
            if any(getattr(command, name, False) for name in (
                    "is_ability_activate", "is_play_resource",
                    "is_play_troop", "is_play_artifact", "is_play_spell",
                    "is_play_champion", "is_set_ability_data")):
                try:
                    from rules_port.persistence import load_state
                    settled_state = load_state(session)
                    main_phase = port.current_turn_phase in (
                        game_engine.ETurnPhases.FirstMainPhase,
                        game_engine.ETurnPhases.SecondMainPhase)
                    settled = (
                        main_phase and
                        not getattr(port, "pending_activation", None) and
                        getattr(port, "chain", None) is not None and
                        port.chain.is_empty and
                        not settled_state.get("stack") and
                        not any(settled_state.get(key) for key in (
                            "pending_choice", "pending_trigger",
                            "pending_deck_search", "pending_discard_ability",
                            "pending_conversation", "resolution_paused")))
                    if settled:
                        log_req("    RulesPort post-ability main-phase options "
                                "refresh (settled)")
                        if (session.session_name or "").startswith(
                                "tourney-"):
                            from services.tournament_game import (
                                pvp_load_state,
                                pvp_push_main_phase_options)
                            pvp_push_main_phase_options(
                                session, pvp_load_state(session) or {})
                        elif game is not None:
                            self._push_main_phase_options(
                                session, game.player_uid, game.ai_uid)
                except Exception as exc:
                    log_req(f"    RulesPort option refresh failed: {exc}")
        else:
            from rules_port.wire import describe_transaction
            described = normalize_player_transaction(
                command, player_uid, current_phase=port.current_turn_phase,
                payload=payload)
            log_req("    RulesPort handler reject: "
                    f"{describe_transaction(described, port)} "
                    f"phase={getattr(port, 'current_turn_phase', None)} "
                    f"priority={getattr(getattr(port, 'action_stack', None), 'priority_player_id', None)} "
                    f"action={type(getattr(getattr(port, 'action_stack', None), 'peek', lambda: None)()).__name__}")
            if getattr(command, "is_ability_activate", False):
                self._refresh_options_after_rejected_ability(
                    session, port, projected_game, player_uid)
        setattr(session, "_rules_port_dispatch_command", None)
        return bool(handled)

    def _publish_application_events(self, events):
        """Publish events after an application command has committed.

        Session removal currently needs only an audit/log notification. Game
        commands will extend this callback to route committed domain events
        to the appropriate client event projection.
        """
        for event in events:
            log_req(f"    Committed application event: {event!r}")

    def _refresh_options_after_rejected_ability(
            self, session, port, projected_game, request_player):
        """Restore the authoritative main-phase options after a stale click.

        Unity enables manual abilities from its cached PlayerOptionList. If a
        player submits an ability after its cost became unaffordable, RulesPort
        rejects the activation; replace the cached list while the same player
        still owns an ordinary main-phase priority window.
        """
        if (projected_game is None or
                (session.session_name or "").startswith("tourney-")):
            return False
        try:
            coerce_player = getattr(
                port, "coerce_transaction_player_id", None)
            intent_player = (coerce_player(request_player)
                             if callable(coerce_player) else request_player)
            main_phase = port.current_turn_phase in (
                game_engine.ETurnPhases.FirstMainPhase,
                game_engine.ETurnPhases.SecondMainPhase)
            action = port.action_stack.peek()
            player_has_priority = (
                _same_uid(intent_player, port.action_stack.priority_player_id)
                and _same_uid(intent_player, port.active_player_id))
            from rules_port.lifecycle import load_state
            battle_state = load_state(session)
            waiting_for_input = (
                bool(getattr(port, "pending_activation", None)) or
                any(battle_state.get(key) for key in (
                    "pending_choice", "pending_trigger", "pending_deck_search",
                    "pending_discard_ability", "pending_conversation",
                    "resolution_paused")))
            if not (
                    main_phase and player_has_priority and
                    type(action).__name__ == "PriorityWindowAction" and
                    getattr(getattr(port, "chain", None), "is_empty", True) and
                    not waiting_for_input):
                return False
            player_uid = getattr(projected_game, "player_uid", None)
            ai_uid = getattr(projected_game, "ai_uid", None)
            if player_uid is None or ai_uid is None:
                return False
            sent = self._push_main_phase_options(
                session, player_uid, ai_uid)
            if sent:
                log_req("    RulesPort rejected stale ability click; "
                        "refreshed main-phase options")
            return bool(sent)
        except Exception as refresh_exc:
            log_req("    RulesPort rejected ability option refresh failed: "
                    f"{refresh_exc}")
            return False

    def _set_client_identity_from_profile(self, profile=None):
        """Restore the stable protocol IDs when auth was skipped on reconnect."""
        profile = profile or self.user_profile
        if not profile:
            return False
        base_id = profile["id"] & 0xFFFFFFFFFFFF
        self.client_auth_id = str(base_id * 10 + 45)
        self.client_reck_id = str(base_id * 10 + 90)
        self.client_uid = make_uid(UID_TYPE["ServicePlayer"],
                                   int(self.client_reck_id))
        return True

    def send(self, headers: dict, body: bytes = b""):
        with self._send_lock:
            h = dict(headers)
            if self.sid:
                h.setdefault("sid", self.sid)
            h.setdefault("scnt", self.scnt)
            h.setdefault("ccnt", self.ccnt)
            pkt = make_packet(h, body)
            if h.get('target') != 'pong':
                log(
                    f"SEND {h.get('target','')}/{h.get('instance','')}: body={len(body)}b",
                    level="DEBUG")
            self.conn.sendall(pkt)
            # Track session syncs on the recipient connection so the 3029
            # dispatcher can distinguish an event-bearing completion from a
            # successful transaction that still needs an empty 3055 ack.
            if h.get("target") == "ServiceGameSession":
                try:
                    data_wrapper = parse_datawrapper(body, preserve_complex=True)
                    if int(data_wrapper.get("DataType", 0) or 0) == 3055:
                        self._game_sync_send_count = int(getattr(
                            self, "_game_sync_send_count", 0)) + 1
                except Exception:
                    pass
            # Record actual 3055 socket delivery only after sendall succeeds.
            # This is opt-in diagnostic capture and must never affect delivery.
            trace = session_transaction_trace_state()
            if trace and h.get("target") == "ServiceGameSession":
                try:
                    data_wrapper = parse_datawrapper(body, preserve_complex=True)
                    if (trace.get("session_id") is not None and
                            int(data_wrapper.get("DataType", 0) or 0) == 3055):
                        player_uid = make_uid(
                            UID_TYPE["ServicePlayer"],
                            int(self.client_reck_id))
                        db_mark_session_packet_sent(
                            trace.get("session_id"), player_uid, body)
                except Exception as capture_error:
                    log_req(f"    Packet delivery capture failed: {capture_error}")

    def send_and_cache(self, headers, body, data_type, reqid, target, instance):
        self.send(headers, body)

    # ------------------------------------------------------------------
    # Battle turn engine (AI opponent)
    # ------------------------------------------------------------------

    def _announce_champion_board_cards(self, game, session, pl_t, state=None):
        """Re-announce the champions' HUD cards once the client is in battle.

        ``ChampionCardPlayed`` owns champion card-view placement: it is the
        only event that moves that view into the player's ``ChampionsView``,
        which is the defender the client's ``BattleStateDeclareAttackers`` and
        ``UIBattle.OnAttackDeclared`` draw the troop-to-champion line to.  That
        event is sent once, inside the game-start burst, while Unity is still
        entering the battle scene; a view created after that (on demand, at the
        card root) is never moved, so the attack line ends in the middle of the
        board.  Every other card is re-pushed by ``_push_warzone_card_updates``
        on each phase push - this gives the champions the same treatment, once,
        after the client's first transaction proves the battle UI is live.

        The champion CardUpdated carries whatever the champion CardDef holds
        (abilities, counters, spell-point cost mods, live health), so the
        CardDefs are rebuilt for this packet first: a champion re-pushed
        without them wipes the client's champion ability buttons, counters and
        spell-power escalation display.
        """
        # Both flags are keyed to this game's session id: a handler (and its
        # session object) outlives the game, and the next game needs its own
        # announcement once its own client is in the battle UI.
        game_id = getattr(session, "session_id", None)
        if getattr(session, "_champion_board_announced", None) == game_id:
            return False
        if getattr(session, "_client_in_battle", None) != game_id:
            return False
        entries = [entry for entry in (
            getattr(self, "_player_champ_board", None),
            getattr(self, "_ai_champ_board", None)) if entry]
        if len(entries) != 2:
            return False
        # ``OnChampionCardPlayed`` moves the champion's GoCardView into the
        # ChampionsView. If the first live packet already contains a trigger
        # sourced by that champion, the client queues the trigger's
        # AddToChain animation before this announcement. Appending the
        # announcement would then move the same card view back out of the
        # ChainView, leaving a Resolve button with no visible top item. Put
        # the one-time board setup first so later chain animations own the
        # final card-view position.
        event_start = len(game.events)
        setattr(session, "_champion_board_announced", game_id)
        player_uid = getattr(game, "player_uid", pl_t)
        ai_uid = getattr(game, "ai_uid", None)
        if not isinstance(state, dict):
            state = getattr(self, "_current_bstate", None) or {}
        # Register the champion CardDefs (abilities + persisted counters and
        # spell-point cost mods) and copy the live health onto this Game.  This
        # is the same rebuild the phase walk uses; it deliberately emits no
        # CardUpdated of its own.
        self._push_champions_warm(session, player_uid, ai_uid, state, game)
        for scid, is_ai, name, champ_guid in entries:
            owner = ai_uid if is_ai else player_uid
            # The CardDef's defense is the champion's live health (which is
            # what the HUD reports through ChampionHealth).
            game.push_card_updated(
                scid, owner, game_engine.ECardCollections.None_,
                game_engine.ECardTypes.Champion, attack=0,
                defense=int(getattr(
                    game, "ai_health" if is_ai else "player_health", 0) or 0),
                template_id=champ_guid)
            game.push_champion_card_played(
                owner, bool(is_ai), name, scid)
        game.push_player_updated(
            player_uid, champ_id=getattr(self, "_player_champ_scid", None))
        if ai_uid is not None:
            game.push_player_updated(
                ai_uid, champ_id=getattr(self, "_ai_champ_scid", None))
        announcement_events = game.events[event_start:]
        del game.events[event_start:]
        game.events[0:0] = announcement_events
        log_req("    Re-announced champion board cards (client in battle)",
                level="DEBUG")
        return True

    def _send_battle_events(self, session, game, pl_t):
        """Send a Game's queued events as one 3055 sync packet."""
        # Serializing this projection is the drain point.  A projection the
        # port has already moved off can still hold publications no packet
        # serialized (Practice builds a fresh Game per packet, and a native
        # phase entry - an AI attack declaration, for example - publishes
        # before the host builds the next one).
        sink = getattr(getattr(session, "_rules_port_session", None),
                       "event_sink", None)
        if sink is not None:
            sink.drain_into(game)
        from rules_port.visibility import (project_visible_hands,
                                           project_visible_top_decks)
        current_state = getattr(self, "_current_bstate", {}) or {}
        project_visible_hands(
            _db, session, self, game, pl_t,
            getattr(game, "ai_uid", game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)),
            current_state)
        project_visible_top_decks(
            _db, session, self, game, pl_t,
            getattr(game, "ai_uid", game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)),
            current_state)
        # Placing the champions on the client's board is a one-shot projection
        # at game start; re-announce it in the first packet the client receives
        # from inside the battle scene, exactly like the warzone CardUpdated
        # re-push, so the attack line has a placed target.
        self._announce_champion_board_cards(game, session, pl_t, current_state)
        if not game.events:
            return False
        # RulesPort transactions are normalized with the decoded numeric UID,
        # whereas the ObjFmt encoder requires the typed UID object.  Keep the
        # ingress representation out of the packet boundary.
        if not isinstance(pl_t, game_engine.UID):
            pl_t = game_engine.UID(int(getattr(pl_t, "uid64", pl_t)))
        if not getattr(game, "_visibility_by_uid", None):
            from rules_port.visibility import \
                apply_player_visibility_to_game
            apply_player_visibility_to_game(game, current_state)
        # Unity places warzone cards under CardUpdated/ CardMoved.Controller
        # (the event player id), not from an intrinsic owner field on the
        # card.  A stale projection can therefore make an opponent constant
        # appear on the local side when the layout is rebuilt.  Reconcile
        # Reconcile public permanent-zone events against the authoritative
        # game_cards owner. Underground is included because tunneling emits
        # its CardMoved/CardUpdated pair there; leaving it out lets a stale
        # event player UID make an opponent's tunneled card render locally
        # until the next full board refresh. Hand/deck events retain their
        # visibility projection semantics.
        try:
            from abilities.framework._shared import owner_uid
            for event in game.events:
                collection = getattr(event, "collection", None)
                if collection not in (
                        game_engine.ECardCollections.Warzone,
                        game_engine.ECardCollections.Underground):
                    continue
                sid = getattr(event, "session_card_id", None)
                raw_sid = getattr(getattr(sid, "uid", sid), "uid64", sid)
                if raw_sid is None:
                    continue
                owner_id = db_card_owner_id(
                    session.session_id, int(raw_sid), conn=_db)
                if owner_id is None:
                    continue
                projected_owner = owner_uid(owner_id, game.player_uid,
                                             getattr(game, "ai_uid", None),
                                             current_state)
                if hasattr(event, "player_id"):
                    event.player_id = projected_owner
                if hasattr(event, "controller"):
                    event.controller = projected_owner
        except Exception as exc:
            log_req(f"    Card owner projection skipped: {exc}")
        pkt = game.make_network_packet(pl_t)
        dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                                "00000000-0000-0000-0000-000000000000")
        self._game_scnt = max(self._game_scnt, self.scnt) + 1
        self.scnt = self._game_scnt
        gs_inst = str(session.server_id)
        self.send({
            "issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
            "target": "ServiceGameSession", "instance": gs_inst,
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        self._event_q.append((self.scnt, dw, {}))
        return True

    def _extract_enum_int(self, inner_bytes, field):
        """Extract the integer value of an enum field from 3029 inner_bytes.

        Enums serialize as `field;...;value__;<idx>;<size>;<type>;0;<hex>;`.
        """
        idx = inner_bytes.find(field.encode())
        if idx < 0:
            return None
        v_idx = inner_bytes.find(b"value__", idx)
        if v_idx < 0:
            return None
        rest = inner_bytes[v_idx + len(b"value__") + 1:]
        parts = rest.split(b";", 6)
        if len(parts) >= 4:
            try:
                return struct.unpack('<I', bytes.fromhex(parts[3].decode("ascii", errors="replace")))[0]
            except Exception:
                pass
        return None

    def _extract_int32_field(self, inner_bytes, field):
        """Extract a plain System.Int32 field from 3029 inner_bytes.

        Int32s serialize as `field;<idx>;<type>;0;<8-hex-le>;` (see
        objfmt_builder.field_int).  Used for the client's
        AbilityActivationData.xCostData.m_ResourceXCost — the X the player
        chose in the X-cost dialog when playing a variable-cost spell.
        """
        idx = inner_bytes.find(field.encode())
        if idx < 0:
            return None
        rest = inner_bytes[idx + len(field) + 1:]
        parts = rest.split(b";", 4)
        if len(parts) >= 4:
            try:
                return struct.unpack('<i', bytes.fromhex(parts[3].decode("ascii", errors="replace")))[0]
            except Exception:
                pass
        return None

    def _extract_enum_list(self, inner_bytes, field, next_field):
        """Best-effort parse of a List<ETurnPhases> field from 3029 inner_bytes."""
        import re as _re
        idx = inner_bytes.find(field.encode())
        if idx < 0:
            return None
        end = inner_bytes.find(next_field.encode(), idx) if next_field else len(inner_bytes)
        seg = inner_bytes[idx:end]
        vals = []
        for m in _re.finditer(rb"value__;\d+;\d+;\d+;([0-9A-Fa-f]{8});", seg):
            try:
                vals.append(struct.unpack('<I', bytes.fromhex(m.group(1).decode("ascii")))[0])
            except Exception:
                pass
        return vals

    def _card_troop_requirements(self, ability_guids):
        """Set of troop-target requirements for a card's abilities, driven by
        the gamedata target-template FILTER (not card text): {'friendly',
        'enemy','any'}.  Champion (PlayerTargetTemplate / IsHero filter)
        targets always exist and never gate playability."""
        from pvp_db import db_ability_meta_targets
        reqs = set()
        for ag in (ability_guids or []):
            meta = db_ability_meta_targets(ag)
            if meta and meta[4]:  # is_manual — warzone activation, skip
                continue
            if not meta or not meta[0]:
                continue
            try:
                tpl_ids = json.loads(meta[0])
            except Exception:
                continue
            for tid in tpl_ids:
                trow = db_target_template_definition(tid, conn=_db)
                if not trow:
                    continue
                kind = trow[1] or ""
                fj = trow[0] or "{}"
                if kind == "PlayerTargetTemplate":
                    continue  # auto champion target — always available
                if "IsHero" in fj:
                    continue  # champion targets always exist in play
                if "IsTroop" not in fj:
                    continue
                # A target such as Prophecy's "the next troop in your deck"
                # is an automatic deck search, not a requirement for a troop
                # already on the battlefield.  Only a Warzone-only target
                # should make a card unplayable when the board is empty.
                zones = {z.strip().lower() for z in (trow[2] or "").split("|")
                         if z.strip()}
                if zones != {"warzone"}:
                    continue
                if "IsControlledBy" in fj and "IsNotControlledBy" not in fj:
                    reqs.add("friendly")
                elif "IsNotControlledBy" in fj:
                    reqs.add("enemy")
                else:
                    reqs.add("any")
        return reqs

    def _card_target_requirements_met(self, session, play_plan,
                                      battle_state=None):
        """A hand card is only playable when every EXPLICIT target template of
        its non-manual abilities has at least one legal candidate.  This is
        what makes Countermagic ("Interrupt target card" — CollectionFlags
        CastSpells) unplayable with nothing on the chain: its only legal
        targets are cards in CastSpells, so with an empty chain the card is
        not offered.  Auto targets and player-champion targets never gate.
        """
        # Choice cards such as Engulf have a wrapper followed by alternative
        # child abilities.  Only one branch must have a legal target; the
        # client does not require unavailable alternatives to be playable.
        choice_card = any(
            int(getattr(ability.graph, "ability_index", -1)) < 0 and
            str(getattr(ability.graph, "game_text", "")).strip().lower()
            in {"choose one:", "choose one"}
            for ability in play_plan.abilities if ability.graph is not None)
        requirements = []
        for ability in play_plan.abilities:
            if (ability.graph is None or ability.graph.manual or
                    ability.is_triggered):
                continue
            has_explicit_target = False
            targets_available = True
            from rules_port.targeting import legal_targets_for
            graph = ability.graph
            for index in ability.referenced_target_indexes:
                if index < 0 or index >= len(graph.targets):
                    continue
                target = graph.targets[index]
                if not target.requires_input or target.minimum < 1:
                    continue
                has_explicit_target = True
                candidates = legal_targets_for(
                    _db, session.session_id, self.user_profile["id"], target,
                    # Playability is asking whether the card can be cast from
                    # hand.  The source is not yet in a gameplay collection;
                    # preserve the pre-port target query's source-less
                    # semantics while still using RulesPort filtering.
                    0, both_players=True,
                    champions=self._champion_targets(),
                    battle_state=battle_state)
                if not candidates:
                    targets_available = False
            if has_explicit_target:
                requirements.append(targets_available)
        if requirements and ((choice_card and not any(requirements)) or
                             (not choice_card and not all(requirements))):
            log_req("    Playability: no legal target for required card ability")
            return False
        return True

    def _warzone_troop_count(self, session, user_id):
        return db_warzone_troop_count(session.session_id, user_id)

    def _card_play_plan(self, template_guid, card_uid=0, owner_id=None,
                        session=None):
        """Return the Records-backed plan for this live card instance.

        The current Records snapshot is the only supported card-data source.
        The instance ability list is included because permanent grants can add
        abilities to cards in hand (for example, Graven Geist grants Scrounge).
        A missing record is invalid card data, not a request to use the old
        database-materialized rules path.
        """
        if not template_guid:
            return None
        try:
            source_uid = int(getattr(card_uid, "uid64", card_uid) or 0)
        except (TypeError, ValueError):
            source_uid = 0
        profile = self.user_profile if isinstance(self.user_profile, dict) else {}
        resolved_owner_id = int(
            owner_id if owner_id is not None else profile.get("id", 0) or 0)
        instance_ability_guids = tuple(
            self._card_ability_list(session, source_uid)
            if session is not None and source_uid else ())
        store = getattr(self, "_play_plan_store", None)
        if store is None:
            from gamedata import DEFAULT_RECORD_STORE
            store = DEFAULT_RECORD_STORE
            self._play_plan_store = store
        cache = getattr(self, "_play_plan_cache", None)
        if cache is None:
            cache = {}
            self._play_plan_cache = cache
        key = (str(template_guid).lower(), source_uid, resolved_owner_id,
               instance_ability_guids)
        if key not in cache:
            from gamedata import PlayPlan
            try:
                cache[key] = PlayPlan.from_card(
                    store, key[0], source_uid=source_uid or None,
                    owner_id=resolved_owner_id,
                    instance_ability_guids=instance_ability_guids)
            except KeyError as exc:
                raise RuntimeError(
                    f"card template {key[0]} is missing from the current Records "
                    "snapshot") from exc
        return cache[key]

    def _plan_target_bounds(self, template_guid):
        """Read min/max directly from the current target record."""
        store = getattr(self, "_play_plan_store", None)
        if store is None:
            from gamedata import DEFAULT_RECORD_STORE
            store = DEFAULT_RECORD_STORE
            self._play_plan_store = store
        target = store.get("AbilityTargetTemplate", template_guid)
        if target is None:
            return 1, 1
        minimum = max(0, int(target.minimum))
        maximum = int(target.maximum)
        if maximum <= 0:
            maximum = max(1, minimum)
        return minimum, maximum

    def _valid_targets_for_template(self, session, pl_t, ai_t, tid,
                                    battle_state=None):
        """Compute the valid SessionCardId targets for an AbilityTargetTemplate,
        driven by the gamedata target template (kind + card filter): champions
        come from the filter's IsHero, troops from the filter, and
        PlayerTargetTemplate ('You') resolves to the controller's champion."""
        from rules_port.targeting import legal_targets
        store = getattr(self, "_play_plan_store", None)
        if store is None:
            from gamedata import DEFAULT_RECORD_STORE
            store = DEFAULT_RECORD_STORE
            self._play_plan_store = store
        target = store.get("AbilityTargetTemplate", str(tid).lower())
        if target is None:
            return None
        spec = target.target_spec
        kind = spec.target_kind
        if spec.is_auto:
            # Auto targets (e.g. a shard's 'You') resolve automatically server-
            # side — never attach a hand-play target picker for them.
            return None
        if kind == "PlayerTargetTemplate":
            champ = getattr(self, "_player_champ_scid", None)
            return [champ] if champ else []
        # RecordObject filters can implement a zero-length protocol object and
        # therefore evaluate false even when a real filter is present.  Test
        # for None explicitly; otherwise every metadata-targeted troop spell
        # is incorrectly treated as having no legal target.
        if spec.card_filter is None:
            return None
        if battle_state is None:
            battle_state = getattr(self, "_current_bstate", None)
        candidates = legal_targets(
            _db, session.session_id, self.user_profile["id"], tid, 0,
            both_players=True, champions=self._champion_targets(),
            battle_state=battle_state)
        return [game_engine.SessionCardId(game_engine.UID(int(u)))
                for u in candidates]

    def _play_ability_targets(self, session, play_plan, battle_state=None):
        """For a hand card's abilities return [(ability_guid, index, template_id,
        [SessionCardId])] for each targeting template with computable targets."""
        result = []
        for ability in play_plan.abilities:
            graph = ability.graph
            if graph is None or graph.manual or ability.is_triggered:
                continue
            for index in ability.referenced_target_indexes:
                if index >= len(graph.targets):
                    continue
                target = graph.targets[index]
                if not target.requires_input:
                    continue
                targets = self._valid_targets_for_template(
                    session, None, None, target.guid,
                    battle_state=battle_state)
                if targets is not None and targets:
                    result.append((ability.ability_guid, index, target.guid,
                                   targets))
        return result

    @staticmethod
    def _play_target_bounds(play_plan, ability_guid, index):
        """Return the authored (minimum, maximum) for one target template.

        The client reads min/max by the position of the template in the
        ability's TargetTemplateIds list, so each TargetInstance added to one
        OptionInstance must carry its authored bounds.
        """
        minimum, maximum = 1, 1
        for ability in play_plan.abilities:
            if ability.ability_guid != ability_guid or index >= len(ability.targets):
                continue
            target = ability.targets[index]
            minimum = max(0, target.minimum)
            maximum = max(minimum, target.maximum or minimum or 1)
            break
        return minimum, maximum

    def _add_play_target_options(self, game, session, pl_t, ai_t):
        """Attach targeting TargetInstances to the most recent PlayerOptionList.

        The client's play-card flow (BattleStatePlayCard.NextStep) only opens the
        target picker for a played card's ability when
        State.CanUseAbility(cardId, abilityId) is true, i.e. the ability has an
        OptionInstance + TargetInstance in PlayerOptions.m_Targets. Without this,
        a targeted spell (e.g. Bravery "+1/+1 target troop") is played with no
        target and no effect."""
        if not game.events:
            return
        last_ev = game.events[-1]
        if not isinstance(last_ev, game_engine.PlayerOptionListSessionEventArgs):
            return
        for opt in last_ev.options:
            card_uid = opt.card.uid.uid64
            card_details = db_card_zone_details(
                session.session_id, card_uid, conn=_db)
            if not card_details:
                continue
            play_plan = self._card_play_plan(card_details[0], card_uid,
                                             self.user_profile["id"]
                                             if self.user_profile else 0,
                                             session=session)
            if play_plan is None:
                continue
            # PlayerOptions.Update keys target instances by (card, ability) and
            # CLEARS the stored list each time it sees another OptionInstance
            # with the same opt_id (PlayerOptions.cs:117-129).  Emitting one
            # OptionInstance per target template therefore drops every target
            # but the last: Survival of the Fittest ("Target troop you control
            # battles target opposing troop") prompted only for the opposing
            # troop, so the faction TargetMap was missing index 0 and the
            # pending battle never resolved.  Group every template of one
            # ability into a single OptionInstance, exactly like the manual
            # activation path above and the client's authored
            # AbilityTargetTemplateIds order.
            grouped: dict[str, dict[str, list]] = {}
            for ag, idx, tid, targets in self._play_ability_targets(
                    session, play_plan,
                    battle_state=getattr(self, "_current_bstate", None)):
                grouped.setdefault(ag, {"targets": [], "costs": []})[
                    "targets"].append((idx, tid, targets))
            from rules_port.costs import (
                ability_cost_targets, card_cost_targets, cost_type_for_kind)
            owner_id = self.user_profile["id"] if self.user_profile else 0
            for ability in play_plan.cast_abilities:
                graph = ability.graph
                if graph is None or not graph.additional_cost_targets:
                    continue
                costs = ability_cost_targets(
                    graph, _db, session.session_id, owner_id, card_uid,
                    champions=self._champion_targets(),
                    battle_state=getattr(self, "_current_bstate", None))
                # The client only opens an optional/additional-cost picker
                # when every required part of that ability payment is
                # currently affordable.
                if any(not cost.is_source_auto_target and
                       len(cost.candidates) < cost.minimum
                       for cost in costs):
                    continue
                if costs:
                    grouped.setdefault(
                        ability.ability_guid.lower(),
                        {"targets": [], "costs": []})["costs"].extend(costs)
            for ag, group in grouped.items():
                # Troop abilities are NOT playable from hand.  Only the
                # card itself may be played (cost/threshold gate).
                # Manual abilities activate from the warzone, not hand.
                inst = game._make_event(game_engine.OptionInstanceSessionEventArgs)
                inst.opt_id = game_engine.ResourceId.from_str(ag)
                entries = group["targets"]
                for idx, tid, targets in entries:
                    min_count, max_count = self._play_target_bounds(
                        play_plan, ag, idx)
                    inst.min_target_counts.append(min_count)
                    inst.max_target_counts.append(max_count)
                    inst.target_ids.append(game_engine.ResourceId.from_str(tid))
                    tgt = game._make_event(game_engine.TargetInstanceSessionEventArgs)
                    tgt.target_index = idx
                    tgt.target_id = game_engine.ResourceId.from_str(tid)
                    tgt.targets = list(targets)
                    inst.target_instances.append(tgt)
                for cost in group["costs"]:
                    if cost.is_source_auto_target or not cost.candidates:
                        continue
                    minimum = cost.minimum
                    maximum = (len(cost.candidates) if cost.maximum < 0
                               else cost.maximum)
                    ci = game._make_event(
                        game_engine.CostInstanceSessionEventArgs)
                    HCPHandler._set_cost_instance_bounds(
                        ci, minimum, maximum)
                    ci.cost_type = cost_type_for_kind(cost.kind)
                    ci.target_template_id = game_engine.ResourceId.from_str(
                        cost.guid)
                    ci.targets = [game_engine.SessionCardId(
                        game_engine.UID(int(uid))) for uid in cost.candidates]
                    inst.target_instances.append(ci)
                opt.instances.append(inst)
                # Also attach the picker to the PlayCard option itself so the
                # client's CanUseAbility finds the target whether it checks the
                # card's ability or the built-in PlayCard ability (the play-card
                # flow keys on BuiltInResources.PlayCardAbilityTemplateId).
                for inst2 in opt.instances:
                    if str(inst2.opt_id.guid) != game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID:
                        continue
                    for idx, tid, targets in entries:
                        min_count, max_count = self._play_target_bounds(
                            play_plan, ag, idx)
                        inst2.target_ids.append(
                            game_engine.ResourceId.from_str(tid))
                        inst2.min_target_counts.append(min_count)
                        inst2.max_target_counts.append(max_count)
                        tgt2 = game._make_event(
                            game_engine.TargetInstanceSessionEventArgs)
                        tgt2.target_index = idx
                        tgt2.target_id = game_engine.ResourceId.from_str(tid)
                        tgt2.targets = list(targets)
                        inst2.target_instances.append(tgt2)
                    break
            # Additional-cost sacrifice (e.g. Abominate "sacrifice a troop you
            # control"): attach a SacrificeAbilityCostType CostInstance to the
            # PlayCard instance so the client's BattleStateAssignXCost prompts for
            # the sacrificed troop BEFORE the effect target. Without it the client
            # skips the cost and only asks for the single effect target.
            cost_candidates = card_cost_targets(
                play_plan, _db, session.session_id,
                self.user_profile["id"] if self.user_profile else 0,
                card_uid, champions=self._champion_targets(),
                battle_state=getattr(self, "_current_bstate", None))
            for cost in cost_candidates:
                if cost.is_source_auto_target:
                    continue
                cost_guid = cost.guid
                cost_uids = cost.candidates
                if not cost_uids:
                    continue
                minimum = cost.minimum
                maximum = cost.maximum
                if maximum < 0:
                    maximum = len(cost_uids)
                # Find the PlayCard instance — its opt_id MUST match the
                # client's built-in PlayCard ability, or it cannot see this
                # CostInstance.
                for inst in opt.instances:
                    if (str(inst.opt_id.guid) ==
                            game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID):
                        ci = game._make_event(
                            game_engine.CostInstanceSessionEventArgs)
                        HCPHandler._set_cost_instance_bounds(
                            ci, minimum, maximum)
                        ci.cost_type = cost_type_for_kind(cost.kind)
                        ci.target_template_id = game_engine.ResourceId.from_str(
                            cost_guid)
                        ci.targets = [game_engine.SessionCardId(
                            game_engine.UID(int(uid))) for uid in cost_uids]
                        inst.target_instances.append(ci)
                        break
            # Variable X cost (e.g. Burn to the Ground "Deal X damage"): attach
            # an XCostAbilityCostType CostInstance to the PlayCard instance so
            # the client's BattleStateAssignXCost pushes the X slider
            # (BattleStateResourceXCost).  Without this the cost list is empty
            # and the dialog auto-commits at X=0 without ever showing.
            has_x_cost = play_plan.cost.variable
            if has_x_cost:
                for inst in opt.instances:
                    if str(inst.opt_id.guid) == game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID:
                        ci = game._make_event(game_engine.CostInstanceSessionEventArgs)
                        minimum = play_plan.cost.variable_minimum
                        HCPHandler._set_cost_instance_bounds(ci, minimum, 0)
                        ci.cost_type = 256  # EAbilityCostType.XCostAbilityCostType
                        ci.target_template_id = game_engine.ResourceId.invalid()
                        ci.targets = []
                        inst.target_instances.append(ci)
                        break

    def _cost_targets_available(self, session, tid, count):
        """True if the player controls at least `count` cards that can pay a
        cost targeting template `tid` (e.g. Abominate's "a troop you control").
        Artifacts/other kinds are resolved from the template's game text."""
        from pvp_db import db_target_template_text, db_warzone_troop_count
        text = db_target_template_text(tid)
        if "artifact" in text.lower():
            n = db_zone_card_count(
                session.session_id, self.user_profile["id"], "warzone",
                "Artifact", conn=_db)
        else:
            n = db_warzone_troop_count(session.session_id, self.user_profile["id"])
        return n >= count

    def _card_play_cost_selections(self, session, play_plan, source_uid,
                                   selected_uids, client_cost_map=None):
        """Bind a card-play TargetMap to its authored CostInstances.

        ``client_cost_map`` carries the decoder's already-separated cost
        selections (XCostData.CardsToSacrifice -> ability_data
        cost_target_map).  Client picks are validated and used as-is; the
        TargetMap positional/best-effort fallback only applies to costs the
        client did not name.
        """
        from rules_port.costs import (
            card_cost_targets, cost_type_for_kind,
            validate_cost_target_selection,
        )
        selected_uids = [int(uid) for uid in (selected_uids or [])]
        named_costs = {
            int(index): [int(uid) for uid in selected]
            for index, selected in (client_cost_map or {}).items()
            if selected
        }
        used = set()
        selections = []
        if not play_plan.cost_instances:
            return selections, used
        def trace(reason, cost, available):
            # Rejection-only trace: selected names the client's TargetMap,
            # candidates the legal cost pool, so a refused play can be
            # replayed without the raw ObjFmt envelope.
            log_req(f"    Card cost selection reject: "
                    f"card={card_label(session.session_id, source_uid)} "
                    f"kind={cost.kind} guid={cost.guid} "
                    f"reason={reason} "
                    f"selected={[card_label(session.session_id, uid) for uid in selected_uids]} "
                    f"available={[card_label(session.session_id, uid) for uid in available]} "
                    f"candidates({len(cost.candidates)})="
                    f"{[card_label(session.session_id, uid) for uid in list(cost.candidates)[:8]]} "
                    f"minimum={cost.minimum} maximum={cost.maximum} "
                    f"source_auto={cost.is_source_auto_target} "
                    f"best_effort={cost.allow_best_effort_minimum}")

        for cost in card_cost_targets(
                play_plan, _db, session.session_id,
                self.user_profile["id"] if self.user_profile else 0,
                source_uid, champions=self._champion_targets(),
                battle_state=getattr(self, "_current_bstate", None)):
            spec = {"kind": cost.kind, "target_guid": cost.guid,
                    "index": int(cost.index),
                    "cost_type": cost_type_for_kind(cost.kind),
                    "minimum": cost.minimum, "maximum": cost.maximum,
                    "auto": cost.is_source_auto_target,
                    "allow_best_effort_minimum":
                        cost.allow_best_effort_minimum}
            candidates = cost.candidates
            if cost.is_source_auto_target:
                selections.append((spec, candidates))
                continue
            if not candidates:
                trace("no-candidates", cost, ())
                return None
            named = named_costs.get(int(cost.index))
            if named:
                # The client named this cost's payment (XCostData
                # CardsToSacrifice).  Never steal an effect target to pay it.
                chosen = validate_cost_target_selection(
                    _db, session.session_id,
                    self.user_profile["id"] if self.user_profile else 0,
                    source_uid, cost, tuple(named),
                    champions=self._champion_targets(),
                    battle_state=getattr(self, "_current_bstate", None))
                if chosen is None:
                    trace("client-selection-invalid", cost, tuple(named))
                    return None
                used.update(chosen)
                selections.append((spec, chosen))
                continue
            candidate_set = {int(uid) for uid in candidates}
            available = [uid for uid in selected_uids
                         if uid in candidate_set and uid not in used]
            minimum = cost.minimum
            maximum = cost.maximum
            if maximum < 0:
                maximum = len(available)
            if len(available) < minimum:
                # Older Mono clients sometimes omit the CostInstance's
                # TargetMap entry for a sacrifice while still submitting the
                # effect target.  Authored optional/best-effort costs may be
                # completed server-side from the same legal candidate set;
                # never consume the effect target when selecting that fallback.
                if (int(spec.get("cost_type", 0)) == 2 and
                        spec.get("allow_best_effort_minimum", False)):
                    available = [uid for uid in candidates
                                 if uid not in used and uid not in selected_uids]
                if len(available) < minimum:
                    trace("selected<minimum", cost, available)
                    return None
            chosen = tuple(available[:maximum])
            chosen = validate_cost_target_selection(
                _db, session.session_id,
                self.user_profile["id"] if self.user_profile else 0,
                source_uid, cost, chosen,
                champions=self._champion_targets(),
                battle_state=getattr(self, "_current_bstate", None))
            if chosen is None:
                trace(f"validate-rejected({[hex(int(uid)) for uid in available[:maximum]]})",
                      cost, available)
                return None
            used.update(chosen)
            selections.append((spec, chosen))
        return selections, used

    def _ability_card_play_cost_selections(
            self, session, play_plan, source_uid, activations, *,
            owner_id=None, champions=None, battle_state=None):
        """Validate additional costs authored on a cast ability graph."""
        from rules_port.costs import (
            ability_cost_targets, cost_type_for_kind,
            validate_cost_target_selection,
        )
        owner_id = (int(owner_id) if owner_id is not None else
                    int(self.user_profile["id"])
                    if self.user_profile else 0)
        champions = (self._champion_targets() if champions is None
                     else champions)
        battle_state = (getattr(self, "_current_bstate", None)
                        if battle_state is None else battle_state)
        activation_map = {str(key).lower(): value
                          for key, value in (activations or {}).items()}
        selections = []
        used = set()
        for ability in play_plan.cast_abilities:
            graph = ability.graph
            if graph is None or not graph.additional_cost_targets:
                continue
            activation = activation_map.get(ability.ability_guid.lower())
            if activation is None:
                activation = ability.activation
            cost_map = getattr(activation, "cost_target_map", {}) or {}
            if (ability.is_scrounge and not getattr(activation, "opted", False)
                    and not cost_map):
                # Declining optional Scrounge is a valid card play and has no
                # payment rows to apply.
                continue
            for cost in ability_cost_targets(
                    graph, _db, session.session_id, owner_id, source_uid,
                    champions=champions, battle_state=battle_state):
                selected = cost_map.get(cost.index,
                                        cost_map.get(str(cost.index), ()))
                if cost.is_source_auto_target and not selected:
                    selected = (() if source_uid is None else (int(source_uid),))
                chosen = validate_cost_target_selection(
                    _db, session.session_id, owner_id, source_uid, cost,
                    selected, champions=champions,
                    battle_state=battle_state)
                if chosen is None:
                    return None
                spec = {
                    "kind": cost.kind,
                    "target_guid": cost.guid,
                    "index": int(cost.index),
                    "cost_type": cost_type_for_kind(cost.kind),
                    "minimum": cost.minimum,
                    "maximum": cost.maximum,
                    "auto": cost.is_source_auto_target,
                    "allow_best_effort_minimum":
                        cost.allow_best_effort_minimum,
                    "ability_guid": ability.ability_guid.lower(),
                }
                selections.append((spec, chosen))
                used.update(chosen)
        return selections, used

    def _card_costs_valid(self, session, selections, expected_owner_id=None):
        """Validate card costs before any resource/effect mutation."""
        for spec, selected in selections or ():
            if spec.get("kind") != "sacrifice":
                continue
            for uid in selected:
                row = db_card_sacrifice_info(
                    session.session_id, int(uid), conn=_db)
                if not row:
                    return False
                owner_id, location, card_type = row
                if (str(location or "").lower() != "warzone" or
                        "troop" not in str(card_type or "").lower()):
                    return False
                if (expected_owner_id is not None and
                        int(owner_id or 0) != int(expected_owner_id)):
                    return False
        return True

    def _apply_card_play_costs(self, game, session, bstate, pl_t, ai_t,
                               selections, source_uid=None, scrounge=False,
                               expected_owner_id=None,
                               defer_voided_cards=False):
        """Apply card-level CostInstances before a card enters the chain."""
        from pvp_db import db_discard_card, db_randomly_insert_deck_cards

        if defer_voided_cards:
            # Card-play void costs are carried by the pending chain item.
            # Clear a leftover flat list from older resolutions before any
            # event trigger can observe it.
            lists = bstate.get("ability_lists")
            if isinstance(lists, dict):
                lists.pop("VoidedCards", None)
                if not lists:
                    bstate.pop("ability_lists", None)

        def emit_trigger(event_type, source_uid, owner_id=None, **data):
            from rules_port.triggers import dispatch_native_trigger
            return dispatch_native_trigger(
                db=_db, handler=self, game=game, session=session,
                player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                event_type=event_type, source_card_id=source_uid,
                source_player_id=owner_id,
                target_card_id=data.pop("target_card_id", None),
                data=data)

        def push_zone(uid, owner_id, location):
            row = db_card_zone_projection(
                session.session_id, int(uid), conn=_db)
            if not row:
                return
            scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
            self._card_full_data(game, scid, row[0])
            from abilities.framework._shared import owner_uid
            owner = owner_uid(owner_id, pl_t, ai_t, bstate)
            collection = {
                "hand": game_engine.ECardCollections.Hand,
                "deck": game_engine.ECardCollections.Deck,
                "discard": game_engine.ECardCollections.Discard,
                "void": game_engine.ECardCollections.Void,
                "warzone": game_engine.ECardCollections.Warzone,
            }.get(location, game_engine.ECardCollections.Warzone)
            cdef = game.card_defs.get(scid)
            game.push_card_updated(
                scid, owner, collection, game_engine.card_type_from_db(row[1]),
                template_id=row[0], state=int(row[2] or 0),
                cost=cdef.cost if cdef else 0,
                attack=cdef.attack if cdef else 0,
                defense=cdef.defense if cdef else 0,
                nulling=location == "deck")

        voided_uids = []
        cost_list_names = {
            "sacrifice": "SacrificedCards",
            "discard": "DiscardedCards",
            "void": "VoidedCards",
            "put_into_deck": "CardsPutIntoDeck",
            "shuffle_into_deck": "CardsPutIntoDeck",
            "put_into_hand": "CardsPutIntoHand",
        }

        def record_cost_card(kind, uid):
            list_name = cost_list_names.get(str(kind).lower())
            if not list_name:
                return
            # Card-play costs resolve later from a chain descriptor. Keep
            # these voided-card results with that descriptor instead of in
            # the session-wide list, where another response or later Scrounge
            # could accidentally reuse them.
            if defer_voided_cards and list_name == "VoidedCards":
                return
            values = bstate.setdefault("ability_lists", {}).setdefault(
                list_name, [])
            values.append(int(uid))

        source_owner_id = expected_owner_id
        if source_owner_id is None and source_uid is not None:
            source_owner = db_card_owner_id(
                session.session_id, int(source_uid), conn=_db)
            if source_owner is not None:
                source_owner_id = int(source_owner)

        if not self._card_costs_valid(session, selections, source_owner_id):
            log_req(f"    Card cost rejected before mutation: owner={source_owner_id}")
            return False

        for spec, selected in selections:
            kind = spec["kind"]
            for uid in selected:
                uid = int(uid)
                row = db_card_owner_zone_state(
                    session.session_id, uid, conn=_db)
                if not row:
                    continue
                owner_id = int(row[0] or 0)
                if kind == "sacrifice":
                    if not self._sacrifice_troop(
                        game, session, pl_t, ai_t, uid,
                        expected_owner_id=source_owner_id):
                        return False
                    record_cost_card(kind, uid)
                    continue
                if kind == "exhaust":
                    db_update_card_state(
                        session.session_id, uid,
                        set_bits=game_engine.ECardStates.Tapped, conn=_db)
                    _db.commit()
                    push_zone(uid, owner_id, row[1])
                    continue
                destination = {
                    "discard": "discard",
                    "void": "void",
                    "put_into_deck": "deck",
                    "shuffle_into_deck": "deck",
                    "put_into_hand": "hand",
                }.get(kind)
                if destination is None:
                    if kind == "reveal":
                        event = game._make_event(
                            game_engine.CardsRevealedSessionEventArgs)
                        event.player_id = (pl_t if owner_id else ai_t)
                        event.session_card_ids = [
                            game_engine.SessionCardId(game_engine.UID(uid))]
                        event.collections = [game_engine.ECardCollections.Warzone]
                        event.owning_players = [event.player_id]
                        event.positions = [0]
                        game._push(event)
                    continue
                if destination == "discard":
                    db_discard_card(session.session_id, uid, connection=_db)
                elif destination == "hand":
                    db_move_card_to_location(
                        session.session_id, uid, "hand", position=100, conn=_db)
                    _db.commit()
                else:
                    db_move_card_to_location(
                        session.session_id, uid, destination,
                        reset_state=True, conn=_db)
                    _db.commit()
                    if destination == "void":
                        voided_uids.append(uid)
                    if destination == "deck":
                        db_randomly_insert_deck_cards(
                            session.session_id, owner_id, [uid], connection=_db)
                push_zone(uid, owner_id, destination)
                record_cost_card(kind, uid)
                if destination in ("discard", "void"):
                    emit_trigger(
                        "CardEnteredZoneEvent", uid, owner_id,
                        event_source_collection=row[1],
                        event_destination_collection=destination,
                        event_previous_state=int(row[2] or 0))
                if destination == "discard":
                    emit_trigger(
                        "CardDiscardedEvent", uid, owner_id,
                        event_source_collection=row[1],
                        event_destination_collection="discard")

        # Scrounge fires after the void costs have been paid. The client also
        # exposes those exact instances through the active ability's
        # VoidedCards list, so downstream metadata effects can use them. The
        # queued chain item owns that list for card plays.
        if scrounge and voided_uids and source_uid is not None:
            emit_trigger(
                "CardScroungedEvent", int(source_uid), source_owner_id,
                target_card_id=int(source_uid),
                event_tac={"voided_cards": list(voided_uids)})
        return tuple(int(uid) for uid in voided_uids)

    @staticmethod
    def _set_cost_instance_bounds(cost_instance, minimum, maximum):
        """Populate class-66 bounds for both event-schema revisions."""
        if hasattr(cost_instance, "min_target_count"):
            cost_instance.min_target_count = int(minimum)
            cost_instance.max_target_count = int(maximum)
        else:
            cost_instance.min = int(minimum)
            cost_instance.max = int(maximum)

    @staticmethod
    def _ability_x_cost_metadata(ability_guid):
        """Read variable activation cost fields from the current graph."""
        from gamedata import DEFAULT_RECORD_STORE, ability_graph
        store = getattr(HCPHandler._ability_x_cost_metadata,
                        "_record_store", None)
        if store is None:
            store = DEFAULT_RECORD_STORE
            setattr(HCPHandler._ability_x_cost_metadata, "_record_store", store)
        graph = ability_graph(store, str(ability_guid).lower())
        if graph is None:
            return 0, 0
        return (int(graph.costs.variable_activation or 0),
                int(graph.costs.variable_minimum or 0))

    @staticmethod
    def _template_has_x_cost(tpl_guid):
        """True if a card template has a variable X cost — data-driven from the
        gamedata card field m_VariableCost (card_templates.variable_cost), e.g.
        Burn to the Ground "1X" = 1 base + X.  These cards must show the
        client's X-cost dialog and the chosen X is paid as extra resources.
        """
        from gamedata import DEFAULT_RECORD_STORE
        card = DEFAULT_RECORD_STORE.get("CardTemplate", str(tpl_guid).lower())
        return bool(card and card.has_variable_cost)

    def _resolve_gem_abilities(self, active_gems):
        """Bake a deck's socketed gems into per-instance ability lists at DECK
        SAVE time: {instance_id: [ability guids]} from gem_templates, e.g.
        Shamed Gladiator's Minor Blood Orb of Hatred (gem 5) -> "Rage 1 in all
        zones".  The game later copies these onto game_cards.card_abilities so
        the card is updated with the gem's ability."""
        out = {}
        for inst, gem in (active_gems or {}).items():
            try:
                gem = int(gem)
            except (TypeError, ValueError):
                continue
            if gem <= 0:
                continue
            abilities = db_gem_ability_guids(gem, conn=_db)
            if abilities:
                out[str(inst)] = abilities
        return out

    def _card_gem_type(self, game, scid, instance_id=None,
                       persisted_gem=_NO_PERSISTED_GEM):
        """Resolve the authoritative socketed gem for one card instance.

        The deck stores FRA instance ids, while mirrored/AI cards retain the
        gem on their game_cards row.  Keeping this lookup in one place lets
        Practice and PvP derive abilities from the current socket instead of
        trusting a possibly stale decks.gem_abilities cache.
        """
        if instance_id is None and scid is not None:
            card_details = db_card_zone_details(
                game.session_id.uid64, scid.uid.uid64, conn=_db)
            if card_details and card_details[1]:
                try:
                    instance_id = int(card_details[1])
                except (TypeError, ValueError):
                    pass
        has_persisted_gem = persisted_gem is not _NO_PERSISTED_GEM
        try:
            gem_type = int(persisted_gem or 0) if has_persisted_gem else 0
        except (TypeError, ValueError):
            gem_type = 0
        if instance_id is not None:
            try:
                # This is a display lookup.  Do not initialize arena_state
                # from inside card encoding: that adds a write/commit to a
                # client-facing turn transition and can hit SQLite's writer
                # lock while the game state is being advanced.
                arena = db_get_arena_state(
                    self.user_profile["id"], initialize=False)
                deck_id = self._resolve_fra_deck_id(arena["deck_id"]) or 0
                active_gems_json = db_deck_active_gems(deck_id, conn=_db)
                if active_gems_json:
                    try:
                        gems = json.loads(active_gems_json)
                        gem_type = int(gems.get(str(instance_id), 0) or 0)
                    except Exception:
                        pass
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                # A socketed gem is optional display metadata.  If another
                # process briefly owns SQLite's write lock, continue with the
                # card's persisted game_cards gem (the fallback below) rather
                # than aborting the entire turn transition.
                gem_type = 0
        if (not gem_type and not has_persisted_gem and scid is not None):
            try:
                persisted_gem = db_card_gem_type(
                    game.session_id.uid64, scid.uid.uid64, conn=_db)
                gem_type = int(persisted_gem or 0)
            except Exception:
                # Older focused fixtures may not carry the optional gem
                # column; the deck lookup above is still authoritative.
                gem_type = 0
        return gem_type

    def _deck_search_ability(self, ability_guid):
        """Find the nested "search your deck" ability + its target template by
        following the ActivateAbility BOM chain to the MoveCardToZone-from-deck
        leaf — e.g. Darkspire Priestess 9853659b -> ... -> 37955055, whose
        target template 0ad94887 has Choosing in its collection flags and the
        Darkspire filter.  The class-39 prompt must reference THIS ability (not
        the top deathcry): the client's ConfigureAbility resolves targets
        against the prompt ability's OWN AbilityTargetTemplateIds, and only
        accepts candidates whose collection is covered by that template.
        Returns (ability_guid, target_template_id) or (None, None)."""
        from gamedata.records import reference_guid

        def contains(node, type_name, field=None, wanted=None):
            if isinstance(node, dict):
                if str(node.get("_t", "")).rsplit(".", 1)[-1] == type_name:
                    if field is None:
                        return True
                    value = node.get(field)
                    if wanted is None or str(value).lower() == str(wanted).lower():
                        return True
                return any(contains(child, type_name, field, wanted)
                           for child in node.values())
            if isinstance(node, list):
                return any(contains(child, type_name, field, wanted)
                           for child in node)
            return False

        seen = set()
        stack = [str(ability_guid).lower()]
        while stack:
            ag = stack.pop()
            if ag in seen:
                continue
            seen.add(ag)
            graph = _records_ability_graph(self, ag)
            if graph is None:
                continue
            for target in graph.targets:
                if ("Choosing" in (target.collection_flags or "") and
                        "deck" in (target.collection_flags or "").lower()):
                    return ag, str(target.guid)
            for effect in graph.effects:
                if effect.concrete_type not in (
                        "ActivateAbilityEffectTemplate",
                        "ActivateTriggeredAbilityEffectTemplate"):
                    continue
                child = reference_guid(
                    effect.template.field("m_AbilityToInvoke")
                    if effect.template is not None else None)
                if child:
                    stack.append(child.lower())
        return None, None

    def _shards_of_fate_template(self, ability_guids):
        """Data-driven Shards of Fate detection: walk an ability's
        ActivateAbility chain and return (ability_guid, target_template_id)
        when a target template filters a Standard RESOURCE in the DECK
        ("Choose a Standard resource in your deck. Gain the thresholds it
        provides.").  Returns (None, None) for ordinary resources."""
        from gamedata.records import reference_guid

        def contains(node, type_name, field=None, wanted=None):
            if isinstance(node, dict):
                if str(node.get("_t", "")).rsplit(".", 1)[-1] == type_name:
                    if field is None:
                        return True
                    value = node.get(field)
                    if wanted is None or str(value).lower() == str(wanted).lower():
                        return True
                return any(contains(child, type_name, field, wanted)
                           for child in node.values())
            if isinstance(node, list):
                return any(contains(child, type_name, field, wanted)
                           for child in node)
            return False

        seen = set()
        stack = [str(a).lower() for a in (ability_guids or [])]
        while stack:
            ag = stack.pop()
            if ag in seen:
                continue
            seen.add(ag)
            graph = _records_ability_graph(self, ag)
            if graph is None:
                continue
            for target in graph.targets:
                card_filter = target.card_filter
                if hasattr(card_filter, "to_dict"):
                    card_filter = card_filter.to_dict()
                if (contains(card_filter, "IsSubType", "m_SubType", "Standard")
                        and contains(card_filter, "IsResource")
                        and contains(card_filter, "InZone", "m_Collection", "Deck")):
                    return ag, str(target.guid)
            for effect in graph.effects:
                if effect.concrete_type not in (
                        "ActivateAbilityEffectTemplate",
                        "ActivateTriggeredAbilityEffectTemplate"):
                    continue
                child = reference_guid(
                    effect.template.field("m_AbilityToInvoke")
                    if effect.template is not None else None)
                if child:
                    stack.append(child.lower())
        return None, None

    def _resolve_shards_of_fate(self, game, session, pl_t, ai_t, bstate,
                                played_card_uid, ability_guid, shard_tpl,
                                owner_id):
        """Shards of Fate: the controller chooses a Standard resource in their
        deck and gains its threshold.  A HUMAN gets the class-39 choosing
        prompt (the shard stays in the deck); the AI auto-picks a random one."""
        if getattr(session, "_rules_port_session", None) is not None:
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        from rules_port.targeting import legal_targets as _lt
        candidates = _lt(_db, session.session_id, owner_id, shard_tpl,
                         int(played_card_uid or 0), both_players=False,
                         champions=[])
        candidates = [int(c) for c in candidates]
        if not candidates:
            log_req(f"    Shards of Fate: no Standard resource left in deck "
                    f"(owner {owner_id})")
            return "shards of fate: no standard resource in deck"
        if owner_id != 0:
            # Do not expose the deck's current shard order in the picker.
            random.shuffle(candidates)
            return self._prompt_deck_search(
                game, session, pl_t, ai_t, bstate, ability_guid,
                int(played_card_uid or 0), owner_id, candidates, kind="shard")
        # AI: random Standard resource from its deck -> gain its threshold.
        import random as _rnd
        chosen = _rnd.choice(candidates)
        from rules_port.resources import (
            apply_resource_change, card_template_threshold_flags)
        provided = card_template_threshold_flags(
            _db, session.session_id, int(chosen))
        threshold_colors = sorted({
            int(flag) for flag in game_engine.SHARD_TO_FLAG.values()
            if provided & int(flag)})
        if threshold_colors:
            th = bstate.setdefault("ai_threshold", {})
            changes = []
            for flag in threshold_colors:
                changes.append(apply_resource_change(
                    bstate, "ai", "threshold", 1, color=flag))
            _be.save_state(session, bstate)
            game.ai_threshold = dict(th)
            for flag, change in zip(threshold_colors, changes):
                ev_th = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                ev_th.player_id = ai_t
                ev_th.color = flag
                ev_th.operation = 1
                ev_th.delta = 1
                ev_th.new_value = change.new_value
                game._push(ev_th)
                from rules_port.triggers import dispatch_native_trigger
                champion = getattr(self, "_ai_champ_scid", None)
                if champion is not None:
                    dispatch_native_trigger(
                        db=_db, handler=self, game=game, session=session,
                        player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                        event_type="GainThresholdEvent",
                        source_card_id=int(champion.uid.uid64),
                        source_player_id=owner_id,
                        data={"gain_threshold_color": int(flag)})
            game.push_player_updated(
                ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        log_req(f"    Shards of Fate (AI): gained threshold flags "
                f"{threshold_colors}")
        return f"shards of fate: AI gained {threshold_colors} threshold flags"

    def _mobilize_discount(self, session, card_uid):
        """Session.CheckMobilize's affordable reduction for one hand card."""
        from rules_port.static_rules import mobilize_discount
        owner = int(self.user_profile.get("id", 0) or 0) \
            if isinstance(self.user_profile, dict) else 0
        try:
            return mobilize_discount(
                _db, session.session_id, owner, int(card_uid))
        except Exception:
            return 0

    def _hand_card_playable(self, session, card_uid, ct, cost, thresh_json,
                            ability_guids, resources, threshold, resource_played,
                            friendly_count, enemy_count):
        """A hand card is playable iff:
          - Resource: not already played one this turn.
          - Any other type: affordable (cost <= resources) AND thresholds met.
          - Its troop-target requirement has valid targets (e.g. a buff needing
            a friendly troop when you have none is not playable).
          - Its additional sacrifice cost (e.g. Abominate "sacrifice a troop you
            control") can be paid — not playable with no eligible troops."""
        if ct == 'Resource':
            return not resource_played
        if cost is not None and cost > resources:
            if cost - self._mobilize_discount(session, card_uid) > resources:
                return False
        if not self._thresholds_met(thresh_json, threshold):
            from rules_port.static_rules import champion_int_attribute
            battle_state = getattr(self, "_current_bstate", None)
            if not battle_state:
                try:
                    from rules_port import lifecycle as _lifecycle
                    battle_state = _lifecycle.load_state(session)
                except Exception:
                    battle_state = {}
            owner = (int(self.user_profile.get("id", 0) or 0)
                     if isinstance(self.user_profile, dict) else 0)
            if champion_int_attribute(
                    battle_state, owner,
                    "CanIgnoreCardsThresholds") <= 0:
                return False
        card_details = db_card_zone_details(
            session.session_id, card_uid, conn=_db)
        play_plan = self._card_play_plan(
            card_details[0] if card_details else "", card_uid,
            self.user_profile["id"] if self.user_profile else 0,
            session=session)
        if play_plan is None:
            return False
        # Zone-bound explicit targets (e.g. Countermagic's CastSpells-only
        # "Interrupt target card") must have a legal candidate to be playable.
        if not self._card_target_requirements_met(
                session, play_plan,
                battle_state=getattr(self, "_current_bstate", None)):
            return False
        # Every non-automatic card-level cost must have enough legal targets
        # before the card is offered.  This mirrors the client's
        # GetPotentialCostsForAbility/AreXCostsComplete gates.
        if play_plan.cost_instances:
            from rules_port.costs import card_cost_targets
            for cost in card_cost_targets(
                    play_plan, _db, session.session_id,
                    self.user_profile["id"] if self.user_profile else 0,
                    card_uid, champions=self._champion_targets(),
                    battle_state=getattr(self, "_current_bstate", None)):
                if (not cost.is_source_auto_target and
                        len(cost.candidates) < cost.minimum):
                    return False
        return True

    def _push_main_phase_options(self, session, pl_t, ai_t, game=None,
                                 refresh_warzone=True):
        """Push the playable-card options (golden outlines) for a main phase.

        Computes which cards in the player's hand are affordable from the
        DB-backed battle state and pushes a PlayerOptionList on its own packet.
        Shards: playable if resource not yet played this turn.
        Troops: playable if cost <= available resources AND all threshold
        requirements are met.
        """
        if getattr(session, "_rules_port_session", None) is not None:
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        from abilities.framework._shared import owner_uid
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        # A card/resource mutation can arrive after the port session was
        # reattached with an empty action stack.  Recreate the client-facing
        # main-phase priority checkpoint before emitting options; otherwise
        # the subsequent PassPriority has no PriorityWindowAction to consume.
        port = getattr(session, "_rules_port_session", None)
        native_chain = getattr(port, "chain", None)
        chain_pending = (
            not getattr(native_chain, "is_empty", True)
            if native_chain is not None else bool(bstate.get("stack")))
        waiting_for_input = (
            bool(getattr(port, "pending_activation", None)) or
            any(bstate.get(key) for key in (
                "pending_choice", "pending_trigger", "pending_deck_search",
                "pending_conversation", "pending_discard_ability",
                "resolution_paused")))
        if waiting_for_input:
            # A picker owns the client's UI state. Leave its options intact.
            return
        if chain_pending:
            # Ordinary cards are illegal until the chain empties. Replace any
            # stale main-phase list with the current priority-window options.
            self._push_phase_options_empty(
                session, pl_t, ai_t,
                refresh_warzone=refresh_warzone)
            return
        if (port is not None and
                getattr(port, "action_stack", None) is not None and
                port.action_stack.count == 0 and
                port.current_turn_phase in (
                    game_engine.ETurnPhases.FirstMainPhase,
                    game_engine.ETurnPhases.SecondMainPhase)):
            from collections import deque
            from rules_port.kernel import PriorityWindowAction, TurnPhasePlayers
            priority = PriorityWindowAction(TurnPhasePlayers.ACTIVE)
            port.action_stack.push(priority)
            # The host drives the AI internally; expose one client-facing
            # pass and let the normal phase/AI driver continue afterwards.
            priority._priority_queue = deque([pl_t])
            port.action_stack.priority_player_id = pl_t
        resources = bstate.get("player_resources", 0)
        threshold = bstate.get("player_threshold", {})
        from rules_port.resources import can_play_resource, resource_play_limit
        resource_limit = resource_play_limit(
            _db, session.session_id, bstate,
            int(self.user_profile.get("id", 0)))
        resource_played = not can_play_resource(
            bstate, "player", limit=resource_limit)
        friendly_count = self._warzone_troop_count(session, self.user_profile["id"])
        enemy_count = self._warzone_troop_count(session, 0)
        from pvp_db import (db_hand_cards_with_templates,
                            db_deck_top_card_details,
                            db_card_play_info,
                            db_card_template_thresholds)
        playable = []
        abilities = []
        owner_id = int(self.user_profile["id"])
        rows = list(db_hand_cards_with_templates(
            session.session_id, owner_id))
        from rules_port.static_rules import player_int_attributes
        player_attrs = player_int_attributes(
            _db, session.session_id, bstate, owner_id)
        if int(player_attrs.get("CanPlayTopOfDeck", 0) or 0) > 0:
            top = db_deck_top_card_details(
                session.session_id, owner_id, conn=_db)
            if top:
                top_uid = int(top[1])
                info = db_card_play_info(
                    session.session_id, top_uid, conn=_db)
                if info:
                    threshold_row = db_card_template_thresholds(
                        info[0], conn=_db)
                    top_template = db_template_by_guid(info[0])
                    rows.append((
                        top_uid, top_template[3] if top_template else 0, info[1],
                        threshold_row[0] if threshold_row else None,
                        info[5]))
        # Enlightened Seeker: "You can't play cards." — a continuous static
        # ability; no hand card is playable while the controller has one in play.
        from rules_port.static_rules import controller_flags
        if "cant_play_cards" in controller_flags(
                _db, session.session_id, bstate, self.user_profile["id"]):
            rows = []
        for card_uid, cost, ct, thresh_json, abilities_json in rows:
            scid = game_engine.SessionCardId(game_engine.UID(card_uid))
            # Use the card's EFFECTIVE cost (base + permanent/triggered cost
            # modifiers, e.g. Fury of the Mountain God's "-1 per damage dealt"
            # while in hand) — the raw template cost would wrongly hide a
            # discounted card.
            from rules_port.static_rules import effective_cost
            try:
                cost = effective_cost(_db, session.session_id, bstate, card_uid)
            except Exception:
                pass
            ability_guids = []
            if abilities_json:
                try:
                    ability_guids = [g.lower() for g in json.loads(abilities_json)]
                except Exception:
                    pass
            if self._hand_card_playable(session, card_uid, ct, cost, thresh_json,
                                        ability_guids, resources, threshold,
                                        resource_played, friendly_count, enemy_count):
                playable.append(scid)
            else:
                log_req(f"    Playability: skip "
                        f"{card_label(session.session_id, card_uid)} "
                        f"type {ct} "
                        f"(cost {cost} > resources {resources} / thresholds / no troop target)")
        game2 = game or self._fresh_game(session, pl_t, ai_t, bstate)
        game2.push_options(pl_t, playable)
        # Attach targeting TargetInstances so the client opens the target picker
        # for played spells (BattleStatePlayCard -> CanUseAbility).
        self._add_play_target_options(game2, session, pl_t, ai_t)
        # Include champion ability options (charge/spell powers) so the
        # client's PlayerOptions.m_Targets is populated and CanActivateAbility
        # returns true for abilities the champion can afford.
        player_champ_cid = getattr(self, "_player_champ_scid", None)
        if player_champ_cid:
            abilities = getattr(self, "_player_champ_abilities", [])
            if abilities:
                champ_uid = (player_champ_cid.uid.to_uint64()
                             if hasattr(player_champ_cid, "uid") else 0)
                champ_targets = self._champion_ability_targets(
                    session, abilities, champ_uid)
                champ_costs = self._champion_ability_costs(
                    session, abilities, champ_uid)
                game2.add_champion_to_options(
                    pl_t, player_champ_cid,
                    self._filter_affordable_abilities(abilities, bstate,
                                                      _be.current_phase(bstate),
                                                      target_data=champ_targets,
                                                      cost_data=champ_costs,
                                                      session=session),
                    self._discard_costs_for(session, abilities),
                    champ_targets, champ_costs)
        # Warzone-troop manual abilities (e.g. Shift): light up as Activate.
        affordable = self._affordable_troop_abilities(session, bstate)
        if affordable:
            self._add_troop_ability_options(game2, pl_t, session, affordable,
                                             bstate)
        # Always carry a fresh PlayerUpdated so the client's resource/charge/SP
        # display reflects the real battle state (a bare Game defaults them to
        # 0, wiping the UI).
        game2.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game2.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        if refresh_warzone:
            self._push_warzone_card_updates(game2, session, pl_t, ai_t)
        # Options are sometimes delivered as the packet immediately after a
        # choice continuation. Repeat the authoritative priority event in the
        # same packet so the client's one-request UI state cannot remain
        # stranded on the finished choice dialog.
        game2.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
        sent = self._send_battle_events(session, game2, pl_t)
        if sent:
            log_req(f"    Pushed main-phase options ({len(playable)} playable, champ_ab={len(abilities) if player_champ_cid and abilities else 0}, troop_ab={sum(len(v) for v in affordable.values())})")
        return sent

    def _handle_rules_port_resource(self, session, transaction):
        """Apply an ordinary resource play without the legacy card dispatcher.

        RulesPort has already validated ownership, zone, phase, priority, and
        the once-per-turn gate. This method owns the resource state/event
        projection and delegates any authored resource BOM to the metadata
        resolver, including chooser continuations.
        """
        from rules_port import lifecycle as _be
        card_uid = int((getattr(transaction, "payload", {}) or {}).get("card_id", 0))
        if not card_uid or not self.user_profile:
            return False
        from pvp_db import db_rules_port_card_projection
        row = db_rules_port_card_projection(session.session_id, card_uid)
        if not row or row[4] != "Resource":
            return False
        if int(row[2] or 0) != int(self.user_profile.get("id", 0)):
            return False
        source_location = str(row[1] or "").lower()
        if source_location == "deck":
            from pvp_db import db_deck_top_card_details
            from rules_port.static_rules import player_int_attributes
            top = db_deck_top_card_details(
                session.session_id, int(self.user_profile["id"]), conn=_db)
            allowed = player_int_attributes(
                _db, session.session_id, _be.load_state(session),
                int(self.user_profile["id"])).get("CanPlayTopOfDeck", 0)
            if not top or int(top[1]) != card_uid or int(allowed or 0) <= 0:
                return False
        elif source_location != "hand":
            return False
        try:
            ability_guids = json.loads(row[5]) if row[5] else []
        except (TypeError, ValueError):
            return False
        shard_ability = shard_tpl = resource_choice_ability = None
        if ability_guids:
            shard_ability, shard_tpl = self._shards_of_fate_template(
                ability_guids)
            if not shard_tpl:
                from rules_port.resources import printed_resource_choice_ability
                resource_choice_ability = printed_resource_choice_ability(
                    ability_guids)
        bstate = _be.load_state(session)
        from rules_port.resources import can_play_resource, resource_play_limit
        resource_limit = resource_play_limit(
            _db, session.session_id, bstate,
            int(self.user_profile.get("id", 0)))
        if not can_play_resource(
                bstate, "player", limit=resource_limit):
            return False
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        from domain.constants import PLAYED_CARD_POSITION
        from rules_port.zone_effects import move_card_to_zone
        from pvp_db import db_card_owner_zone_state
        prior_card_zone = db_card_owner_zone_state(
            session.session_id, card_uid, conn=_db)
        if not move_card_to_zone(
                _db, session.session_id, card_uid, "PlayedResources",
                owner_id=int(self.user_profile["id"]),
                expected_location=source_location,
                position=PLAYED_CARD_POSITION):
            return False
        current_grant = int(row[6] or 0)
        max_grant = int(row[7] or 0)
        from rules_port.cast_stats import record_card_cast
        record_card_cast(
            bstate, int(self.user_profile.get("id", 0)), resource=True,
            card_uid=card_uid, card_type="Resource")
        from rules_port.resources import (apply_resource_change, play_resource,
                                          resource_threshold_grants)
        threshold_changes = []
        if not shard_tpl and not resource_choice_ability:
            for flag, amount in resource_threshold_grants(
                    _db, session.session_id, int(self.user_profile.get("id", 0)),
                    ability_guids, bstate, source_uid=card_uid):
                change = apply_resource_change(
                    bstate, "player", "threshold", amount, color=flag)
                threshold_changes.append((flag, amount, change))
        resource_play = play_resource(
            bstate, "player", current_grant, max_grant,
            additional_plays=max(0, resource_limit - 1))
        charge_delta = max(
            0, int(resource_play.charge.new_value) -
            int(resource_play.charge.old_value))
        _be.save_state(session, bstate)
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        from rules_port import chain_items as _chain_items
        _chain_items.dispatch_card_cast(
            self, session, game, bstate, pl_t, ai_t, card_uid,
            int(self.user_profile.get("id", 0)))
        _chain_items.dispatch_card_zone_transition(
            self, session, game, bstate, pl_t, ai_t, card_uid,
            int(self.user_profile.get("id", 0)), source_location,
            "PlayedResources",
            int(prior_card_zone[2] or 0) if prior_card_zone else 0)
        # The printed parent ability below invokes the authored target and
        # threshold effect.  Keep the resource play to one activation; the
        # manual picker would replace that ability's continuation.
        scid = game_engine.SessionCardId(game_engine.UID(card_uid))
        game.push_card_updated(
            scid, pl_t, game_engine.ECardCollections.PlayedResources,
            game_engine.ECardTypes.Resource, template_id=row[0])
        game.push_resource_card_played(scid, pl_t, free=False)
        if current_grant or max_grant:
            current = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
            current.player_id = pl_t; current.operation = 1
            current.delta = current_grant
            current.new_value = bstate["player_resources"]
            game._push(current)
            total = game_engine.PlayerTotalResourcePoolChangedSessionEventArgs()
            total.player_id = pl_t; total.operation = 1
            total.delta = max_grant
            total.new_value = bstate["player_total_resources"]
            game._push(total)
        if threshold_changes:
            from rules_port.triggers import dispatch_native_trigger
            for flag, amount, change in threshold_changes:
                threshold = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                threshold.player_id = pl_t; threshold.color = flag
                threshold.operation = 1; threshold.delta = amount
                threshold.new_value = change.new_value
                game._push(threshold)
                old_threshold_color = bstate.get("gain_threshold_color")
                bstate["gain_threshold_color"] = int(flag)
                try:
                    dispatch_native_trigger(
                        db=_db, handler=self, game=game, session=session,
                        player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                        event_type="GainThresholdEvent",
                        source_card_id=int(self._player_champ_scid.uid.uid64),
                        source_player_id=int(self.user_profile.get("id", 0)),
                        data={"gain_threshold_color": int(flag)})
                finally:
                    if old_threshold_color is None:
                        bstate.pop("gain_threshold_color", None)
                    else:
                        bstate["gain_threshold_color"] = old_threshold_color
        if charge_delta:
            charge = game_engine.ChampionChargePointsChangedSessionEventArgs()
            charge.player_id = pl_t; charge.operation = 1
            charge.delta = charge_delta
            charge.new_value = bstate["player_charges"]
            game._push(charge)
            from rules_port.triggers import dispatch_native_trigger
            for _ in range(charge_delta):
                dispatch_native_trigger(
                    db=_db, handler=self, game=game, session=session,
                    player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                    event_type="GainChargeEvent",
                    source_card_id=int(self._player_champ_scid.uid.uid64),
                    source_player_id=int(self.user_profile.get("id", 0)))
        from rules_port.resources import (
            resolve_granted_resource_abilities,
        )
        resource_owner = int(self.user_profile.get("id", 0))
        granted_logs = resolve_granted_resource_abilities(
            game, session, _db, self, pl_t, ai_t, bstate,
            card_uid, resource_owner)
        if granted_logs:
            log_req("    RulesPort resource grants: " + "; ".join(granted_logs))
        if resource_choice_ability:
            from rules_port.resolution import resolve_port_ability
            resolve_port_ability(
                self, game, session, _db, pl_t, ai_t, bstate,
                resource_choice_ability, card_uid, resource_owner,
                target_map={})
        from rules_port.resources import resolve_printed_resource_abilities
        printed_logs = resolve_printed_resource_abilities(
            game, session, _db, self, pl_t, ai_t, bstate,
            card_uid, resource_owner,
            skip_guids=({resource_choice_ability}
                        if resource_choice_ability else ()))
        if printed_logs:
            log_req("    RulesPort resource abilities: " + "; ".join(printed_logs))
        _be.save_state(session, bstate)
        waiting_for_input = any(bstate.get(key) for key in (
            "pending_choice", "pending_trigger", "pending_deck_search",
            "pending_conversation", "pending_discard_ability",
            "resolution_paused"))
        if not waiting_for_input and _be.stack_empty(bstate):
            # Keep the resource/charge mutation, fresh affordability list, and
            # priority window in one sync packet. Unity enables abilities from
            # its cached PlayerOptionList, so a separate follow-up packet leaves
            # a short window where an unaffordable power can still be clicked.
            self._push_main_phase_options(
                session, pl_t, ai_t, game=game)
        elif _be.stack_empty(bstate):
            # The resource event packet already carried the picker/continuation;
            # do not overwrite it with ordinary main-phase options.
            self._send_battle_events(session, game, pl_t)
            log_req("    RulesPort resource waiting for continuation")
        else:
            # A threshold/charge trigger placed an item on the chain.  Expose
            # only response options and the resolve checkpoint in the same
            # packet as the resource mutation.
            self._push_phase_options_empty(
                session, pl_t, ai_t, game=game, send=False)
            game.push_green_light(
                pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
            self._send_battle_events(session, game, pl_t)
        log_req(f"    RulesPort resource applied uid={card_uid} "
                f"resources={bstate['player_resources']} "
                f"threshold={bstate.get('player_threshold', {})}")
        return True

    def _handle_rules_port_card_play(self, session, transaction):
        """Cast a typed troop/artifact/spell through the RulesPort seam.

        The port has already performed phase, priority, ownership, threshold,
        and typed X-cost checks. This operation owns the shared card-play
        transition (payment, CastSpells projection, and one chain item); the
        existing metadata resolver only runs later when that chain item
        resolves.
        """
        from rules_port import lifecycle as _be
        if not isinstance(self.user_profile, dict):
            return False
        owner_id = int(self.user_profile.get("id", 0) or 0)
        payload = getattr(transaction, "payload", {}) or {}
        card_uid = int(payload.get("card_id") or 0)
        kind = str(getattr(transaction, "kind", ""))

        def reject(reason):
            log_req(f"    Card play reject: "
                    f"card={card_label(session.session_id, card_uid)} "
                    f"kind={kind or '-'} reason={reason}")
            return False

        if not card_uid or kind not in {
                "play_troop", "play_artifact", "play_spell",
                "play_champion"}:
            return reject("untyped-intent")
        from pvp_db import db_rules_port_card_projection
        row = db_rules_port_card_projection(session.session_id, card_uid)
        if not row or int(row[2] or 0) != owner_id:
            return reject(f"missing-or-not-owner(row_owner="
                          f"{int(row[2]) if row else '-'})")
        source_location = str(row[1] or "").lower()
        if source_location == "deck":
            from pvp_db import db_deck_top_card_details
            from rules_port.static_rules import player_int_attributes
            owner = owner_id
            top = db_deck_top_card_details(
                session.session_id, owner, conn=_db)
            allowed = player_int_attributes(
                _db, session.session_id, _be.load_state(session), owner).get(
                    "CanPlayTopOfDeck", 0)
            if not top or int(top[1]) != card_uid or int(allowed or 0) <= 0:
                return reject("deck-top-not-allowed")
        elif source_location != "hand":
            return reject(f"not-in-hand(location={source_location or '-'})")
        if row[4] == "Resource":
            return reject("resource-card")
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        scid = game_engine.SessionCardId(game_engine.UID(card_uid))
        _tpl, _ct, _name, cost, attack, defense, gem = self._card_full_data(
            game, scid, row[0], None)
        from rules_port.static_rules import effective_cost
        try:
            cost = int(effective_cost(_db, session.session_id, bstate, card_uid))
        except Exception:
            cost = int(cost or 0)
        play_plan = self._card_play_plan(
            row[0], card_uid, owner_id, session=session)
        if play_plan is None:
            return reject(f"no-play-plan(template={row[0]})")
        ability_data = payload.get("ability_data") or ()
        targets = []
        client_cost_uids = {}
        x_cost = 0
        cast_ability_guids = {
            ability.ability_guid.lower()
            for ability in play_plan.cast_abilities
        }
        for activation in ability_data:
            if not isinstance(activation, dict):
                continue
            raw_ability_guid = activation.get(
                "ability_template_id", activation.get("ability_guid", ""))
            if isinstance(raw_ability_guid, dict):
                raw_ability_guid = raw_ability_guid.get(
                    "guid", raw_ability_guid.get("m_Guid", ""))
            activation_is_cast_ability = (
                str(raw_ability_guid or "").lower() in cast_ability_guids)
            x_cost = max(x_cost, int(activation.get("x_cost", 0) or 0))
            target_map = activation.get("target_map", {})
            if isinstance(target_map, dict):
                for selected in target_map.values():
                    if not isinstance(selected, (list, tuple, set)):
                        selected = (selected,)
                    for value in selected:
                        try:
                            uid = int(getattr(value, "uid64", value))
                        except (TypeError, ValueError):
                            continue
                        # TargetMap is already typed by the ObjFmt decoder;
                        # retain both SessionCardId and PlayerId targets.
                        # Only the source card itself is excluded.
                        if uid != card_uid:
                            targets.append(uid)
            # XCostData.CardsToSacrifice arrives separately from TargetMap
            # (the decoder normalized it to cost_target_map). Card-template
            # costs are kept here; ability graph costs stay with their
            # ability_template_id and are bound by PlayPlan below.
            if activation_is_cast_ability:
                continue
            cost_map = activation.get("cost_target_map")
            if isinstance(cost_map, dict):
                for index, selected in cost_map.items():
                    try:
                        index = int(index)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(selected, (list, tuple, set)):
                        selected = (selected,)
                    for value in selected:
                        try:
                            uid = int(getattr(value, "uid64", value))
                        except (TypeError, ValueError):
                            continue
                        if uid not in client_cost_uids.setdefault(index, []):
                            client_cost_uids[index].append(uid)
        if not x_cost:
            # ``XCostData`` is a nested record the generic ObjFmt walker can
            # stop before, so the typed activation can lose the value the
            # player chose in the X-cost dialog.  Recover the labelled Int32
            # from the raw envelope (the same seam the legacy card-play and
            # tournament paths use) so a variable-cost card cannot resolve
            # for 0 and deal no damage.
            inner_bytes = getattr(transaction, "inner_bytes", None)
            if isinstance(inner_bytes, (bytes, bytearray)):
                x_cost = max(0, int(self._extract_int32_field(
                    bytes(inner_bytes), "m_ResourceXCost") or 0))
        cost_selection = self._card_play_cost_selections(
            session, play_plan, card_uid, targets,
            client_cost_map=client_cost_uids)
        if cost_selection is None:
            return reject("cost-selection")
        cost_selections, cost_uids = cost_selection
        if play_plan.cost.variable:
            if x_cost < int(play_plan.cost.variable_minimum or 0):
                return reject(f"x-cost {x_cost} < minimum "
                              f"{int(play_plan.cost.variable_minimum or 0)}")
        elif x_cost:
            return reject(f"x-cost {x_cost} on fixed-cost card")
        playing_for_free = bool(payload.get("playing_for_free", False))
        x_payment = x_cost * play_plan.cost.variable_multiplier
        payment = 0 if playing_for_free else cost + x_payment
        mobilized = []
        for activation in ability_data:
            if not isinstance(activation, dict):
                continue
            selected = activation.get("cards_to_mobilize")
            if selected is None and isinstance(
                    activation.get("x_cost_data"), dict):
                selected = activation["x_cost_data"].get("cards_to_mobilize")
            if selected is None:
                continue
            if not isinstance(selected, (list, tuple, set)):
                selected = (selected,)
            for value in selected:
                try:
                    uid = int(getattr(value, "uid64", value))
                except (TypeError, ValueError):
                    continue
                if uid not in mobilized:
                    mobilized.append(uid)
        if mobilized and not playing_for_free:
            # Session.CheckMobilize: each mobilized card is a ready troop the
            # player controls, at most the card's Mobilize value, and each one
            # tapped this way reduces the resource cost by two.
            from rules_port.static_rules import mobilize_payment
            reduced = mobilize_payment(
                _db, session.session_id, owner_id, card_uid, payment,
                mobilized)
            if reduced is None:
                return reject("mobilize-payment")
            payment = reduced
        resources = int(bstate.get("player_resources", 0) or 0)
        if payment > resources:
            return reject(f"cost {payment} > pool {resources}")
        selected_cost_map = {
            int(spec.get("index", position)): tuple(selected)
            for position, (spec, selected) in enumerate(cost_selections)
            if selected
        }
        activations, _cost_target_map = play_plan.activation_bundle(
            targets, x_cost=x_cost, cost_target_map=selected_cost_map,
            ability_data=ability_data)
        ability_cost_selection = self._ability_card_play_cost_selections(
            session, play_plan, card_uid, activations, owner_id=owner_id,
            battle_state=bstate)
        if ability_cost_selection is None:
            return reject("ability-cost-selection")
        ability_cost_selections, ability_cost_uids = ability_cost_selection
        cost_selections.extend(ability_cost_selections)
        cost_uids.update(ability_cost_uids)
        validation_errors = play_plan.validate(
            variable_cost=x_cost,
            activations=activations,
            cost_target_map=selected_cost_map)
        if validation_errors:
            return reject(f"plan-validate {list(validation_errors)}")
        if not self._card_costs_valid(
                session, cost_selections, owner_id):
            return reject(f"cost-invalid "
                          f"{[(spec.get('kind'), [hex(u) for u in chosen]) for spec, chosen in cost_selections]}")
        if mobilized:
            from pvp_db import db_update_card_state
            for uid in mobilized:
                db_update_card_state(
                    session.session_id, int(uid),
                    set_bits=game_engine.ECardStates.Tapped, conn=_db)
            _db.commit()
        from rules_port.card_transactions import apply_card_play
        from pvp_db import db_card_state_value
        previous_state = int(db_card_state_value(
            session.session_id, card_uid, conn=_db) or 0)
        transition = apply_card_play(
            _db, session.session_id, bstate, card_uid,
            owner_id, payment,
            destination="CastSpells",
            expected_location=source_location)
        if transition is None:
            return reject("zone-transition")
        resource_change = transition.resource_change
        from rules_port.cast_stats import record_card_cast
        record_card_cast(
            bstate, owner_id, resource=False,
            card_uid=card_uid, card_type=row[4], resource_cost=cost)
        voided_cost_uids = self._apply_card_play_costs(
            game, session, bstate, pl_t, ai_t, cost_selections,
            source_uid=card_uid,
            expected_owner_id=owner_id,
            scrounge=any(getattr(ability, "is_scrounge", False)
                         for ability in play_plan.cast_abilities),
            defer_voided_cards=True)
        card_type = game_engine.card_type_from_db(row[4])
        game.push_card_updated(
            scid, pl_t, game_engine.ECardCollections.CastSpells, card_type,
            template_id=row[0], cost=cost, attack=attack, defense=defense,
            gems=gem)
        game.push_troop_card_played(scid, pl_t)
        for uid in mobilized:
            from pvp_db import db_card_zone_details
            mugid = int(uid)
            mscid = game_engine.SessionCardId(game_engine.UID(mugid))
            details = db_card_zone_details(
                session.session_id, mugid, conn=_db)
            mtpl, mct, _mname, mcost, matk, mdef, mgem = \
                self._card_full_data(
                    game, mscid, details[0] if details else None)
            game.push_card_updated(
                mscid, pl_t, game_engine.ECardCollections.Warzone, mct,
                template_id=mtpl, cost=mcost, attack=matk, defense=mdef,
                gems=mgem, state=int(game_engine.ECardStates.Tapped))
        perm = card_type & (game_engine.ECardTypes.Troop |
                            game_engine.ECardTypes.Artifact |
                            game_engine.ECardTypes.Constant)
        # The client keys its chain view by a unique ability instance id (C#
        # ``Session.m_NextAbilityInstanceId``).  A hardcoded id collides with a
        # still-pending projected chain item, and ``queue_projected_chain``
        # then silently reuses the old ability instead of queueing this card.
        inst_id = int(bstate.get("_next_instance_id", 1) or 1)
        bstate["_next_instance_id"] = inst_id + 1
        if perm:
            effect_targets = [uid for uid in targets if uid not in cost_uids]
            from rules_port.card_transactions import \
                automatic_instance_ability_guids
            ability_guids = automatic_instance_ability_guids(
                _db, session.session_id, card_uid,
                [ability.ability_guid
                 for ability in play_plan.cast_abilities])
            chain_descriptor = {
                "kind": "troop", "source_uid": card_uid,
                "ability_guids": ability_guids,
                "target_uid": (effect_targets[-1]
                                if effect_targets else None),
                "instance_id": inst_id, "x_cost": x_cost,
                "voided_cost_uids": list(voided_cost_uids or ()),
                "activations": {
                    guid: activation.as_dict()
                    for guid, activation in activations.items()
                },
                "played_from_hand": source_location == "hand",
                "card_cast_event_dispatched": True,
            }
            _be.stack_push(bstate, chain_descriptor)
        else:
            effect_targets = [uid for uid in targets if uid not in cost_uids]
            from rules_port.card_transactions import \
                automatic_instance_ability_guids
            ability_guids = automatic_instance_ability_guids(
                _db, session.session_id, card_uid,
                [ability.ability_guid
                 for ability in play_plan.cast_abilities])
            chain_descriptor = {
                "kind": "spell", "source_uid": card_uid,
                "ability_guids": ability_guids,
                "target_uid": (effect_targets[-1]
                                if effect_targets else None),
                "instance_id": inst_id, "x_cost": x_cost,
                "voided_cost_uids": list(voided_cost_uids or ()),
                "played_from_hand": source_location == "hand",
                "activations": {
                    guid: activation.as_dict()
                    for guid, activation in activations.items()
                },
                "card_cast_event_dispatched": True,
            }
            _be.stack_push(bstate, chain_descriptor)
        native_port = getattr(session, "_rules_port_session", None)
        if native_port is not None and hasattr(native_port, "queue_projected_chain"):
            native_port.queue_projected_chain(
                chain_descriptor, owner_id,
                first_player_id=pl_t)
        game.push_ability_on_chain(
            scid, game_engine.ResourceId.from_str(
                game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID),
            target_card_ids=(
                [game_engine.SessionCardId(game_engine.UID(int(uid)))
                 for uid in effect_targets]
                if perm else None),
            ability_instance_id=inst_id)
        from rules_port.chain_items import (
            dispatch_card_cast, dispatch_card_zone_transition)
        # C# MoveCard enqueues zone events before CastSpell enqueues
        # CardCastEvent. The underlying card item is already queued, so any
        # listeners are pushed above it with their native instance IDs.
        dispatch_card_zone_transition(
            self, session, game, bstate, pl_t, ai_t, card_uid, owner_id,
            source_location, "CastSpells", previous_state)
        dispatch_card_cast(
            self, session, game, bstate, pl_t, ai_t, card_uid, owner_id)
        _be.save_state(session, bstate)
        game.push_green_light(
            ai_t, game_engine.EPriorityContext.ResolveTopOfChain)
        if payment:
            event = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
            event.player_id = pl_t; event.operation = 2
            event.delta = payment
            event.new_value = resource_change.new_value
            game._push(event)
        game.player_resources = resource_change.new_value
        game.push_player_updated(
            pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        self._send_battle_events(session, game, pl_t)
        # The initial packet contains the chain item and AI GreenLight. The
        # follow-up checkpoint exposes only response options until both sides
        # pass; it must not reopen main-phase card options.
        self._push_phase_options_empty(session, pl_t, ai_t)
        chain_game = self._fresh_game(session, pl_t, ai_t, bstate)
        chain_game.push_green_light(
            pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
        self._send_battle_events(session, chain_game, pl_t)
        log_req(f"    RulesPort card play queued {row[3]} uid={card_uid} "
                f"payment={payment} x={x_cost}")
        return True

    def _affordable_troop_abilities(self, session, bstate):
        """Return {(card_uid, scid): [ability_guid, ...]} for player cards whose
        MANUAL abilities are activatable.

        Gating (mirrors the client's CanActivateAbilityBase + the champion
        talent gating in _filter_affordable_abilities):
          - is_manual (player-activated, not a trigger)
          - source card is in a metadata-allowed collection
          - casting_behavior matches the current phase (QuickAction=64 any
            window; BasicAction=8 only main phases)
          - activation_cost <= available resources
          - UsesPerGame / UsesPerTurn not exhausted for THIS instance
            (tracked in game_cards.card_uses)
        """
        if bstate.get("_rules_port_attached"):
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        # This helper is also used while rebuilding option lists after a
        # priority resync.  During the AI's own turn the human must not retain
        # activation options from the previous player stop; the AI-turn
        # marker is set only when an opponent stop has explicitly handed
        # priority back to the human.
        if (bstate.get("turn_player") == _be.AI
                and bstate.get("ai_turn_phase_idx") is None):
            return {}
        phase = _be.current_phase(bstate)
        resources = bstate.get("player_resources", 0)
        rows = db_ability_option_cards(
            session.session_id, self.user_profile["id"], conn=_db)
        result = {}
        from rules_port.triggers import trigger_collection_allows
        for card_uid, tpl_guid, card_state, attrs, _card_type, card_location in rows:
            ab_list = self._card_ability_list(session, card_uid)
            if not ab_list:
                continue
            affordable = []
            for ag in ab_list:
                graph = _records_ability_graph(self, ag)
                if graph is None:
                    continue
                if not graph.manual:
                    continue
                # Manual abilities may be authored for Hand as well as
                # Warzone (Tunnel is the canonical example). Keep the
                # collection gate metadata-driven, matching the client's
                # Card.PassesCollectionFlagRequirements behavior.
                if not trigger_collection_allows(
                        graph.trigger_collection_flags, card_location):
                    continue
                # Ability conditions gate manual activation (client's
                # CanActivateAbilityBase: TriggerCondition.IsValid). E.g.
                # Droo's Colossal Walker "While this is exhausted: ..." has a
                # RequiresSourcePassesFilterCondition(IsTapped) trigger
                # condition — it is only activatable while the troop is tapped.
                if graph.ability_condition or graph.trigger_condition:
                    if bstate.get("_rules_port_attached"):
                        from rules_port.condition_context import ConditionContext
                        from rules_port.conditions import trigger_condition_met
                    else:
                        from abilities.framework.condition_engine import (
                            ConditionContext,
                            trigger_condition_met,
                        )
                    cond_ctx = ConditionContext(
                        _db, session, bstate,
                        ability_source_uid=card_uid,
                        ability_source_owner_id=self.user_profile["id"])
                    if not trigger_condition_met(graph.source.to_dict(), cond_ctx):
                        continue
                # The ability must have at least one legal target for its
                # explicit target templates, and attacking-target abilities
                # (Prairie Scout's "target attacking troop") are only
                # activatable during combat steps — after attackers were
                # declared, until the second main phase.
                from rules_port.targeting import legal_targets_for
                from rules_port.costs import ability_cost_targets
                cost_guids = {str(guid).lower() for _kind, guid in
                              graph.additional_cost_targets or ()}
                target_refs = [(index, target) for index, target in
                               enumerate(graph.targets)
                               if str(target.guid).lower() not in cost_guids]
                if target_refs:
                    wants_attacking = False
                    has_target = False
                    explicit_target_missing = False
                    for _target_index, target in target_refs:
                        target_filter = target.card_filter
                        if hasattr(target_filter, "to_dict"):
                            target_filter = target_filter.to_dict()
                        if "IsAttacking" in json.dumps(target_filter or {}):
                            wants_attacking = True
                        if not target.requires_input:
                            # Auto targets (e.g. Incubation Slave's 'You' —
                            # "Remove all egg counters from this and sacrifice
                            # it") resolve automatically; no picker, always
                            # available.
                            has_target = True
                            continue
                        cands = legal_targets_for(
                            _db, session.session_id, self.user_profile["id"],
                            target, card_uid,
                            champions=self._champion_targets(),
                            battle_state=bstate)
                        if cands:
                            has_target = True
                        else:
                            explicit_target_missing = True
                    if wants_attacking and phase not in _be.COMBAT_STEPS:
                        continue
                    # An automatic source/player target does not satisfy a
                    # separate explicit target requirement.  Taming Sphere,
                    # for example, has an automatic "You" target followed by
                    # a mandatory Untamed-troop target; it must stay dark when
                    # no legal Untamed troop exists.
                    if explicit_target_missing:
                        continue
                    if not has_target:
                        continue
                if graph.costs.exhausts_card_on_use:
                    # Exhaust-as-cost (e.g. Prairie Scout): the card must be
                    # able to pay the tap — not already tapped, and not
                    # summoning sick (entered this turn without Speed).
                    cstate = card_state or 0
                    if cstate & game_engine.ECardStates.Tapped:
                        continue
                    if (not (cstate & game_engine.ECardStates.StartedATurnOnYourSide)
                            and not ((attrs or 0) & game_engine.ECardAttributes.Speed)):
                        continue
                casting = 64 if graph.casting_behavior == "QuickAction" else 8
                if casting != 64:
                    # BasicAction etc. — main phases only; QuickAction(64) any.
                    # Main phase is meaningful only on this player's turn,
                    # and a non-empty chain cannot be interrupted by a Basic
                    # activation. This prevents Taming Sphere appearing while
                    # the AI has handed back a stale priority window.
                    if bstate.get("turn_player") != _be.PLAYER:
                        continue
                    if phase not in (game_engine.ETurnPhases.FirstMainPhase,
                                     game_engine.ETurnPhases.SecondMainPhase):
                        continue
                    if not _be.stack_empty(bstate):
                        continue
                cost = int(graph.costs.activation or 0)
                if cost > resources:
                    continue
                if graph.costs.variable_activation and resources < int(
                        graph.costs.variable_minimum or 0):
                    continue
                payment_unavailable = False
                for cost in ability_cost_targets(
                        graph, _db, session.session_id,
                        self.user_profile["id"], card_uid,
                        champions=self._champion_targets(),
                        battle_state=bstate):
                    if cost.is_source_auto_target:
                        continue
                    if len(cost.candidates) < cost.minimum:
                        payment_unavailable = True
                        break
                if payment_unavailable:
                    continue
                from pvp_db import (db_card_ability_use_counts,
                                    db_card_cooldown_counts)
                used_game, used_turn = db_card_ability_use_counts(
                    session.session_id, card_uid, ag,
                    bstate.get("turn_number", 1), conn=_db)
                if (graph.costs.cooldown and
                        int(db_card_cooldown_counts(
                            session.session_id, card_uid, conn=_db).get(
                                ag, 0) or 0) > 0):
                    continue
                if graph.costs.uses_per_game and used_game >= graph.costs.uses_per_game:
                    continue
                if graph.costs.uses_per_turn and used_turn >= graph.costs.uses_per_turn:
                    continue
                affordable.append(ag)
            if affordable:
                result[(card_uid, tpl_guid)] = affordable
        return result

    def _add_troop_ability_options(self, game, pl_t, session, affordable,
                                   bstate=None):
        """Append card ability options (ECardUsage.Activate) to the
        most recent PlayerOptionList, one OptionInstance per affordable ability.

        Each ability carries one TargetInstance PER target template id, keyed by
        the ability's own m_AbilityTargetTemplateIds. The client's target picker
        (GoCardView.GetTargetsFor) matches a TargetInstance whose TemplateId
        equals the ability's target template id for index i — using Invalid does
        NOT match a real template id, so the picker would show zero valid
        targets (this is why Soothsaying's discard option used the exact
        DISCARD_TARGET_TEMPLATE). min/max_target_counts mirrors the number of
        target templates so BattleStateConfigureAbility gets matching lists.
        """
        # CardUpdated/PlayerUpdated events may be appended while this packet
        # is assembled.  The option list owns the option metadata; it is not
        # necessarily the final event in the Game.  Requiring events[-1]
        # silently drops manual hand activations such as Tunnel.
        option_list = next(
            (event for event in reversed(game.events)
             if isinstance(event, game_engine.PlayerOptionListSessionEventArgs)),
            None)
        if option_list is None:
            return
        for (card_uid, tpl_guid), abilities in affordable.items():
            scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
            # Default candidate pool: the player's warzone troops.  Abilities
            # with explicit target templates get the pool filtered through the
            # gamedata card filter (e.g. Prairie Scout's IsAttacking+IsTroop).
            from rules_port.targeting import legal_targets_for
            from rules_port.costs import ability_cost_targets, cost_type_for_kind
            # A hand card such as Tunnel can also be normally playable.  The
            # client caches one usage value per card, so merge Activate into
            # the existing Play option instead of appending a second option
            # that would overwrite Play.
            opt = game.get_or_add_card_option(
                option_list, scid, game_engine.ECardUsage.Activate)
            for ag in abilities:
                inst = game._make_event(game_engine.OptionInstanceSessionEventArgs)
                inst.opt_id = game_engine.ResourceId.from_str(ag)
                graph = _records_ability_graph(self, ag)
                cost_guids = {str(guid).lower() for _kind, guid in
                              (graph.additional_cost_targets or ())} \
                    if graph is not None else set()
                target_refs = ([(index, target) for index, target in
                                enumerate(graph.targets)
                                if target.requires_input and
                                str(target.guid).lower() not in cost_guids]
                               if graph is not None else [])
                if target_refs:
                    built = []
                    min_counts = []
                    max_counts = []
                    for i, target in target_refs:
                        tid = target.guid
                        built.append(i)
                        min_counts.append(target.minimum)
                        max_counts.append(max(0, target.maximum)
                                          if target.maximum > 0 else 0)
                        inst.target_ids.append(
                            game_engine.ResourceId.from_str(tid))
                        others = legal_targets_for(
                            _db, session.session_id, self.user_profile["id"],
                            target, int(card_uid),
                            champions=self._champion_targets(),
                            battle_state=bstate)
                        if not others:
                            # Auto/self templates with no real filter (e.g.
                            # Living Totem "this") fall back to the full
                            # friendly pool; restricting filters (e.g. Prairie
                            # Scout's IsAttacking) keep their empty pool so the
                            # ability is never offered without a valid target.
                            filt = target.card_filter
                            if hasattr(filt, "to_dict"):
                                filt = filt.to_dict()
                            if not filt:
                                others = [r[0] for r in db_card_uids_in_zone(
                                    session.session_id, self.user_profile["id"],
                                    "warzone", conn=_db)]
                        tgt = game._make_event(game_engine.TargetInstanceSessionEventArgs)
                        tgt.target_index = i
                        tgt.target_id = game_engine.ResourceId.from_str(tid)
                        tgt.targets = [game_engine.SessionCardId(game_engine.UID(int(u))) for u in others]
                        inst.target_instances.append(tgt)
                    inst.min_target_counts = min_counts if built else []
                    inst.max_target_counts = max_counts if built else []
                if graph is not None:
                    for cost in ability_cost_targets(
                            graph, _db, session.session_id,
                            self.user_profile["id"], int(card_uid),
                            champions=self._champion_targets(),
                            battle_state=bstate):
                        if cost.is_source_auto_target:
                            continue
                        candidates = cost.candidates
                        if len(candidates) < cost.minimum:
                            continue
                        ci = game._make_event(
                            game_engine.CostInstanceSessionEventArgs)
                        minimum = cost.minimum
                        maximum = cost.maximum
                        if maximum < 0:
                            maximum = len(candidates)
                        HCPHandler._set_cost_instance_bounds(
                            ci, minimum, maximum)
                        ci.cost_type = cost_type_for_kind(cost.kind)
                        ci.target_template_id = game_engine.ResourceId.from_str(
                            cost.guid)
                        ci.targets = [game_engine.SessionCardId(
                            game_engine.UID(int(uid))) for uid in candidates]
                        inst.target_instances.append(ci)
                variable_x, variable_min = HCPHandler._ability_x_cost_metadata(ag)
                if variable_x:
                    ci = game._make_event(game_engine.CostInstanceSessionEventArgs)
                    HCPHandler._set_cost_instance_bounds(ci, variable_min, 0)
                    ci.cost_type = 256  # EAbilityCostType.XCostAbilityCostType
                    ci.target_template_id = game_engine.ResourceId.invalid()
                    ci.targets = []
                    inst.target_instances.append(ci)
                opt.instances.append(inst)
                discard_prompt = self._discard_prompt_data(ag, bstate=bstate)
                if discard_prompt:
                    child_ability, discard_target = discard_prompt
                    hand = [
                        game_engine.SessionCardId(game_engine.UID(int(uid)))
                        for (uid,) in db_card_uids_in_zone(
                            session.session_id, self.user_profile["id"],
                            "hand", conn=_db)]
                    if hand:
                        child = game._make_event(
                            game_engine.OptionInstanceSessionEventArgs)
                        child.opt_id = game_engine.ResourceId.from_str(
                            child_ability)
                        child.target_ids.append(
                            game_engine.ResourceId.from_str(discard_target))
                        child.min_target_counts = [1]
                        child.max_target_counts = [1]
                        child_target = game._make_event(
                            game_engine.TargetInstanceSessionEventArgs)
                        child_target.target_index = 0
                        child_target.target_id = game_engine.ResourceId.from_str(
                            discard_target)
                        child_target.targets = hand
                        child.target_instances.append(child_target)
                        opt.instances.append(child)

    def _prompt_optional_trigger(self, game, pl_t, ai_t, session, bstate,
                                 source_uid, ability_guid, target_template_ids,
                                 candidates, owner_id=None,
                                 trigger_target_uid=None):
        """Prompt a human controller for an authored optional trigger.

        Optional triggers use the same class-39 continuation as triggered
        target choices.  Keeping the packet construction in one path is
        important: an optional trigger may also have an explicit target, in
        which case the client first asks whether to opt in and then opens the
        authored target picker.
        """
        return self._prompt_trigger_targets(
            game, pl_t, ai_t, session, bstate, source_uid, ability_guid,
            target_template_ids, candidates, owner_id=owner_id,
            trigger_target_uid=trigger_target_uid, optional=True)

    def _prompt_trigger_targets(self, game, pl_t, ai_t, session, bstate,
                                source_uid, ability_guid, target_template_ids,
                                candidates, owner_id=None,
                                trigger_target_uid=None, optional=False):
        """Prompt for an optional trigger and/or its explicit targets.

        Mirrors the client's WaitForTriggeredAbilitiesAction: a
        PlayerOptionList carrying the legal candidates (when authored target
        input exists) plus the class-39
        TriggeredAbilityActivationDataRequired event.  The follow-up
        SetAbilityActivationDataTransaction resolves the trigger, or cancels
        it when an optional ability is declined.
        """
        _be = self._checkpoint_engine(session)
        inst_id = int(bstate.get("_next_instance_id", 1))
        bstate["_next_instance_id"] = inst_id + 1
        prompt_owner_id = owner_id
        if prompt_owner_id is None:
            prompt_owner_id = bstate.get("resolving_owner_id")
        if prompt_owner_id is None:
            prompt_owner_id = db_card_owner_id(
                session.session_id, source_uid, conn=_db)
        if prompt_owner_id is None:
            prompt_owner_id = 0
        prompt_owner_id = int(prompt_owner_id)
        # Resolve every explicit target template of this trigger.  The client
        # keys target instances by (card, ability) and clears the stored list
        # for each OptionInstance with the same opt_id (PlayerOptions.cs), so
        # all of them must ride ONE OptionInstance or only the last is offered
        # (Rowdy Ringmaster's Deploy asked for the friendly troop but never the
        # opposing one, so its battle had no defender).  ``candidates`` is the
        # already-computed pool for the first template; additional templates
        # are computed here with the same evaluator.
        all_target_ids = []
        if target_template_ids:
            try:
                all_target_ids = json.loads(
                    db_ability_target_template_ids(
                        ability_guid, conn=_db) or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                all_target_ids = []
        if candidates and all(isinstance(c, (list, tuple, set))
                              for c in candidates):
            candidate_sets = [list(c) for c in candidates]
        elif candidates:
            candidate_sets = [list(candidates)]
        else:
            candidate_sets = []
        entries = []
        for position, tid in enumerate(target_template_ids or ()):
            index = next(
                (i for i, known in enumerate(all_target_ids)
                 if str(known).lower() == str(tid).lower()), position)
            if position < len(candidate_sets) and candidate_sets[position]:
                uids = [int(u) for u in candidate_sets[position]]
            else:
                try:
                    from rules_port.targeting import legal_targets
                    uids = [int(u) for u in legal_targets(
                        _db, session.session_id, prompt_owner_id, tid,
                        source_uid, both_players=True,
                        champions=self._champion_targets(),
                        battle_state=bstate)]
                except Exception:
                    uids = []
            minimum, maximum = self._plan_target_bounds(tid)
            entries.append((index, tid, uids, int(minimum), int(maximum)))
        target_index = entries[0][0] if entries else 0
        bstate["pending_trigger"] = {
            "ability_guid": ability_guid,
            "source_uid": int(source_uid),
            "owner_id": prompt_owner_id,
            "instance_id": inst_id,
            "target_index": target_index,
            "target_template_id": (entries[0][1] if entries
                                   else (target_template_ids or [None])[0]),
            "target_entries": [
                {"index": index, "template_id": tid,
                 "minimum": minimum, "maximum": maximum}
                for index, tid, _uids, minimum, maximum in entries],
            "trigger_target_uid": (None if trigger_target_uid is None
                                   else int(trigger_target_uid)),
            "optional": bool(optional),
        }
        # PvP: the trigger choice lives in the PvP state (not the battle
        # state), and the prompt goes only to the choosing player via the
        # tournament packet path — same pattern as _prompt_deck_search.
        if bstate.get("pvp"):
            from services.tournament_game import (
                pvp_load_state, pvp_save_state, _send_pvp_packet)
            from gamemodes.tournament_engine import player_handlers
            from pvp_db import db_game_session_pids
            pend = bstate["pending_trigger"]
            state = pvp_load_state(session) or {}
            state["pending_trigger"] = pend
            pvp_save_state(session, state)
            pids = db_game_session_pids(session.session_id)
            chooser_pid = int(pend.get("owner_id", 0)) or int(pl_t.uid64 >> 8)
            if chooser_pid not in pids:
                chooser_pid = pids[0] if pids else 0
            opp_pid = [p for p in pids if p != chooser_pid][0] \
                if len(pids) > 1 else 0
            chooser = game_engine.UID.make(244, chooser_pid)
            g2 = game_engine.Game(int(session.session_id), chooser,
                                  game_engine.UID.make(244, opp_pid))
            g2.events = [ev for ev in game.events]
            # The prompt must NOT ride the shared event stream sent to both
            # players by _pvp_play_troop — strip it here so only the chooser
            # sees the option list / class-39 prompt.
            strip_types = (
                "PlayerOptionListSessionEventArgs",
                "TriggeredAbilityActivationDataRequiredSessionEventArgs",
                "GreenLightSessionEventArgs",
            )
            game.events = [ev for ev in game.events
                           if ev.__class__.__name__ not in strip_types]
            h = player_handlers.get(chooser_pid)
            if h is not None:
                # Build the actual private picker packet.  The shared event
                # stream only contains the chain-resolution events; merely
                # labelling this as a prompt leaves PvP clients with no
                # PlayerOptionList or class-39 request to drive the picker.
                ev = g2._make_event(game_engine.PlayerOptionListSessionEventArgs)
                ev.player_id = chooser
                opt = g2._make_event(game_engine.PlayerOptionSessionEventArgs)
                opt.card = game_engine.SessionCardId(game_engine.UID(int(source_uid)))
                opt.state = game_engine.ECardUsage.Activate
                opt_inst = g2._make_event(game_engine.OptionInstanceSessionEventArgs)
                opt_inst.opt_id = game_engine.ResourceId.from_str(ability_guid)
                for index, tid, uids, minimum, maximum in entries:
                    opt_inst.target_ids.append(
                        game_engine.ResourceId.from_str(tid))
                    tgt = g2._make_event(
                        game_engine.TargetInstanceSessionEventArgs)
                    # Preserve the authored index. ConfigureAbility uses it
                    # when it serializes this choice into TargetMap.
                    tgt.target_index = index
                    tgt.target_id = game_engine.ResourceId.from_str(tid)
                    tgt.targets = [game_engine.SessionCardId(
                        game_engine.UID(int(u))) for u in uids]
                    opt_inst.target_instances.append(tgt)
                    opt_inst.min_target_counts.append(minimum)
                    opt_inst.max_target_counts.append(maximum)
                opt.instances.append(opt_inst)
                ev.options.append(opt)
                g2._push(ev)
                ev39 = g2._make_event(
                    game_engine.TriggeredAbilityActivationDataRequiredSessionEventArgs)
                ev39.player_id = chooser
                ev39.ability_instance_ids = [inst_id]
                ev39.ability_template_ids = [
                    game_engine.ResourceId.from_str(ability_guid)]
                ev39.source_card_ids = [
                    game_engine.SessionCardId(game_engine.UID(int(source_uid)))]
                g2._push(ev39)
                g2.push_green_light(chooser, game_engine.EPriorityContext.Normal)
                _send_pvp_packet(h, session, g2, chooser, "trigger")
            log_req(f"    PvP trigger prompt: {ability_guid[:8]} -> pid "
                    f"{chooser_pid} targets={len(entries)}")
            return
        _be.save_state(session, bstate)
        # PlayerOptionList: trigger card + the ability as an option + the
        # legal candidates as a TargetInstance keyed by the target template.
        ev = game._make_event(game_engine.PlayerOptionListSessionEventArgs)
        ev.player_id = pl_t
        opt = game._make_event(game_engine.PlayerOptionSessionEventArgs)
        opt.card = game_engine.SessionCardId(game_engine.UID(int(source_uid)))
        opt.state = game_engine.ECardUsage.Activate
        inst = game._make_event(game_engine.OptionInstanceSessionEventArgs)
        inst.opt_id = game_engine.ResourceId.from_str(ability_guid)
        # ConfigureAbility matches TargetInstances through this parallel list.
        # PvP otherwise receives candidates but cannot open the target picker.
        for index, tid, uids, minimum, maximum in entries:
            inst.target_ids.append(game_engine.ResourceId.from_str(tid))
            tgt = game._make_event(game_engine.TargetInstanceSessionEventArgs)
            # The explicit target may follow auto targets such as "this".
            tgt.target_index = index
            tgt.target_id = game_engine.ResourceId.from_str(tid)
            tgt.targets = [game_engine.SessionCardId(game_engine.UID(int(u)))
                           for u in uids]
            inst.target_instances.append(tgt)
            inst.min_target_counts.append(minimum)
            inst.max_target_counts.append(maximum)
        opt.instances.append(inst)
        ev.options.append(opt)
        game._push(ev)
        # Class 39 — TriggeredAbilityActivationDataRequired.
        ev39 = game_engine.TriggeredAbilityActivationDataRequiredSessionEventArgs()
        ev39.player_id = pl_t
        ev39.ability_instance_ids = [inst_id]
        ev39.ability_template_ids = [game_engine.ResourceId.from_str(ability_guid)]
        ev39.source_card_ids = [game_engine.SessionCardId(game_engine.UID(int(source_uid)))]
        game._push(ev39)
        # The picker needs priority to commit (the client's
        # BattleStateTriggeredAbilities -> BattleStateConfigureAbility flow).
        game.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
        log_req(f"    Trigger target prompt: {ability_guid[:8]} source={hex(source_uid)} "
                f"targets={len(entries)} inst={inst_id}")

    def _queue_conversation_prompt(self, game, session, pl_t, ai_t, bstate,
                                   conversation_id):
        """Publish class-55 conversation UI and persist its BOM continuation.

        The client implementation of ConversationAbilityEffectTemplate does
        not resolve the next effect immediately.  It queues an encounter mod,
        enters WaitForConversationAction, and resumes only after the client
        submits EncounterModDialogTransaction.  Keep that same boundary in
        both battle modes; the PvP state is copied to its tournament-owned
        JSON by the existing prompt pattern.
        """
        _be = self._checkpoint_engine(session)

        parent = bstate.get("_choice_parent") or {}
        ability_guid = str(parent.get("ability_guid") or
                           bstate.get("resolving_ability") or "").lower()
        source_uid = bstate.get("resolving_source_uid")
        owner_id = int(parent.get(
            "owner_id", bstate.get("resolving_owner_id", 0)) or 0)
        # The original client always applies an encounter conversation to its
        # UserPlayer.  In PvE the source can be an AI encounter card, so keep
        # the ability owner for resumption but route the answer to the human
        # controller.
        answer_owner_id = owner_id
        if not bstate.get("pvp"):
            answer_owner_id = int(
                self.user_profile["id"] if self.user_profile else owner_id)
        resume_order = int(parent.get(
            "resume_effect_order",
            int(bstate.get("resolving_effect_order", 0)) + 1))
        if bstate.get("_rules_port_attached"):
            from rules_port.abilities import AbilityContinuation
        else:
            from rules_port.builder import AbilityContinuation
        pending = AbilityContinuation.from_state(
            bstate, ability_guid=ability_guid, source_uid=source_uid,
            owner_id=answer_owner_id,
            target_map=(parent.get("target_map") or
                        bstate.get("ability_target_map") or {}),
            variables=(parent.get("variables") or
                       bstate.get("ability_variables") or {}),
            resume_effect_order=resume_order).to_dict()
        pending.update({"kind": "conversation",
                        "conversation_id": str(conversation_id).lower(),
                        "ability_owner_id": owner_id})
        event_player = pl_t
        if bstate.get("pvp"):
            from services.tournament_game import pvp_load_state, pvp_save_state
            state = pvp_load_state(session) or {}
            state["pending_conversation"] = pending
            state["resolution_paused"] = True
            state["conversation_resume_priority_pid"] = int(
                state.get("priority_pid", answer_owner_id) or answer_owner_id)
            pvp_save_state(session, state)
            event_player = game_engine.UID.make(244, owner_id)
        else:
            bstate["pending_conversation"] = pending
            bstate["resolution_paused"] = True
            _be.save_state(session, bstate)

        event = game._make_event(
            game_engine.EncounterModDialogSessionEventArgs)
        event.player_id = event_player
        event.conversation_template_id = game_engine.ResourceId.from_str(
            str(conversation_id))
        # Conversation UI is controller-private in PvP.  PvE has one human
        # controller, so leaving this unset preserves the normal event path.
        if bstate.get("pvp"):
            event._private_player_uid = event_player
        game._push(event)
        log_req(f"    Conversation queued: {str(conversation_id)[:8]} "
                f"ability={ability_guid[:8]} owner={answer_owner_id}")
        return f"conversation: awaiting {str(conversation_id).lower()}"

    def _apply_fra_battle_modifications(self, game, session, pl_t, ai_t,
                                        bstate, modifications,
                                        player_deck_cards, ai_deck_cards,
                                        drain_setup_stack):
        """Apply round-zero FRA mods through the same live card/rules state.

        Arena's campaign service owns selection and persistence. This method
        projects its typed result into the authoritative PvE session before
        the initial PreGame packet is sent.
        """
        from pvp_db import db_insert_game_card, db_game_cards_at_location_scalar
        from rules_port.resources import apply_resource_change

        owners = {
            0: ("ai", 0, ai_t, ai_deck_cards),
            2: ("player", int(self.user_profile["id"]), pl_t,
                player_deck_cards),
        }
        new_decks = {0: False, 2: False}
        template_cache = {}
        collection_values = {
            "deck": game_engine.ECardCollections.Deck,
            "hand": game_engine.ECardCollections.Hand,
            "warzone": game_engine.ECardCollections.Warzone,
            "castspells": game_engine.ECardCollections.CastSpells,
            "playedresources": game_engine.ECardCollections.PlayedResources,
            "underground": game_engine.ECardCollections.Underground,
            "discard": game_engine.ECardCollections.Discard,
            "crypt": game_engine.ECardCollections.Discard,
            "void": game_engine.ECardCollections.Void,
        }

        def _owner_targets(target):
            target = int(target or 0)
            return (tuple(owners) if target == 1 else
                    (target,) if target in owners else ())

        def _push_player_state():
            game.player_resources = int(bstate.get("player_resources", 0) or 0)
            game.player_total_resources = int(
                bstate.get("player_total_resources", 0) or 0)
            game.player_charges = int(bstate.get("player_charges", 0) or 0)
            game.player_threshold = {
                game_engine.ECardShards(int(color)): int(value or 0)
                for color, value in (bstate.get("player_threshold") or {}).items()
                if int(value or 0) > 0
            }
            game.ai_resources = int(bstate.get("ai_resources", 0) or 0)
            game.ai_total_resources = int(
                bstate.get("ai_total_resources", 0) or 0)
            game.ai_charges = int(bstate.get("ai_charges", 0) or 0)
            game.ai_threshold = {
                game_engine.ECardShards(int(color)): int(value or 0)
                for color, value in (bstate.get("ai_threshold") or {}).items()
                if int(value or 0) > 0
            }

        shared_grant_cache = {}

        def _shared_all_player_grant(ability_guids):
            """Whether one source grants the same permanent aura to both sides.

            EncounterModAddCard targets All by creating one copy per owner.
            For an authored permanent GrantAbility whose auto target spans
            MultiplePlayers, that duplicates the same grant on every target.
            Project a single source for that metadata shape; keep per-owner
            copies for every other card ability.
            """
            key = tuple(dict.fromkeys(
                str(value).lower() for value in (ability_guids or ()) if value))
            if key in shared_grant_cache:
                return shared_grant_cache[key]
            if not key:
                shared_grant_cache[key] = False
                return False
            from gamedata import DEFAULT_RECORD_STORE, ability_graph
            result = True
            for ability_guid in key:
                graph = ability_graph(DEFAULT_RECORD_STORE, ability_guid)
                if graph is None or not graph.effects:
                    result = False
                    break
                for effect in graph.effects:
                    try:
                        target_index = int(effect.target_index)
                    except (TypeError, ValueError):
                        result = False
                        break
                    if (effect.concrete_type != "GrantAbilityEffectTemplate" or
                            str(effect.duration or "").lower() != "permanent" or
                            target_index < 0 or target_index >= len(graph.targets)):
                        result = False
                        break
                    target = graph.targets[target_index]
                    if (not target.is_auto or
                            str(target.player_filter or "").lower() !=
                            "multipleplayers"):
                        result = False
                        break
                if not result:
                    break
            shared_grant_cache[key] = result
            return result

        for modification in modifications or ():
            if int(modification.get("round_to_apply", 0) or 0) != 0:
                continue
            wire_type = str(modification.get("wire_type") or "")
            target_owners = _owner_targets(modification.get("target_player"))
            if not target_owners:
                continue

            if wire_type.endswith("EncounterModAddChampionHealth"):
                amount = int(modification.get("amount", 0) or 0)
                absolute = bool(modification.get("absolute", False))
                for target in target_owners:
                    side = owners[target][0]
                    key = f"{side}_health"
                    old = int(bstate.get(key, getattr(
                        game, f"{side}_health", 20)) or 0)
                    bstate[key] = max(1, amount if absolute else old + amount)
                continue

            if wire_type.endswith("EncounterModAddResource"):
                absolute = bool(modification.get("absolute", False))
                for target in target_owners:
                    side = owners[target][0]
                    total_key = f"{side}_total_resources"
                    current_key = f"{side}_resources"
                    charge_key = f"{side}_charges"
                    total_old = int(bstate.get(total_key, 0) or 0)
                    resource_amount = int(
                        modification.get("max_resource_value", 0) or 0)
                    resource_delta = (resource_amount - total_old
                                      if absolute else resource_amount)
                    apply_resource_change(
                        bstate, side, "totalresource", resource_delta)
                    apply_resource_change(
                        bstate, side, "currentresource", resource_delta)
                    charge_amount = int(
                        modification.get("charge_value", 0) or 0)
                    charge_delta = (charge_amount - int(
                        bstate.get(charge_key, 0) or 0)
                        if absolute else charge_amount)
                    apply_resource_change(
                        bstate, side, "chargepoints", charge_delta)
                    thresholds = modification.get("threshold_values", ())
                    for threshold in thresholds if isinstance(
                            thresholds, (list, tuple)) else ():
                        color = int(threshold.get("color_flags", 0) or 0)
                        desired = int(threshold.get("threshold", 0) or 0)
                        old = int((bstate.get(f"{side}_threshold") or {}).get(
                            color, (bstate.get(f"{side}_threshold") or {}).get(
                                str(color), 0)) or 0)
                        apply_resource_change(
                            bstate, side, "threshold",
                            desired - old if absolute else desired,
                            color=color)
                _push_player_state()
                continue

            if not wire_type.endswith("EncounterModAddCard"):
                # EncounterModDrawCards is applied after the opening hand is
                # dealt; all other unsupported round-zero mod classes remain
                # visible in the shared setup result and log for follow-up.
                if wire_type.endswith("EncounterModDrawCards"):
                    bstate["fra_refill_hands_after_draw"] = True
                else:
                    log_req(f"    FRA encounter mod skipped: {wire_type}")
                continue

            template_guid = str(modification.get("card_guid") or "").lower()
            if not template_guid or not db_template_exists(
                    template_guid, conn=_db):
                log_req(f"    FRA card mod skipped: invalid template {template_guid}")
                continue
            amount = max(0, int(modification.get("amount", 1) or 0))
            collection = str(modification.get("collection", "Deck") or "Deck")
            collection_key = collection.lower()
            location_by_collection = {
                "deck": "deck", "hand": "hand", "warzone": "warzone",
                "castspells": "CastSpells", "playedresources": "PlayedResources",
                "underground": "underground", "discard": "discard",
                "crypt": "discard", "void": "void",
            }
            location = location_by_collection.get(collection_key)
            if location is None:
                log_req(f"    FRA card mod skipped: unsupported collection {collection}")
                continue
            row = db_template_card_info(template_guid, conn=_db)
            if not row:
                log_req(f"    FRA card mod skipped: template row missing {template_guid}")
                continue
            setup_data = self._battle_setup_template_data(
                template_guid, template_cache)
            if setup_data:
                abilities_json, attributes, ability_guids, _threshold = setup_data
            else:
                abilities_json, attributes, ability_guids = "[]", 0, []
            if isinstance(abilities_json, str):
                try:
                    ability_guids = json.loads(abilities_json)
                except (TypeError, ValueError, json.JSONDecodeError):
                    ability_guids = []
            ability_guids = [str(value).lower() for value in
                             (ability_guids or []) if value]
            card_target_owners = target_owners
            if (len(target_owners) > 1 and
                    _shared_all_player_grant(ability_guids)):
                card_target_owners = (target_owners[0],)
                log_req("    FRA shared all-player grant projected once: "
                        f"card={template_guid[:8]} copies={len(target_owners)}")
            card_type_name = str(row[0] or "Unknown")
            card_type = game_engine.card_type_from_db(card_type_name)

            for owner_key in card_target_owners:
                _side, owner_id, owner_uid, deck_cards = owners[owner_key]
                for _ in range(amount):
                    scid = game._new_card_id()
                    card_uid = int(scid.uid.uid64)
                    position = db_zone_card_count(
                        session.session_id, owner_id, location, conn=_db)
                    db_insert_game_card(
                        session.session_id, owner_id, card_uid, template_guid,
                        location, card_type=card_type_name, position=position,
                        abilities_json=json.dumps(ability_guids),
                        attributes=int(attributes or 0), conn=_db)
                    _tpl, _ctype, _name, _cost, _attack, _defense, _gems = \
                        self._card_full_data(game, scid, template_guid)
                    collection_value = collection_values[collection_key]
                    game.push_card_updated(
                        scid, owner_uid, collection_value,
                        card_type, template_id=template_guid,
                        nulling=(location == "deck" or
                                 (location == "underground" and owner_id == 0)))

                    self._dispatch_native_trigger(
                        game, session, pl_t, ai_t, bstate,
                        "CardCreatedEvent", card_uid, owner_id)
                    drain_setup_stack(bstate)

                    if location == "CastSpells":
                        from rules_port.chain_items import (
                            dispatch_card_cast, resolve_card_item)
                        dispatch_card_cast(
                            self, session, game, bstate, pl_t, ai_t,
                            card_uid, owner_id)
                        resolve_card_item(
                            self, session, _db, game, bstate,
                            {"kind": "spell", "source_uid": card_uid,
                             "ability_guids": ability_guids}, pl_t, ai_t)
                        drain_setup_stack(bstate)
                    elif location == "warzone":
                        if card_type & game_engine.ECardTypes.Artifact:
                            game.push_artifact_card_played(scid, owner_uid)
                        elif card_type & game_engine.ECardTypes.Troop:
                            game.push_troop_card_played(scid, owner_uid)
                        self._dispatch_native_trigger(
                            game, session, pl_t, ai_t, bstate,
                            "CardEnteredZoneEvent", card_uid, owner_id,
                            event_source_collection="Deck",
                            event_destination_collection="warzone")
                        drain_setup_stack(bstate)
                    elif location == "deck":
                        game.push_card_updated(
                            scid, owner_uid, game_engine.ECardCollections.Deck,
                            card_type, nulling=True)
                        deck_cards.append(scid)
                        new_decks[owner_key] = True
            if bool(modification.get("shuffle")) and collection_key == "deck":
                import random as _fra_random
                for owner_key in target_owners:
                    _side, owner_id, _owner_uid, deck_cards = owners[owner_key]
                    ordered = db_game_cards_at_location_scalar(
                        session.session_id, "deck", user_id=owner_id, conn=_db)
                    _fra_random.shuffle(ordered)
                    _db.executemany(
                        "UPDATE game_cards SET position=? WHERE session_id=? "
                        "AND card_uid=?",
                        [(index, session.session_id, int(uid))
                         for index, uid in enumerate(ordered)])

            _db.commit()

        # Keep the final authoritative deck list/count in the same setup
        # packet after any challenge-injected cards were appended.
        for owner_key, changed in new_decks.items():
            if not changed:
                continue
            _side, _owner_id, owner_uid, deck_cards = owners[owner_key]
            sleeve = (getattr(self, "_ai_deck_sleeve_guid", "")
                      if owner_key == 0 else
                      getattr(self, "_player_deck_sleeve_guid", ""))
            game.push_deck_created_with_cards(
                owner_uid, deck_cards, sleeve_guid=sleeve)

        _push_player_state()
        for key, uid, template_guid in (
                ("player_health", self._player_champ_scid,
                 getattr(self, "_player_champ_guid", "")),
                ("ai_health", self._ai_champ_scid,
                 getattr(self, "_ai_champ_guid", ""))):
            health = int(bstate.get(key, 20) or 20)
            if key == "player_health":
                game.player_health = health
            else:
                game.ai_health = health
            card_def = game.card_defs.get(uid)
            if card_def is not None:
                card_def.defense = health
            owner_uid = pl_t if key == "player_health" else ai_t
            game.push_card_updated(
                uid, owner_uid, game_engine.ECardCollections.None_,
                game_engine.ECardTypes.Champion, defense=health,
                template_id=template_guid)
        game.push_player_updated(
            pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game.push_player_updated(
            ai_t, champ_id=getattr(self, "_ai_champ_scid", None))

        # The following Mulligan keep transaction builds a fresh state. Save
        # the mod-adjusted start values there so setup bonuses survive it.
        encounter_data = dict(session.encounter_data or {})
        encounter_data["_fra_starting_battle_state"] = {
            key: bstate.get(key) for key in (
                "player_health", "ai_health", "player_resources",
                "player_total_resources", "ai_resources",
                "ai_total_resources", "player_charges", "ai_charges",
                "player_threshold", "ai_threshold")}
        session.encounter_data = encounter_data
        persist = getattr(session, "_persist", None)
        if callable(persist):
            persist()

    def _resolve_pending_conversation(self, session, transaction):
        """Resume a BOM after the client closes class-55 conversation UI."""
        _be = self._checkpoint_engine(session)
        pending_state = _be.load_state(session)
        pending = pending_state.get("pending_conversation")
        conversation_id = extract_resource_guid(
            transaction.inner_bytes, "ConversationId")
        if not pending:
            # FRA challenge conversations are queued directly by battle
            # setup, rather than by a BOM ability continuation.  The stock
            # client only sends the conversation GUID when its authored
            # answer (the row's answer_text) is clicked, so record that as
            # accepting the current challenge before acknowledging the
            # class-55 transaction.
            if conversation_id:
                from services.arena import (
                    record_fra_challenge_conversation_answer)
                if record_fra_challenge_conversation_answer(
                        self.user_profile["id"], conversation_id):
                    log_req(
                        "    FRA challenge answer accepted: "
                        f"{conversation_id[:8]}")
            self._push_transaction_ack(session)
            return True
        if (conversation_id and conversation_id != str(
                pending.get("conversation_id", "")).lower()):
            log_req(f"    Conversation answer rejected: got {conversation_id}, "
                    f"expected {pending.get('conversation_id')}")
            self._push_transaction_ack(session)
            return True
        owner_id = int(pending.get("owner_id", 0) or 0)
        if owner_id != int(self.user_profile["id"] if self.user_profile else 0):
            log_req(f"    Conversation answer rejected for owner {owner_id}")
            self._push_transaction_ack(session)
            return True

        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        pending_state.pop("pending_conversation", None)
        pending_state.pop("resolution_paused", None)
        game = self._fresh_game(session, pl_t, ai_t, pending_state)
        from rules_port.resolution import resolve_port_ability
        ability_owner_id = int(pending.get(
            "ability_owner_id", owner_id) or owner_id)
        resolve_port_ability(
            self, game, session, _db, pl_t, ai_t, pending_state,
            pending.get("ability_guid", ""), pending.get("source_uid"),
            ability_owner_id, target_map={int(k): v for k, v in
                                  (pending.get("target_map") or {}).items()},
            variables=pending.get("variables") or {},
            resume_from_order=int(pending.get("resume_effect_order", 0)),
        )
        _be.save_state(session, pending_state)

        pending_input = (pending_state.get("pending_conversation") or
                         pending_state.get("pending_choice") or
                         pending_state.get("pending_trigger") or
                         pending_state.get("pending_deck_search") or
                         pending_state.get("pending_discard_ability"))
        if pending_input:
            # The prompt helper owns any replacement picker.  The current
            # packet still carries public mutations and/or class 55 itself.
            self._send_battle_events(session, game, pl_t)
            return True

        if _be.stack_empty(pending_state):
            game.push_chain_empty()
            phase = _be.current_phase(pending_state)
            game.push_green_light(
                pl_t, self._priority_context_for(phase, pending_state))
            self._send_battle_events(session, game, pl_t)
            if phase in (game_engine.ETurnPhases.FirstMainPhase,
                         game_engine.ETurnPhases.SecondMainPhase):
                self._push_main_phase_options(session, pl_t, ai_t)
        else:
            game.push_green_light(
                pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
            self._send_battle_events(session, game, pl_t)
        self._push_transaction_ack(session)
        log_req(f"    Conversation resolved: "
                f"{pending.get('conversation_id', '')[:8]}")
        return True

    def _prompt_deck_search(self, game, session, pl_t, ai_t, bstate,
                            ability_guid, source_uid, owner_id, candidates,
                            kind="search", continuation=None):
        """Ask a HUMAN controller to pick which matching deck card a
        "search your deck" effect puts into their hand (e.g. Darkspire
        Priestess's Deathcry).  The AI auto-picks a random match instead.

        Mirrors the client's SearchYourDeck flow: a PlayerOptionList carrying
        every matching deck card plus the class-39
        TriggeredAbilityActivationDataRequired event.  The follow-up
        SetAbilityActivationDataTransaction resolves with the chosen card.

        FRA: the prompt rides on the shared ``game`` event stream.  PvP: the
        prompt is sent only to the choosing player and the pending choice is
        persisted in the PvP state for the tournament transaction handler.
        """
        _be = self._checkpoint_engine(session)
        inst_id = int(bstate.get("_next_instance_id", 1))
        bstate["_next_instance_id"] = inst_id + 1
        # The class-39 prompt must reference the nested SEARCH ability (e.g.
        # 37955055) whose own target template drives the choosing window — the
        # top deathcry's "You" template makes the client find no targets and
        # instantly commit.
        search_ability, search_template = self._deck_search_ability(ability_guid)
        prompt_ability = search_ability or ability_guid
        pend = {
            "ability_guid": prompt_ability,
            "source_uid": int(source_uid),
            "owner_id": int(owner_id),
            "instance_id": inst_id,
            "candidates": [int(u) for u in (candidates or [])],
            "kind": kind,
        }
        if continuation:
            pend["continuation"] = continuation
        # The TargetInstance's target_id MUST be the "search your deck" target
        # template (Choosing in its CollectionFlags + the Darkspire filter) —
        # the client's BattleStateTarget only accepts candidates whose
        # collection is covered by the template.  The deathcry's own template
        # ("You"/Warzone) left the picker with a bare cursor.
        target_id = search_template or prompt_ability

        def build_prompt(g, player_uid):
            # Present temporary candidates in the client's Choosing zone.  The
            # database cards remain in the deck; the follow-up resolver moves
            # every candidate back to the deck view.  The TargetInstance still
            # carries the real search template and candidate IDs, which is
            # what makes this a selectable deck-search prompt rather than a
            # generic hand-card click.
            for cu in pend["candidates"]:
                c_scid = game_engine.SessionCardId(game_engine.UID(int(cu)))
                c_row = db_card_zone_details(
                    session.session_id, int(cu), conn=_db)
                if not c_row:
                    continue
                c_tpl = c_row[0]
                _tpl2, ct2, _n2, cost2, atk2, def2, _g2 = self._card_full_data(
                    g, c_scid, c_tpl, c_row[1])
                g.push_card_updated(c_scid, player_uid,
                                    game_engine.ECardCollections.Choosing, ct2,
                                    template_id=c_tpl, cost=cost2, attack=atk2,
                                    defense=def2)
            ev = g._make_event(game_engine.PlayerOptionListSessionEventArgs)
            ev.player_id = player_uid
            opt = g._make_event(game_engine.PlayerOptionSessionEventArgs)
            opt.card = game_engine.SessionCardId(game_engine.UID(int(source_uid)))
            opt.state = game_engine.ECardUsage.Activate
            inst = g._make_event(game_engine.OptionInstanceSessionEventArgs)
            inst.opt_id = game_engine.ResourceId.from_str(prompt_ability)
            tgt = g._make_event(game_engine.TargetInstanceSessionEventArgs)
            tgt.target_index = 0
            tgt.target_id = game_engine.ResourceId.from_str(target_id)
            # ConfigureAbility matches target instances through this parallel
            # target-template list.  Without it the client can render the
            # candidates but keep the outer BattleStateTarget alive after a
            # selection because it cannot associate the chosen card with the
            # nested search ability's target.
            inst.target_ids.append(game_engine.ResourceId.from_str(target_id))
            tgt.targets = [game_engine.SessionCardId(game_engine.UID(int(u)))
                           for u in pend["candidates"]]
            inst.target_instances.append(tgt)
            inst.min_target_counts = [1]
            inst.max_target_counts = [1]
            opt.instances.append(inst)
            ev.options.append(opt)
            g._push(ev)
            ev39 = game_engine.TriggeredAbilityActivationDataRequiredSessionEventArgs()
            ev39.player_id = player_uid
            ev39.ability_instance_ids = [inst_id]
            ev39.ability_template_ids = [
                game_engine.ResourceId.from_str(prompt_ability)]
            ev39.source_card_ids = [
                game_engine.SessionCardId(game_engine.UID(int(source_uid)))]
            g._push(ev39)
            g.push_green_light(player_uid, game_engine.EPriorityContext.Normal)

        if bstate.get("pvp"):
            from services.tournament_game import (
                pvp_load_state, pvp_save_state, _send_pvp_packet)
            from gamemodes.tournament_engine import player_handlers
            from pvp_db import db_game_session_pids
            state = pvp_load_state(session) or {}
            state["pending_deck_search"] = pend
            pvp_save_state(session, state)
            h = player_handlers.get(int(owner_id))
            pids = db_game_session_pids(session.session_id)
            opp_pid = [p for p in pids if p != int(owner_id)][0]
            chooser = game_engine.UID.make(244, int(owner_id))
            g2 = game_engine.Game(int(session.session_id), chooser,
                                  game_engine.UID.make(244, opp_pid))
            build_prompt(g2, chooser)
            if h is not None:
                _send_pvp_packet(h, session, g2, chooser, "deck-search")
            return (f"deck search: awaiting {len(pend['candidates'])} "
                    f"candidates for pid {owner_id}")
        bstate["pending_deck_search"] = pend
        _be.save_state(session, bstate)
        build_prompt(game, pl_t)
        log_req(f"    Deck-search prompt: {ability_guid[:8]} source={hex(source_uid)} "
                f"candidates={len(pend['candidates'])} inst={inst_id}")
        return f"deck search: awaiting {len(pend['candidates'])} candidates"

    def _prompt_choice_cards(self, game, session, pl_t, ai_t, bstate,
                             pending):
        """Publish the client's built-in ChooseAndPlay card picker.

        DoubleChoice creates real Choice-card instances in the Choosing zone,
        then advertises the built-in ability that plays one of them for free.
        PvP choice cards and their picker are private to the controller; the
        selected card's eventual PlayedResources move is public.
        """
        _be = self._checkpoint_engine(session)
        from abilities.framework.effects.choices import (
            CHOICE_COPY_ABILITY, CHOICE_TARGET_TEMPLATE)
        log_req("    PvP choice prompt DEBUG: "
                f"kind={pending.get('kind')} "
                f"source={pending.get('source_uid')} "
                f"parent={pending.get('parent', {}).get('ability_guid')} "
                f"parent_resume="
                f"{pending.get('parent', {}).get('resume_effect_order')} "
                f"child={pending.get('continuation', {}).get('ability_guid')}",
                level="DEBUG")

        def build_prompt(target_game, player_uid):
            ev = target_game._make_event(
                game_engine.PlayerOptionListSessionEventArgs)
            ev.player_id = player_uid
            opt = target_game._make_event(
                game_engine.PlayerOptionSessionEventArgs)
            opt.card = game_engine.SessionCardId(
                game_engine.UID(int(pending["source_uid"])))
            opt.state = game_engine.ECardUsage.Activate
            inst = target_game._make_event(
                game_engine.OptionInstanceSessionEventArgs)
            inst.opt_id = game_engine.ResourceId.from_str(
                CHOICE_COPY_ABILITY)
            inst.target_ids.append(game_engine.ResourceId.from_str(
                CHOICE_TARGET_TEMPLATE))
            inst.min_target_counts = [1]
            inst.max_target_counts = [1]
            target = target_game._make_event(
                game_engine.TargetInstanceSessionEventArgs)
            target.target_index = 0
            target.target_id = game_engine.ResourceId.from_str(
                CHOICE_TARGET_TEMPLATE)
            target.targets = [game_engine.SessionCardId(game_engine.UID(int(uid)))
                              for uid in pending["choice_uids"]]
            inst.target_instances.append(target)
            opt.instances.append(inst)
            ev.options.append(opt)
            target_game._push(ev)
            # DoubleChoice is resolved by the client's normal triggered-ability
            # configuration flow: PlayerOptionList supplies the generated cards,
            # then class 23 tells UIBattle to push BattleStateUseTriggeredAbility
            # for the built-in ChooseAndPlay ability. Without this event the
            # options only update the cache; no picker state is pushed.
            req = target_game._make_event(
                game_engine.AbilityActivationDataRequiredSessionEventArgs)
            req.player_id = player_uid
            req.ability_instance_id = int(pending.get("instance_id", 1))
            req.ability_parent_id = 0
            req.source_card_id = game_engine.SessionCardId(
                game_engine.UID(int(pending["source_uid"])))
            req.ability_template_id = game_engine.ResourceId.from_str(
                CHOICE_COPY_ABILITY)
            req.effect_group_id = 1
            # ChooseAndPlay has one explicit target effect (the generated
            # card in Choosing).  The client uses this effect-instance list
            # while building BattleStateConfigureAbility; an empty list can
            # leave the class-23 state with no selectable threshold even
            # though the PlayerOptionList contains the two choice cards.
            req.effect_instance_ids = [0]
            req.resolve_chain = False
            target_game._push(req)
            target_game.push_green_light(
                player_uid, game_engine.EPriorityContext.Normal)

        bstate["pending_choice"] = pending
        bstate["resolution_paused"] = True
        if bstate.get("pvp"):
            from services.tournament_game import (
                pvp_load_state, pvp_save_state, _send_pvp_packet)
            from gamemodes.tournament_engine import player_handlers
            from pvp_db import db_game_session_pids
            state = pvp_load_state(session) or {}
            state["pending_choice"] = pending
            state["resolution_paused"] = True
            pvp_save_state(session, state)
            chooser_id = int(pending["owner_id"])
            pids = db_game_session_pids(session.session_id)
            opponent_id = next((pid for pid in pids if int(pid) != chooser_id), 0)
            chooser = game_engine.UID.make(244, chooser_id)
            private = game_engine.Game(
                int(session.session_id), chooser,
                game_engine.UID.make(244, opponent_id))
            choice_ids = ({int(uid) for uid in pending["choice_uids"]} |
                          {int(uid) for uid in
                           (bstate.pop("private_choice_uids", []) or [])})
            # The generated cards and picker are controller-private. Keep the
            # chain-resolution events in the shared game stream.
            private.events = []
            for event in list(game.events):
                card_id = getattr(event, "session_card_id", None)
                card_uid = getattr(getattr(card_id, "uid", None), "uid64", None)
                if card_uid is not None and int(card_uid) in choice_ids:
                    private.events.append(event)
            game.events = [
                event for event in game.events
                if not (
                    event.__class__.__name__ in (
                        "PlayerOptionListSessionEventArgs",
                        "GreenLightSessionEventArgs")
                    or (getattr(getattr(
                        getattr(event, "session_card_id", None), "uid", None),
                        "uid64", None) in choice_ids))]
            build_prompt(private, chooser)
            prompt_handler = player_handlers.get(chooser_id)
            # Tell the opponent this player is choosing a card FIRST, so the
            # picker packet below is the last thing the chooser receives and
            # nothing can cover it.
            from services.tournament_game import _pvp_push_waiting_on
            _pvp_push_waiting_on(session, chooser_id)
            if prompt_handler is not None:
                _send_pvp_packet(prompt_handler, session, private, chooser,
                                 "choice")
            return
        _be.save_state(session, bstate)
        build_prompt(game, pl_t)

    def _resolve_pending_choice(self, session, pl_t, ai_t, inner_bytes,
                                ability_guid=None, typed_payload=None):
        """Play a selected Choice token and resume its parent BOM."""
        _be = self._checkpoint_engine(session)
        from abilities.framework.effects.choices import (
            CHOOSE_AND_PLAY_ABILITY, CHOICE_COPY_ABILITY, extract_card_uids)
        from rules_port.choice_effects import (
            _play_choice_card as play_choice_card,
            _resolve_choice_card_abilities as resolve_choice_card_abilities)
        from rules_port.resolution import (
            resolve_port_ability,
            resume_ability_continuation_parents)
        bstate = _be.load_state(session)
        pending = bstate.get("pending_choice")
        if not pending:
            return False
        if (ability_guid and str(ability_guid).lower() not in (
                CHOOSE_AND_PLAY_ABILITY, CHOICE_COPY_ABILITY)):
            return False
        selected = extract_card_uids(inner_bytes)
        if not selected and isinstance(typed_payload, dict):
            activation = typed_payload.get("activation_data")
            target_map = (activation.get("target_map")
                          if isinstance(activation, dict) else None)
            if isinstance(target_map, dict):
                for target in target_map.values():
                    values = target if isinstance(target, (list, tuple, set)) else (target,)
                    for value in values:
                        try:
                            uid = int(value)
                        except (TypeError, ValueError):
                            continue
                        if (uid & 0xFF) == 1:
                            selected.append(uid)
        chosen_uid = next((uid for uid in reversed(selected)
                           if int(uid) in {
                               int(value) for value in
                               pending.get("choice_uids", [])}), None)
        if chosen_uid is None:
            log_req("    Choice answer invalid: no legal choice card")
            self._push_transaction_ack(session)
            return True
        owner_id = int(pending.get("owner_id", self.user_profile["id"]))
        if owner_id != int(self.user_profile["id"]):
            log_req(f"    Choice answer rejected for owner {owner_id}")
            self._push_transaction_ack(session)
            return True
        bstate.pop("pending_choice", None)
        bstate.pop("resolution_paused", None)
        g = self._fresh_game(session, pl_t, ai_t, bstate)
        if pending.get("kind") == "choice_zone_target":
            # The picker was opened by the authored child target, not by a
            # summon.  Let that child play and resolve the selected token,
            # then resume the enclosing ability after its ActivateAbility
            # effect.  This preserves one picker for all generated options.
            continuation = pending.get("continuation") or {}
            child_guid = str(continuation.get("ability_guid") or "").lower()
            child_source = int(continuation.get(
                "source_uid", pending.get("source_uid", 0)) or 0)
            child_owner = int(continuation.get(
                "owner_id", owner_id) or owner_id)
            child_targets = _numeric_target_map(
                continuation.get("target_map"))
            child_targets[int(continuation.get("target_index", 0))] = \
                int(chosen_uid)
            resolve_port_ability(
                self, g, session, _db, pl_t, ai_t, bstate,
                child_guid, child_source, child_owner,
                target_map=child_targets,
                variables=continuation.get("variables") or {},
                resume_from_order=int(
                    continuation.get("resume_effect_order", 0) or 0))
            parent = pending.get("parent") or {}
            parent_guid = str(parent.get("ability_guid") or "").lower()
            if parent_guid and not bstate.get("resolution_paused"):
                resolve_port_ability(
                    self, g, session, _db, pl_t, ai_t, bstate,
                    parent_guid, parent.get("source_uid"),
                    int(parent.get("owner_id", owner_id) or owner_id),
                    target_map=_numeric_target_map(parent.get("target_map")),
                    variables=parent.get("variables") or {},
                    resume_from_order=int(
                        parent.get("resume_effect_order", 0) or 0))
            # The child and its enclosing activation are resolved.  Release the
            # paused-item hold and mark the chain item's BOM done so the next
            # pass only finishes the card instead of reopening this picker.
            if not bstate.get("resolution_paused"):
                completed = int(bstate.pop(
                    "paused_chain_instance_id", 0) or 0)
                if completed:
                    bstate["completed_chain_instance_id"] = completed
            _be.save_state(session, bstate)
            projected = False
            if bstate.get("pending_choice"):
                self._send_battle_events(session, g, pl_t)
            else:
                completed_chain = self._completed_rules_port_chain(
                    session, bstate)
                if completed_chain is not None:
                    # Publish the selected card's effects before the finish
                    # pass removes the waiting chain item.
                    self._send_battle_events(session, g, pl_t)
                    finished_chain = self._resume_completed_rules_port_chain(
                        session, bstate, completed_chain)
                    if (finished_chain and
                            not bstate.get("resolution_paused")):
                        # Completing the item can expose the next native
                        # chain item (a trigger the chosen card raised) or end
                        # the paused phase; re-project that priority window
                        # instead of leaving the client on a stale pass button.
                        projected = self._advance_rules_port_to_priority(
                            session, pl_t, ai_t, bstate)
                else:
                    # Non-chain choice effects still need the legacy empty
                    # chain/priority projection. A paused RulesPort chain item
                    # emits those after its TopOfChainResolved and
                    # RemovedTopOfChain events in the shared chain resolver.
                    g.push_chain_empty()
                    g.push_green_light(
                        pl_t, game_engine.EPriorityContext.Normal)
                    self._send_battle_events(session, g, pl_t)
                if not projected:
                    if _be.current_phase(bstate) in (
                            game_engine.ETurnPhases.FirstMainPhase,
                            game_engine.ETurnPhases.SecondMainPhase):
                        self._push_main_phase_options(session, pl_t, ai_t)
            if not projected:
                self._push_transaction_ack(session)
            log_req(f"    Choice selected: {hex(int(chosen_uid))} "
                    f"for {child_guid[:8]} target")
            return True
        from rules_port.context import EffectContext
        bstate["_rules_port_attached"] = True
        # Mark the lifecycle phase while TurnStarted triggers resolve so
        # temporary current-resource gains can survive the later Prep refill.
        _previous_phase = bstate.get("phase")
        bstate["phase"] = game_engine.ETurnPhases.StartTurn
        choice_context = EffectContext.from_rules_port(
            g, session, _db, self, pl_t, ai_t, bstate,
            "", ability=None)
        if not play_choice_card(choice_context, chosen_uid, owner_id):
            log_req(f"    Choice answer rejected: {hex(int(chosen_uid))}")
            bstate["pending_choice"] = pending
            bstate["resolution_paused"] = True
            _be.save_state(session, bstate)
            self._push_transaction_ack(session)
            return True
        resolve_choice_card_abilities(
            choice_context, chosen_uid, pending.get("source_uid"), owner_id)
        target_map = _numeric_target_map(pending.get("target_map"))
        resolve_port_ability(
            self, g, session, _db, pl_t, ai_t, bstate,
            pending["ability_guid"], pending.get("source_uid"), owner_id,
            target_map=target_map, variables=pending.get("variables") or {},
            resume_from_order=int(pending.get("resume_effect_order", 0)))
        _be.save_state(session, bstate)
        if bstate.get("pending_choice"):
            # The resumed BOM has opened its second choice. The prompt helper
            # already supplied the new PlayerOptionList and green light.
            self._send_battle_events(session, g, pl_t)
        else:
            g.push_chain_empty()
            g.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
            self._send_battle_events(session, g, pl_t)
            if _be.current_phase(bstate) in (
                    game_engine.ETurnPhases.FirstMainPhase,
                    game_engine.ETurnPhases.SecondMainPhase):
                self._push_main_phase_options(session, pl_t, ai_t)
        self._push_transaction_ack(session)
        log_req(f"    Choice selected: {hex(int(chosen_uid))} "
                f"for {pending['ability_guid'][:8]}")
        return True

    def _push_private_revealed_cards(self, session, bstate, owner_id, rows,
                                     pl_t, ai_t):
        """Send a controller-only CardsRevealed packet for a PvP look effect.

        ``CardsRevealedSessionEventArgs`` is recipient-scoped by delivery, not
        by the ``player_id`` field inside the event.  A shared PvP packet would
        therefore disclose a "look at" effect to the opponent.  The reveal
        visibility comes from the extracted RevealCards effect metadata; this
        helper only handles the protocol delivery and card definitions.
        """
        if not (bstate or {}).get("pvp"):
            return False
        from services.tournament_game import _send_pvp_packet
        from gamemodes.tournament_engine import player_handlers
        chooser_pid = int(owner_id)
        pids = db_game_session_pids(session.session_id)
        if chooser_pid not in pids:
            return False
        opponent_pid = next((p for p in pids if int(p) != chooser_pid), 0)
        chooser = game_engine.UID.make(244, chooser_pid)
        opponent = game_engine.UID.make(244, opponent_pid)
        private = game_engine.Game(int(session.session_id), chooser, opponent)
        ids = []
        positions = []
        for card_uid, position, template_guid, _user_id, card_state in rows:
            scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
            _tpl, ctype, _name, cost, attack, defense, _gem = \
                self._card_full_data(private, scid, template_guid)
            private.push_card_updated(
                scid, chooser, game_engine.ECardCollections.Deck, ctype,
                state=int(card_state or 0), template_id=template_guid,
                cost=cost, attack=attack, defense=defense, nulling=False)
            ids.append(scid)
            positions.append(int(position or 0))
        ev = private._make_event(game_engine.CardsRevealedSessionEventArgs)
        ev.player_id = chooser
        ev.session_card_ids = ids
        ev.collections = [game_engine.ECardCollections.Deck] * len(ids)
        ev.owning_players = [chooser] * len(ids)
        ev.positions = positions
        private._push(ev)
        handler = player_handlers.get(chooser_pid)
        if handler is not None:
            _send_pvp_packet(handler, session, private, chooser,
                             "private-reveal")
        bstate["private_reveal_sent"] = True
        return True

    def _prompt_revealed_choice(self, game, session, pl_t, ai_t, bstate,
                                ability_guid, source_uid, owner_id,
                                candidates, revealed_cards, optional=False,
                                continuation=None):
        """Prompt for an explicit choice from cards just revealed by BOM data.

        Oakhenge's child target is a SourceRevealedTargetTemplate filtered by
        IsTroop.  The client turns the reveal into a selectable target when
        the same packet carries a PlayerOptionList plus the class-23
        activation-data request for the revealing ability instance; it then
        merges the two into one revealed-card picker.  PvP sends the reveal
        and picker only to the revealing player; the shared chain stream is
        stripped of the sensitive class-51 event.
        """
        _be = self._checkpoint_engine(session)
        inst_id = int(bstate.get("_next_instance_id", 1))
        bstate["_next_instance_id"] = inst_id + 1
        pend = {
            "ability_guid": str(ability_guid),
            "source_uid": int(source_uid),
            "owner_id": int(owner_id),
            "instance_id": inst_id,
            "candidates": [int(u) for u in (candidates or [])],
            "revealed_cards": [int(u) for u in (revealed_cards or [])],
            "kind": "revealed_card",
            "optional": bool(optional),
        }
        if continuation:
            pend["continuation"] = continuation
        # The continuation must follow the authored child effect.  In
        # particular, Starsphere moves the selected card to the top of the
        # deck; Oakhenge-style effects move their selected card to hand.
        move_row = next((row[2] for row in db_ability_effect_rows(
            ability_guid, conn=_db)
                         if row[1] == "MoveCardToZoneEffectTemplate"), None)
        try:
            move_spec = json.loads(move_row or "{}") if move_row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            move_spec = {}
        pend["move_destination"] = str(
            move_spec.get("destination") or "Hand").lower()
        pend["move_location"] = str(
            move_spec.get("location") or "").lower()

        def build_prompt(g, player_uid):
            rows = db_card_positions(
                session.session_id, pend["revealed_cards"], conn=_db)
            # The client only turns a reveal into a picker when the same
            # packet carries the class-23 activation-data request for the
            # revealing ability instance: NetworkPacketSessionEventArgs.
            # CombineRevealAndChoose drops the plain Coverflow and turns the
            # revealed cards that are not legal targets into the picker's
            # AdditionalTargets.  Without the shared instance id the client
            # keeps the informational Coverflow -- whose OK button cannot
            # select a card -- and no troop picker ever opens.
            #
            # The reveal leaf normally already emitted CardsRevealed into this
            # same Game/packet, so reuse that event instead of queueing a
            # second Coverflow.  A controller-private PvP reveal is delivered
            # in its own packet and is not repeated here.
            if not (bstate or {}).get("private_reveal_sent"):
                revealed_ids = {int(u) for u in pend["revealed_cards"]}
                reveal_event = None
                for event in getattr(g, "events", ()):
                    if not isinstance(
                            event,
                            game_engine.CardsRevealedSessionEventArgs):
                        continue
                    event_ids = {
                        int(card.uid.uid64)
                        for card in (event.session_card_ids or [])
                    }
                    if event_ids == revealed_ids:
                        reveal_event = event
                        break
                if reveal_event is None:
                    reveal_event = g._make_event(
                        game_engine.CardsRevealedSessionEventArgs)
                    reveal_event.player_id = player_uid
                    reveal_event.session_card_ids = [
                        game_engine.SessionCardId(game_engine.UID(int(cu)))
                        for cu, _pos in rows]
                    reveal_event.collections = (
                        [game_engine.ECardCollections.Deck] * len(rows))
                    reveal_event.owning_players = [player_uid] * len(rows)
                    reveal_event.positions = [
                        int(pos or 0) for _cu, pos in rows]
                    g._push(reveal_event)
                reveal_event.ability_instance_id = inst_id

            opt_list = g._make_event(
                game_engine.PlayerOptionListSessionEventArgs)
            opt_list.player_id = player_uid
            opt = g._make_event(game_engine.PlayerOptionSessionEventArgs)
            opt.card = game_engine.SessionCardId(
                game_engine.UID(int(source_uid)))
            opt.state = game_engine.ECardUsage.Activate
            inst = g._make_event(game_engine.OptionInstanceSessionEventArgs)
            inst.opt_id = game_engine.ResourceId.from_str(ability_guid)
            target_ids_json = db_ability_target_template_ids(
                ability_guid, conn=_db)
            try:
                target_id = json.loads(target_ids_json or "[]")[0]
            except (TypeError, ValueError, IndexError, json.JSONDecodeError):
                target_id = "00000000-0000-0000-0000-000000000000"
            inst.target_ids.append(game_engine.ResourceId.from_str(target_id))
            tgt = g._make_event(game_engine.TargetInstanceSessionEventArgs)
            tgt.target_index = 0
            tgt.target_id = game_engine.ResourceId.from_str(target_id)
            tgt.targets = [
                game_engine.SessionCardId(game_engine.UID(int(u)))
                for u in pend["candidates"]]
            inst.target_instances.append(tgt)
            inst.min_target_counts = [0 if optional else 1]
            inst.max_target_counts = [1]
            opt.instances.append(inst)
            opt_list.options.append(opt)
            g._push(opt_list)

            # Class 23 is the client's activation-data picker for one ability
            # instance (UIBattle.OnAbilityActivationDataRequested pushes
            # BattleStateUseTriggeredAbility -> BattleStateConfigureAbility).
            # The class-39 triggered-ability request would instead show the
            # triggered-ability stack and answer with a different transaction.
            req = g._make_event(
                game_engine.AbilityActivationDataRequiredSessionEventArgs)
            req.player_id = player_uid
            req.ability_instance_id = inst_id
            req.ability_parent_id = 0
            req.source_card_id = game_engine.SessionCardId(
                game_engine.UID(int(source_uid)))
            req.ability_template_id = game_engine.ResourceId.from_str(
                ability_guid)
            req.effect_group_id = 1
            # Read back by the client only as the picker's effect scope; the
            # revealed-card choice is the ability's first effect instance.
            req.effect_instance_ids = [0]
            req.resolve_chain = False
            g._push(req)
            g.push_green_light(player_uid, game_engine.EPriorityContext.Normal)

        if bstate.get("pvp"):
            from services.tournament_game import (
                pvp_load_state, pvp_save_state, _send_pvp_packet)
            from gamemodes.tournament_engine import player_handlers
            from pvp_db import db_game_session_pids
            state = pvp_load_state(session) or {}
            state["pending_deck_search"] = pend
            pvp_save_state(session, state)
            pids = db_game_session_pids(session.session_id)
            chooser_pid = int(owner_id)
            opp_pid = [p for p in pids if p != chooser_pid][0] \
                if len(pids) > 1 else 0
            chooser = game_engine.UID.make(244, chooser_pid)
            private = game_engine.Game(
                int(session.session_id), chooser,
                game_engine.UID.make(244, opp_pid))
            build_prompt(private, chooser)
            # The reveal was initially appended to the shared resolution
            # game. Remove it before the caller broadcasts that game.
            game.events = [ev for ev in game.events
                           if ev.__class__.__name__ !=
                           "CardsRevealedSessionEventArgs"]
            h = player_handlers.get(chooser_pid)
            if h is not None:
                _send_pvp_packet(h, session, private, chooser,
                                 "revealed-choice")
            log_req(f"    PvP revealed-choice prompt: {ability_guid[:8]} "
                    f"-> pid {chooser_pid} candidates={len(candidates or [])}")
            return "revealed choice: awaiting player"

        bstate["pending_deck_search"] = pend
        _be.save_state(session, bstate)
        build_prompt(game, pl_t)
        log_req(f"    Revealed-choice prompt: {ability_guid[:8]} "
                f"source={hex(source_uid)} candidates={len(candidates or [])}")
        return "revealed choice: awaiting player"

    def _resolve_pending_trigger_target(self, session, pl_t, ai_t, inner_bytes,
                                        ability_guid=None):
        """Handle the player's target choice for a triggered ability with
        explicit targets (e.g. Solitary Exile's Deploy "Void another target
        card").

        The client answers the class-39 prompt with a SetAbilityActivationData
        transaction, which on the wire looks like an AbilityActivationData
        transaction — so this is called from BOTH the ability-activation branch
        and the set-ability-data branch.  Extracts the chosen card from the
        TargetMap, pushes the trigger onto the chain, and resolves it with that
        target.  Returns True when a pending trigger was consumed.
        """
        _be = self._checkpoint_engine(session)
        from abilities.framework._shared import owner_uid
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        pend = bstate.get("pending_trigger")
        if not pend:
            return False
        if ability_guid and str(pend.get("ability_guid", "")).lower() != str(ability_guid).lower():
            return False
        chosen_uid = pend.get("selected_uid")
        if isinstance(inner_bytes, bytes):
            for m_du in re.finditer(
                    rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
                    inner_bytes):
                try:
                    uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                    if (uid64 & 0xFF) == 1:
                        chosen_uid = int(uid64)
                except Exception:
                    continue
        bstate.pop("pending_trigger")
        ag = pend["ability_guid"]
        src = int(pend["source_uid"])
        inst_id = int(pend["instance_id"])
        port = getattr(session, "_rules_port_session", None)
        if port is not None:
            owner_id = int(pend.get("owner_id", 0) or 0)
            target_index = int(pend.get("target_index", 0) or 0)

            def normalize_target_map(raw):
                """Normalize a decoded TargetMap to ``{index: [uids]}``.

                The activation record may carry ``{index: [uid, ...]}`` or
                ``{index: {"session_card_ids": [...]}}``; a triggered ability
                with two chosen targets must forward both so the resolver's
                authored target indexes line up.
                """
                result = {}
                if not isinstance(raw, Mapping):
                    return result
                for key, value in raw.items():
                    try:
                        index = int(key)
                    except (TypeError, ValueError):
                        continue
                    values = value
                    if isinstance(values, Mapping):
                        values = (values.get("session_card_ids") or
                                  values.get("SessionCardIds") or ())
                    if not isinstance(values, (list, tuple, set)):
                        values = (values,) if values is not None else ()
                    uids = []
                    for item in values:
                        while isinstance(item, Mapping):
                            nested = next((item[field] for field in (
                                "uid64", "UID", "m_UID64", "value")
                                if field in item), None)
                            if nested is None or nested is item:
                                break
                            item = nested
                        try:
                            uid = int(item)
                        except (TypeError, ValueError):
                            continue
                        if uid > 0 and (uid & 0xFF) == 1:
                            uids.append(uid)
                    if uids:
                        result[index] = uids
                return result

            activation_data = pend.get("activation_data")
            activation_data = (dict(activation_data)
                               if isinstance(activation_data, Mapping) else {})
            target_map = normalize_target_map(
                activation_data.get("target_map"))
            # ``chosen_uid`` is the single-pick legacy signal; only fill its
            # slot when the fuller TargetMap did not already name it.
            if chosen_uid is not None and target_index not in target_map:
                target_map[target_index] = [int(chosen_uid)]
            if chosen_uid is None:
                primary = target_map.get(target_index)
                if not primary:
                    primary = next(iter(target_map.values()), None)
                if primary:
                    chosen_uid = int(primary[-1])
            activation_data["target_map"] = {
                str(index): uids for index, uids in target_map.items()}
            selected_uids = [uid for uids in target_map.values()
                             for uid in uids]
            descriptor = {
                "kind": "trigger",
                "ability_guid": ag,
                "source_uid": src,
                "source_owner_uid": owner_id,
                "target_uid": (None if chosen_uid is None
                               else int(chosen_uid)),
                "trigger_target_uid": pend.get("trigger_target_uid"),
                "activation_data": activation_data,
                "instance_id": inst_id,
            }
            queue_chain = getattr(port, "queue_projected_chain", None)
            if not callable(queue_chain):
                return False
            queue_chain(
                descriptor, owner_id,
                first_player_id=(getattr(port, "active_player_id", None)
                                 or pl_t))
            _be.save_state(session, bstate)
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            from gamedata import DEFAULT_RECORD_STORE, ability_graph
            graph = ability_graph(DEFAULT_RECORD_STORE, str(ag).lower())
            target_cards = [game_engine.SessionCardId(
                game_engine.UID(int(uid))) for uid in selected_uids]
            g.push_ability_on_chain(
                game_engine.SessionCardId(game_engine.UID(src)),
                game_engine.ResourceId.from_str(ag),
                ability_instance_id=inst_id,
                target_card_ids=target_cards,
                ignores_chain=bool(graph and graph.ignores_chain))
            event_sink = getattr(port, "event_sink", None)
            if event_sink is not None:
                # ``queue_projected_chain`` starts APNAP priority with the
                # active player, which is commonly the Practice AI here.  Do
                # not project a human GreenLight until that server-owned
                # response has gone through the same native pass/drive path
                # used by ordinary chain continuations.
                self._advance_rules_port_to_priority(
                    session, pl_t, ai_t, bstate)
            else:
                g.push_green_light(
                    pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
                self._send_battle_events(session, g, pl_t)
            self._push_transaction_ack(session)
            log_req(f"    RulesPort trigger target chosen: {ag[:8]} "
                    f"target={hex(chosen_uid) if chosen_uid else 'none'}")
            return True
        _be.stack_push(bstate, {
            "kind": "trigger", "ability_guid": ag,
            "source_uid": src, "target_uid": chosen_uid,
            "instance_id": inst_id,
        })
        _be.save_state(session, bstate)
        g = self._fresh_game(session, pl_t, ai_t, bstate)
        g.push_ability_on_chain(
            game_engine.SessionCardId(game_engine.UID(src)),
            game_engine.ResourceId.from_str(ag),
            ability_instance_id=inst_id)
        g.push_green_light(pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
        self._send_battle_events(session, g, pl_t)
        self._push_transaction_ack(session)
        log_req(f"    Trigger target chosen: {ag[:8]} "
                f"target={hex(chosen_uid) if chosen_uid else 'none'}")
        return True

    def _resolve_rules_port_trigger_continuation(self, session, transaction):
        """Consume a typed RulesPort trigger-target continuation."""
        payload = transaction.payload.get("activation_data", {}) or {}
        _be = self._checkpoint_engine(session)
        state = _be.load_state(session)
        pending = state.get("pending_trigger")
        if not pending:
            # The class-39 "choose a card in your deck" picker shares this
            # continuation envelope.  Its resolver decodes the chosen card
            # from the original transaction bytes.
            if state.get("pending_deck_search"):
                original = getattr(
                    session, "_rules_port_dispatch_command", None)
                inner_bytes = (getattr(original, "inner_bytes", b"")
                               if original is not None else b"")
                pl_t = game_engine.UID.make(244, int(self.client_reck_id))
                ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
                handled = bool(self._resolve_pending_deck_search(
                    session, pl_t, ai_t, inner_bytes))
                if handled:
                    session._rules_port_mutation_emitted = True
                return handled
            return False

        def activation_record(value):
            """Select this prompt's AbilityActivationData record."""
            values = value if isinstance(value, (list, tuple)) else (value,)
            first = None
            wanted_instance = int(pending.get("instance_id", 0) or 0)
            wanted_guid = str(pending.get("ability_guid") or "").lower()
            for candidate in values:
                if not isinstance(candidate, Mapping):
                    continue
                if first is None:
                    first = candidate
                try:
                    instance = int(candidate.get(
                        "ability_instance_id", candidate.get("instance_id", 0)) or 0)
                except (TypeError, ValueError):
                    instance = 0
                guid = str(candidate.get("ability_template_id",
                                        candidate.get("ability_guid", "")) or "").lower()
                if ((wanted_instance and instance == wanted_instance) or
                        (wanted_guid and guid == wanted_guid)):
                    return dict(candidate)
            return dict(first or {})

        def flag(value, default=False):
            if value is None:
                return default
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)

        activation = activation_record(payload)
        if pending.get("optional"):
            # The client sets either OptedIn=false or Disable=true when the
            # optional trigger is declined. Missing fields are accepted for
            # older clients that submit the target envelope without the opt-in
            # members; an explicit false is the only decline signal.
            declined = (flag(activation.get("disable")) or
                        ("opted" in activation and
                         not flag(activation.get("opted"))))
            pending["activation_data"] = activation
            if declined:
                instance_id = int(pending.get("instance_id", 1) or 1)
                state.pop("pending_trigger", None)
                _be.save_state(session, state)
                pl_t = game_engine.UID.make(244, int(self.client_reck_id))
                ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
                game = self._fresh_game(session, pl_t, ai_t, state)
                # This RulesPort bridge handles Practice/PvE prompts, where
                # pending.owner_id is the raw game_cards.user_id, not the
                # ServicePlayer UID instance. The prompted controller is the
                # connected player, so use its already-canonical protocol UID.
                game.push_ability_cancelled(instance_id, pl_t)
                port = getattr(session, "_rules_port_session", None)
                event_sink = getattr(port, "event_sink", None)
                native_scheduler = port is not None and event_sink is not None
                if native_scheduler:
                    # The trigger prompt temporarily owns the client UI, but
                    # its answer must return through the native scheduler.
                    # In Practice the active AI is first in the response
                    # queue; let it pass before projecting the next human
                    # window instead of sending a GreenLight for a player
                    # that does not yet own native priority.
                    self._advance_rules_port_to_priority(
                        session, pl_t, ai_t, state)
                else:
                    if _be.stack_empty(state):
                        game.push_chain_empty()
                        phase = _be.current_phase(state)
                        game.push_green_light(
                            pl_t, self._priority_context_for(phase, state))
                    else:
                        game.push_green_light(
                            pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
                    self._send_battle_events(session, game, pl_t)
                self._push_transaction_ack(session)
                if (not native_scheduler and _be.stack_empty(state) and
                        _be.current_phase(state) in (
                            game_engine.ETurnPhases.FirstMainPhase,
                            game_engine.ETurnPhases.SecondMainPhase)):
                    self._push_main_phase_options(session, pl_t, ai_t)
                log_req(f"    Optional trigger declined: "
                        f"{pending.get('ability_guid', '')[:8]}")
                session._rules_port_mutation_emitted = True
                return True
            pending.pop("optional", None)
            _be.save_state(session, state)

        # Forward the whole decoded activation record (including the complete
        # TargetMap) so a triggered ability that advertises more than one
        # chosen target resolves with every authored index, not just the
        # primary slot.
        if activation:
            pending["activation_data"] = dict(activation)
            _be.save_state(session, state)

        found = []
        def walk(value):
            if isinstance(value, Mapping):
                for item in value.values():
                    walk(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item)
            else:
                try:
                    number = int(value)
                    if number > 0 and (number & 0xFF) == 1:
                        found.append(number)
                except (TypeError, ValueError):
                    pass

        # Trigger pickers retain the authored target index, which can be
        # nonzero when earlier target slots are auto-filled (for example,
        # target 0 is "this" and the player selects target 1). Prefer that
        # slot so unrelated activation values cannot be mistaken for the
        # selected card.
        target_index = int(pending.get("target_index", 0) or 0)
        target_map = (activation.get("target_map", {})
                      if isinstance(activation, Mapping) else {})
        if isinstance(target_map, Mapping):
            selected = target_map.get(
                target_index, target_map.get(str(target_index)))
            walk(selected)
        if not found:
            walk(activation if activation else payload)
        if found:
            pending["selected_uid"] = found[-1]
            _be.save_state(session, state)
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        handled = self._resolve_pending_trigger_target(
            session, pl_t, ai_t, b"",
            ability_guid=pending.get("ability_guid"))
        if handled:
            session._rules_port_mutation_emitted = True
        return handled

    def _resolve_rules_port_discard_continuation(self, session, transaction):
        """Consume a typed RulesPort class-23 discard continuation."""
        activation_data = transaction.payload.get("activation_data", {}) or {}
        target_map = (activation_data.get("target_map", {})
                      if isinstance(activation_data, Mapping) else {})

        def target_card_uids(values):
            if isinstance(values, Mapping):
                values = (values.get("session_card_ids") or
                          values.get("SessionCardIds") or ())
            if not isinstance(values, (list, tuple, set)):
                values = (values,) if values is not None else ()
            result = []
            for value in values:
                while isinstance(value, Mapping):
                    nested = next((value[key] for key in (
                        "uid64", "UID", "m_UID64", "value")
                        if key in value), None)
                    if nested is None or nested is value:
                        break
                    value = nested
                try:
                    uid = int(cast(Any, value))
                except (TypeError, ValueError):
                    continue
                if uid > 0 and (uid & 0xFF) == 1:
                    result.append(uid)
            return result

        _be = self._checkpoint_engine(session)
        state = _be.load_state(session)
        native_continuation = state.get("pending_discard_continuation")
        if native_continuation:
            from gamedata import DEFAULT_RECORD_STORE, ability_graph
            from rules_port.resolution import resolve_port_ability
            parent = dict(native_continuation)
            guid = str(parent.get("ability_guid") or "").lower()
            graph = ability_graph(DEFAULT_RECORD_STORE, guid)
            if graph is None:
                raise RuntimeError(
                    f"RulesPort discard continuation ability missing: {guid}")
            target_guid = str(
                state.get("pending_discard_target_template") or "").lower()
            target_index = next((index for index, target in enumerate(
                graph.targets) if str(target.guid).lower() == target_guid), None)
            if target_index is None:
                raise RuntimeError(
                    "RulesPort discard continuation target missing: " +
                    target_guid)

            # AbilityActivationData.TargetMap stores choices by the target's
            # metadata index. Do not recursively collect every card UID in the
            # envelope: that also sees SourceCardId and can mistake the
            # Stargazer itself (or another unrelated card) for the discard.
            selected = (target_map.get(target_index,
                                       target_map.get(str(target_index), ()))
                        if isinstance(target_map, Mapping) else ())
            selected_uids = target_card_uids(selected)

            from pvp_db import db_rules_port_hand_card
            hand_owner_id = (self.user_profile.get("id", 0)
                             if isinstance(self.user_profile, dict) else 0)

            def owned_hand_cards(uids):
                valid = []
                for uid in dict.fromkeys(uids):
                    row = db_rules_port_hand_card(
                        session.session_id, uid, hand_owner_id)
                    if row:
                        valid.append((uid, row))
                return valid

            valid_targets = owned_hand_cards(selected_uids)
            wire_uids = ()
            if not selected_uids:
                # Some Mono builds omit TargetMap from the decoded wrapper but
                # still serialize the selected SessionCardId in the original
                # SetAbilityActivationData payload. Recover only explicitly
                # labelled card UIDs, exclude the source, and accept the
                # fallback only when it names exactly one card in this
                # authenticated player's hand.
                original = getattr(
                    session, "_rules_port_dispatch_command", None)
                raw = getattr(original, "inner_bytes", b"")
                from rules_port.wire import extract_session_card_uids
                wire_uids = extract_session_card_uids(
                    raw, exclude=(parent.get("source_uid", 0),))
                valid_targets = owned_hand_cards(wire_uids)
            submitted_uids = selected_uids or list(wire_uids)
            if len(submitted_uids) != 1 or len(valid_targets) != 1:
                log_req(
                    "    RulesPort discard continuation rejected: "
                    f"target_index={target_index} selected={selected_uids} "
                    f"wire_cards={list(wire_uids)} "
                    f"valid_hand_cards={[uid for uid, _row in valid_targets]}")
                return False
            chosen_uid, _row = valid_targets[0]

            target_map = dict(parent.get("target_map") or {})
            target_map[target_index] = (chosen_uid,)
            state.pop("pending_discard_continuation", None)
            state.pop("pending_discard_continuation_instance_id", None)
            for key in ("pending_discard_ability", "pending_discard_ability_instance_id",
                        "pending_discard_target_template", "pending_discard_scid"):
                state.pop(key, None)
            state.pop("resolution_paused", None)
            state["rules_port_resume_effect_order"] = int(
                parent.get("resume_effect_order", 0) or 0)
            _be.save_state(session, state)
            pl_t = game_engine.UID.make(244, int(self.client_reck_id))
            ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
            game = self._fresh_game(session, pl_t, ai_t, state)
            result = resolve_port_ability(
                self, game, session, _db, pl_t, ai_t, state, guid,
                parent.get("source_uid"), int(parent.get("owner_id", 0) or 0),
                target_map=target_map,
                variables=parent.get("variables") or {},
                resume_from_order=int(parent.get("resume_effect_order", 0) or 0),
                instance_id=int(native_continuation.get(
                    "instance_id", 1) or 1))
            from rules_port.resolution import \
                resume_ability_continuation_parents
            resume_ability_continuation_parents(
                self, game, session, _db, pl_t, ai_t, state,
                native_continuation)
            _be.save_state(session, state)
            completed_chain = self._completed_rules_port_chain(session, state)
            if completed_chain is None and not state.get("resolution_paused"):
                game.push_green_light(
                    pl_t, game_engine.EPriorityContext.Normal)
            self._send_battle_events(session, game, pl_t)
            finished_chain = False
            if completed_chain is not None:
                finished_chain = self._resume_completed_rules_port_chain(
                    session, state, completed_chain)
            session._rules_port_mutation_emitted = True
            log_req(f"    RulesPort discard continuation: {chosen_uid} -> {result}")
            # The Deathcry that opened this picker fired during the AI's turn
            # (Bloatcap's "each opposing champion chooses and discards a
            # card"), which paused its phase loop so the picker survived.
            # Resume that turn where it stopped, exactly like an answered
            # deck-search prompt.
            ai_idx = state.get("ai_turn_phase_idx")
            if ai_idx is not None and not state.get("resolution_paused"):
                self._push_transaction_ack(session)
                state.pop("ai_turn_phase_idx", None)
                _be.save_state(session, state)
                log_req(f"    Discard answered — resuming AI turn at idx {ai_idx}")
                self._run_ai_turn(session, pl_t, ai_t, state,
                                  start_idx=int(ai_idx))
                return True
            # A picker continuation that finishes its chain item can expose
            # the next native chain item (a trigger the draw/discard raised,
            # e.g. Mysterious Rune's "when a resource enters your hand") or
            # end the paused phase.  Re-project the native priority window;
            # an empty acknowledgement left the client on a Normal pass
            # button while the trigger stayed on the chain.
            if not (finished_chain and not state.get("resolution_paused") and
                    self._advance_rules_port_to_priority(
                        session, pl_t, ai_t, state)):
                self._push_transaction_ack(session)
            return True
        if not state.get("pending_discard_ability"):
            return False
        legacy_uids = []
        if isinstance(target_map, Mapping):
            for selected in target_map.values():
                legacy_uids.extend(target_card_uids(selected))
        if not legacy_uids:
            original = getattr(
                session, "_rules_port_dispatch_command", None)
            from rules_port.wire import extract_session_card_uids
            legacy_uids = list(extract_session_card_uids(
                getattr(original, "inner_bytes", b""),
                exclude=(state.get("resolving_source_uid", 0),)))
        hand_owner_id = (self.user_profile.get("id", 0)
                         if isinstance(self.user_profile, dict) else 0)
        valid_legacy_uids = []
        from pvp_db import db_rules_port_hand_card
        for uid in dict.fromkeys(legacy_uids):
            if db_rules_port_hand_card(
                    session.session_id, uid, hand_owner_id):
                valid_legacy_uids.append(uid)
        if len(legacy_uids) == 1 and len(valid_legacy_uids) == 1:
            state["pending_discard_scid"] = valid_legacy_uids[0]
            _be.save_state(session, state)
        else:
            log_req(
                "    RulesPort legacy discard continuation rejected: "
                f"selected={legacy_uids} valid_hand_cards={valid_legacy_uids}")
            return False
        handled = self._handle_set_ability_data_transaction(
            session, type("_Continuation", (), {"inner_bytes": b""})())
        # The continuation resolver already emitted the authoritative card and
        # priority events.  Mark that fact so the outer RulesPort dispatcher
        # does not broadcast the same projection a second time.
        if handled:
            session._rules_port_mutation_emitted = True
        return handled

    def _resolve_pending_deck_search(self, session, pl_t, ai_t, inner_bytes,
                                     ability_guid=None):
        """Handle the player's card choice from a "search your deck" class-39
        prompt (e.g. Darkspire Priestess's Deathcry): move the chosen matching
        deck card into the controller's hand and re-grant priority.

        The client answers with a SetAbilityActivationDataTransaction that
        carries the deathcry's ability GUID — route it here BEFORE the manual
        troop / champion ability paths so it never activates a champion power
        instead (the bug that put Poca on the chain)."""
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        pend = bstate.get("pending_deck_search")
        if not pend:
            return False
        if ability_guid and str(pend.get("ability_guid", "")).lower() != str(ability_guid).lower():
            return False
        chosen_uid = None
        if isinstance(inner_bytes, bytes):
            for m_du in re.finditer(
                    rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
                    inner_bytes):
                try:
                    uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                    if (uid64 & 0xFF) == 1:
                        chosen_uid = int(uid64)
                except Exception:
                    continue
        if pend.get("kind") in ("revealed_troop", "revealed_card"):
            return self._resolve_pending_revealed_choice(
                session, pl_t, ai_t, bstate, pend, chosen_uid)
        bstate.pop("pending_deck_search")
        if pend.get("kind") == "shard":
            # Shards of Fate ("Choose a Standard resource in your deck. Gain
            # the thresholds it provides."): the chosen shard STAYS in the
            # deck — only its threshold is gained.
            return self._resolve_pending_shard_choice(
                session, pl_t, ai_t, bstate, pend, chosen_uid, inner_bytes)
        if pend.get("kind") == "matching_target":
            return self._resolve_pending_matching_target(
                session, pl_t, ai_t, bstate, pend, chosen_uid)
        if chosen_uid and chosen_uid in pend["candidates"]:
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            if bstate.get("_rules_port_attached"):
                # A pending picker is a continuation of the original native
                # ability.  Keep the actual zone mutation in RulesPort; this
                # handler is only responsible for decoding the client's
                # choice and projecting the resulting events.
                from rules_port.context import EffectContext
                from rules_port.deck_effects import move_deck_card_to_hand
                native_context = EffectContext.from_rules_port(
                    g, session, _db, self, pl_t, ai_t, bstate,
                    str(pend.get("ability_guid") or "").lower())
                move_deck_card_to_hand(
                    native_context, chosen_uid, pend["owner_id"])
            else:
                from abilities.framework.effects.search import move_deck_card_to_hand
                move_deck_card_to_hand(g, session, _db, self, pl_t, ai_t,
                                       chosen_uid, pend["owner_id"], bstate)
            # Hide the unchosen candidates again (back to the face-down deck)
            # so the Choosing zone empties when the pick resolves.
            self._hide_candidates_to_deck(
                g, session, pl_t, ai_t,
                [cu for cu in pend["candidates"] if int(cu) != int(chosen_uid)])
            self._send_battle_events(session, g, pl_t)
            log_req(f"    Deck-search chosen: {hex(chosen_uid)} -> hand "
                    f"(owner {pend['owner_id']})")
            # Resume the turn that paused for this prompt (the deathcry fired
            # during combat).  The AI turn resumes where it stopped; a player
            # combat pause advances off AssignDamage; otherwise just re-grant
            # priority as a normal triggered prompt would.
            ai_idx = bstate.get("ai_turn_phase_idx")
            if ai_idx is not None:
                bstate.pop("ai_turn_phase_idx", None)
                _be.save_state(session, bstate)
                log_req(f"    Deck-search answered — resuming AI turn at idx {ai_idx}")
                self._run_ai_turn(session, pl_t, ai_t, bstate, start_idx=ai_idx)
            elif _be.current_phase(bstate) in (
                    game_engine.ETurnPhases.AssignDamage,
                    game_engine.ETurnPhases.AssignFirstStrikeDamage):
                _be.advance_phase(bstate)
                _be.save_state(session, bstate)
                self._advance_to_priority(session, pl_t, ai_t, bstate)
            else:
                g2 = self._fresh_game(session, pl_t, ai_t, bstate)
                g2.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
                self._send_battle_events(session, g2, pl_t)
                self._push_main_phase_options(session, pl_t, ai_t)
        else:
            log_req(f"    Deck-search answer invalid: "
                    f"chosen={hex(chosen_uid) if chosen_uid else 'none'} "
                    f"candidates={pend['candidates']}")
        _be.save_state(session, bstate)
        self._push_transaction_ack(session)
        return True

    def _resolve_pending_matching_target(self, session, pl_t, ai_t, bstate,
                                         pend, chosen_uid):
        """Resume a typed deck target that does not move the selected card.

        The class-39 picker temporarily presents legal deck cards in the
        client's Choosing collection, but the database cards never leave the
        deck.  Re-enter the selected child ability with its TargetMap so its
        StoreTargets/CreateToken BOM runs normally, then continue the parent
        activation (if the child was invoked from one).
        """
        _be = self._checkpoint_engine(session)

        bstate.pop("resolution_paused", None)
        candidates = [int(uid) for uid in (pend.get("candidates") or [])]
        if not chosen_uid or int(chosen_uid) not in candidates:
            log_req(f"    Matching-target answer invalid: "
                    f"chosen={hex(int(chosen_uid)) if chosen_uid else 'none'} "
                    f"candidates={candidates}")
            _be.save_state(session, bstate)
            self._push_transaction_ack(session)
            return True

        g = self._fresh_game(session, pl_t, ai_t, bstate)
        # All presented cards were only a client-side Choosing projection.
        # Return every one to the face-down deck before publishing the
        # generated copies.  No CardDrawn/CardMoved-to-Hand event is emitted.
        self._hide_candidates_to_deck(g, session, pl_t, ai_t, candidates)

        continuation = pend.get("continuation") or {}
        from rules_port.resolution import (
            resolve_port_ability,
            resume_ability_continuation_parents)
        child_guid = str(continuation.get("ability_guid") or "").lower()
        child_source = int(continuation.get("source_uid") or 0)
        child_owner = int(continuation.get("owner_id", pend.get("owner_id", 0)) or 0)
        child_targets = _numeric_target_map(continuation.get("target_map"))
        child_targets[int(continuation.get("target_index", 0))] = \
            int(chosen_uid)
        resolve_port_ability(
            self, g, session, _db, pl_t, ai_t, bstate,
            child_guid, child_source, child_owner,
            target_map=child_targets,
            variables=continuation.get("variables") or {},
            resume_from_order=int(
                continuation.get("resume_effect_order", 0) or 0),
            instance_id=int(continuation.get(
                "ability_instance_id", continuation.get("instance_id", 1))
                or 1))
        resume_ability_continuation_parents(
            self, g, session, _db, pl_t, ai_t, bstate, continuation)
        _be.save_state(session, bstate)
        pending_input = (bstate.get("pending_choice") or
                         bstate.get("pending_trigger") or
                         bstate.get("pending_deck_search") or
                         bstate.get("pending_conversation"))
        if pending_input:
            self._send_battle_events(session, g, pl_t)
        elif bstate.get("ai_turn_phase_idx") is not None:
            resume_idx = int(bstate.pop("ai_turn_phase_idx"))
            _be.save_state(session, bstate)
            self._send_battle_events(session, g, pl_t)
            self._run_ai_turn(session, pl_t, ai_t, bstate,
                              start_idx=resume_idx)
        else:
            phase = _be.current_phase(bstate)
            if _be.stack_empty(bstate):
                g.push_chain_empty()
                if bstate.get("turn_player") == _be.PLAYER:
                    if phase in (game_engine.ETurnPhases.FirstMainPhase,
                                 game_engine.ETurnPhases.SecondMainPhase):
                        bstate["turn_phases"] = _be.build_turn_phases(bstate)
                        _be.save_state(session, bstate)
                    if phase is not None:
                        g.push_turn_phase(phase, pl_t, pl_t)
                    self._push_phase_options(session, pl_t, ai_t, phase)
                g.push_green_light(
                    pl_t, self._priority_context_for(phase, bstate))
            else:
                g.push_green_light(
                    pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
            self._send_battle_events(session, g, pl_t)
        self._push_transaction_ack(session)
        log_req(f"    Matching deck target chosen: {hex(int(chosen_uid))}; "
                f"created matching cards and resumed priority")
        return True

    def _resolve_pending_revealed_choice(self, session, pl_t, ai_t, bstate,
                                         pend, chosen_uid):
        """Finish a SourceRevealed choice using its child effect metadata."""
        _be = self._checkpoint_engine(session)
        bstate.pop("pending_deck_search", None)
        bstate.pop("resolution_paused", None)
        candidates = [int(u) for u in (pend.get("candidates") or [])]
        revealed = [int(u) for u in (pend.get("revealed_cards") or [])]
        if chosen_uid and int(chosen_uid) in candidates:
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            native_continuation = pend.get("continuation")
            if native_continuation:
                # The reveal itself only changed the client projection.  Put
                # every presented card back behind the deck projection, then
                # let the typed MoveCardToZone effect consume the selected
                # target and continue the same RulesPort ability instance.
                self._hide_candidates_to_deck(
                    g, session, pl_t, ai_t, revealed)
                target_map = _numeric_target_map(
                    native_continuation.get("target_map"))
                target_map[int(native_continuation.get("target_index", 0))] = \
                    int(chosen_uid)
                from rules_port.resolution import (
                    resolve_port_ability,
                    resume_ability_continuation_parents)
                bstate.pop("pending_deck_search", None)
                bstate.pop("resolution_paused", None)
                resolve_port_ability(
                    self, g, session, _db, pl_t, ai_t, bstate,
                    str(native_continuation.get("ability_guid") or "").lower(),
                    int(native_continuation.get("source_uid") or 0),
                    int(native_continuation.get("owner_id", pend.get("owner_id", 0)) or 0),
                    target_map=target_map,
                    variables=native_continuation.get("variables") or {},
                    resume_from_order=int(native_continuation.get(
                        "resume_effect_order", 0)),
                    instance_id=int(native_continuation.get(
                        "ability_instance_id", pend.get("instance_id", 1))))
                resume_ability_continuation_parents(
                    self, g, session, _db, pl_t, ai_t, bstate,
                    native_continuation)
                _be.save_state(session, bstate)
                self._send_battle_events(session, g, pl_t)
                completed = self._resume_completed_rules_port_chain(
                    session, bstate)
                if completed:
                    # Finishing the chain item can end the paused phase: a
                    # trigger resolved during Draw advances to First Main.
                    # Re-project the native priority window, or the client
                    # keeps the stale ResolveTopOfChain button and its next
                    # click passes the phase the player never saw.
                    if not self._advance_rules_port_to_priority(
                            session, pl_t, ai_t, bstate):
                        self._push_transaction_ack(session)
                else:
                    self._push_transaction_ack(session)
                return True
            destination = str(pend.get("move_destination") or "hand").lower()
            location = str(pend.get("move_location") or "").lower()
            if destination == "deck" and location == "top":
                from abilities.framework._shared import state_after_zone_exit
                chosen = int(chosen_uid)
                # Keep the card in the deck, but make it the next card drawn.
                # Shift existing positions first so the ordering is unambiguous
                # even when the revealed card was already at position zero.
                db_move_card_to_deck_top(
                    session.session_id, chosen, state_after_zone_exit(0),
                    conn=_db)
                _db.commit()
                row = db_card_zone_details(
                    session.session_id, chosen, conn=_db)
                if row:
                    scid = game_engine.SessionCardId(game_engine.UID(chosen))
                    owner = pl_t if (row[2] or 0) != 0 else ai_t
                    _tpl, ct, _name, cost, atk, defense, _guid = \
                        self._card_full_data(g, scid, row[0], row[1])
                    g.push_card_updated(
                        scid, owner, game_engine.ECardCollections.Deck, ct,
                        template_id=row[0], cost=cost, attack=atk,
                        defense=defense, state=0, nulling=True)
                log_req(f"    Revealed choice: {hex(chosen)} -> deck top "
                        f"(owner {pend['owner_id']})")
            else:
                from abilities.framework.effects.search import move_deck_card_to_hand
                move_deck_card_to_hand(g, session, _db, self, pl_t, ai_t,
                                       int(chosen_uid), pend["owner_id"], bstate)
                log_req(f"    Revealed choice: {hex(int(chosen_uid))} -> hand "
                        f"(owner {pend['owner_id']})")
            remaining = [cu for cu in revealed if cu != int(chosen_uid)]
            if remaining:
                from pvp_db import db_randomly_insert_deck_cards
                db_randomly_insert_deck_cards(
                    session.session_id, int(pend["owner_id"]), remaining)
            # Hide every other revealed card.  The cards were presented by
            # CardsRevealed, not moved in the database, so they all remain in
            # the deck until this continuation resolves.
            self._hide_candidates_to_deck(
                g, session, pl_t, ai_t,
                remaining)
            self._send_battle_events(session, g, pl_t)
        elif pend.get("optional"):
            # An optional SourceRevealed target may be declined.  Return the
            # revealed cards to the face-down deck without applying the child
            # MoveCardToZone effect.  The legacy client serializes a declined
            # picker using the source card ID rather than a zero target, so
            # accept any non-candidate value as the decline sentinel.
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            # The client-side authoritative move back to Deck with an
            # unknown location inserts the card at a random position.  Do the
            # same in the server DB before hiding it; leaving the old position
            # (often zero) makes a declined card remain visually on top and
            # can make a later draw disagree with what the client showed.
            from pvp_db import db_randomly_insert_deck_cards
            db_randomly_insert_deck_cards(
                session.session_id, int(pend["owner_id"]), revealed)
            self._hide_candidates_to_deck(g, session, pl_t, ai_t, revealed)
            self._send_battle_events(session, g, pl_t)
            log_req("    Revealed choice declined; returned cards to deck")
        else:
            log_req(f"    Revealed choice invalid: "
                    f"chosen={hex(int(chosen_uid)) if chosen_uid else 'none'} "
                    f"candidates={candidates}")
        _be.save_state(session, bstate)
        g2 = self._fresh_game(session, pl_t, ai_t, bstate)
        g2.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
        self._send_battle_events(session, g2, pl_t)
        self._push_main_phase_options(session, pl_t, ai_t)
        self._push_transaction_ack(session)
        return True

    def _completed_rules_port_chain(self, session, bstate):
        """Find the native chain item released by a completed continuation."""
        try:
            instance_id = int(
                (bstate or {}).get("completed_chain_instance_id") or 0)
        except (TypeError, ValueError):
            return None
        port = getattr(session, "_rules_port_session", None)
        if port is None:
            return None
        chain = getattr(port, "chain", None)
        if instance_id <= 0 or chain is None:
            return None
        if chain.peek_ability(instance_id) is None:
            return None
        return port, instance_id

    def _resume_completed_rules_port_chain(self, session, bstate,
                                           completed_chain=None):
        """Finish a chain item whose picker continuation just completed.

        The continuation has already applied the authored effects and stored
        ``completed_chain_instance_id``. Resume the existing native resolver
        now so it can consume that marker, finish the card's zone projection,
        and publish the client-facing chain removal sequence exactly once.
        """
        completed_chain = (completed_chain or
                           self._completed_rules_port_chain(session, bstate))
        if completed_chain is None:
            return False
        port, instance_id = completed_chain
        from rules_port.actions import AbilityResolutionState
        result = port.resolve_top_of_chain(instance_id)
        if result is not AbilityResolutionState.COMPLETED:
            log_req("    RulesPort completed choice did not finish chain item "
                    f"instance={instance_id} state={result!r}")
        return result is AbilityResolutionState.COMPLETED

    def _resolve_pending_shard_choice(self, session, pl_t, ai_t, bstate, pend,
                                      chosen_uid, inner_bytes):
        """Resolve Shards of Fate's pick: gain the chosen Standard resource's
        threshold (the shard remains in the deck) and hide the unchosen
        candidates again."""
        _be = self._checkpoint_engine(session)
        if chosen_uid and chosen_uid in pend["candidates"]:
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            from rules_port.resources import card_template_threshold_flags
            provided = card_template_threshold_flags(
                _db, session.session_id, int(chosen_uid))
            threshold_colors = sorted({
                int(flag) for flag in game_engine.SHARD_TO_FLAG.values()
                if provided & int(flag)})
            from pvp_db import db_randomly_insert_deck_cards
            db_randomly_insert_deck_cards(
                session.session_id, int(pend["owner_id"]), pend["candidates"])
            if threshold_colors:
                th = bstate.setdefault("player_threshold", {})
                for flag in threshold_colors:
                    th[flag] = int(th.get(flag, th.get(str(flag), 0)) or 0) + 1
                _be.save_state(session, bstate)
                g.player_threshold = dict(th)
                for flag in threshold_colors:
                    ev_th = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                    ev_th.player_id = pl_t
                    ev_th.color = flag
                    ev_th.operation = 1
                    ev_th.delta = 1
                    ev_th.new_value = th[flag]
                    g._push(ev_th)
                    from rules_port.triggers import dispatch_native_trigger
                    champion = getattr(self, "_player_champ_scid", None)
                    if champion is not None:
                        dispatch_native_trigger(
                            db=_db, handler=self, game=g, session=session,
                            player_uid=pl_t, ai_uid=ai_t,
                            battle_state=bstate,
                            event_type="GainThresholdEvent",
                            source_card_id=int(champion.uid.uid64),
                            source_player_id=int(pend["owner_id"]),
                            data={"gain_threshold_color": int(flag)})
                g.push_player_updated(
                    pl_t, champ_id=getattr(self, "_player_champ_scid", None))
            # Hide EVERY candidate again (back to the face-down deck) — the
            # chosen shard stays in the deck, and all of them must be rendered
            # nullable so nothing is left face-up on top after the Choosing
            # window closes.
            self._hide_candidates_to_deck(g, session, pl_t, ai_t,
                                          pend["candidates"])
            self._send_battle_events(session, g, pl_t)
            log_req(f"    Shards of Fate: gained threshold flags "
                    f"{threshold_colors} "
                    f"(chosen {hex(int(chosen_uid))})")
        else:
            log_req(f"    Shards of Fate: invalid pick "
                    f"chosen={hex(chosen_uid) if chosen_uid else 'none'}")
        _be.save_state(session, bstate)
        # Re-grant the player priority + fresh main-phase options (the resource
        # play consumed the window; the pick resumes the turn).
        g2 = self._fresh_game(session, pl_t, ai_t, bstate)
        g2.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
        self._send_battle_events(session, g2, pl_t)
        self._push_main_phase_options(session, pl_t, ai_t)
        self._push_transaction_ack(session)
        return True

    def _hide_candidates_to_deck(self, game, session, pl_t, ai_t, uids):
        """Move deck-search / Shards-of-Fate candidates from the client's
        Choosing zone back to the deck, rendered face-down (nullable).  A bare
        CardUpdated without the CardMoved leaves the client's cache showing the
        cards face-up on top of the deck."""
        from abilities.framework._shared import state_after_zone_exit
        for cu in uids:
            c_scid = game_engine.SessionCardId(game_engine.UID(int(cu)))
            c_row = db_card_zone_details(
                session.session_id, int(cu), conn=_db)
            if not c_row:
                continue
            db_move_card_to_deck(
                session.session_id, int(cu), state_after_zone_exit(0),
                conn=_db)
            owner = pl_t if (c_row[2] or 0) != 0 else ai_t
            _tpl2, ct2, _n2, _c2, _a2, _d2, _g2 = self._card_full_data(
                game, c_scid, c_row[0], c_row[1])
            game.push_card_updated(c_scid, owner,
                                   game_engine.ECardCollections.Deck, ct2,
                                   template_id=c_row[0], cost=_c2, attack=_a2,
                                   defense=_d2, state=0, nulling=True)

    @staticmethod
    def _champion_health_by_guid(guid):
        """Look up a champion template's starting health by its GUID."""
        if not guid:
            return 20
        return db_champion_template_health(guid)

    @staticmethod
    def _champion_starting_health(race_name, cls_name):
        """Look up a champion's starting health from champion_class_data."""
        if not race_name or not cls_name:
            return 20
        from pvp_db import db_champion_template_health_by_class
        return db_champion_template_health_by_class(race_name, cls_name)

    def _resolve_fra_deck_id(self, arena_deck_id):
        """The practice/FRA deck: the arena-assigned deck when it is valid for
        this player, otherwise the player's most recently saved deck.  NEVER a
        hardcoded id — each player's own collection is used."""
        uid = self.user_profile["id"] if self.user_profile else None
        if uid and arena_deck_id:
            if db_find_deck_owner(arena_deck_id, conn=_db) == uid:
                return arena_deck_id
        if uid:
            row = db_latest_deck_id_for_user(uid, conn=_db)
            if row:
                return row
        return None

    def _filter_affordable_abilities(self, abilities, bstate, phase=None, *,
                                     target_data=None, cost_data=None,
                                     session=None):
        """Filter champion abilities to only those activatable in the current phase
        and affordable (charges/SP >= cost). Pass `phase` (an ETurnPhases value) to
        also gate on activatable_phases (a bitmask where bit N = 1<<N).

        QuickAction abilities (casting_behavior=64) ignore the phase gate — they
        may be activated in ANY priority window (mirrors the client's
        CanActivateAbilityBase, which skips the main-phase check for QuickAction).

        ``target_data``/``cost_data`` are the exact per-ability selection pools
        the caller passes to ``add_champion_to_options``.  Offering an ability
        whose authored payment or explicit target has no legal card makes the
        champion card light up for an activation that can never be completed
        (Bunoshi's "sacrifice a troop"), so the offer gate reads the same pools
        the client's picker will.
        """
        if bstate.get("_rules_port_attached"):
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        charges = bstate.get("player_charges", 0)
        sp = bstate.get("player_spell_points", 0)
        phase_bit = (1 << phase) if phase is not None else 0
        chain_pending = bool(bstate.get("stack"))
        # Session.CanActivateChargeAbilitiesAsQuick: a warzone card with this
        # flag lets the player treat charge powers as QuickActions.
        quick_charges = False
        if session is not None:
            try:
                from rules_port.static_rules import owner_has_card_int_attr
                owner = (int(self.user_profile.get("id", 0) or 0)
                         if isinstance(self.user_profile, dict) else 0)
                quick_charges = owner_has_card_int_attr(
                    _db, session.session_id, owner,
                    "YouCanActivateYourChargePowersAsThoughTheyWereQuick")
            except Exception:
                quick_charges = False
        # Spell-power escalation: each use permanently bumps that spell's SP cost
        # by +1 (mirrors Session.cs:1154 IncrementSpellPointCostModifier).
        # Only spell powers (spell_cost > 0) escalate; charge powers don't.
        sp_uses = bstate.get("player_sp_uses", {}) or {}
        # Use the shared runtime DB helper here.  Importing the PVE helper
        # binds a second connection seam and makes live/test DB swaps observe
        # different state.
        from db import db_talent_ability_costs
        afford = []
        for aid in (abilities or []):
            # Champion ability lists contain both player-activated powers and
            # automatic talent triggers.  The latter must remain available to
            # the trigger dispatcher, but they are not client-activatable
            # abilities and must not light up the champion card in an option
            # packet.  Use the typed Records graph as the authority so this
            # stays data-driven for talents and signature powers alike.
            graph = _records_ability_graph(self, str(aid.guid))
            if graph is not None and not graph.manual:
                continue
            # A spent ONE-SHOT power must stop lighting up the champion card
            # (the client keeps offering it because champions are not
            # game_cards rows and cannot carry ``card_uses``).
            try:
                player_champ_uid = int(self._player_champ_scid.uid.uid64)
            except (AttributeError, TypeError, ValueError):
                player_champ_uid = None
            if not self._champion_ability_available(
                    bstate, aid.guid, graph, source_uid=player_champ_uid):
                continue
            payable = self._champion_payment_available(
                str(aid.guid), graph, target_data, cost_data)
            row = db_talent_ability_costs(str(aid.guid))
            if row is None:
                # Champion signature charge powers (champion_abilities) aren't
                # talents — their costs/casting come from the gamedata seed.
                from pvp_db import db_champion_ability_costs
                row = db_champion_ability_costs(str(aid.guid))
            if row:
                cc, sc, aphases, casting = row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0
                if casting != 64:
                    if not (cc > 0 and quick_charges):
                        # BasicAction abilities cannot be activated while a
                        # chain item is resolving. The source champion still
                        # appears on the chain, but its BasicAction button must
                        # not remain clickable from the previous option list.
                        if chain_pending:
                            continue
                        # BasicAction etc.: only on the PLAYER's own main
                        # phases. During the opponent's turn there is no player
                        # main phase, so a BasicAction charge power must not be
                        # offered even if the AI happens to be in FirstMain.
                        if bstate.get("turn_player") != _be.PLAYER:
                            continue
                        if phase is not None and not (aphases & phase_bit):
                            continue  # not activatable (QuickAction exempt)
                eff_sc = sc + (int(sp_uses.get(str(aid.guid), 0)) if sc > 0 else 0)
                if (charges >= cc and sp >= eff_sc
                        and payable
                        and self._champion_thresholds_met(str(aid.guid), bstate)):
                    afford.append(aid)
            else:
                # Unknown cost — include only if phase gating passes (or no
                # phase given) and any gamedata threshold is met.
                if (payable and (phase is None or phase_bit)
                        and self._champion_thresholds_met(str(aid.guid), bstate)):
                    afford.append(aid)
        return afford

    def _champion_payment_available(self, ability_guid, graph, target_data,
                                    cost_data):
        """Whether a champion ability's authored card costs and explicit
        targets can be satisfied from the pools the client is offered.

        Mirrors the client's own activation rule: a non-optional effect target
        that is neither auto/random nor ``m_AllowBestEffortMinimumTargetCount``
        must have at least its minimum legal cards
        (``Session.CanActivateAbility``), and a sacrifice/exhaust/void cost
        needs its payment cards.  With no pools supplied there is nothing to
        check, so the ability stays offered.
        """
        if target_data is None and cost_data is None:
            return True
        ag = str(ability_guid)
        specs = {str(target.guid).lower(): target
                 for target in (getattr(graph, "targets", ()) or ())}

        def relaxed(target_guid):
            spec = specs.get(str(target_guid).lower())
            return bool(spec is not None and (
                getattr(spec, "optional", False) or
                getattr(spec, "allow_best_effort_minimum", False)))

        for tid, _ctype, candidates, minimum, _maximum in (
                (cost_data or {}).get(ag) or ()):
            if relaxed(tid):
                continue
            if len(candidates or ()) < int(minimum or 0):
                return False
        for tid, candidates, minimum, _maximum, _index in (
                (target_data or {}).get(ag) or ()):
            if relaxed(tid):
                continue
            # The authored minimum decides (0 = an "up to N" target the player
            # may skip); the client compares CountLegalTargets against exactly
            # this value.
            if len(candidates or ()) < int(minimum or 0):
                return False
        return True

    def _champion_thresholds_met(self, ability_guid, bstate):
        """True if the champion's threshold requirements (from gamedata
        champion_abilities.thresholds_json, e.g. Dimmid's [DIAMOND][DIAMOND])
        are met by the player's current threshold counts."""
        from pvp_db import db_champion_ability_thresholds
        reqs = db_champion_ability_thresholds(ability_guid)
        if not reqs:
            return True
        th = bstate.get("player_threshold", {}) or {}
        # The AI activates its own champion powers with its own thresholds.
        if getattr(self, "_resolving_ai_champion", False):
            th = bstate.get("ai_threshold", {}) or {}
        for color, qty in reqs:
            flag = game_engine.SHARD_TO_FLAG.get(str(color).lower(), 0)
            if flag and int(th.get(flag, 0) or 0) < qty:
                return False
        return True

    def _bom_has_discard(self, ability_guid, *, native=False):
        """Whether an ability graph contains an authored discard effect."""
        from rules_port.metadata import ability_has_effect
        return ability_has_effect(
            ability_guid, "DiscardCardAbilityEffectTemplate")

    def _discard_prompt_data(self, ability_guid, *, bstate=None):
        """Return ``(ability_guid, hand_target_template)`` for a discard.

        Only a dedicated discard BOM leaf opens the class-23 follow-up prompt.
        An ability-level discard payment is a normal typed CostInstance and is
        paid with the other authored activation costs.
        """
        from rules_port.metadata import ability_effect_prompt
        prompt = ability_effect_prompt(
            ability_guid, "DiscardCardAbilityEffectTemplate")
        if prompt and prompt[1]:
            return prompt
        return None

    def _ability_requires_discard(self, ability_guid):
        """Return whether activation requires the controller to discard.

        The typed BOM/ability cost graph is the sole source of this payment.
        """
        return self._discard_prompt_data(ability_guid) is not None

    def _discard_costs_for(self, session, ability_ids):
        """Return {ability_guid: (hand SessionCardId list, target_template_id str)}
        for the abilities the class-23 discard prompt needs target instances for.

        Only the invoked discard child ability gets a target entry. The granted
        ability must NOT carry a target instance, otherwise the client shows
        the targeting arrow on click (before activation); the class-23 prompt
        happens after the draw instead."""
        if not ability_ids:
            return {}
        hand_uids = db_game_cards_at_location_scalar(session.session_id, "hand", self.user_profile["id"])
        hand = [game_engine.SessionCardId(game_engine.UID(r)) for r in hand_uids]
        if not hand:
            return {}
        out = {}
        for aid in ability_ids:
            prompt = self._discard_prompt_data(str(aid.guid))
            if prompt and prompt[1]:
                child_ability, target_template = prompt
                out[child_ability] = (list(hand), target_template)
        return out

    def _champion_ability_targets(self, session, ability_ids, champ_uid=0):
        """Return {ability_guid: [(target_template_id, [candidate card uids],
        min, max, target_template_index)]}
        for champion abilities with explicit targets (e.g. Dimmid's "Target
        troop gets Lifedrain this turn").  Candidates come from the gamedata
        target template (collection + card filter)."""
        if not ability_ids:
            return {}
        from rules_port.targeting import legal_targets_for
        out = {}
        for aid in ability_ids:
            ag = str(aid.guid)
            graph = _records_ability_graph(self, ag)
            if graph is None:
                continue
            cost_tids = {str(guid).lower() for _kind, guid in
                         graph.additional_cost_targets or ()}
            for target_index, target in enumerate(graph.targets):
                if (not target.requires_input or
                        str(target.guid).lower() in cost_tids):
                    continue
                tid = target.guid
                cands = legal_targets_for(
                    _db, session.session_id, self.user_profile["id"],
                    target, int(champ_uid or 0),
                    champions=self._champion_targets())
                mn = target.minimum
                mx = target.maximum if target.maximum > 0 else -1
                out.setdefault(ag, []).append(
                    (tid, cands, mn, mx, target_index))
        return out

    def _ability_cost_templates(self, ability_guid):
        """Return payment targets from the current typed AbilityGraph."""
        from rules_port.costs import cost_type_for_kind
        graph = _records_ability_graph(self, ability_guid)
        if graph is None:
            return []
        return [(guid, cost_type_for_kind(kind))
                for kind, guid in graph.additional_cost_targets or ()]

    def _current_ability_graph(self, ability_guid):
        from gamedata import ability_graph
        store = getattr(self, "_play_plan_store", None)
        if store is None:
            from gamedata import DEFAULT_RECORD_STORE
            store = DEFAULT_RECORD_STORE
            self._play_plan_store = store
        return ability_graph(store, str(ability_guid).lower())

    def _current_target_spec(self, target_guid):
        store = getattr(self, "_play_plan_store", None)
        if store is None:
            from gamedata import DEFAULT_RECORD_STORE
            store = DEFAULT_RECORD_STORE
            self._play_plan_store = store
        target = store.get("AbilityTargetTemplate", str(target_guid).lower())
        return target.target_spec if target is not None else None

    def _champion_ability_costs(self, session, ability_ids, champ_uid=0):
        """{ability_guid: [(target_template_id, EAbilityCostType, [candidate
        card uids], min, max)]} for the champion ability's CARD costs — the
        client's BattleStateAssignXCost prompts for these (e.g. Bun'jitsu's
        "Void two ready troops you control")."""
        from rules_port.costs import ability_cost_targets, cost_type_for_kind
        out = {}
        for aid in (ability_ids or []):
            ag = str(aid.guid)
            graph = _records_ability_graph(self, ag)
            if graph is None:
                continue
            for cost in ability_cost_targets(
                    graph, _db, session.session_id,
                    self.user_profile["id"], int(champ_uid or 0),
                    champions=self._champion_targets()):
                out.setdefault(ag, []).append(
                    (cost.guid, cost_type_for_kind(cost.kind), cost.candidates,
                     cost.minimum, cost.maximum))
        return out

    def _select_champion_activation_targets(self, session, bstate,
                                            ability_guid, champ_uid,
                                            selected_uids):
        """Separate champion card-payment targets from effect targets.

        Champion powers use the same AbilityTargetTemplate contracts as card
        abilities.  The client submits all selected cards in one TargetMap,
        so a sacrifice target must be removed from the effect-target pool
        before the chain item is created.
        """
        from rules_port.costs import (ability_cost_targets,
                                      cost_type_for_kind,
                                      validate_cost_target_selection)
        from rules_port.targeting import legal_targets_for
        graph = _records_ability_graph(self, ability_guid)
        if graph is None:
            return None
        selected_uids = [int(uid) for uid in (selected_uids or [])]
        used = set()
        sacrifices = []
        for cost in ability_cost_targets(
                graph, _db, session.session_id, self.user_profile["id"],
                int(champ_uid), champions=self._champion_targets(),
                battle_state=bstate):
            tid = cost.guid
            cost_type = cost_type_for_kind(cost.kind)
            minimum = cost.minimum
            maximum = cost.maximum
            if cost.is_source_auto_target:
                candidates = [int(champ_uid)]
                available = candidates
            else:
                candidates = cost.candidates
                available = [uid for uid in selected_uids
                             if uid in {int(c) for c in candidates}
                             and uid not in used]
            if len(available) < minimum:
                return None
            limit = len(available) if maximum < 0 else maximum
            chosen = available[:limit]
            chosen = validate_cost_target_selection(
                _db, session.session_id, self.user_profile["id"],
                int(champ_uid), cost, chosen,
                champions=self._champion_targets(), battle_state=bstate)
            if chosen is None:
                return None
            used.update(chosen)
            if int(cost_type) == 2:
                sacrifices.extend(chosen)

        effect_candidates = []
        explicit_required = False
        cost_tids = {str(guid).lower() for _kind, guid in
                     graph.additional_cost_targets or ()}
        for target in graph.targets:
            if (not target.requires_input or
                    str(target.guid).lower() in cost_tids):
                continue
            explicit_required = explicit_required or bool(
                target.minimum if target.minimum is not None else 1)
            effect_candidates.extend(int(uid) for uid in legal_targets_for(
                _db, session.session_id, self.user_profile["id"], target,
                int(champ_uid), champions=self._champion_targets(),
                battle_state=bstate))
        legal_effects = set(effect_candidates)
        effect_selected = [uid for uid in selected_uids
                           if uid not in used and uid in legal_effects]
        if explicit_required and not effect_selected:
            return None
        return (effect_selected[-1] if effect_selected else None, sacrifices)

    @staticmethod
    def _thresholds_met(thresh_json, player_threshold):
        """Check if the player's thresholds meet the card's requirements.

        thresh_json is a JSON string like {"list":[0,1]} where indices map:
        0=Colorless, 1=Blood, 2=Ruby, 3=Sapphire, 4=Wild, 5=Diamond.
        Shard flags in player_threshold use bitmask values: Blood=4, Ruby=8,
        Sapphire=16, Wild=32, Diamond=64.
        The list can repeat a color ({"list":[2,2]} = TWO Ruby), so each
        color's required count is its number of occurrences.
        Returns True if all card thresholds are met.

        Handles both int and string keys (JSON serialization can convert
        int dict keys to strings across save/load cycles).
        """
        if not thresh_json:
            return True
        try:
            import json as _j
            req = _j.loads(thresh_json)
            req_list = req.get("list", [])
            if not req_list:
                return True
            shard_fmt = {0:0, 1:4, 2:8, 3:16, 4:32, 5:64}
            need = {}
            for s in req_list:
                flag = shard_fmt.get(s, s)
                need[flag] = need.get(flag, 0) + 1
            for flag, count in need.items():
                # Try int key first, then string key (JSON converts int→str)
                val = player_threshold.get(flag)
                if val is None:
                    val = player_threshold.get(str(flag), 0)
                if (val or 0) < count:
                    return False
            return True
        except Exception:
            return True

    def _push_phase_options_empty(self, session, pl_t, ai_t, game=None,
                                  send=True, refresh_warzone=True):
        """Push a PlayerOptionList with champion abilities + playable hand
        QuickActions (instant-speed — castable in ANY priority window).

        Called for stop phases that are NOT main phases so the champion's
        charge/spell power buttons and QuickActions are always interactive when
        the player has priority. Always carries a fresh PlayerUpdated so the
        client's resource / charge / SP display reflects the real battle state.
        """
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        game = game or self._fresh_game(session, pl_t, ai_t, bstate)
        # QuickActions may be played in ANY priority window (your turn or the
        # opponent's turn when a stop hands you priority).
        from pvp_db import db_hand_cards_with_templates
        from rules_port.static_rules import effective_option_projection
        playable = []
        rows = db_hand_cards_with_templates(
            session.session_id, self.user_profile["id"])
        resources = bstate.get("player_resources", 0)
        threshold = bstate.get("player_threshold", {})
        fc = self._warzone_troop_count(session, self.user_profile["id"])
        ec = self._warzone_troop_count(session, 0)
        for card_uid, cost, ct, thresh_json, abilities_json in rows:
            try:
                # One native static scan per hand card: the effective
                # attributes and the effective cost are both projections of
                # the same board view, and scanning twice doubled the cost of
                # every opponent-turn priority window.
                effective_attrs, cost = effective_option_projection(
                    _db, session.session_id, bstate, card_uid)
                effective_attrs = int(effective_attrs or 0)
            except Exception:
                effective_attrs = 0
            if ("QuickAction" not in (ct or "") and not
                    (effective_attrs & game_engine.ECardAttributes.QuickAction)):
                continue
            ab = []
            if abilities_json:
                try:
                    ab = [g.lower() for g in json.loads(abilities_json)]
                except Exception:
                    pass
            if self._hand_card_playable(session, card_uid, ct, cost, thresh_json,
                                        ab, resources, threshold, True, fc, ec):
                playable.append(game_engine.SessionCardId(game_engine.UID(card_uid)))
        game.push_options(pl_t, playable)
        # Attach targeting TargetInstances for played spells (target picker).
        self._add_play_target_options(game, session, pl_t, ai_t)
        player_champ_cid = getattr(self, "_player_champ_scid", None)
        affordable_champ = []
        if player_champ_cid:
            abilities = getattr(self, "_player_champ_abilities", [])
            if abilities:
                champ_uid = (player_champ_cid.uid.to_uint64()
                             if hasattr(player_champ_cid, "uid") else 0)
                champ_targets = self._champion_ability_targets(
                    session, abilities, champ_uid)
                champ_costs = self._champion_ability_costs(
                    session, abilities, champ_uid)
                affordable_champ = self._filter_affordable_abilities(
                    abilities, bstate, _be.current_phase(bstate),
                    target_data=champ_targets, cost_data=champ_costs,
                    session=session)
                game.add_champion_to_options(
                    pl_t, player_champ_cid, affordable_champ,
                    self._discard_costs_for(session, abilities),
                    champ_targets, champ_costs)
        # Warzone-troop QuickAction abilities (e.g. Living Totem's 3-cost activations)
        # are castable in ANY priority window — include them here for non-main phases.
        affordable = self._affordable_troop_abilities(session, bstate)
        if affordable:
            self._add_troop_ability_options(game, pl_t, session, affordable,
                                             bstate)
        game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        if refresh_warzone:
            self._push_warzone_card_updates(game, session, pl_t, ai_t)
        if send:
            self._send_battle_events(session, game, pl_t)
        log_req(f"    Phase options (priority window): {len(playable)} playable "
                f"quick actions, champ_ab={len(affordable_champ)}")

    def _push_practice_ability_chain_window(self, session, port, game):
        """Give the single Practice client the chain response window.

        An ability activation that leaves a chain item pending must replace the
        stale main-phase options with a chain-only window plus a
        ResolveTopOfChain green light — the tournament path does the same for
        both participants.  Practice has no client-submitted table state, so
        the identities come from the live projection: the single client is the
        service player and the other side is the server AI.  Deriving them here
        keeps the projection independent of caller-local names (a missing
        binding used to abort it, leaving the chain item unresolved and every
        later Basic Action unplayable).
        """
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        priority = port.action_stack.priority_player_id
        if not _same_uid(priority, pl_t):
            return False
        self._push_phase_options_empty(session, game.player_uid, game.ai_uid)
        from rules_port.persistence import load_state as _load_state
        chain_game = self._fresh_game(
            session, pl_t, ai_t, _load_state(session, default=lambda: {}))
        # OnTurnPhaseUpdated checks the client's current HasPriority bit. Send
        # GreenLight first so a client that just lost priority to the AI does
        # not request a main-phase resync before this response window arrives.
        chain_game.push_green_light(
            pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
        chain_game.push_turn_phase(
            port.current_turn_phase,
            pl_t if _same_uid(port.active_player_id, pl_t) else ai_t,
            pl_t)
        self._send_battle_events(session, chain_game, pl_t)
        log_req("    RulesPort Practice ability priority window: "
                f"phase={port.current_turn_phase} priority=player")
        return True

    def _push_attack_options(self, session, pl_t, ai_t):
        """Push a PlayerOptionList marking the player's ready warzone troops
        as attackable (ECardUsage.Attack) during the DeclareAttack phase.

        Only troops that have StartedATurnOnYourSide (survived to this turn,
        i.e. not summoning sick) and are not tapped may attack — the same rule
        the client enforces in Card.HasSummoningSickness() / CanAttack().

        ForceAttack ("Must attack", e.g. Savage Raider) troops are declared as
        attackers HERE, server-side — the authoritative DeclareAttackState in
        the client does the same, and the server must not rely on the player
        remembering to drag them.
        """
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        self._auto_declare_force_attackers(session, pl_t, ai_t, bstate, game)
        rows = db_warzone_troops_basic(session.session_id, self.user_profile["id"])
        from rules_port.static_rules import effective_attributes
        ready = []
        for uid, ct, state in rows:
            state = state or 0
            attrs = int(effective_attributes(
                _db, session.session_id, bstate, int(uid)) or 0)
            # A troop with Speed may attack the turn it comes into play (the
            # client's HasSummoningSickness() exempts Speed) — e.g. a Blaze
            # Elemental summoned by Poca's charge power.
            if (((state & game_engine.ECardStates.StartedATurnOnYourSide)
                 or ((attrs or 0) & game_engine.ECardAttributes.Speed))
                    and not (state & game_engine.ECardStates.Tapped)
                    and not (state & game_engine.ECardStates.Attacking)
                    and not ((attrs or 0) &
                             (game_engine.ECardAttributes.CantAttack |
                              game_engine.ECardAttributes.Defensive))):
                ready.append(game_engine.SessionCardId(game_engine.UID(uid)))
        ev = game._make_event(game_engine.PlayerOptionListSessionEventArgs)
        ev.player_id = pl_t
        for scid in ready:
            opt = game._make_event(game_engine.PlayerOptionSessionEventArgs)
            opt.card = scid
            opt.state = game_engine.ECardUsage.Attack
            opt.instances = []
            ev.options.append(opt)
        game._push(ev)
        game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        self._push_warzone_card_updates(game, session, pl_t, ai_t)
        self._send_battle_events(session, game, pl_t)
        log_req(f"    Attack options: {len(ready)} ready troop(s)")

    def _auto_declare_force_attackers(self, session, pl_t, ai_t, bstate, game):
        """Server-authoritative "Must attack": every ready player warzone troop
        with ForceAttack is declared as an attacker automatically, exactly like
        the client's DeclareAttackState does on the authoritative side — the
        player cannot forget (or refuse) to attack with them.

        Idempotent: troops already attacking (or already in
        bstate['player_attackers']) are skipped, so re-pushing the DeclareAttack
        options never double-declares.
        """
        _be = self._checkpoint_engine(session)
        from pvp_db import db_card_state_value as db_card_state_raw
        from rules_port.static_rules import effective_attributes
        rows = db_warzone_troops_basic(session.session_id,
                                       self.user_profile["id"])
        ai_champ_uid = getattr(self, "_ai_champ_scid", None)
        ai_champ_uid64 = ai_champ_uid.uid.to_uint64() if ai_champ_uid else 0
        attackers = {int(k): int(v)
                     for k, v in (bstate.get("player_attackers") or {}).items()}
        combats = []
        for uid, ct, state in rows:
            state = state or 0
            attrs = int(effective_attributes(
                _db, session.session_id, bstate, int(uid)) or 0)
            if not (attrs & game_engine.ECardAttributes.ForceAttack):
                continue
            if (state & (game_engine.ECardStates.Attacking |
                         game_engine.ECardStates.Tapped)):
                continue
            if not (state & game_engine.ECardStates.StartedATurnOnYourSide) \
                    and not (attrs & game_engine.ECardAttributes.Speed):
                continue
            if attrs & (game_engine.ECardAttributes.CantAttack |
                        game_engine.ECardAttributes.Defensive):
                continue
            u = int(uid)
            if u in attackers:
                continue
            attackers[u] = int(ai_champ_uid64)
            scid = game_engine.SessionCardId(game_engine.UID(u))
            combat_id = game_engine.CombatId(pl_t, u & 0xFFFF)
            port = getattr(session, "_rules_port_session", None)
            if port is not None:
                # ForceAttack declarations must exist in the same native
                # CombatManager as ordinary client declarations.  The event
                # projection below is not a second combat authority.
                port_attacker = port.get_card(u) or u
                port_defender = (port.get_card(ai_champ_uid64)
                                 if ai_champ_uid64 else None)
                port_combat = port.combat_manager.create_attack(
                    combat_id, pl_t, port_defender or ai_champ_uid64)
                port_combat.declare_attacker(port_attacker)
                combat_id = self._wire_combat_id_for_attacker(
                    session, pl_t, u, u & 0xFFFF)
            game.push_attack_declared(
                combat_id, pl_t,
                ai_champ_uid or game_engine.SessionCardId(ai_t), scid)
            state_bits = (game_engine.ECardStates.Attacking |
                          game_engine.ECardStates.HasAttacked)
            if not (attrs & game_engine.ECardAttributes.Steadfast):
                state_bits |= game_engine.ECardStates.Tapped
            db_card_set_attacking_state(session.session_id, u, state_bits)
            card_details = db_card_zone_details(
                session.session_id, u, conn=_db)
            tpl_guid = card_details[0] if card_details else None
            self._card_full_data(game, scid, tpl_guid)
            pushed_state = db_card_state_raw(session.session_id, u) or state_bits
            game.push_card_updated(
                scid, pl_t, game_engine.ECardCollections.Warzone,
                game_engine.ECardTypes.Troop, template_id=tpl_guid,
                state=pushed_state)
            if state_bits & game_engine.ECardStates.Tapped:
                self._dispatch_native_trigger(
                    game, session, pl_t, ai_t, bstate, "CardTappedEvent", u,
                    self.user_profile["id"])
            cs = game_engine.CombatSessionEventArgs()
            cs.player_id = pl_t
            cs.id = combat_id
            cs.attacker = scid
            cs.blockers = []
            combats.append(cs)
            # Fire "when this attacks" triggers (e.g. Chimera Guard Outrider).
            self._dispatch_native_trigger(
                game, session, pl_t, ai_t, bstate, "CardAttackedEvent", u,
                self.user_profile["id"])
            self._dispatch_native_trigger(
                game, session, pl_t, ai_t, bstate,
                "CardAttackedOrBlockedEvent", u, self.user_profile["id"])
            from rules_port.combat_effects import apply_rage
            from rules_port.context import EffectContext
            apply_rage(EffectContext.from_rules_port(
                game, session, _db, self, pl_t, ai_t, bstate,
                "", ability=None), u)
        if combats:
            bstate["player_attackers"] = {
                str(k): str(v) for k, v in attackers.items()}
            _be.save_state(session, bstate)
            game.push_combat_listing(pl_t, combats)
            log_req(f"    Auto-declared {len(combats)} ForceAttack attacker(s): "
                    f"{[hex(c.attacker.uid.uid64) for c in combats]}")
        return combats

    def _push_blocker_options(self, session, pl_t, ai_t):
        """Push a PlayerOptionList enabling the player to declare blockers during
        the DeclareDefense phase of the AI's turn.

        The client's BattleStateDeclareBlockers only lets a troop block when
        State.HasUsage(troop, ECardUsage.Defend) is set AND
        State.GetTargetsFor(troop, ResourceId.Blocking) lists at least one
        attacker (BattleStateDeclareBlockers.CanEnterCombat). So every eligible
        (untapped) player warzone troop gets a Defend usage plus a Blocking
        TargetInstance whose targets are the AI's declared attackers
        (bstate['ai_attackers']). ResourceId.Blocking =
        83659505-152d-4ddc-89df-7c29bdfba16d (client ResourceId.cs).
        """
        if not self._player_can_block(session):
            return False
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        attackers = bstate.get("ai_attackers") or {}
        attacker_scids = [game_engine.SessionCardId(game_engine.UID(int(u)))
                          for u in attackers]
        # Use the same metadata-driven combat legality as resolution. This
        # includes conditional static rules such as Electroid's Dwarf/Robot
        # control threshold, which is not a persisted keyword bit.
        from rules_port.combat_rules import can_block
        rows = db_warzone_defender_rows(
            session.session_id, self.user_profile["id"],
            game_engine.ECardStates.Tapped, conn=_db)
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        ev = game._make_event(game_engine.PlayerOptionListSessionEventArgs)
        ev.player_id = pl_t
        blocking_id = game_engine.ResourceId.from_str(
            "83659505-152d-4ddc-89df-7c29bdfba16d")
        for uid, _battrs in rows:
            # This blocker's blockable targets: every attacker it may block.
            blockable = []
            for scid, u in zip(attacker_scids, attackers):
                if can_block(_db, session.session_id, bstate,
                             int(u), int(uid)):
                    blockable.append(scid)
            if not blockable:
                continue
            opt = game._make_event(game_engine.PlayerOptionSessionEventArgs)
            opt.card = game_engine.SessionCardId(game_engine.UID(int(uid)))
            opt.state = game_engine.ECardUsage.Defend
            inst = game._make_event(game_engine.OptionInstanceSessionEventArgs)
            inst.opt_id = blocking_id
            inst.min_target_counts.append(0)
            inst.max_target_counts.append(len(blockable))
            inst.target_ids.append(blocking_id)
            tgt = game._make_event(game_engine.TargetInstanceSessionEventArgs)
            tgt.target_index = 0
            tgt.target_id = blocking_id
            tgt.targets = list(blockable)
            inst.target_instances.append(tgt)
            opt.instances.append(inst)
            ev.options.append(opt)
        game._push(ev)
        game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        self._push_warzone_card_updates(game, session, pl_t, ai_t)
        self._send_battle_events(session, game, pl_t)
        log_req(f"    Blockers options: {len(rows)} defender troop(s) for {len(attacker_scids)} AI attacker(s)")
        return True

    def _player_can_block(self, session):
        """True if at least one player troop can block an AI attacker.

        A generic untapped troop is not sufficient here: when all attackers
        have Flight, only Flight/SkyGuard troops are legal blockers.  Opening
        DeclareBlockers for an otherwise ineligible board leaves the client
        asking for a block it cannot make.
        """
        import game_engine as _ge
        from rules_port.persistence import load_state
        bstate = load_state(session)
        attackers = [int(uid) for uid in (bstate.get("ai_attackers") or {})]
        if not attackers:
            # Keep the broad board check for callers that ask before attacker
            # declarations are persisted; the DeclareDefense path itself has
            # the attacker list and applies the full pairwise legality test.
            attackers = None
        rows = db_warzone_blocker_uids(
            session.session_id, self.user_profile["id"],
            _ge.ECardStates.Tapped, conn=_db)
        from rules_port.combat_rules import can_block
        from rules_port.static_rules import effective_attributes
        if attackers is None:
            return any(not (int(effective_attributes(
                _db, session.session_id, bstate, int(row[0])) or 0) &
                _ge.ECardAttributes.CantBlock) for row in rows)
        return any(can_block(_db, session.session_id, bstate, attacker_uid,
                             int(row[0]))
                   for row in rows for attacker_uid in attackers)

    def _finish_battle_gameend(self, session, won, winners, losers):
        """Persist FRA progress before publishing the terminal event.

        ArenaClient immediately joins the Arena lobby after it receives
        ``GameEnded``.  The FRA result therefore has to advance the persisted
        challenger index before that event is sent.  Campaign and tournament
        sessions retain their existing post-event result handling.
        """
        import commands as _cmd
        prepared = campaign.prepare_fra_battle_gameend(
            self, _db, session, won)
        _cmd.push_battle_game_end(
            handler=self, session=session,
            winners=winners, losers=losers)
        if prepared is not None:
            campaign.publish_fra_battle_gameend(
                self, prepared, SERVICE_MAIL_UID)
        else:
            campaign.handle_battle_gameend(
                self, _db, session, won, SERVICE_MAIL_UID,
                UID_TYPE["ServiceCampaign"])
        return prepared

    def _check_champion_health(self, session, pl_t, ai_t, bstate):
        """End the game immediately when a champion's health is 0 or less.
        State checks run at EVERY phase (and after chain resolutions), not
        only when combat damage happens — e.g. the AI killing itself with its
        own Fang of the Mountain God must not leave a 0-health champion alive
        until the player's next combat."""
        ph = int(bstate.get("player_health", 20))
        ah = int(bstate.get("ai_health", 20))
        # Give metadata-defined survival abilities their client-equivalent
        # replacement event before publishing GameEnded.  These abilities
        # resolve immediately (ChampionWouldLose is authored as an
        # ignores-chain event), so re-read health after each dispatch.
        if ph <= 0:
            loss_game = self._fresh_game(session, pl_t, ai_t, bstate)
            from rules_port.context import EffectContext
            from rules_port.death_effects import champion_would_lose
            bstate["_rules_port_attached"] = True
            champion_would_lose(EffectContext.from_rules_port(
                loss_game, session, _db, self, pl_t, ai_t, bstate,
                "", ability=None), self.user_profile["id"])
            if loss_game.events:
                self._send_battle_events(session, loss_game, pl_t)
            ph = int(bstate.get("player_health", 20))
        if ah <= 0:
            loss_game = self._fresh_game(session, pl_t, ai_t, bstate)
            from rules_port.context import EffectContext
            from rules_port.death_effects import champion_would_lose
            bstate["_rules_port_attached"] = True
            champion_would_lose(EffectContext.from_rules_port(
                loss_game, session, _db, self, pl_t, ai_t, bstate,
                "", ability=None), 0)
            if loss_game.events:
                self._send_battle_events(session, loss_game, pl_t)
            ah = int(bstate.get("ai_health", 20))
        if ph <= 0:
            self._finish_battle_gameend(
                session, False, winners=[ai_t], losers=[pl_t])
            log_req(f"    Game over: player health {ph} <= 0 (AI wins)")
            return True
        if ah <= 0:
            self._finish_battle_gameend(
                session, True, winners=[pl_t], losers=[ai_t])
            log_req(f"    Game over: AI health {ah} <= 0 (player wins)")
            return True
        return False

    def _push_phase_options(self, session, pl_t, ai_t, phase):
        """Push the PlayerOptionList appropriate for `phase` (used when re-
        granting priority after a chain resolves / at a stop): playable cards for
        main phases, attack options for DeclareAttack, the combat listing at
        damage steps, otherwise just champion abilities + QuickActions."""
        _be = self._checkpoint_engine(session)
        if phase in (game_engine.ETurnPhases.FirstMainPhase,
                     game_engine.ETurnPhases.SecondMainPhase):
            self._push_main_phase_options(session, pl_t, ai_t)
        elif phase == game_engine.ETurnPhases.DeclareAttack:
            self._push_attack_options(session, pl_t, ai_t)
        elif phase in (game_engine.ETurnPhases.AssignDamage,
                       game_engine.ETurnPhases.AssignFirstStrikeDamage):
            bstate = _be.load_state(session)
            self._current_bstate = bstate
            self._push_combat_listing_phase(session, pl_t, ai_t, bstate)
        elif phase == game_engine.ETurnPhases.DeclareDefense:
            self._push_blocker_options(session, pl_t, ai_t)
        else:
            self._push_phase_options_empty(session, pl_t, ai_t)

    def _ai_pass_declare_defense(self, session, pl_t, ai_t, bstate, game):
        import ai
        return ai.ai_pass_declare_defense(self, session, pl_t, ai_t, bstate, game)

    def _wire_combat_id_for_attacker(self, session, player_uid,
                                     attacker_uid, fallback_serial):
        """Use the native CombatManager ID in every client combat packet.

        The client returns the ID from its current CombatListing in its damage
        order transaction. Rebuilding that ID from a dictionary position or a
        card UID can point at a different native Combat when multiple attacks
        are in flight.
        """
        port = getattr(session, "_rules_port_session", None)
        if port is not None:
            try:
                attacker = port.get_card(int(attacker_uid)) or int(attacker_uid)
                combat = port.combat_manager.combat_for_attacker(attacker)
                serial = int(combat.combat_id.serial_number) if combat else 0
                if serial > 0:
                    return game_engine.CombatId(player_uid, serial)
            except (AttributeError, TypeError, ValueError):
                pass
        return game_engine.CombatId(player_uid, int(fallback_serial))

    def _push_combat_listing_phase(self, session, pl_t, ai_t, bstate):
        """Push the current combat listing (no blockers yet) at a damage step
        so the client's BattleStateAssignDamage can auto-commit and send
        AssignDamageOrderTransaction."""
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        self._maybe_attach_rules_port(session, game, bstate)
        attackers = bstate.get("player_attackers") or {}
        combats = []
        for i, u in enumerate(attackers):
            attacker_uid = int(u)
            scid = game_engine.SessionCardId(game_engine.UID(attacker_uid))
            combat_id = self._wire_combat_id_for_attacker(
                session, pl_t, attacker_uid, i + 1)
            cs = game_engine.CombatSessionEventArgs()
            cs.player_id = pl_t
            cs.id = combat_id
            cs.attacker = scid
            cs.blockers = []
            combats.append(cs)
        if combats:
            game.push_combat_listing(pl_t, combats)
        self._send_battle_events(session, game, pl_t)

    def _player_can_attack_troops(self, session, user_id=None):
        bstate = getattr(self, "_current_bstate", None)
        if (getattr(session, "_rules_port_session", None) is not None or
                (bstate or {}).get("_rules_port_attached")):
            from rules_port.combat_rules import player_has_eligible_attackers
            if bstate is None:
                from rules_port.persistence import load_state
                bstate = load_state(session)
            if user_id is None:
                user_id = (self.user_profile.get("id")
                           if isinstance(self.user_profile, dict) else 0)
            if user_id is None:
                return False
            return player_has_eligible_attackers(
                _db, session.session_id, bstate or {}, int(user_id))
        import ai
        return ai.player_can_attack_troops(self, session, user_id)

    def _ai_can_attack_troops(self, session):
        bstate = getattr(self, "_current_bstate", None)
        if (getattr(session, "_rules_port_session", None) is not None or
                (bstate or {}).get("_rules_port_attached")):
            from rules_port.combat_rules import player_has_eligible_attackers
            if bstate is None:
                from rules_port.persistence import load_state
                bstate = load_state(session)
            return player_has_eligible_attackers(
                _db, session.session_id, bstate or {}, 0)
        import ai
        return ai.ai_can_attack_troops(self, session)

    def _refresh_native_combat_branch(self, session, battle_state, *,
                                      active_is_ai, active_phase):
        """Refresh the combat-branch fact the native FirstMainPhase reads.

        The checkpoint fact is written at the turn's Prep, so a troop that
        entered play afterwards - the client's own Speed troop, or an AI card
        that resolved inside the human's response window - leaves it stale.
        The native phase graph then leaves First Main straight for Second Main
        and skips DeclareAttack entirely.  Returns True when the persisted
        phase plan was rebuilt and therefore must be re-read by the caller.
        """
        self._current_bstate = battle_state
        ready = bool(self._ai_can_attack_troops(session) if active_is_ai
                     else self._player_can_attack_troops(session))
        if active_is_ai:
            # The AI driver rebuilds its own plan when it reaches First Main.
            battle_state["player_has_ready_troop"] = ready
            return False
        if active_phase != game_engine.ETurnPhases.FirstMainPhase:
            # Only First Main may be rebuilt: from Second Main on, swapping the
            # base plan for the combat plan renumbers ``phase_idx`` (Second
            # Main becomes DeclareCombat) and strands the client's next pass.
            return False
        if bool(battle_state.get("player_has_ready_troop")) == ready:
            return False
        from rules_port.lifecycle import build_turn_phases
        from rules_port.persistence import save_state
        battle_state["player_has_ready_troop"] = ready
        battle_state["turn_phases"] = build_turn_phases(battle_state)
        save_state(session, battle_state)
        return True

    def _ai_declare_attackers(self, game, session, ai_t, pl_t, battle_state):
        import ai
        return ai.ai_declare_attackers(self, game, session, ai_t, pl_t, battle_state)

    def _resolve_ai_combat_damage(self, session, pl_t, ai_t, bstate):
        import ai
        return ai.resolve_ai_combat_damage(self, session, pl_t, ai_t, bstate)

    def _resolve_combat_damage(self, session, pl_t, ai_t, bstate,
                               first_strike=False):
        """Resolve player combat through the native RulesPort."""
        from rules_port.context import EffectContext
        from rules_port.combat_damage import resolve
        port = getattr(session, "_rules_port_session", None)
        if port is not None:
            # CombatManager is authoritative while the session is live; keep
            # the legacy-shaped checkpoint as its persistence/wire projection
            # for the native damage adapter and reconnects.
            def card_uid(value):
                value = getattr(value, "session_card_id", value)
                value = getattr(value, "uid", value)
                return int(getattr(value, "uid64", value))
            declared = {}
            blockers = {}
            order = {}
            for combat in port.combat_manager.combats:
                if combat.attacker is None:
                    continue
                try:
                    attacker_uid = card_uid(combat.attacker)
                    defender_uid = card_uid(combat.defender)
                except (TypeError, ValueError):
                    continue
                declared[str(attacker_uid)] = str(defender_uid)
                blocker_ids = []
                for blocker in combat.blockers:
                    try:
                        blocker_ids.append(card_uid(blocker))
                    except (TypeError, ValueError):
                        continue
                if blocker_ids:
                    blockers[str(attacker_uid)] = [str(uid)
                                                    for uid in blocker_ids]
                    order[str(attacker_uid)] = [str(uid)
                                                for uid in blocker_ids]
            if declared:
                bstate["player_attackers"] = declared
                bstate["ai_blockers"] = blockers
                bstate["player_damage_order"] = order
                from rules_port.persistence import save_state
                save_state(session, bstate)
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        context = EffectContext.from_rules_port(
            game, session, _db, self, pl_t, ai_t, bstate,
            "", ability=None)
        result = resolve(context, first_strike=first_strike,
                         attacker_key="player_attackers",
                         blocker_key="ai_blockers")
        if game.events:
            self._send_battle_events(session, game, pl_t)
        # Combat damage is simultaneous, so the champion's defeat is decided
        # once the whole step (damage + lifedrain) is applied.
        self._check_champion_health(session, pl_t, ai_t, bstate)
        return result

    def _priority_context_for(self, phase, bstate=None):
        """Pick the EPriorityContext describing where priority goes when the
        player passes the given phase — drives the client's Pass button label
        (e.g. "Proceed to Next Main Phase" / "Proceed to End Turn"). Mirrors
        the client's PriorityWindowAction.PostUpdate choice for the active player.

        Pass `bstate` so the FirstMainPhase case can pick the correct label
        (ProcedeToCombat when the player can declare an attack, otherwise
        ProcedeToSecondMain) using the already-computed
        player_has_ready_troop flag — the server is authoritative, so it must
        choose the same context the client's PostUpdate would.
        """
        # A non-empty chain: the player's pass button becomes "Resolve <CardName>"
        # (EPriorityContext.ResolveTopOfChain), mirroring the client's
        # BattleStateBase.RebuildPassButton.
        if bstate is not None and (bstate.get("stack") or []):
            return game_engine.EPriorityContext.ResolveTopOfChain
        if phase == game_engine.ETurnPhases.FirstMainPhase:
            if bstate is None or bstate.get("player_has_ready_troop"):
                return game_engine.EPriorityContext.ProcedeToCombat
            return game_engine.EPriorityContext.ProcedeToSecondMain
        if phase == game_engine.ETurnPhases.SecondMainPhase:
            return game_engine.EPriorityContext.ProceedToEndTurn
        if phase == game_engine.ETurnPhases.DeclareCombatPriorityWindow:
            return game_engine.EPriorityContext.ProcedeToCombat
        if phase == game_engine.ETurnPhases.DeclareAttackPriorityWindow:
            return game_engine.EPriorityContext.ProcedeToBlockers
        if phase == game_engine.ETurnPhases.DeclareDefensePriorityWindow:
            return game_engine.EPriorityContext.ResolveCombat
        if phase == game_engine.ETurnPhases.Ready:
            return game_engine.EPriorityContext.Ready
        if phase == game_engine.ETurnPhases.EndPhase:
            return game_engine.EPriorityContext.EndPhase
        return game_engine.EPriorityContext.Normal

    def _push_transaction_ack(self, session):
        """Send an empty 3055 sync packet to the client.

        The client's SessionClient.SubmitTransaction sends only ONE transaction
        at a time and silently DROPS any further transaction while
        m_HasPreviousTransactionBeenRespondedByServer is false
        (Game/Client/SessionClient.cs:45). That flag is reset to true ONLY when
        a NetworkPacketSessionEventArgs (class 255 — the top level of every 3055
        packet) arrives (ClientSessionBase.cs:30). For 3029 transactions the
        server handles WITHOUT pushing 3055 events (SetTurnPhases, stale or
        no-op passes), we must still send an empty sync packet or the next
        transaction — including the Withdraw (QuitGameTransaction) — is silently
        dropped client-side and "nothing happens".
        """
        try:
            pl_t = game_engine.UID.make(244, int(self.client_reck_id))
            game = game_engine.Game(session.session_id, pl_t, game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000))
            pkt = game.make_network_packet(pl_t)  # no events -> empty packet
            dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                                    "00000000-0000-0000-0000-000000000000")
            self._game_scnt = max(self._game_scnt, self.scnt) + 1
            self.scnt = self._game_scnt
            gs_inst = str(session.server_id)
            self.send({
                "issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                "target": "ServiceGameSession", "instance": gs_inst, "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
            }, dw)
            self._event_q.append((self.scnt, dw, {}))
            log_req(f"    Pushed transaction ack (empty 3055, {len(dw)}b)")
        except Exception as e:
            log_req(f"    Transaction ack failed: {e}")

    def _discard_card_to_owner(self, session, pl_t, ai_t, card_uid):
        """Move a card to its OWNER's graveyard.

        Restores user_id = owner_user_id so a stolen card (e.g. Mind Grasp)
        returns to its original owner's graveyard rather than the controller's,
        then updates location to 'discard'. Returns (game_with_events, owner_uid)
        so the caller can send the events targeting the owner. Returns
        (None, None) if the card isn't found.
        """
        from rules_port.host_mutations import discard_card_to_owner
        return discard_card_to_owner(self, session, pl_t, ai_t, card_uid)

    def _extract_transaction_targets(self, inner_bytes, exclude_uid):
        """Extract Card-type UIDs from a 3029 transaction's TargetMap (the
        picked targets), excluding the source/played card."""
        targets = []
        if not isinstance(inner_bytes, bytes):
            return targets
        for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
            try:
                uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                if (uid64 & 0xFF) == 1 and uid64 != int(exclude_uid or 0):
                    targets.append(int(uid64))
            except Exception:
                continue
        return targets

    def _sacrifice_troop(self, game, session, pl_t, ai_t, card_uid,
                         expected_owner_id=None):
        """Pay a sacrifice cost (e.g. Abominate's "sacrifice a troop you
        control"): move the troop to its owner's graveyard (no Dead state — a
        sacrifice isn't combat damage). The troop still DIES, so its Deathcry
        triggers (mirrors the client; e.g. Spiritbound Spy -> Phantom)."""
        row = db_card_basic(session.session_id, card_uid)
        if not row:
            return False
        tpl_guid, owner_id = row
        # Sacrifice costs are restricted to a troop controlled by the
        # activating player.  Re-check the authoritative row immediately
        # before mutating it: a stale TargetMap or a cross-player UID must
        # never move an opponent's permanent into the wrong graveyard.
        card_row = db_card_sacrifice_info(
            session.session_id, int(card_uid), conn=_db)
        if not card_row:
            log_req(f"    Sacrifice rejected missing card {int(card_uid)}")
            return False
        actual_owner, location, card_type = card_row
        if (str(location or "").lower() != "warzone" or
                "troop" not in str(card_type or "").lower()):
            log_req(
                f"    Sacrifice rejected invalid zone/type uid={int(card_uid)} "
                f"owner={int(actual_owner or 0)} location={location!r} type={card_type!r}")
            return False
        if expected_owner_id is not None and int(actual_owner or 0) != int(expected_owner_id):
            log_req(
                f"    Sacrifice rejected wrong owner uid={int(card_uid)} "
                f"expected={int(expected_owner_id)} actual={int(actual_owner or 0)}")
            return False
        owner_id = int(actual_owner or 0)
        # Sacrifice is a native death transition: zone events, replacement
        # checks, CardSacrificed, and Deathcry resolution stay inside one
        # RulesPort lifecycle.
        from rules_port.context import EffectContext
        from rules_port.death_effects import kill_troop
        from rules_port.persistence import load_state, save_state
        sacrifice_state = load_state(session)
        sacrifice_state["_rules_port_attached"] = True
        sacrifice_state["_rules_port_native_effect"] = True
        result = kill_troop(
            EffectContext.from_rules_port(
                game, session, _db, self, pl_t, ai_t, sacrifice_state,
                "", ability=None), int(card_uid), cause="sacrifice")
        save_state(session, sacrifice_state)
        return str(result).startswith("killed")

    def _push_discard_prompt(self, game, session, pl_t, ai_t, bstate,
                             ability_guid=None):
        """Push a class-23 AbilityActivationDataRequired prompt so the PLAYER
        picks a hand card to discard (e.g. a Deathcry that forces each opposing
        champion to discard, like Bloatcap). The follow-up
        SetAbilityActivationDataTransaction discards the chosen card."""
        _be = self._checkpoint_engine(session)
        player_champ_scid = getattr(self, "_player_champ_scid", None)
        resolving_ability = ability_guid or bstate.get("resolving_ability", "")
        # AI-controlled abilities must never open a human card picker.  The
        # responsible owner is carried by the resolver checkpoint; choose a
        # random card from that owner's hand and let the normal discard
        # mutation/event path apply it.
        owner_id = bstate.get("resolving_owner_id")
        human_owner = self.user_profile.get("id") if isinstance(
            self.user_profile, dict) else None
        if owner_id is not None and int(owner_id or 0) != int(human_owner or -1):
            rows = [(row[1],) for row in db_ordered_zone_rows(
                session.session_id, int(owner_id), "hand", conn=_db)]
            if rows:
                chosen = int(random.choice(rows)[0])
                gd, _owner_uid = self._discard_card_to_owner(
                    session, pl_t, ai_t, chosen)
                if gd is not None:
                    game.events.extend(gd.events)
                bstate["ai_discarded_uid"] = chosen
                log_req(f"    AI discard resolved without player prompt: {chosen}")
            else:
                bstate["ai_discarded_uid"] = None
            return
        # Ability configuration path (same protocol used by Necrotic Mage's
        # PVE ability): class-23 opens BattleStateConfigureAbility and the
        # selected hand card is returned in SetAbilityActivationData.
        hand = [game_engine.SessionCardId(game_engine.UID(int(uid))) for (uid,) in
                db_hand_card_uids(
                    session.session_id,
                    self.user_profile["id"] if self.user_profile else 0,
                    conn=_db)]
        if not hand:
            # The authored discard target allows a best-effort minimum.  An
            # empty hand therefore resolves the Deathcry as a no-op; opening
            # class 23 with min/max=1 and zero targets leaves the client in a
            # picker it cannot answer and strands the native chain.
            log_req("    Deathcry discard: no legal hand target")
            return "discard: no legal target"
        prompt = self._discard_prompt_data(resolving_ability, bstate=bstate)
        if not prompt or not prompt[1]:
            log_req("    Deathcry discard: missing metadata prompt target")
            return
        child_ability, target_template = prompt
        source_uid = bstate.get("resolving_source_uid")
        source_card_id = (game_engine.SessionCardId(game_engine.UID(
            int(source_uid))) if source_uid else
            (player_champ_scid if player_champ_scid else pl_t))
        options = game._make_event(game_engine.PlayerOptionListSessionEventArgs)
        options.player_id = pl_t
        opt = game._make_event(game_engine.PlayerOptionSessionEventArgs)
        opt.card = source_card_id
        opt.state = game_engine.ECardUsage.Activate
        child = game._make_event(game_engine.OptionInstanceSessionEventArgs)
        child.opt_id = game_engine.ResourceId.from_str(child_ability)
        child.target_ids = [game_engine.ResourceId.from_str(target_template)]
        tgt = game._make_event(game_engine.TargetInstanceSessionEventArgs)
        tgt.target_index = 0
        tgt.target_id = game_engine.ResourceId.from_str(target_template)
        tgt.targets = hand
        child.target_instances = [tgt]
        child.min_target_counts = [1]
        child.max_target_counts = [1]
        opt.instances = [child]
        options.options = [opt]
        game._push(options)
        req = game_engine.AbilityActivationDataRequiredSessionEventArgs()
        req.player_id = pl_t
        req.ability_instance_id = 1
        req.ability_parent_id = 0
        req.source_card_id = source_card_id
        req.ability_template_id = game_engine.ResourceId.from_str(child_ability)
        req.effect_group_id = 1
        req.effect_instance_ids = [0]
        req.resolve_chain = False
        game._push(req)
        game.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
        bstate["pending_discard_ability"] = child_ability
        bstate["pending_discard_ability_instance_id"] = 1
        bstate["pending_discard_target_template"] = target_template
        bstate["pending_discard_scid"] = None
        _be.save_state(session, bstate)
        log_req("    Deathcry discard: class-23 prompt pushed (player discards)")

    def _resolve_champion_void_targets(self, game, session, pl_t, ai_t,
                                       bstate, ability_guid):
        """Void the troops a champion power chose via its gamedata m_VoidTarget
        (e.g. Bun'jitsu's "Void two ready troops you control") and remember the
        SUM of the voided troops' ATK/DEF so the summoned-token buff leaves can
        be data-driven ("+[ATK] equal to the voided troops' [ATK] plus 3")."""
        import json as _j
        uids = bstate.get("champion_void_uids") or []
        if not uids:
            return
        raw_json = db_ability_raw_json(ability_guid, conn=_db)
        if not raw_json:
            return
        try:
            rec = _j.loads(raw_json)
        except Exception:
            return
        vt = (rec.get("m_VoidTarget") or {})
        if not (vt.get("m_Guid") or ""):
            return
        voided = 0
        voided_uids = []
        stats = {"atk": 0, "def": 0}
        for uid in uids:
            row = db_card_combat_identity(
                session.session_id, int(uid), conn=_db)
            if not row or row[1] != "warzone":
                continue
            # Bun'jitsu: "+[ATK] equal to the VOIDED TROOPS' [ATK] plus 3" —
            # with two voided troops the summoned token's buff sums BOTH
            # troops' attack/defense, then adds the fixed +3.
            stats["atk"] += int(row[3] or 0)
            stats["def"] += int(row[4] or 0)
            db_move_card_to_location(
                session.session_id, int(uid), "void", position=0, conn=_db)
            _db.commit()
            scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
            tpl_guid = row[2]
            _tpl, ct, _n, _c, atk, def_, _g = self._card_full_data(
                game, scid, tpl_guid)
            owner = pl_t if (row[0] or 0) != 0 else ai_t
            game.push_card_updated(scid, owner,
                                   game_engine.ECardCollections.Void, ct,
                                   template_id=tpl_guid, attack=atk,
                                   defense=def_)
            voided += 1
            voided_uids.append(int(uid))
        if stats:
            bstate["champion_voided_stats"] = stats
        if voided_uids:
            bstate.setdefault("ability_lists", {})["VoidedCards"] = \
                list(voided_uids)
        log_req(f"    Champion void targets: {voided} voided "
                f"(stats={stats})")

    def _autopass_game_started_chain(self, session, pl_t, ai_t, bstate,
                                     game):
        """Resolve the mandatory chain created by ``GameStartedEvent``.

        Opening setup must not expose a ResolveTopOfChain pass button.  Use
        the same stack resolver as an ordinary pass, so nested triggers and
        generated cards retain normal ordering and event serialization.  If a
        setup ability genuinely needs player input, leave its pending state in
        place and let the regular prompt path take over.
        """
        port = getattr(session, "_rules_port_session", None)
        if port is not None:
            # Setup triggers are already registered with the native action
            # stack.  Auto-pass their native priority window so opening-game
            # resolution cannot select or pop an item from the compatibility
            # stack.  The latter remains only a durable event projection.
            if port.chain.is_empty:
                port.rehydrate_projected_chain()
            if not port.chain.is_empty:
                resolved = 0
                for _ in range(256):
                    action = port.action_stack.peek()
                    if action is None:
                        break
                    from rules_port.kernel import PriorityWindowAction
                    if isinstance(action, PriorityWindowAction):
                        if not port.auto_pass_internal_priority(port.player_ids):
                            break
                    elif not port.tick():
                        break
                    resolved += 1
                    if port.chain.is_empty:
                        break
                port.persist()
                if resolved:
                    log_req(f"    Auto-passed {resolved} native GameStarted "
                            "chain action(s)")
                return resolved
            if bstate.get("stack"):
                # A native session must not silently downgrade setup to the
                # legacy resolver if native rehydration is incomplete.
                raise RuntimeError(
                    "RulesPort GameStarted chain has no native action")
            return 0

        _be = self._checkpoint_engine(session)

        resolved = 0
        had_chain = not _be.stack_empty(bstate)
        # A malformed/cyclic setup effect must not hang the game-start
        # transaction forever.  Normal authored setup chains are much smaller
        # than this, including generated follow-up triggers.
        max_items = 256
        while (not _be.stack_empty(bstate) and
               resolved < max_items):
            if (bstate.get("pending_choice") or
                    bstate.get("pending_deck_search") or
                    bstate.get("pending_trigger") or
                    bstate.get("pending_discard_ability") or
                    bstate.get("pending_conversation")):
                break
            item = _be.stack_pop(bstate)
            _be.stack_reset_passes(bstate)
            self._resolve_stack_item(
                session, pl_t, ai_t, bstate, item, game)
            resolved += 1

        if resolved >= max_items and not _be.stack_empty(bstate):
            log_req("    GameStarted chain exceeded 256 items; leaving "
                    "remaining items for normal priority")
        elif resolved:
            log_req(f"    Auto-passed {resolved} GameStarted chain item(s)")

        if (had_chain and _be.stack_empty(bstate) and
                not bstate.get("pending_choice") and
                not bstate.get("pending_deck_search") and
                not bstate.get("pending_trigger") and
                not bstate.get("pending_discard_ability") and
                not bstate.get("pending_conversation")):
            game.push_chain_empty()
        return resolved

    def _suppress_completed_game_started_chain_events(self, game,
                                                       event_start):
        """Hide a fully auto-passed setup chain from the client animation UI.

        GameStarted abilities are resolved before their initial network packet
        is sent.  Sending their push/pop pair in that same packet makes the
        client move a source champion into the chain while it is still
        building the opening board; the remove animation can then be applied
        before the corresponding chain card is registered and leave the
        champion displayed at the bottom of the chain.  The ability's effect
        events remain in the packet; only the transient chain presentation is
        removed.

        ``event_start`` scopes this to events created by GameStarted setup and
        preserves any earlier events in the transaction.
        """
        chain_event_types = (
            game_engine.AbilityPushedOnChainSessionEventArgs,
            game_engine.TopOfChainResolvedSessionEventArgs,
            game_engine.RemovedTopOfChainSessionEventArgs,
            game_engine.ChainEmptySessionEventArgs,
        )
        prefix = game.events[:event_start]
        setup_events = game.events[event_start:]
        game.events = prefix + [
            event for event in setup_events
            if not isinstance(event, chain_event_types)
        ]

    # --- rules_port.chain_items host hooks (Practice/PvE projection) -------
    def chain_load(self, session):
        from rules_port import lifecycle
        return lifecycle.load_state(session) or {}

    def chain_save(self, session, state):
        from rules_port import lifecycle
        lifecycle.save_state(session, state)

    def chain_new_game(self, session, state, player_uid, ai_uid):
        return self._fresh_game(session, player_uid, ai_uid, state)

    def chain_send(self, session, game, player_uid, ai_uid):
        self._send_battle_events(session, game, player_uid)

    def chain_resume_continuation(self, session, state, game, player_uid,
                                  ai_uid, continuation):
        from rules_port.resolution import resume_ability_continuation_parents
        return resume_ability_continuation_parents(
            self, game, session, _db, player_uid, ai_uid, state,
            continuation)

    def chain_card_data(self, game, scid, template_guid):
        return self._card_full_data(game, scid, template_guid, None)

    def chain_dispatch(self, session, game, state, player_uid, ai_uid,
                       event_type, source_uid, owner_id, **event_data):
        return self._dispatch_native_trigger(
            game, session, player_uid, ai_uid, state, event_type, source_uid,
            owner_id, **event_data)

    def chain_state_based(self, session, state, game, player_uid, ai_uid):
        """Apply lethal state-based actions before chain priority returns."""
        from rules_port.context import EffectContext
        from rules_port.death_effects import state_based_deaths
        state["_rules_port_attached"] = True
        previous = getattr(self, "_current_bstate", None)
        self._current_bstate = state
        try:
            return bool(state_based_deaths(EffectContext.from_rules_port(
                game, session, _db, self, player_uid, ai_uid, state,
                "state_based_death", ability=None)))
        finally:
            if previous is None:
                try:
                    del self._current_bstate
                except AttributeError:
                    pass
            else:
                self._current_bstate = previous

    def chain_mark_warzone_entry(self, session, source_uid):
        """Permanent bookkeeping for a troop/artifact reaching the warzone."""
        db_card_set_warzone_arrival(session.session_id, source_uid)
        db_set_card_resolved_at(
            session.session_id, source_uid,
            self._next_resolve_counter(session))

    def chain_push_empty(self, port, state, item, pending):
        if pending or state.get("stack"):
            return False
        # The compatibility stack empties after each projected item, but
        # RulesPort may still own further native chain items (for example
        # Nerissa's two troop summons followed by the champion ability).
        # Only the final native item empties the client's chain.
        native_chain = getattr(port, "chain", None)
        return len(getattr(native_chain, "_instance_ids", ())) <= 1

    def chain_finalize(self, session, state, game, item, pending,
                       player_uid, ai_uid):
        """Return main-phase priority with the completed chain packet.

        Practice/PvE has one client, so its last chain item must publish the
        active human's GreenLight alongside the chain removal. Tournament PvP
        uses its own two-player priority projection hook.
        """
        if pending or state.get("stack"):
            return
        port = getattr(session, "_rules_port_session", None)
        native_chain = getattr(port, "chain", None)
        if len(getattr(native_chain, "_instance_ids", ())) > 1:
            return
        active_player = getattr(port, "active_player_id", None)
        if active_player is None or not _same_uid(active_player, player_uid):
            return
        game.push_green_light(
            player_uid, game_engine.EPriorityContext.Normal)

    def chain_after_item(self, session, state, game, item, player_uid, ai_uid):
        """A resolved spell/ability/trigger can deal the last health."""
        self._check_champion_health(session, player_uid, ai_uid, state)

    def chain_ability_owner(self, state, item, owner_id, player_uid, ai_uid):
        """Normalize a manual/champion ability item's owner for this mode.

        Native PVE descriptors use typed ServicePlayer/AI identities for
        scheduler ownership.  Generated cards and deck mutations require the
        persisted profile owner (player) or the canonical AI owner (0).
        """
        if state.get("pvp"):
            return owner_id
        player_owner = int((self.user_profile or {}).get("id", 0)) \
            if isinstance(self.user_profile, dict) else 0
        if _same_uid(owner_id, player_uid) or owner_id == player_owner:
            return player_owner
        if _same_uid(owner_id, ai_uid) or owner_id == 0:
            return 0
        return owner_id

    def _resolve_stack_item(self, session, pl_t, ai_t, bstate, item, game):
        """Execute the top of the chain (`item`, already popped) and push the
        resolve chain events onto `game` (TopOfChainResolved + RemovedTopOfChain).

        Item kinds:
          - troop:  CastSpells -> Warzone (came out this turn, summoning sick).
          - trigger: a Deathcry (ability.resolve_stack_trigger).
          - spell:   execute the typed spell graph then CastSpells -> Discard.
          - ability: a champion ability — resolve its typed graph + class-23
                     discard prompt when authored metadata requires one.
        """
        _be = self._checkpoint_engine(session)
        kind = item.get("kind")
        instance_id = int(item.get("instance_id", 1))
        game.push_top_of_chain_resolved(instance_id)
        game.push_removed_top_of_chain(instance_id)
        if kind == "troop":
            uid = int(item.get("source_uid") or 0)
            if uid:
                loc = db_card_location(session.session_id, uid, conn=_db)
                if loc != "CastSpells":
                    log_req(f"    Troop {uid} already left the chain "
                            f"(loc={loc}) — skipped")
                    _be.save_state(session, bstate)
                    return game
                # Permanent cast abilities resolve before the card enters
                # Warzone. A cast graph may grant a Deploy ability, which
                # must be present when CardEnteredZoneEvent is dispatched.
                from rules_port.resolution import \
                    resolve_port_played_permanent
                owner_id = int(db_card_owner_id(
                    session.session_id, uid, conn=_db) or 0)
                resolve_port_played_permanent(
                    game, session, _db, self, pl_t, ai_t, bstate, item,
                    owner_id)
                if bstate.get("resolution_paused"):
                    _be.save_state(session, bstate)
                    return game
                scid = game_engine.SessionCardId(game_engine.UID(uid))
                db_card_set_warzone_arrival(session.session_id, uid)
                db_set_card_resolved_at(session.session_id, uid, self._next_resolve_counter(session))
                crow2 = db_card_with_template(session.session_id, uid)
                if crow2:
                    owner_id = db_card_owner_id(
                        session.session_id, uid, conn=_db) or 0
                    owner_sid = pl_t if (owner_id or 0) != 0 else ai_t
                    # Populate the CardDef (cost/atk/def/thresholds/abilities)
                    # so the re-pushed CardUpdated renders the full card — without
                    # it a troop played mid-game (e.g. via a spell) shows zeros.
                    self._card_full_data(game, scid, crow2[0], None)
                    game.push_card_updated(scid, owner_sid, game_engine.ECardCollections.Warzone,
                                           game_engine.card_type_from_db(crow2[1]),
                                           template_id=crow2[0])
                    game.push_troop_card_played(scid, owner_sid)
                    # A troop resolving to the warzone from the stack fires its
                    # own CardEnteredZone triggers plus those of other warzone
                    # troops under the same controller (Adamanthian Scrivener).
                    self._dispatch_native_trigger(
                        game, session, pl_t, ai_t, bstate,
                        "CardEnteredZoneEvent", uid, owner_id,
                        event_source_collection="CastSpells",
                        event_destination_collection="warzone")
                    # Normal plays publish CardCastEvent when they enter the
                    # chain. Keep a fallback for legacy/effect-created chain
                    # items, using the same champion-source/card-target shape.
                    if not item.get("card_cast_event_dispatched"):
                        from rules_port.chain_items import dispatch_card_cast
                        dispatch_card_cast(
                            self, session, game, bstate, pl_t, ai_t, uid,
                            owner_id)
        elif kind == "trigger":
            from rules_port.resolution import resolve_port_trigger
            resolve_port_trigger(self, game, session, _db, pl_t, ai_t,
                                 bstate, item)
        elif kind == "spell":
            uid = int(item.get("source_uid") or 0)
            # The spell must still be on the chain (CastSpells) to resolve.  A
            # Countermagic/interrupt already moved it to the graveyard while it
            # waited for the stack — skip its BOM so a countered spell never
            # also draws/buffs/damages (the client's interrupted card fizzles).
            loc_details = db_card_owner_zone_state(
                session.session_id, uid, conn=_db)
            loc = loc_details[1] if loc_details else "discard"
            if loc != "CastSpells":
                log_req(f"    Spell {uid} already left the chain "
                        f"(loc={loc}) — countered/interrupted, BOM skipped")
                _be.save_state(session, bstate)
                return game
            # Track actions cast this turn (for ChampionActionsCastThisTurn).
            turn_now = int(bstate.get("turn_number", 1))
            cast_side = "ai" if (loc_details[0] or 0) == 0 else "player"
            cast_turn_key = f"{cast_side}_actions_cast_turn"
            cast_count_key = f"{cast_side}_actions_cast_this_turn"
            if bstate.get(cast_turn_key) != turn_now:
                bstate[cast_count_key] = 0
                bstate[cast_turn_key] = turn_now
            bstate[cast_count_key] = int(bstate.get(cast_count_key, 0)) + 1
            bstate["player_spell_target"] = item.get("target_uid")
            bstate["resolving_source_uid"] = item.get("source_uid")
            # The caster (0 = AI) decides which champion "you"/"opposing"
            # effects target; never assume the human cast the spell.
            bstate["resolving_owner_id"] = (
                loc_details[0] if loc_details else self.user_profile["id"])
            bstate["x_cost"] = int(item.get("x_cost") or 0)
            from rules_port.resolution import resolve_port_played_spell
            resolve_port_played_spell(
                game, session, _db, self, pl_t, ai_t, bstate,
                item.get("ability_guids", []),
                activations=item.get("activations"),
                played_from_hand=bool(item.get("played_from_hand")),
                voided_cost_uids=item.get("voided_cost_uids", ()))
            # A reveal/deck-search or revealed-card selection is a real client
            # continuation.  Do not move the spell to the graveyard or emit
            # ChainEmpty until that continuation has supplied its target; the
            # previous code discarded Oakhenge immediately and left the
            # reveal packet detached from the waiting UI.
            if bstate.get("resolution_paused"):
                _be.save_state(session, bstate)
                return game
            # "When you play an action/... " triggers (e.g. Chimes of the
            # Zodiac's "copy it") fire against the played spell.
            if (item.get("source_uid") and
                    not item.get("card_cast_event_dispatched")):
                # Keep the event source/target identities consistent for AI
                # and player casts. Champion triggers such as Rowdy inspect
                # the cast card through TriggerTarget.
                from rules_port.chain_items import dispatch_card_cast
                cast_owner_id = int(loc_details[0] or 0) if loc_details else 0
                dispatch_card_cast(
                    self, session, game, bstate, pl_t, ai_t,
                    int(item["source_uid"]), cast_owner_id)
            bstate.pop("player_spell_target", None)
            bstate.pop("resolving_source_uid", None)
            bstate.pop("x_cost", None)
            if uid:
                scid = game_engine.SessionCardId(game_engine.UID(uid))
                # Re-read the card's zone AFTER its BOM resolved: a leaf may
                # have moved it into the deck (Eternal Youth / Chronic Madness
                # "Put this into your deck") — only a spell still in CastSpells
                # goes to the graveyard.
                loc = db_card_location(session.session_id, uid, conn=_db) or "discard"
                if loc != "deck":
                    # Default: the spent spell goes to the graveyard. A spell
                    # that moved itself into the deck (Eternal Youth's
                    # Escalation "PutThisIntoYourDeck") was already pushed by
                    # the leaf — skip the discard.
                    db_card_discard_spell(session.session_id, uid)
                    # The spell entered the crypt — "when a card enters an
                    # opposing crypt" triggers (Incantation of Fear) fire here.
                    self._dispatch_native_trigger(
                        game, session, pl_t, ai_t, bstate,
                        "CardEnteredZoneEvent", uid,
                        (loc_details[0] if loc_details else 0),
                        event_source_collection="CastSpells",
                        event_destination_collection="discard")
                    crow2 = db_card_with_template(session.session_id, uid)
                    if crow2:
                        card_owner = (loc_details[0] if loc_details else 0)
                        owner_sid = pl_t if (card_owner or 0) != 0 else ai_t
                        # Populate the CardDef so the graveyard card renders its
                        # full data (cost/atk/def/thresholds/abilities) — a bare
                        # push_card_updated leaves the grave card blank.
                        self._card_full_data(game, scid, crow2[0], None)
                        game.push_card_updated(scid, owner_sid, game_engine.ECardCollections.Discard,
                                               game_engine.card_type_from_db(crow2[1]),
                                               template_id=crow2[0])
        elif kind == "ability":
            ag = item.get("ability_guid", "")
            if (str(ag).lower() ==
                    "f2d6797b-1a24-4c3d-9239-a27a2e0de0ff"):
                from rules_port.context import EffectContext
                from rules_port.tunneling import resolve_surface
                result = resolve_surface(EffectContext.from_rules_port(
                    game, session, _db, self, pl_t, ai_t, bstate, str(ag),
                    ability=None), item.get("source_uid"))
                log_req(f"    Tunneling Surface {item.get('source_uid')}: "
                        f"{result}")
                ag = ""
            if ag:
                # The activation's chosen target (e.g. Dimmid's Lifedrain troop)
                # must reach the BOM leaves.
                bstate["player_mod_target"] = item.get("target_uid")
                bstate["player_spell_target"] = item.get("target_uid")
                bstate["resolving_ability"] = str(ag)
                # Champion ability context: the source is the champion card and
                # its owner decides which side heals/takes damage (e.g.
                # Dimmid's Lifedrain).  Leaves need resolving_source_uid +
                # resolving_owner_id even though the champion is not a
                # game_cards row.
                src_uid = item.get("source_uid")
                bstate["resolving_source_uid"] = src_uid
                p_scid = getattr(self, "_player_champ_scid", None)
                a_scid = getattr(self, "_ai_champ_scid", None)
                if p_scid is not None and src_uid is not None \
                        and int(src_uid) == int(p_scid.uid.uid64):
                    bstate["resolving_owner_id"] = (
                        self.user_profile["id"] if self.user_profile else 0)
                elif a_scid is not None and src_uid is not None \
                        and int(src_uid) == int(a_scid.uid.uid64):
                    bstate["resolving_owner_id"] = 0
                else:
                    source_owner = db_card_owner_id(
                        session.session_id, int(src_uid or 0), conn=_db)
                    bstate["resolving_owner_id"] = (
                        int(source_owner) if source_owner is not None else
                        (self.user_profile["id"] if self.user_profile else 0))
                # Multi-target champion powers with an m_VoidTarget (e.g.
                # Bun'jitsu's "Void two ready troops you control") void the
                # chosen troops now and remember their stats so the summoned
                # token's buffs ("+[ATK] equal to the voided troop's [ATK] plus
                # 3") are data-driven.
                ability_event_start = len(game.events)
                ability_player_health_before = int(
                    bstate.get("player_health", game.player_health))
                ability_ai_health_before = int(
                    bstate.get("ai_health", game.ai_health))
                self._resolve_champion_void_targets(
                    game, session, pl_t, ai_t, bstate, str(ag))
                from rules_port.resolution import resolve_port_ability
                target_map = {}
                if item.get("target_uid") is not None:
                    # Champion powers often contain duplicate target-template
                    # entries: the effect mapping may refer to index 1 even
                    # though the activation payload carries the single chosen
                    # target at index 0. Preserve that explicit choice for
                    # every equivalent authored target slot; otherwise the
                    # native resolver falls back to the first legal robot.
                    target_uid = int(item["target_uid"])
                    try:
                        from gamedata import DEFAULT_RECORD_STORE, ability_graph
                        graph = ability_graph(DEFAULT_RECORD_STORE, str(ag).lower())
                        targets = list(getattr(graph, "targets", ()) or ())
                        first_guid = (getattr(targets[0], "guid", None)
                                      if targets else None)
                        for index, spec in enumerate(targets):
                            if index == 0 or getattr(spec, "guid", None) == first_guid:
                                target_map[index] = target_uid
                    except Exception:
                        target_map = {0: target_uid}
                ability_log = resolve_port_ability(
                    self, game, session, _db, pl_t, ai_t, bstate, ag,
                    source_uid=bstate.get("resolving_source_uid"),
                    owner_id=bstate.get("resolving_owner_id", 0),
                    target_map=target_map)
                # Damage/heal leaves normally emit class 38.  Add a fallback
                # for ability implementations that update the battle state
                # directly, so the champion HUD changes immediately rather
                # than waiting for a later phase refresh.
                ability_player_health_after = int(
                    bstate.get("player_health", game.player_health))
                ability_ai_health_after = int(
                    bstate.get("ai_health", game.ai_health))
                game.player_health = ability_player_health_after
                game.ai_health = ability_ai_health_after
                game.push_champion_health_changed_if_missing(
                    pl_t, ability_player_health_before,
                    ability_player_health_after, since=ability_event_start)
                game.push_champion_health_changed_if_missing(
                    ai_t, ability_ai_health_before,
                    ability_ai_health_after, since=ability_event_start)
                log_req(f"    Champion ability BOM {str(ag)[:8]}: "
                        f"{ability_log} health="
                        f"{ability_player_health_before}->{ability_player_health_after}/"
                        f"{ability_ai_health_before}->{ability_ai_health_after}")
                if self._bom_has_discard(
                        ag, native=bool(bstate.get("_rules_port_attached"))):
                    self._push_discard_prompt(
                        game, session, pl_t, ai_t, bstate, str(ag))
        # Clear transient resolution targets so a later trigger/ability cannot
        # pick up a stale target (e.g. Solitary Exile's Deploy voiding the
        # wrong card because a previous champion power left player_mod_target).
        bstate.pop("player_spell_target", None)
        bstate.pop("player_mod_target", None)
        bstate.pop("resolving_owner_id", None)
        bstate.pop("resolving_ability", None)
        bstate.pop("champion_void_uids", None)
        bstate.pop("champion_voided_stats", None)
        bstate.pop("created_token_uids", None)
        if kind in ("troop", "spell"):
            from rules_port.resolution import \
                restore_played_card_ability_lists
            restore_played_card_ability_lists(
                bstate, item.get("source_uid"))
        if isinstance(bstate.get("ability_lists"), dict):
            bstate["ability_lists"].pop("VoidedCards", None)
        _be.save_state(session, bstate)
        # Refresh both players' HUD (health/resources) after the resolution —
        # a heal from a trigger (e.g. Adamanthian Scrivener) otherwise only
        # shows on the next PlayerUpdated.
        game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        # State-based effects: when the stack is now empty, any troop whose
        # effective defense (base + mod) is 0 or less dies (Immortal included).
        if _be.stack_empty(bstate):
            from rules_port.context import EffectContext
            from rules_port.death_effects import state_based_deaths
            state_based_deaths(EffectContext.from_rules_port(
                game, session, _db, self, pl_t, ai_t, bstate,
                "state_based_death", ability=None))
            if _be.stack_empty(bstate):
                surface_owner = (self.user_profile["id"]
                                 if bstate.get("turn_player") == _be.PLAYER
                                 else 0)
                if bstate.get("_rules_port_attached"):
                    from rules_port.context import EffectContext
                    from rules_port.tunneling import queue_surfaces
                    queue_surfaces(EffectContext.from_rules_port(
                        game, session, _db, self, pl_t, ai_t, bstate,
                        "", ability=None), surface_owner)
                else:
                    from abilities.framework.effects.counters import queue_tunneling_surfaces
                    queue_tunneling_surfaces(
                        _db, session, self, game, pl_t, ai_t, bstate,
                        surface_owner)
        # Champion state check: a spell/trigger that brought a champion to 0
        # health ends the game right here.
        self._check_champion_health(session, pl_t, ai_t, bstate)
        return game

    def _max_hand_size(self, session):
        """Return the effective maximum hand size for the active battle.

        Client ``Player.MaximumHandSize``: ten in PvE/PvE Arena, seven in PvP
        (tournament) sessions.  Campaign class/talent metadata can raise or
        lower the PvE value through ``MaximumHandSizeModifiers``.
        """
        try:
            if (session.session_name or "").startswith("tourney-"):
                return 7
        except Exception:
            pass
        return int(getattr(self, "_campaign_max_hand_size", 10))

    def _starting_hand_size(self, session):
        """Return the effective opening-hand count for the active battle."""
        try:
            if (session.session_name or "").startswith("camp_"):
                return int(getattr(self, "_campaign_starting_hand_size", 7))
        except Exception:
            pass
        return 7

    def _hand_size_unlimited(self, session, bstate):
        """Whether a champion passive currently removes the hand limit.

        Champions have no ``game_cards`` row, so ``champ_guid_map`` carries
        their template GUIDs for the continuous-static scan.  Games started
        before that key existed are repaired here from the handler's champion
        identities so an in-progress battle picks the flag up immediately.
        """
        state = bstate if isinstance(bstate, dict) else {}
        if not state.get("champ_guid_map"):
            guid_map = {}
            owner = 0
            if isinstance(self.user_profile, dict):
                owner = int(self.user_profile.get("id", 0) or 0)
            player_guid = getattr(self, "_player_champ_guid", None)
            ai_guid = getattr(self, "_ai_champ_guid", None)
            if player_guid:
                guid_map[str(owner)] = player_guid
            if ai_guid:
                guid_map["0"] = ai_guid
            if guid_map:
                state["champ_guid_map"] = guid_map
        from rules_port.static_rules import hand_size_unlimited
        return hand_size_unlimited(_db, session.session_id, state)

    def _next_resolve_counter(self, session):
        """Increment and return the resolve counter from battle state.
        Used to stamp resolved_at on cards entering the warzone."""
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        c = bstate.get("resolve_counter", 0) + 1
        bstate["resolve_counter"] = c
        _be.save_state(session, bstate)
        return c

    def _fresh_game(self, session, pl_t, ai_t, bstate):
        """A fresh Game event-builder fully populated from the DB battle state.

        Fresh-per-push is the thread-safe pattern: a Game holds only transient
        event/def data; the authoritative state is in the DB. This helper copies
        EVERY live value (both players: resources, threshold, health, charges,
        spell points) into the fresh Game so any PlayerUpdated it emits reports
        the real numbers — not the 20/0 defaults of a bare Game(). Use this for
        any phase/priority path that pushes PlayerUpdated.
        """
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        game.max_hand_size = self._max_hand_size(session)
        game.player_resources = bstate.get("player_resources", 0)
        game.player_total_resources = bstate.get("player_total_resources", 0)
        game.player_threshold = dict(bstate.get("player_threshold", {}))
        game.player_charges = bstate.get("player_charges", 0)
        game.player_spell_points = bstate.get("player_spell_points", 0)
        game.ai_resources = bstate.get("ai_resources", 0)
        game.ai_total_resources = bstate.get("ai_total_resources", 0)
        game.ai_threshold = dict(bstate.get("ai_threshold", {}))
        game.ai_charges = bstate.get("ai_charges", 0)
        game.ai_spell_points = bstate.get("ai_spell_points", 0)
        game.player_health = bstate.get("player_health", 20)
        game.ai_health = bstate.get("ai_health", 10)
        # Carry the champion SessionCardIds onto this fresh Game so a
        # push_player_updated that falls back to player_champion_card_id /
        # ai_champion_card_id (i.e. no explicit champ_id) still emits a VALID
        # champion id — without this it serializes UID type 0 (undefined),
        # the client stores SessionCardId.Invalid as ChampionSessionCardId,
        # and UIBattle.OnTurnPhaseUpdated KeyNotFound-crashes on it.
        game.player_champion_card_id = getattr(self, "_player_champ_scid", None)
        game.ai_champion_card_id = getattr(self, "_ai_champ_scid", None)
        # Deck sleeves are battle state, not event state: re-apply them to every
        # fresh projection so a PlayerUpdated for either side always carries the
        # sleeve the client's card backs depend on.
        for key, uid, cached in (
                ("player_deck_sleeve_id", pl_t, "_player_deck_sleeve_guid"),
                ("ai_deck_sleeve_id", ai_t, "_ai_deck_sleeve_guid")):
            sleeve = bstate.get(key) or getattr(self, cached, None)
            if sleeve:
                game.set_deck_sleeve(uid, sleeve)
        # Visibility permissions are persisted in battle state, but each
        # network checkpoint uses a fresh Game projection. Re-apply the
        # persisted map here or an underground Subterranean Spy appears to
        # lose opponent-hand visibility at the next phase transition.
        from rules_port.visibility import (
            apply_player_visibility_to_game, project_visible_hands)
        apply_player_visibility_to_game(game, bstate)
        # A fresh packet buffer must carry full opposing-hand definitions when
        # an underground Subterranean Spy permission is already active. The
        # persisted flag alone updates PlayerUpdated but leaves Unity's card
        # cache as black/nulling rectangles until a full CardUpdated arrives.
        project_visible_hands(
            _db, session, self, game, pl_t, ai_t, bstate)
        # Attach the semantic rules host on every normal game build,
        # including Practice.  Combat-only construction is too late for
        # card-play transactions and leaves the dispatcher without a host.
        self._maybe_attach_rules_port(session, game, bstate)
        # _maybe_attach_rules_port can repair a partial legacy checkpoint (or
        # replace an attach-time projection with the shared authoritative
        # dictionary). Refresh the projection after that boundary; otherwise
        # this fresh Game would retain the pre-repair zero-health value in its
        # PlayerUpdated event even though the checkpoint is now valid.
        game.player_health = bstate.get("player_health", 20)
        game.ai_health = bstate.get("ai_health", 10)
        return game

    def _push_fra_reconnect_snapshot(self, session, player_pid):
        """Rebuild the saved FRA board for a client returning to the match.

        This only projects persisted ``game_cards`` and the saved turn
        checkpoint. It never deals, shuffles, advances the phase, or replays
        the rejected card transaction.
        """
        profile = self.user_profile if isinstance(self.user_profile, dict) else {}
        try:
            owner_id = int(profile.get("id") or 0)
            player_pid = int(player_pid)
        except (TypeError, ValueError):
            return False
        if owner_id <= 0 or player_pid <= 0:
            return False

        from rules_port.persistence import load_state as load_rules_state
        state = load_rules_state(session)
        if not isinstance(state, dict) or "turn_player" not in state:
            return False

        def checkpoint_champion(champion_owner_id):
            """Read a synthetic champion identity from the saved checkpoint."""
            champion_uids = state.get("champ_map")
            champion_guids = state.get("champ_guid_map")
            if (not isinstance(champion_uids, dict) or
                    not isinstance(champion_guids, dict)):
                return None
            keys = (str(champion_owner_id), champion_owner_id)
            uid = next((champion_uids[key] for key in keys
                        if key in champion_uids), None)
            guid = next((champion_guids[key] for key in keys
                         if key in champion_guids), None)
            if isinstance(guid, dict):
                guid = guid.get("m_Guid", guid.get("guid"))
            try:
                uid = int(uid)
            except (TypeError, ValueError):
                return None
            if uid <= 0 or not guid:
                return None
            return uid, str(guid)

        # FRA champions are synthetic SessionCardIds and intentionally have
        # no game_cards rows. Prefer a row if another session provides one,
        # then restore the saved UID/template pair from the battle checkpoint.
        player_champion = (db_game_champion(
            session.session_id, owner_id, conn=_db) or
            checkpoint_champion(owner_id))
        ai_champion = (db_game_champion(
            session.session_id, 0, conn=_db) or checkpoint_champion(0))
        if not player_champion or not ai_champion:
            log_req("    FRA reconnect snapshot aborted: missing champion "
                    "rows and checkpoint identities")
            return False
        player_champ_uid, player_champ_guid = player_champion
        ai_champ_uid, ai_champ_guid = ai_champion
        try:
            player_champ_uid = int(player_champ_uid)
            ai_champ_uid = int(ai_champ_uid)
        except (TypeError, ValueError):
            return False
        if (player_champ_uid <= 0 or ai_champ_uid <= 0 or
                (player_champ_uid & 0xff) == 0 or (ai_champ_uid & 0xff) == 0):
            log_req("    FRA reconnect snapshot aborted: invalid champion UID")
            return False

        player_uid = game_engine.UID.make(244, player_pid)
        ai_uid = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        player_champ_scid = game_engine.SessionCardId(
            game_engine.UID(player_champ_uid))
        ai_champ_scid = game_engine.SessionCardId(
            game_engine.UID(ai_champ_uid))
        player_name = str(profile.get("name") or "Player")
        player_champ_name = db_template_name(
            player_champ_guid, conn=_db) or player_name
        ai_name = db_template_name(ai_champ_guid, conn=_db) or "AI Opponent"
        player_sleeve = state.get("player_deck_sleeve_id") or ""
        ai_sleeve = state.get("ai_deck_sleeve_id") or ""
        self._player_champ_scid = player_champ_scid
        self._ai_champ_scid = ai_champ_scid
        self._player_champ_board = (
            player_champ_scid, False, player_champ_name, player_champ_guid)
        self._ai_champ_board = (
            ai_champ_scid, True, ai_name, ai_champ_guid)

        def send_sync(game, label):
            if not game.events:
                return
            packet = game.make_network_packet(player_uid)
            body = encode_datawrapper(
                0, 3055,
                compress_gzip(encode_sync_event(packet)), 1,
                "00000000-0000-0000-0000-000000000000")
            self._game_scnt = max(
                int(getattr(self, "_game_scnt", 0) or 0),
                int(getattr(self, "scnt", 0) or 0)) + 1
            self.scnt = self._game_scnt
            self.send({
                "issuer": (f"0.0.0.0.ServiceGameSession."
                           f"{SERVICE_GAME_SESSION_UID}.{session.session_id}."
                           f"{self.scnt}"),
                "target": "ServiceGameSession",
                "instance": str(session.server_id),
                "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
            }, body)
            log_req(f"    FRA reconnect: sent {label}")

        # JoinDisconnectedGame adds only the reconnecting client locally. Add
        # the AI before GameStarted so Unity has both Player objects when it
        # handles the champion, deck, and board snapshot that follows.
        added = encode_objfmt_response(
            ["Game.Shared.Network.GameSession.PlayerAddedEventArgs",
             "Game.Shared.UID", "Game.Shared.PlayerState", "System.Int32"],
            [("RoutingPlayerId", "uid", int(ai_uid.uid64)),
             ("PlayerState", "struct", ("Game.Shared.PlayerState", [
                 ("PlayerId", "uid", int(ai_uid.uid64)),
                 ("PlayerPosition", "int", 1),
             ]))])
        added_dw = encode_datawrapper(
            0, 3050, compress_gzip(added), 1, client_session_guid(self))
        self.scnt += 1
        self.send({
            "issuer": (f"0.0.0.0.ServiceGameSession.246."
                       f"{session.session_id}.{self.scnt}"),
            "target": "ServiceGameSession",
            "instance": str(session.server_id),
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, added_dw)

        # The original coin toss/Play-or-Draw choice has already happened.
        # Carry its resulting first player into GameStarted and dictate it so
        # reconnect does not restart the toss UI.
        if "player_draws_first_turn" in state:
            player_first = not bool(state.get("player_draws_first_turn"))
        else:
            turn_number = max(1, int(state.get("turn_number", 1) or 1))
            active_is_player = state.get("turn_player") != "ai"
            player_first = (active_is_player if turn_number % 2 else
                            not active_is_player)
        first_uid = player_uid if player_first else ai_uid
        started = game_engine.Game(session.session_id, player_uid, ai_uid)
        started.push_game_started(
            champion_names=[player_name, ai_name],
            champion_template_ids=[player_champ_guid, ai_champ_guid],
            player_first=player_first,
            sleeve_template_ids=[player_sleeve, ai_sleeve])
        started.push_first_player_dictated(first_uid)
        send_sync(started, "GameStarted and first-player state")

        # _fresh_game attaches/rehydrates RulesPort from the saved checkpoint.
        # Its eager hidden-hand projection belongs after GameStarted, so clear
        # those transient events and let _send_battle_events add them to the
        # complete board snapshot below.
        game = self._fresh_game(session, player_uid, ai_uid, state)
        game.events.clear()
        state = getattr(session, "_rules_port_battle_state", None) or state
        self._current_bstate = state
        game.push_player_updated(
            player_uid, deck_sleeve_id=player_sleeve,
            champ_id=player_champ_scid)
        game.push_player_updated(
            ai_uid, deck_sleeve_id=ai_sleeve, champ_id=ai_champ_scid)
        # _send_battle_events prepends the one-time champion CardUpdated and
        # ChampionCardPlayed announcements ahead of the existing event list.
        # Send the player records in their own packet first so the client's
        # State.Players cache contains both controllers before it builds those
        # champion/card views (BuildSleeve indexes that cache directly).
        send_sync(game, "player state")

        # Rebuild hidden deck counters and card membership without exposing
        # any deck identities. Hand and public zones come from their saved
        # rows below; no card is drawn during reconnect.
        for owner, owner_uid, sleeve in (
                (owner_id, player_uid, player_sleeve),
                (0, ai_uid, ai_sleeve)):
            deck_rows = db_game_cards_at_location(
                session.session_id, "deck", user_id=owner, conn=_db)
            deck_ids = []
            for card_uid, template_guid, _owner, card_type, card_state, _ags, _attrs in deck_rows:
                scid = game_engine.SessionCardId(
                    game_engine.UID(int(card_uid)))
                self._card_full_data(game, scid, template_guid)
                game.push_card_updated(
                    scid, owner_uid, game_engine.ECardCollections.Deck,
                    game_engine.card_type_from_db(card_type),
                    template_id=template_guid, state=int(card_state or 0),
                    nulling=True)
                deck_ids.append(scid)
            game.push_deck_created_with_cards(owner_uid, deck_ids, sleeve)

        for owner, owner_uid in ((owner_id, player_uid), (0, ai_uid)):
            hand_rows = db_game_cards_at_location(
                session.session_id, "hand", user_id=owner, conn=_db)
            for card_uid, template_guid, _owner, card_type, card_state, _ags, _attrs in hand_rows:
                scid = game_engine.SessionCardId(
                    game_engine.UID(int(card_uid)))
                self._card_full_data(game, scid, template_guid)
                game.push_card_updated(
                    scid, owner_uid, game_engine.ECardCollections.Hand,
                    game_engine.card_type_from_db(card_type),
                    template_id=template_guid, state=int(card_state or 0),
                    nulling=owner != owner_id)

        # Warzone is projected through the same owner/state/ability path used
        # by a normal phase refresh. Restore the remaining visible zones from
        # their current card rows, including an unresolved spell on CastSpells.
        self._push_warzone_card_updates(game, session, player_uid, ai_uid)
        for zone, collection in (
                ("discard", game_engine.ECardCollections.Discard),
                ("void", game_engine.ECardCollections.Void),
                ("CastSpells", game_engine.ECardCollections.CastSpells),
                ("PlayedResources", game_engine.ECardCollections.PlayedResources),
                ("underground", game_engine.ECardCollections.Underground)):
            for card_uid, template_guid, owner, card_type, card_state, _ags, _attrs in \
                    db_game_cards_at_location(
                        session.session_id, zone, conn=_db):
                scid = game_engine.SessionCardId(
                    game_engine.UID(int(card_uid)))
                self._card_full_data(game, scid, template_guid)
                controller = (player_uid if int(owner) == owner_id else ai_uid)
                game.push_card_updated(
                    scid, controller, collection,
                    game_engine.card_type_from_db(card_type),
                    template_id=template_guid, state=int(card_state or 0),
                    nulling=(zone == "underground" and
                             int(owner) != owner_id))
        from rules_port.visibility import project_visible_underground
        project_visible_underground(
            _db, session, self, game, player_uid, ai_uid, state)

        # Restore the visible chain from its persisted descriptors. The
        # CastSpells CardUpdateds above restore the card views; these events
        # restore which abilities are waiting to resolve and their targets.
        for descriptor in state.get("stack", ()) or ():
            if not isinstance(descriptor, dict):
                continue
            try:
                source_uid = int(descriptor.get("source_uid") or 0)
            except (TypeError, ValueError):
                continue
            if source_uid <= 0:
                continue
            kind = str(descriptor.get("kind") or "").lower()
            ability_guid = descriptor.get("ability_guid")
            if kind in ("troop", "spell"):
                ability_guid = game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID
            if not ability_guid:
                guids = descriptor.get("ability_guids") or ()
                ability_guid = guids[0] if guids else None
            if not ability_guid:
                continue
            target_uids = []
            target_map = descriptor.get("target_map")
            activation_data = descriptor.get("activation_data") or {}
            if not target_map and isinstance(activation_data, dict):
                target_map = activation_data.get("target_map")
            if isinstance(target_map, dict):
                for values in target_map.values():
                    if not isinstance(values, (list, tuple)):
                        values = [values]
                    for value in values:
                        try:
                            target_uids.append(int(getattr(value, "uid64", value)))
                        except (TypeError, ValueError):
                            continue
            for key in ("target_uid", "trigger_target_uid"):
                value = descriptor.get(key)
                if value:
                    try:
                        target_uids.append(int(getattr(value, "uid64", value)))
                    except (TypeError, ValueError):
                        pass
            targets = list(dict.fromkeys(
                uid for uid in target_uids if uid > 0))
            try:
                instance_id = int(descriptor.get("instance_id", 1) or 1)
            except (TypeError, ValueError):
                instance_id = 1
            game.push_ability_on_chain(
                game_engine.SessionCardId(game_engine.UID(source_uid)),
                game_engine.ResourceId.from_str(str(ability_guid)),
                ability_instance_id=instance_id,
                target_card_ids=[game_engine.SessionCardId(
                    game_engine.UID(uid)) for uid in targets],
                ignores_chain=bool(descriptor.get("ignores_chain", False)))

        port = getattr(session, "_rules_port_session", None)
        fallback_side = ("ai" if state.get("turn_player") == "ai"
                         else "player")
        fallback_active = ai_uid if fallback_side == "ai" else player_uid

        def participant_uid(value, default):
            if value is None:
                return default
            try:
                value = int(getattr(value, "uid64", value))
            except (TypeError, ValueError):
                return default
            if value in (int(player_uid.uid64), player_pid):
                return player_uid
            if value in (int(ai_uid.uid64), 0):
                return ai_uid
            return default

        active_uid = participant_uid(
            getattr(port, "active_player_id", None), fallback_active)
        priority_default = active_uid
        priority_action = getattr(getattr(port, "action_stack", None),
                                  "peek", lambda: None)()
        priority_uid = participant_uid(
            getattr(priority_action, "priority_player_id", None),
            priority_default)
        if port is not None:
            phase = getattr(port, "current_turn_phase", None)
        else:
            from rules_port.persistence import current_phase
            phase = current_phase(state)
        if phase is None:
            phase = state.get("phase")
        try:
            phase = int(phase)
        except (TypeError, ValueError):
            phase = int(game_engine.ETurnPhases.FirstMainPhase)

        game.push_green_light(priority_uid, game_engine.EPriorityContext.Normal)
        game.push_turn_phase(phase, active_uid, priority_uid)
        game.push_reconnect_done()
        # _send_battle_events re-announces the valid champion SessionCardIds
        # before the rest of the board and reapplies viewer-specific visibility.
        session._client_in_battle = session.session_id
        session._champion_board_announced = None
        self._send_battle_events(session, game, player_uid)

        pending_input = bool(
            getattr(port, "pending_activation", None) or
            any(state.get(key) for key in (
                "pending_choice", "pending_trigger", "pending_deck_search",
                "pending_discard_ability", "pending_conversation",
                "resolution_paused")))
        if int(priority_uid.uid64) == int(player_uid.uid64):
            if state.get("stack"):
                self._push_phase_options_empty(session, player_uid, ai_uid)
            elif not pending_input:
                self._push_phase_options(session, player_uid, ai_uid, phase)
        log_req(
            f"    FRA reconnect snapshot restored for pid={player_pid} "
            f"session={session.session_id} phase={phase} "
            f"priority={int(priority_uid.uid64)}")
        return True

    def _push_warzone_card_updates(self, game, session, pl_t, ai_t):
        """Re-push CardUpdateds for ALL warzone cards (both players) so the
        client's card icons/abilities/state always match the DB (MVC: model ->
        view). Called at the end of every phase/options push, mirroring the
        PlayerUpdated refresh — a shifted ability/attribute that changed the
        DB row is reflected on the board immediately.
        """
        _be = self._checkpoint_engine(session)
        from abilities.framework._shared import owner_uid
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        voided_by = (bstate or {}).get("voided_by") or {}
        # A card that is the SOURCE of a pending chain item is currently shown
        # on the chain (CastSpells visual); re-pushing its warzone CardUpdated
        # here makes the client yank the chain image while the Resolve button
        # stays — the "empty chain" the user saw.  Leave it alone until the
        # chain item resolves (the resolution re-pushes it anyway).
        chain_sources = {int(i.get("source_uid") or 0)
                         for i in ((bstate or {}).get("stack") or [])
                         if i.get("source_uid")}
        rows = db_warzone_display_rows(session.session_id, conn=_db)
        projections = db_card_instance_dynamic_projections(
            session.session_id, [row[0] for row in rows], conn=_db)
        for card_uid, tpl_guid, user_id, cstate, _card_type in rows:
            if int(card_uid) in chain_sources:
                continue
            scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
            _tpl, ct, _n, _c, _a, _d, _g = self._card_full_data(
                game, scid, tpl_guid,
                instance_projection=projections.get(int(card_uid)))
            cdef = game.card_defs.get(scid)
            attrs = cdef.attributes if cdef else 0
            owner = owner_uid(user_id, pl_t, ai_t, bstate)
            related = [game_engine.SessionCardId(game_engine.UID(int(v)))
                       for v in (voided_by.get(str(int(card_uid))) or [])]
            game.push_card_updated(scid, owner, game_engine.ECardCollections.Warzone, ct,
                                   template_id=tpl_guid, attributes=attrs,
                                   state=int(cstate or 0), related_cards=related)
        self._push_hand_cost_updates(game, session, pl_t, bstate)

    def _push_hand_cost_updates(self, game, session, pl_t, bstate):
        """Show the current effective cost of the player's hand cards.

        Static cost changes that depend on the board (Pterobot: "cost -1 for
        each Dwarf and/or Robot you control") were charged correctly but the
        card in hand kept showing its printed cost.  Push a hand card whenever
        its effective cost differs from the cost the client last saw.
        """
        if not self.user_profile:
            return
        from pvp_db import db_hand_cards_with_templates
        from rules_port.static_rules import effective_cost
        sent = getattr(self, "_hand_cost_sent", None)
        if sent is None or sent.get("_session") != session.session_id:
            sent = self._hand_cost_sent = {"_session": session.session_id}
        rows = db_hand_cards_with_templates(
            session.session_id, self.user_profile["id"])
        in_hand = set()
        for card_uid, base_cost, ct, _thresh, _abilities in rows:
            card_uid = int(card_uid)
            in_hand.add(card_uid)
            try:
                cost = int(effective_cost(_db, session.session_id, bstate,
                                          card_uid))
            except Exception:
                continue
            if cost == sent.get(card_uid, int(base_cost or 0)):
                continue
            basic = db_card_basic(session.session_id, card_uid)
            if not basic or not basic[0]:
                continue
            tpl_guid = basic[0]
            scid = game_engine.SessionCardId(game_engine.UID(card_uid))
            _tpl, ctype, _n, _c, attack, defense, gems = self._card_full_data(
                game, scid, tpl_guid)
            game.push_card_updated(
                scid, pl_t, game_engine.ECardCollections.Hand, ctype,
                template_id=tpl_guid, cost=cost, attack=attack,
                defense=defense, gems=gems)
            sent[card_uid] = cost
        for card_uid in [uid for uid in sent if uid != "_session"
                         and uid not in in_hand]:
            sent.pop(card_uid, None)

    def _apply_rules_port_prep(self, session, projection_game, bstate):
        """Project the native Prep lifecycle for the active controller."""
        from rules_port.lifecycle import (build_turn_phases,
                                          clear_expired_temporary_attributes,
                                          ready_cards_for_turn, reset_armor)
        from rules_port.resources import project_resource_change

        pl_t, ai_t = projection_game.player_uid, projection_game.ai_uid
        is_player = bstate.get("turn_player") == "player"
        side = "player" if is_player else "ai"
        owner_id = (self.user_profile.get("id", 0)
                    if is_player and isinstance(self.user_profile, dict) else 0)
        current = int(bstate.get(f"{side}_resources", 0) or 0)
        total = int(bstate.get(f"{side}_total_resources", 0) or 0)
        bonus = int(bstate.pop(f"start_turn_resource_bonus_{side}", 0) or 0)
        refill = total + max(0, bonus) - current
        if refill:
            project_resource_change(
                projection_game, session, bstate, pl_t, ai_t, side,
                "currentresource", refill)
        clear_expired_temporary_attributes(
            _db, session.session_id, owner_id, "start_turn",
            clear_stat_buffs=True, battle_state=bstate, handler=self)
        reset_armor(_db, session.session_id, bstate)
        changes = ready_cards_for_turn(_db, session.session_id, owner_id)
        recipient = pl_t if is_player else ai_t
        for uid, template_guid, card_owner, previous_state, new_state in changes:
            scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
            _tpl, card_type, _name, _cost, _atk, _def, _gem = \
                self._card_full_data(projection_game, scid, template_guid)
            projection_game.push_card_updated(
                scid, recipient, game_engine.ECardCollections.Warzone,
                card_type, template_id=template_guid, state=int(new_state))
            if (int(previous_state) & game_engine.ECardStates.Tapped and
                    not (int(new_state) & game_engine.ECardStates.Tapped)):
                from rules_port.triggers import dispatch_native_trigger
                dispatch_native_trigger(
                    db=_db, handler=self, game=projection_game,
                    session=session, player_uid=pl_t, ai_uid=ai_t,
                    battle_state=bstate, event_type="CardReadiedEvent",
                    source_card_id=int(uid), source_player_id=owner_id,
                    data={"event_previous_state": int(previous_state)})
        from rules_port.cooldowns import decrement_and_project_ready_cooldowns
        decrement_and_project_ready_cooldowns(
            _db, session, self, projection_game, owner_id, pl_t, ai_t, bstate)
        clear_expired_temporary_attributes(
            _db, session.session_id, owner_id, "prep",
            clear_stat_buffs=True, battle_state=bstate, handler=self)
        # Recompute the combat branch after Prep for whichever side is
        # active.  Previously this only ran for the human, leaving the AI's
        # stale ``player_has_ready_troop`` value from its prior main phase;
        # a troop such as Brightmoon Brave was ready on the next turn but
        # RulesPort still selected SecondMainPhase.
        self._current_bstate = bstate
        bstate["player_has_ready_troop"] = bool(
            self._player_can_attack_troops(session)
            if is_player else self._ai_can_attack_troops(session))
        bstate["turn_phases"] = build_turn_phases(bstate)
        _db.commit()
        # The phase-entry resolver contract is "update the projection and
        # publish its wire events".  Readiness (and the resource refill above)
        # belong to Prep, but the events stayed queued until the drive stopped
        # at the next priority window, so Unity only untapped the troops when
        # Main began.  Publish them here, as StartTurn already does.
        if projection_game.events:
            self._send_battle_events(session, projection_game, pl_t)
        return changes

    def _apply_rules_port_draw(self, session, projection_game, bstate):
        """Project the native Draw phase for whichever side is active."""
        from rules_port.lifecycle import should_draw_for_turn
        side = "ai" if bstate.get("turn_player") == "ai" else "player"
        if not should_draw_for_turn(bstate, side):
            return False
        if side == "ai":
            return self._ai_draw_card(
                projection_game, session, projection_game.ai_uid, bstate)
        owner_id = (self.user_profile.get("id", 0)
                    if isinstance(self.user_profile, dict) else None)
        return self._player_draw_card(
            projection_game, session, projection_game.player_uid, owner_id)

    def _apply_rules_port_start_turn(self, session, projection_game,
                                     bstate, *, emit_events=True,
                                     resolve_phase_triggers=True,
                                     reset_cards=True):
        """Apply card-owned StartTurn rules from the RulesPort lifecycle.

        The port owns phase entry; this adapter owns only the runtime
        projection of the already-selected active player.  Keeping this
        callback here lets the same port transition perform card aging,
        Tunneling, and Surface queuing for both Practice and PvP.
        """
        _be = self._checkpoint_engine(session)
        from rules_port.tunneling import advance, queue_surfaces

        pl_t, ai_t = projection_game.player_uid, projection_game.ai_uid
        active_owner = (self.user_profile.get("id", 0)
                        if bstate.get("turn_player") == _be.PLAYER
                        and isinstance(self.user_profile, dict) else 0)
        game = projection_game or self._fresh_game(session, pl_t, ai_t, bstate)
        if reset_cards:
            self._reset_active_cards_for_turn(
                game, session, pl_t, ai_t, bstate, active_owner)
        bstate["_rules_port_attached"] = True
        from rules_port.context import EffectContext
        context = EffectContext.from_rules_port(
            game, session, _db, self, pl_t, ai_t, bstate,
            "", ability=None)
        active_champion_uid = context.champion_card_uid(active_owner)
        changes = advance(context, active_owner)
        # Mark the lifecycle phase while TurnStarted triggers resolve so
        # temporary current-resource gains (e.g. Lithe Lyricist's +1) are
        # recorded as a start-turn bonus that Prep reapplies after its refill.
        _previous_phase = bstate.get("phase")
        if resolve_phase_triggers:
            from rules_port.triggers import dispatch_native_trigger
            bstate["phase"] = game_engine.ETurnPhases.StartTurn
            dispatch_native_trigger(
                db=_db, handler=self, game=game, session=session,
                player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                event_type="TurnPhaseEvent", source_card_id=None,
                source_player_id=active_owner,
                data={"phase": int(game_engine.ETurnPhases.StartTurn)})
            dispatch_native_trigger(
                db=_db, handler=self, game=game, session=session,
                player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                # StartTurnState enqueues TurnStartedEvent with the active
                # champion as SourceCardId. TriggerSource targets (including
                # Mindpyre Wraith's reveal ability) depend on this identity.
                event_type="TurnStartedEvent",
                source_card_id=active_champion_uid,
                source_player_id=active_owner)
        # CardCounterTemplate "Stealth": the client's built-in TurnStarted
        # trigger removes one stealth counter from the active champion.  Run
        # it after the authored TurnStarted triggers so an "if you are
        # Stealth" check still sees the counter that is being removed.
        from rules_port.stealth import advance as stealth_advance
        stealth_changes = stealth_advance(context, active_owner)
        from rules_port.zone_effects import lifebound as lifebound_returns
        lifebound_changes = lifebound_returns(context, active_owner)
        replenished = self._replenish_other_resources(
            game, session, bstate, pl_t, ai_t, active_owner)
        surfaces = queue_surfaces(context, active_owner)
        if _previous_phase is None:
            bstate.pop("phase", None)
        else:
            bstate["phase"] = _previous_phase
        if emit_events:
            self._send_battle_events(session, game, pl_t)
        return {"tunneling": changes, "surfaces": surfaces,
                "stealth": stealth_changes,
                "lifebound": lifebound_changes,
                "replenished": replenished}

    def _replenish_other_resources(self, game, session, bstate, pl_t, ai_t,
                                   active_owner):
        """Session.ResetActiveResourcePool: ReplenishResourcesEachTurn.

        The active player's pool refills at Prep; a non-active champion with
        this flag also refills at the start of the opposing turn.
        """
        from rules_port.static_rules import champion_int_attribute
        changed = []
        for participant in (bstate.get("champ_map") or {}):
            try:
                owner = int(participant)
            except (TypeError, ValueError):
                continue
            if owner == int(active_owner or 0):
                continue
            if champion_int_attribute(
                    bstate, owner, "ReplenishResourcesEachTurn") <= 0:
                continue
            if champion_int_attribute(
                    bstate, owner, "CantReplenishResources") > 0:
                continue
            if bstate.get("pvp"):
                key, total_key = f"res_{owner}", f"res_total_{owner}"
                old = int(bstate.get(key, 0) or 0)
                total = int(bstate.get(total_key, 0) or 0)
                if total <= old:
                    continue
                bstate[key] = total
                from rules_port.persistence import save_state
                from rules_port.runtime_helpers import owner_uid
                save_state(session, bstate)
                event = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
                event.player_id = owner_uid(owner, pl_t, ai_t, bstate)
                event.operation = 1
                event.delta = total - old
                event.new_value = total
                game._push(event)
                changed.append((owner, total))
            else:
                side = "player" if owner else "ai"
                total = int(bstate.get(f"{side}_total_resources", 0) or 0)
                old = int(bstate.get(f"{side}_resources", 0) or 0)
                if total <= old:
                    continue
                from rules_port.resources import project_resource_change
                change = project_resource_change(
                    game, session, bstate, pl_t, ai_t, side,
                    "currentresource", total - old)
                changed.append((owner, change.new_value))
        return changed

    def _reset_active_cards_for_turn(self, game, session, pl_t, ai_t,
                                     bstate, active_owner_id):
        """Mirror ``Session.ResetActiveCards`` before TurnStarted triggers.

        Unity clears ``CameOutThisTurn`` from every warzone card, then marks
        only the active player's existing cards with
        ``StartedATurnOnYourSide``.  This happens before TurnStartedEvent, so
        troops summoned by a start-of-turn trigger retain CameOutThisTurn and
        are summoning-sick.  Doing this in Prep instead incorrectly marks
        those freshly summoned tokens as old troops (and the client receives
        the wrong CardUpdated state).
        """
        if bstate.get("_rules_port_attached") or \
                getattr(session, "_rules_port_session", None) is not None:
            from rules_port.lifecycle import prime_cards_for_turn
            from rules_port.runtime_helpers import owner_uid
            changed = prime_cards_for_turn(
                _db, session.session_id, active_owner_id)
            for card_uid, tpl_guid, owner_id, new_state, _card_type in changed:
                scid = game_engine.SessionCardId(game_engine.UID(card_uid))
                _tpl, ct, _name, _cost, _atk, _def, _gem = \
                    self._card_full_data(game, scid, tpl_guid)
                cdef = game.card_defs.get(scid)
                game.push_card_updated(
                    scid, owner_uid(owner_id, pl_t, ai_t, bstate),
                    game_engine.ECardCollections.Warzone, ct,
                    template_id=tpl_guid,
                    attributes=(cdef.attributes if cdef else 0),
                    state=int(new_state))
            if changed:
                log_req(f"    RulesPort ResetActiveCards: active="
                        f"{int(active_owner_id or 0)} updated {len(changed)} "
                        "warzone card(s)")
            return len(changed)

        from abilities.framework._shared import owner_uid

        # Session.PrimeCard clears these transient flags for the active
        # player's warzone before the CameOut/Started pass.  Tapped is left
        # alone here; readiness (including CantReadyAutomatically) belongs to
        # the subsequent Prep checkpoint.
        prime_mask = (
            game_engine.ECardStates.Blocking |
            game_engine.ECardStates.Attacking |
            game_engine.ECardStates.Damaged |
            game_engine.ECardStates.Healed |
            game_engine.ECardStates.HasAttacked |
            game_engine.ECardStates.HasBlocked |
            game_engine.ECardStates.ZoneChangeReplacement |
            game_engine.ECardStates.Activated |
            game_engine.ECardStates.VoidsIfDestroyed |
            game_engine.ECardStates.CameOutThisTurn |
            game_engine.ECardStates.StartedATurnOnYourSide)
        rows = db_warzone_display_rows(session.session_id, conn=_db)
        changed = 0
        for card_uid, tpl_guid, owner_id, old_state, _card_type in rows:
            old_state = int(old_state or 0)
            # ResetCameOutThisTurn applies to both sides.  PrimeCard and
            # SetStartedATurnOnOwnersSide apply only to the active side.
            new_state = old_state & ~game_engine.ECardStates.CameOutThisTurn
            if int(owner_id or 0) == int(active_owner_id or 0):
                new_state = ((new_state & ~prime_mask) |
                             game_engine.ECardStates.StartedATurnOnYourSide)
            if new_state == old_state:
                continue
            db_set_card_state_exact(
                session.session_id, int(card_uid), new_state, conn=_db)
            changed += 1
            scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
            _tpl, ct, _name, _cost, _atk, _def, _gem = \
                self._card_full_data(game, scid, tpl_guid)
            cdef = game.card_defs.get(scid)
            game.push_card_updated(
                scid,
                owner_uid(owner_id, pl_t, ai_t, bstate),
                game_engine.ECardCollections.Warzone,
                ct,
                template_id=tpl_guid,
                attributes=(cdef.attributes if cdef else 0),
                state=int(new_state))
        _db.commit()
        if changed:
            log_req(f"    ResetActiveCards: active={int(active_owner_id or 0)} "
                    f"updated {changed} warzone card(s)")
        return changed

    def _push_champions_warm(self, session, pl_t, ai_t, bstate, game):
        """Rebuild both champion CardDefs for the next packet.

        UIBattle.OnTurnPhaseUpdated reads State.Cards[State.Players[p].ChampionSessionCardId]
        for the OPPONENT at StartTurn (and for combat phases). If the AI champion
        isn't in the cache, the client throws KeyNotFoundException, desyncs
        priority and sends RequestPrioritySyncTransaction. The initial battle
        packet already seeds both entries with CardUpdated(None) before
        ChampionCardPlayed moves them into the champion HUD. Sending another
        CardUpdated with collection Champions here makes the client run its
        normal card-location animation on the champion card, which can leave a
        full-size card on the battlefield. Re-register the CardDefs for any
        later PlayerUpdated/option work, but do not emit another CardUpdated.
        Also carry the persisted health into the Game so any PlayerUpdated from
        this object reports the live health (not the 20 default).
        """
        game.ai_health = bstate.get("ai_health", 10)
        game.player_health = bstate.get("player_health", 20)
        champion_counters = bstate.get("champion_counters") or {}
        ai_champ = getattr(self, "_ai_champ_scid", None)
        if ai_champ:
            ai_cdef = game_engine.CardDef(
                "AI", game_engine.ECardTypes.Champion, 0,
                bstate.get("ai_health", 10), bstate.get("ai_health", 10),
                [], [game_engine.ResourceId.from_str(g) for g in getattr(self, "_ai_champ_ability_guids", [])])
            ai_cdef.counters = dict(champion_counters.get(
                str(int(ai_champ.uid.uid64)), {}) or {})
            game.card_defs[ai_champ] = ai_cdef
        pl_champ = getattr(self, "_player_champ_scid", None)
        if pl_champ:
            pl_abilities = getattr(self, "_player_champ_abilities", [])
            cdef = game_engine.CardDef(
                "Player", game_engine.ECardTypes.Champion, 0,
                bstate.get("player_health", 17), bstate.get("player_health", 17),
                [], list(pl_abilities))
            cdef.counters = dict(champion_counters.get(
                str(int(pl_champ.uid.uid64)), {}) or {})
            # Carry the persisted spell-power escalation (player_sp_uses) onto
            # the champion CardDef so the client's button shows the INCREASED
            # SP cost (e.g. Soothsaying 4->5 after one use) — a re-pushed
            # champion without these mods wipes the escalation display.
            for ag, uses in (bstate.get("player_sp_uses", {}) or {}).items():
                if int(uses) > 0:
                    cdef.spell_point_cost_mods[game_engine.ResourceId.from_str(ag)] = int(uses)
            game.card_defs[pl_champ] = cdef

    def _advance_to_priority(self, session, pl_t, ai_t, bstate):
        """Auto-advance the human's turn through non-stop phases.

        The server pushes ONE phase per packet. If the current phase is NOT a
        stop for the turn player (and not PickGoesFirst/Mulligan, which can
        never be skipped), the server auto-passes it — pushes the phase and
        immediately advances. It stops when it reaches a stop phase (player gets
        priority + GreenLight) or the turn ends (handed to the AI). Returns
        True if the player now holds priority.
        """
        if bstate.get("_rules_port_attached"):
            return self._advance_rules_port_to_priority(
                session, pl_t, ai_t, bstate)
        import battle_engine as _be
        while True:
            phase = _be.current_phase(bstate)
            if self._check_champion_health(session, pl_t, ai_t, bstate):
                return False
            # Start of the player's turn: reset the combat decision and mirror
            # Unity's ResetActiveCards checkpoint before any TurnStarted
            # triggers. Prep later readies eligible troops. Also re-push both champions so the
            # client's OnTurnPhaseUpdated (State.Cards[ChampionSessionCardId])
            # doesn't KeyNotFound at StartTurn / combat phases.
            if phase == game_engine.ETurnPhases.StartTurn and bstate.get("turn_player") == _be.PLAYER:
                bstate["player_has_ready_troop"] = False
                bstate.pop("player_attackers", None)
                bstate["turn_phases"] = _be.BASE_TURN_PHASES
                _be.save_state(session, bstate)
                warm = game_engine.Game(session.session_id, pl_t, ai_t)
                start_result = self._apply_rules_port_start_turn(
                    session, warm, bstate, emit_events=False,
                    resolve_phase_triggers=False)
                tunnel_changes = start_result["tunneling"]
                tunnel_surfaces = start_result["surfaces"]
                stealth_changes = start_result.get("stealth") or []
                self._push_champions_warm(session, pl_t, ai_t, bstate, warm)
                self._send_battle_events(session, warm, pl_t)
                log_req("    Player turn start: combat decision deferred to Prep; "
                        f"tunneling +{len(tunnel_changes)} / "
                        f"surface queued {len(tunnel_surfaces)} / "
                        f"stealth removed {len(stealth_changes)}; champions re-pushed")
            # End of turn: hand over to the AI.
            if phase == game_engine.ETurnPhases.EndTurn:
                # "At the end of your turn" triggers for the player's cards.
                g_end = self._fresh_game(session, pl_t, ai_t, bstate)
                bstate["_rules_port_attached"] = True
                from rules_port.triggers import dispatch_native_trigger
                dispatch_native_trigger(
                    db=_db, handler=self, game=g_end, session=session,
                    player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                    event_type="TurnEndedEvent", source_card_id=None,
                    source_player_id=self.user_profile["id"])
                # "Until end of turn" attribute grants (e.g. Dimmid's
                # Lifedrain) expire NOW — the controller's turn is ending —
                # and the warzone cards are re-pushed so the client drops the
                # badges immediately instead of at the next Ready step.
                # Cleanup order matters: remove combat damage while the
                # outgoing turn's temporary defense bonuses still apply.
                self._clear_combat_damage(session, bstate)
                self._clear_expired_temporary_attributes(
                    session, bstate, self.user_profile["id"], "end_turn",
                    clear_stat_buffs=True)
                for wzr in db_warzone_display_rows(
                        session.session_id, conn=_db):
                    cu, tpl, card_user_id, card_state, _card_type = wzr
                    scid = game_engine.SessionCardId(game_engine.UID(cu))
                    _tpl, ct, _n, _c, _a, _d, _g = self._card_full_data(
                        g_end, scid, tpl)
                    g_end.push_card_updated(
                        scid, pl_t if card_user_id else ai_t,
                        game_engine.ECardCollections.Warzone,
                        ct, template_id=tpl,
                        state=int(card_state or 0))
                self._send_battle_events(session, g_end, pl_t)
                # End-of-turn triggers are ordinary stack items.  Do not hand
                # the turn to the opponent while they are still waiting for
                # both players to pass; the client must receive the chain and
                # priority checkpoint first (e.g. Verdant Wyldeboar).
                if not _be.stack_empty(bstate):
                    _be.save_state(session, bstate)
                    # We are already at the EndTurn stop.  Re-entering
                    # _advance_to_priority here leaves the phase unchanged
                    # and recursively re-discovers the same trigger chain;
                    # emit the priority checkpoint directly instead.
                    self._push_phase_options_empty(session, pl_t, ai_t)
                    g_chain = self._fresh_game(session, pl_t, ai_t, bstate)
                    # The preceding EndTurn packet may have contained only
                    # card updates.  Re-announce the actual EndTurn phase so
                    # the client's subsequent pass is not tagged with the
                    # previous phase number and rejected as stale.
                    g_chain.push_turn_phase(
                        _be.current_phase(bstate), pl_t, pl_t)
                    g_chain.push_green_light(
                        pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
                    self._send_battle_events(session, g_chain, pl_t)
                    log_req("    EndTurn paused: waiting for pending trigger chain")
                    return False
                if getattr(self, "_autoplay_drive_ai_turn", False):
                    # Headless harness drives the AI turn itself (with proper
                    # resume at opponent stops).  Leave turn_player as PLAYER
                    # and let the harness hand over.
                    _be.save_state(session, bstate)
                    return False
                if bstate.get("_rules_port_attached"):
                    from rules_port.lifecycle import complete_turn
                    next_player = complete_turn(bstate)
                else:
                    next_player = _be.next_turn_player(bstate)
                    bstate["turn_player"] = next_player
                    bstate["player_passed"] = False
                    bstate["ai_passed"] = False
                    bstate["phase_idx"] = 0
                    bstate["turn_phases"] = _be.BASE_TURN_PHASES
                    bstate[f"{next_player}_resource_played_this_turn"] = False
                    bstate[f"{next_player}_resource_plays_this_turn"] = 0
                # F10 auto-pass is "to the end of MY turn" — clear it now so the
                # AI's own turn isn't auto-advanced.
                bstate.pop("player_autopass", None)
                _be.save_state(session, bstate)
                if next_player == _be.PLAYER:
                    self._advance_to_priority(session, pl_t, ai_t, bstate)
                    log_req("    Auto-advance reached EndTurn; bonus turn kept player in control")
                else:
                    self._run_ai_turn(session, pl_t, ai_t, bstate)
                    log_req("    Auto-advance reached EndTurn; turn passed to AI")
                return False
            # No attackers declared: skip the remaining combat steps straight
            # to the Second Main Phase.  Declaration happens at/after
            # DeclareAttack, so DeclareAttackPriorityWindow and beyond are
            # skippable; DeclareCombatPriorityWindow / DeclareAttack still
            # give the player the chance to commit attackers first.
            if (phase in _be.COMBAT_STEPS and
                    _be.COMBAT_STEPS.index(phase) >= 2 and
                    bstate.get("turn_player") == _be.PLAYER and
                    not (bstate.get("player_attackers") or {})):
                _be.skip_to_phase(bstate, game_engine.ETurnPhases.SecondMainPhase)
                _be.save_state(session, bstate)
                log_req("    No attackers declared — skipped combat steps to SecondMain")
                continue
            # Swiftstrike damage steps only occur when an attacking or blocking
            # troop has Swiftstrike (FirstStrike) — otherwise skip straight to
            # the normal AssignDamage step.
            if phase in (game_engine.ETurnPhases.AssignFirstStrikeDamage,
                         game_engine.ETurnPhases.FirstStrikePriorityWindow):
                from ai import combat_has_swiftstrike
                if not combat_has_swiftstrike(_db, session, bstate):
                    _be.skip_to_phase(bstate, game_engine.ETurnPhases.AssignDamage)
                    _be.save_state(session, bstate)
                    log_req("    No Swiftstrike combatants — skipped swiftstrike steps")
                    continue
            # Discard with a fitting hand: the client auto-passes it with a
            # no-op animation (no transaction), so skip it server-side.
            if phase == game_engine.ETurnPhases.Discard:
                max_hand = self._max_hand_size(session)
                hand_count = db_hand_card_count(session.session_id, self.user_profile["id"])
                if hand_count <= max_hand:
                    bstate["player_passed"] = True
                    bstate["ai_passed"] = True
                    _be.advance_phase(bstate)
                    _be.save_state(session, bstate)
                    log_req("    Auto-advanced past Discard (hand fits)")
                    continue
                # Hand over the limit: the player must discard, so it's a stop.
                game = self._fresh_game(session, pl_t, ai_t, bstate)
                game.push_turn_phase(phase, pl_t, pl_t)
                game.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
                game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
                self._send_battle_events(session, game, pl_t)
                log_req(f"    Stopped at phase {phase} (discard required); turn={bstate.get('turn_player')} priority=player")
                return True
            # Stop phase (incl. PickGoesFirst/Mulligan): grant priority + wait.
            if _be.is_self_stop(bstate, phase):
                # F10 / "Skip" auto-pass: the player asked to pass all priority
                # to the end of their own turn, so advance through stops instead
                # of granting priority (mandatory setup phases still stop).
                if bstate.get("player_autopass") and phase not in (
                        game_engine.ETurnPhases.PickGoesFirst,
                        game_engine.ETurnPhases.Mulligan,
                        game_engine.ETurnPhases.StartGame):
                    game = self._fresh_game(session, pl_t, ai_t, bstate)
                    game.push_turn_phase(phase, pl_t, pl_t)
                    game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
                    game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
                    self._push_warzone_card_updates(game, session, pl_t, ai_t)
                    self._send_battle_events(session, game, pl_t)
                    log_req(f"    Auto-passed stop phase {phase} (F10)")
                    bstate["player_passed"] = True
                    bstate["ai_passed"] = True
                    _be.advance_phase(bstate)
                    _be.save_state(session, bstate)
                    continue
                # A fully-populated fresh Game so PlayerUpdated reports the live
                # resources/health/charges/SP (not the bare defaults).
                game = self._fresh_game(session, pl_t, ai_t, bstate)
                bstate["_rules_port_attached"] = True
                from rules_port.triggers import dispatch_native_trigger
                dispatch_native_trigger(
                    db=_db, handler=self, game=game, session=session,
                    player_uid=pl_t, ai_uid=ai_t, battle_state=bstate,
                    event_type="TurnPhaseEvent", source_card_id=None,
                    source_player_id=self.user_profile["id"],
                    data={"phase": int(phase)})
                game.push_turn_phase(phase, pl_t, pl_t)
                game.push_green_light(pl_t, self._priority_context_for(phase, bstate))
                game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
                # Keep the AI player representation fresh (State.Players[ai]
                # needs a recent PlayerUpdated with a valid ChampionId for
                # BattleStateDeclareAttackers.GetTargetChampion to resolve the
                # opponent portrait as the attack-line target).
                game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
                self._push_warzone_card_updates(game, session, pl_t, ai_t)
                self._send_battle_events(session, game, pl_t)
                log_req(f"    STOP phase {phase} PlayerUpdated: res={game.player_resources}/{game.player_total_resources} chg={game.player_charges} sp={game.player_spell_points}")
                # Push PlayerOptionList for every stop phase so the client knows
                # what is interactable. Main phases: full playable cards + champion
                # abilities. DeclareAttack: warzone troops with ECardUsage.Attack.
                # Other phases: champion abilities only (or empty).
                if phase in (game_engine.ETurnPhases.FirstMainPhase,
                             game_engine.ETurnPhases.SecondMainPhase):
                    self._push_main_phase_options(session, pl_t, ai_t)
                elif phase == game_engine.ETurnPhases.DeclareAttack:
                    self._push_attack_options(session, pl_t, ai_t)
                elif phase in (game_engine.ETurnPhases.AssignDamage,
                               game_engine.ETurnPhases.AssignFirstStrikeDamage):
                    # Give the client the combat listing so BattleStateAssignDamage
                    # can auto-commit (no blockers) and send AssignDamageOrderTransaction.
                    self._push_combat_listing_phase(session, pl_t, ai_t, bstate)
                else:
                    self._push_phase_options_empty(session, pl_t, ai_t)
                log_req(f"    Stopped at phase {phase}; turn={bstate.get('turn_player')} priority=player "
                        f"(context={self._priority_context_for(phase, bstate)})")
                return True
            # Non-stop phase: push it and auto-advance.
            game = self._fresh_game(session, pl_t, ai_t, bstate)
            self._dispatch_native_trigger(
                game, session, pl_t, ai_t, bstate, "TurnPhaseEvent", None,
                self.user_profile["id"], phase=int(phase))
            if phase == game_engine.ETurnPhases.Draw:
                # The human draws a card at the Draw phase — except on their very
                # first turn when they chose to play first.
                if bstate.get("turn_number", 1) > 1 or bstate.get("player_draws_first_turn"):
                    if self._player_draw_card(game, session, pl_t):
                        # Deck-out: the player drew from an empty deck and lost.
                        return False
            elif phase == game_engine.ETurnPhases.Prep:
                # Prep: refill current resources to max for the turn.
                native_prep = bstate.get("_rules_port_attached")
                if native_prep:
                    from rules_port.resources import apply_resource_change
                    prep_side = ("player" if bstate.get("turn_player")
                                 == "player" else "ai")
                    prep_bonus = int(bstate.pop(
                        f"start_turn_resource_bonus_{prep_side}", 0) or 0)
                    apply_resource_change(
                        bstate, prep_side, "currentresource",
                        int(bstate.get(f"{prep_side}_total_resources", 0) or 0)
                        + max(0, prep_bonus) -
                        int(bstate.get(f"{prep_side}_resources", 0) or 0))
                else:
                    bstate["player_resources"] = bstate.get(
                        "player_total_resources", 0)
                self._clear_expired_temporary_attributes(
                    session, bstate, self.user_profile["id"], "start_turn",
                    clear_stat_buffs=True)
                # Clear summoning sickness from warzone troops: they now have
                # StartedATurnOnYourSide (survived to this turn). Persist the
                # state in game_cards.card_state (DB) so a reconnect and the
                # combat-phase decision can reconstruct it.
                if native_prep:
                    from rules_port.lifecycle import (ready_cards_for_turn,
                                                      reset_armor)
                    reset_armor(_db, session.session_id, bstate)
                    ready_changes = ready_cards_for_turn(
                        _db, session.session_id, self.user_profile["id"])
                    for uid, tpl_guid, owner_id, previous_state, pstate in ready_changes:
                        scid = game_engine.SessionCardId(game_engine.UID(uid))
                        self._card_full_data(game, scid, tpl_guid)
                        tpl = self._template_by_guid(tpl_guid)
                        ct = (game_engine.card_type_from_db(tpl[1]) if tpl
                              else game_engine.ECardTypes.Troop)
                        game.push_card_updated(
                            scid, pl_t, game_engine.ECardCollections.Warzone,
                            ct, template_id=tpl_guid, state=int(pstate))
                        if (previous_state & game_engine.ECardStates.Tapped and
                                not (pstate & game_engine.ECardStates.Tapped)):
                            self._dispatch_native_trigger(
                                game, session, pl_t, ai_t, bstate,
                                "CardReadiedEvent", uid, owner_id)
                    from rules_port.cooldowns import decrement_and_project_ready_cooldowns
                    decrement_and_project_ready_cooldowns(
                        _db, session, self, game,
                        self.user_profile["id"], pl_t, ai_t, bstate)
                    self._clear_expired_temporary_attributes(
                        session, bstate, self.user_profile["id"], "prep",
                        clear_stat_buffs=True)
                    wz_rows = ()
                else:
                    wz_rows = db_warzone_troops_with_state(
                        session.session_id, self.user_profile["id"], conn=_db)
                for wzr in wz_rows:
                    scid = game_engine.SessionCardId(game_engine.UID(wzr[0]))
                    previous_state = int(db_card_state_value(
                        session.session_id, int(wzr[0]), conn=_db) or 0)
                    # Ready/untap: clear combat states.  CameOutThisTurn and
                    # StartedATurnOnYourSide were already resolved at
                    # StartTurn, before TurnStarted triggers; keeping the
                    # former here would incorrectly age freshly summoned
                    # tokens.
                    import game_engine as _ge
                    attrs_row = db_warzone_card_state_attributes(
                        session.session_id, int(wzr[0]), conn=_db)
                    clear_mask = (_ge.ECardStates.Tapped |
                                  _ge.ECardStates.Attacking |
                                  _ge.ECardStates.HasAttacked |
                                  _ge.ECardStates.Blocking |
                                  _ge.ECardStates.HasBlocked)
                    if (attrs_row and int(attrs_row[1] or 0)
                            & _ge.ECardAttributes.CantReadyAutomatically):
                        clear_mask &= ~_ge.ECardStates.Tapped
                    # A permanent that survived from an earlier turn must be
                    # marked as having started a turn on its controller's
                    # side.  Tokens created during the current turn retain
                    # CameOutThisTurn and remain summoning-sick; older
                    # tokens (including Battle Hoppers) must become
                    # attackable even if a reconnect or an earlier phase
                    # packet omitted the StartTurn marker.
                    aged_state = previous_state & ~clear_mask
                    if not (aged_state & _ge.ECardStates.CameOutThisTurn):
                        aged_state |= _ge.ECardStates.StartedATurnOnYourSide
                    db_reset_warzone_troop(
                        session.session_id, int(wzr[0]), clear_mask, conn=_db)
                    if aged_state != previous_state:
                        db_set_card_state_exact(
                            session.session_id, int(wzr[0]), aged_state, conn=_db)
                    self._card_full_data(game, scid, wzr[1])
                    tpl = self._template_by_guid(wzr[1])
                    ct = game_engine.card_type_from_db(tpl[1]) if tpl else game_engine.ECardTypes.Troop
                    pstate = db_card_state_value(session.session_id, int(wzr[0]))
                    if pstate is None:
                        pstate = 0
                    game.push_card_updated(scid, pl_t, game_engine.ECardCollections.Warzone, ct,
                                          template_id=wzr[1], state=pstate)
                    if (previous_state & _ge.ECardStates.Tapped and
                            not (pstate & _ge.ECardStates.Tapped)):
                        self._dispatch_native_trigger(
                            game, session, pl_t, ai_t, bstate,
                            "CardReadiedEvent", int(wzr[0]),
                            self.user_profile["id"],
                            event_previous_state=previous_state)
                _db.commit()
                # "AfterCardsReadyOnPlayersTurn" effects (e.g. Nazhk's
                # CantReadyAutomatically) must survive through this ready
                # step, then expire for the rest of the turn.
                self._clear_expired_temporary_attributes(
                    session, bstate, self.user_profile["id"], "prep",
                    clear_stat_buffs=True)
                # Prep is the first point at which troops have been readied;
                # seed the combat decision before the First Main green light
                # is emitted so its button says ProceedToCombat when legal.
                bstate["player_has_ready_troop"] = self._player_can_attack_troops(session)
                bstate["turn_phases"] = _be.build_turn_phases(bstate)
            elif phase == game_engine.ETurnPhases.DeclareDefense:
                # The AI is the defender and never blocks yet: it passes priority
                # through the DeclareDefense step with no blockers. Push the phase
                # plus a BlockersAssigned event (empty blocker list per attacker)
                # so the client renders the AI declining to block, then advance.
                bstate = self._ai_pass_declare_defense(session, pl_t, ai_t, bstate, game)
            # Re-sync the Game's live values from bstate: the game was built by
            # _fresh_game BEFORE the phase ran (Prep refills resources, Draw
            # draws), so without this the PlayerUpdated pushes the pre-phase
            # values and the client flickers to 0/0 at turn start.
            game.player_resources = bstate.get("player_resources", 0)
            game.player_total_resources = bstate.get("player_total_resources", 0)
            game.player_threshold = dict(bstate.get("player_threshold", {}))
            game.player_charges = bstate.get("player_charges", 0)
            game.player_spell_points = bstate.get("player_spell_points", 0)
            game.player_health = bstate.get("player_health", 20)
            game.ai_resources = bstate.get("ai_resources", 0)
            game.ai_total_resources = bstate.get("ai_total_resources", 0)
            game.ai_threshold = dict(bstate.get("ai_threshold", {}))
            game.ai_charges = bstate.get("ai_charges", 0)
            game.ai_spell_points = bstate.get("ai_spell_points", 0)
            game.ai_health = bstate.get("ai_health", 10)
            game.player_health = bstate.get("player_health", 20)
            game.push_turn_phase(phase, pl_t, pl_t)
            game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
            game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
            self._push_warzone_card_updates(game, session, pl_t, ai_t)
            self._send_battle_events(session, game, pl_t)
            log_req(f"    Auto-passed phase {phase} (turn={bstate.get('turn_player')} priority=player)")
            # A trigger/card went onto the chain this phase (e.g. Twisted
            # Fate's "when you draw" during the Draw phase): the phase must NOT
            # advance while the chain is non-empty — grant priority here so the
            # player resolves the chain first; the pass handler then continues
            # the turn (the player's pass after the chain empties advances it).
            if not _be.stack_empty(bstate):
                bstate["player_passed"] = False
                bstate["ai_passed"] = False
                _be.save_state(session, bstate)
                self._push_phase_options_empty(session, pl_t, ai_t)
                g_chain = game_engine.Game(session.session_id, pl_t, ai_t)
                g_chain.push_green_light(
                    pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
                self._send_battle_events(session, g_chain, pl_t)
                log_req(f"    Phase {phase}: chain pending — priority to "
                        f"player (phase held)")
                return True
            bstate["player_passed"] = True
            bstate["ai_passed"] = True
            _be.advance_phase(bstate)
            _be.save_state(session, bstate)

    def _project_rules_port_priority(self, session, pl_t, ai_t, bstate):
        """Project the native scheduler's current input window to the client.

        RulesPort owns the phase/action transition. This method only builds
        the existing wire/UI projection after the scheduler has stopped at a
        client input, or resumes the server AI when the native queue gives it
        priority.
        """
        from rules_port.kernel import PriorityWindowAction
        from rules_port.persistence import load_state

        port = getattr(session, "_rules_port_session", None)
        if port is None:
            return False
        # Reaching a human projection means the client has answered a
        # transaction from a battle UI state, so its board is live (see
        # ``_announce_champion_board_cards``).
        setattr(session, "_client_in_battle",
                getattr(session, "session_id", None))
        trace_rules_port(log_req, "project-before", port, bstate)
        current = load_state(session, default=lambda: dict(bstate))
        # An authored prompt that already owns the client's UI state must not
        # be replaced by the ordinary phase/priority projection.  A trigger
        # with an explicit target (e.g. Wakizashi Ambusher's Deploy), a card
        # choice, deck search, discard or conversation prompt was pushed by
        # its own private PlayerOptionList/class-39 packet plus green light;
        # re-announcing the phase, the board and the phase options here makes
        # the client pop BattleStateConfigureAbility/BattleStateTarget before
        # the player can answer.  PvP pauses at the same boundary, so wait for
        # the answering SetAbilityActivationData/choice transaction instead.
        pending_input = [key for key in (
            "pending_choice", "pending_trigger", "pending_deck_search",
            "pending_conversation", "pending_discard_ability")
            if current.get(key)]
        if pending_input:
            log_req("    RulesPort projection held for pending client input: "
                    + ",".join(pending_input))
            return True
        phases = list(current.get("turn_phases") or ())
        phase = port.current_turn_phase
        try:
            current["phase_idx"] = phases.index(phase)
        except ValueError:
            pass
        current["turn_player"] = (
            "player" if _same_uid(port.active_player_id, pl_t) else "ai")
        bstate.update(current)
        action = port.action_stack.peek()
        if not isinstance(action, PriorityWindowAction):
            return False
        priority = action.priority_player_id
        if (_same_uid(port.active_player_id, ai_t) and
                _same_uid(priority, ai_t)):
            try:
                ai_idx = phases.index(phase)
            except ValueError:
                ai_idx = int(current.get("phase_idx", 0) or 0)
            self._run_ai_turn(session, pl_t, ai_t, bstate,
                              start_idx=ai_idx)
            trace_rules_port(log_req, "project-after-ai", port, bstate)
            return False
        if _same_uid(priority, ai_t):
            # During a human attack the human remains the native active
            # player, but DeclareDefense belongs to the defending AI.  This
            # is a real server-owned input window, not stale AI priority.
            if phase == game_engine.ETurnPhases.DeclareDefense:
                import ai
                from rules_port.persistence import save_state

                response_game = self._fresh_game(
                    session, pl_t, ai_t, current)
                if getattr(port, "event_sink", None) is not None:
                    port.event_sink.game = response_game
                self._current_bstate = current
                current = ai.ai_pass_declare_defense(
                    self, session, pl_t, ai_t, current, response_game) or current

                # The compatibility AI selector writes the persisted combat
                # assignment and emits the client events.  RulesPort still
                # owns the live combat objects used by AssignDamage, so copy
                # the same typed cards into those objects before consuming
                # the native DeclareDefense priority.
                assignments = current.get("ai_blockers") or {}
                for combat in port.combat_manager.combats:
                    attacker = getattr(combat, "attacker", None)
                    attacker_id = getattr(
                        getattr(attacker, "session_card_id", attacker),
                        "uid64", getattr(attacker, "session_card_id", attacker))
                    try:
                        if attacker_id is None:
                            continue
                        blocker_ids = assignments.get(
                            str(int(cast(Any, attacker_id))), ())
                    except (TypeError, ValueError):
                        blocker_ids = ()
                    blockers = [port.get_card(int(blocker_id))
                                for blocker_id in blocker_ids]
                    if any(blocker is None for blocker in blockers):
                        log_req(
                            "    RulesPort AI defense could not hydrate "
                            f"blockers for attacker={attacker_id!r}")
                        return False
                    combat.declare_blockers(blockers)

                save_state(session, current)
                port.persist()
                self._send_battle_events(session, response_game, pl_t)
                if not port.pass_player_priority(ai_t):
                    log_req(
                        "    RulesPort AI defense priority pass rejected: "
                        f"priority={port.action_stack.priority_player_id!r}")
                    return False
                port.drive_until_input(max_steps=64)
                port.persist()
                trace_rules_port(log_req, "project-after-ai-defense",
                                 port, current)
                return self._project_rules_port_priority(
                    session, pl_t, ai_t, current)
            log_req(
                f"    RulesPort ignored stale AI priority: active="
                f"{port.active_player_id!r} ai={ai_t!r} phase={phase!r}")
            return False
        if not _same_uid(priority, pl_t):
            return False

        game = self._fresh_game(session, pl_t, ai_t, bstate)
        game.push_turn_phase(
            phase,
            pl_t if _same_uid(port.active_player_id, pl_t) else ai_t,
            pl_t)
        priority_context = self._priority_context_for(phase, bstate)
        native_chain = getattr(port, "chain", None)
        if (native_chain is not None and
                not getattr(native_chain, "is_empty", True)):
            priority_context = game_engine.EPriorityContext.ResolveTopOfChain
        game.push_green_light(pl_t, priority_context)
        game.push_player_updated(
            pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game.push_player_updated(
            ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        # The attack options packet applies force attackers and their triggers,
        # then includes the authoritative board refresh. Avoid projecting the
        # pre-declaration board here; it would immediately be rebuilt.
        if phase != game_engine.ETurnPhases.DeclareAttack:
            self._push_warzone_card_updates(game, session, pl_t, ai_t)
        self._send_battle_events(session, game, pl_t)
        trace_rules_port(log_req, "project-human", port, bstate)
        if phase in (game_engine.ETurnPhases.FirstMainPhase,
                     game_engine.ETurnPhases.SecondMainPhase):
            self._push_main_phase_options(
                session, pl_t, ai_t, refresh_warzone=False)
        elif phase == game_engine.ETurnPhases.DeclareAttack:
            self._push_attack_options(session, pl_t, ai_t)
        elif phase in (game_engine.ETurnPhases.AssignDamage,
                       game_engine.ETurnPhases.AssignFirstStrikeDamage):
            self._push_combat_listing_phase(session, pl_t, ai_t, bstate)
        else:
            self._push_phase_options_empty(
                session, pl_t, ai_t, refresh_warzone=False)
        return True

    def _advance_rules_port_to_priority(self, session, pl_t, ai_t, bstate):
        """Drive native phase/action state until a real client input exists."""
        port = getattr(session, "_rules_port_session", None)
        if port is None:
            return False
        trace_rules_port(log_req, "drive-before", port, bstate)
        if (port.current_turn_phase == game_engine.ETurnPhases.StartTurn and
                port.action_stack.count == 0 and
                int(getattr(port, "total_turns_taken", 0) or 0) == 0):
            # A newly-created checkpoint names StartTurn before its native
            # state has run. Materialize that state once so StartTurn effects
            # and the following phase transitions remain RulesPort-owned.
            port.materialize_current_phase()
        port.drive_until_input(max_steps=64)
        # A resolver can queue server-owned chain responses (a troop's Deploy
        # trigger) while it finishes.  Give the AI the same follow-up passes
        # the transaction pass handler would, or the client is left staring at
        # a window that belongs to the server actor.
        if not (session.session_name or "").startswith("tourney-"):
            for _ in range(8):
                action = port.action_stack.peek()
                if not _is_practice_chain_follow_up(action, pl_t, True):
                    break
                if not port.pass_player_priority(
                        action.priority_player_id):
                    break
                port.drive_until_input(max_steps=64)
        trace_rules_port(log_req, "drive-after", port, bstate)
        return self._project_rules_port_priority(
            session, pl_t, ai_t, bstate)

    def _run_ai_turn(self, session, pl_t, ai_t, battle_state, start_idx=0):
        import ai
        return ai.run_ai_turn(self, session, pl_t, ai_t, battle_state, start_idx)

    def _save_player_stops(self, user_id, self_stops, opp_stops):
        """Persist a player's phase-stop preferences (survive across battles)."""
        import json as _j
        from pvp_db import db_card_save_player_stops
        db_card_save_player_stops(user_id, _j.dumps(self_stops), _j.dumps(opp_stops))

    def _load_player_stops(self, user_id):
        """Load a player's saved phase-stop preferences, or (None, None)."""
        from pvp_db import db_card_load_player_stops
        raw = db_card_load_player_stops(user_id)
        if not raw or not raw[0]:
            return None, None
        import json as _j
        try:
            return _j.loads(raw[0]), _j.loads(raw[1]) if raw[1] else None
        except Exception:
            return None, None

    def _template_by_guid(self, template_guid):
        """Fetch card template data for a template GUID.
        Returns (guid, card_type, name, cost, attack, defense) or None."""
        if not template_guid:
            return None
        return db_template_by_guid(template_guid)

    def _resolve_card_ref(self, card_ref, user_id=None):
        """Resolve a game_cards.card_template_id to card template info.

        card_template_id is either an integer card_instances.instance_id
        (player/FRA cards) or a template GUID string (AI encounter deck cards).
        Returns (template_guid, card_type_name, name, cost, attack, defense).
        """
        if card_ref is None:
            return None, None, None, 0, 0, 0
        if isinstance(card_ref, str):
            t = self._template_by_guid(card_ref)
            if t:
                return t
            return None, None, None, 0, 0, 0
        if user_id is None and self.user_profile:
            user_id = self.user_profile.get("id")
        if user_id is None:
            return None, None, None, 0, 0, 0
        row = db_card_instance_display(
            user_id, card_ref, conn=_db)
        if row:
            return row[0], row[1], row[2], row[3] or 0, row[4] or 0, row[5] or 0
        return None, None, None, 0, 0, 0

    def _card_full_data(self, game, scid: game_engine.SessionCardId,
                        template_guid, instance_id=None,
                        instance_projection=None):
        """Resolve full card data for a session card and prepare it for display.

        Fills game.card_defs[scid] (thresholds, abilities) so a subsequent
        push_card_updated renders the complete card (cost, atk/def, shards,
        abilities, gems). Returns (tpl_guid, card_type, name, cost, attack,
        defense, gem_type).

        Instance-aware: the card's CURRENT abilities come from game_cards
        (Shift moves abilities between cards, and PowerShifted triggers grant
        +atk/+def), not the template's printed list — so a Prep re-push keeps a
        shifted Lifedrain / granted ability / buff instead of reverting it.
        """
        # This is the hot CardUpdated path. Keep the instance fields in the
        # historical ``db_card_instance_full`` order, but fetch the owner,
        # zone, and current template GUID in the same indexed lookup. Printed
        # fields come from the process-wide Records cache; a missing record is
        # expected for champion/generated/legacy rows and uses the DB fallback.
        joined = None
        irow = None
        if scid is not None:
            joined = (instance_projection if instance_projection is not None
                      else db_card_instance_dynamic_projection(
                          game.session_id.uid64, scid.uid.uid64, conn=_db))
            irow = joined
        t = None
        records_template = None
        if joined:
            joined_guid = str(joined[13] or "")
            requested_guid = str(template_guid or "")
            if (joined_guid and
                    (not requested_guid or
                     joined_guid.lower() == requested_guid.lower())):
                records_template = _records_card_projection(joined_guid)
                if records_template:
                    t = records_template[:6]
        if t is None:
            records_template = _records_card_projection(template_guid)
            if records_template:
                t = records_template[:6]
        if t is None:
            t = self._template_by_guid(template_guid)
        if t:
            tpl_guid, ct_name, name, cost, atk, def_ = t
            ct = game_engine.card_type_from_db(ct_name)
        else:
            # Champion templates — PvP champs in champion_templates_extended,
            # PvE champs (and some PvP) in champion_templates.  Try both.
            from pvp_db import db_champion_ability_guids
            if db_is_champion_template(template_guid):
                tpl_guid, ct_name, name, cost, atk, def_ = (
                    template_guid, "Champion", "Champion", 0, 0, 20)
                ct = game_engine.ECardTypes.Champion
            else:
                tpl_guid = "00000000-0000-0000-0000-000000000000"
                ct_name = "Troop"
                ct, name, cost, atk, def_ = game_engine.ECardTypes.Troop, "Card", 0, 0, 0
        shards = []
        attributes = game_engine.ECardAttributes.Unknown
        lethal = False
        srow = None
        # Instance-persisted abilities + power/toughness buffs (Shift /
        # PowerShiftedEvent). A bare template read would drop them on every
        # re-push, so read them from the card's game_cards row.
        inst_abilities_json = None
        atk_mod = def_mod = dmg = 0
        perm_atk = perm_def = 0
        temp_atk = temp_def = 0
        inst_attrs = 0
        temp_attrs = 0
        persisted_int_attrs = {}
        threshold_override = None
        subtype_override = None
        instance_damage_shield = False
        orig_tpl = None
        card_location = None
        card_owner_id = None
        card_instance_ref = None
        persisted_gem = _NO_PERSISTED_GEM
        if scid is not None:
            if irow:
                inst_abilities_json = irow[0]
                atk_mod = irow[1] or 0
                def_mod = irow[2] or 0
                dmg = irow[3] or 0
                orig_tpl = irow[4] or None
                inst_attrs = int(irow[9] or 0)
                temp_attrs = int(irow[10] or 0)
                try:
                    pb = json.loads(irow[5] or "{}")
                    perm_atk = int(pb.get("atk", 0) or 0)
                    perm_def = int(pb.get("def", 0) or 0)
                    raw_int_attrs = pb.get("int_attrs", {})
                    if isinstance(raw_int_attrs, dict):
                        persisted_int_attrs = {
                            str(k): int(v or 0)
                            for k, v in raw_int_attrs.items()
                            if int(v or 0) != 0
                        }
                    if isinstance(pb.get("thresholds"), list):
                        threshold_override = [int(value) for value in
                                              pb["thresholds"]]
                    if "subtype" in pb:
                        subtype_override = str(pb.get("subtype") or "")
                    instance_damage_shield = bool(pb.get("damage_shields"))
                except Exception:
                    pass
                try:
                    tb = json.loads(irow[6] or "{}")
                    temp_atk = int(tb.get("atk", 0) or 0)
                    temp_def = int(tb.get("def", 0) or 0)
                    instance_damage_shield = instance_damage_shield or bool(
                        tb.get("damage_shields"))
                except Exception:
                    pass
            card_owner_id = irow[11] if irow else None
            card_location = irow[12] if irow else None
            if irow and len(irow) > 14:
                card_instance_ref = irow[14]
            if irow and len(irow) > 15:
                persisted_gem = irow[15]
        native_bstate = getattr(self, "_current_bstate", None)
        native_cost = None
        if (scid is not None and isinstance(native_bstate, dict) and
                native_bstate.get("_rules_port_attached")):
            from rules_port.static_rules import effective_cost
            native_cost = effective_cost(
                _db, game.session_id.uid64, native_bstate, scid.uid.uid64)
            cost_mod = 0
        else:
            cost_mod = int(irow[7] or 0) if irow else 0
            cost_mod_json = irow[8] if irow else None
            if (scid is not None and cost_mod_json and
                    str(cost_mod_json).strip() not in ("[]", "{}", "")):
                from abilities.framework.cost_mod import cost_mod_delta
                cost_mod += cost_mod_delta(_db, game.session_id.uid64,
                                           scid.uid.uid64, cost_mod_json)
            elif scid is not None and card_location != "warzone":
            # Some all-zone dynamic reductions are represented by a
            # CardCreatedEvent in gamedata.  Cards that started in the deck
            # never receive that event during this match, but their displayed
            # and charged cost still needs to reflect the metadata formula
            # (Pterobot is the canonical example).
                try:
                    from abilities.framework.cost_mod import dynamic_cost_mod_delta
                    cost_mod += dynamic_cost_mod_delta(
                        _db, game.session_id.uid64, scid.uid.uid64)
                except Exception:
                    pass
        if tpl_guid != "00000000-0000-0000-0000-000000000000":
            records_template = (records_template if records_template and
                                str(records_template[0]).lower() ==
                                str(tpl_guid).lower() else None)
            if records_template:
                lethal = bool(records_template[10])
                shards = list(records_template[6])
                srow = (None, json.dumps(list(records_template[7])),
                        records_template[8])
            else:
                lethal = bool(db_card_template_lethal(tpl_guid))
                srow = db_card_template_thresholds(tpl_guid)
            if srow:
                if srow[0]:
                    try:
                        td = json.loads(srow[0])
                        shard_flags = {0:0, 1:4, 2:8, 3:16, 4:32, 5:64}
                        shards = [shard_flags.get(s, s) for s in td.get('list', [])]
                    except Exception:
                        pass
                if srow[2]:
                    try:
                        attributes = int(srow[2])
                    except Exception:
                        pass
            elif ct == game_engine.ECardTypes.Champion:
                # Champion abilities live in champion_abilities, not card_templates.
                from pvp_db import db_champion_ability_guids
                carow = db_champion_ability_guids(tpl_guid)
                if carow:
                    srow = (None, json.dumps(carow), None)
        if threshold_override is not None:
            shards = list(threshold_override)
        try:
            if records_template:
                subtype = records_template[9] or ""
            else:
                subtype = db_template_subtype(tpl_guid, conn=_db) or ""
        except Exception:
            subtype = ""
        if subtype_override is not None:
            subtype = subtype_override
        # Ability list: the instance's persisted list wins (Shift moves
        # abilities between cards).  An empty list is authoritative for
        # ordinary cards: a consumed ONE-SHOT must stay gone when the card is
        # rebuilt for CardUpdated.  Champions are the exception because their
        # game_cards rows are seeded with [] while their signature powers live
        # in champion_abilities rather than card_templates.
        ab_src = inst_abilities_json
        if (not ab_src or str(ab_src).strip() in ("null", "{}")
                or (str(ab_src).strip() == "[]"
                    and ct == game_engine.ECardTypes.Champion)):
            ab_src = srow[1] if srow else None
        ability_guids = []
        abilities = []
        if ab_src:
            try:
                parsed = json.loads(ab_src)
                abilities = [game_engine.ResourceId.from_str(g) for g in parsed]
                ability_guids = [g.lower() for g in parsed]
            except Exception:
                pass
        # Socketed-gem abilities come from the current deck socket, not from
        # the denormalized decks.gem_abilities cache.  The cache can predate a
        # deck edit (which was how a Speed gem retained an old Rage ability).
        # Call through the production class explicitly: focused test stubs
        # intentionally provide only the older helper surface, while the
        # live handler still receives the same method implementation.
        gem_instance_id = (instance_id if instance_id is not None
                           else card_instance_ref)
        gem_type = HCPHandler._card_gem_type(
            self, game, scid, gem_instance_id, persisted_gem=persisted_gem)
        display_gems = int(gem_type or 0)
        if (display_gems > 0x3FF and
                not (display_gems & EMPTY_SOCKET_GEMS)):
            # Older deck saves lost the packed-format flag but retained the
            # socket payload. Restore the marker in the client projection so
            # its socket renderer decodes every slot instead of treating the
            # combined value as one legacy gem enum.
            display_gems |= EMPTY_SOCKET_GEMS
        if gem_type:
            gem_guids = db_gem_ability_guids(gem_type, conn=_db)
            for gem_guid in gem_guids:
                gem_guid = str(gem_guid).lower()
                if gem_guid not in ability_guids:
                    ability_guids.append(gem_guid)
                    abilities.append(game_engine.ResourceId.from_str(gem_guid))
            # Persist the merged list for the active game instance so combat
            # statics and subsequent pushes see the same gem ability.
            if scid is not None and gem_guids:
                try:
                    current = json.loads(inst_abilities_json or "[]")
                except Exception:
                    current = []
                current = [str(g).lower() for g in current]
                merged = list(current)
                if not merged:
                    merged = list(ability_guids)
                else:
                    for gem_guid in gem_guids:
                        if str(gem_guid).lower() not in merged:
                            merged.append(str(gem_guid).lower())
                if merged != current:
                    db_set_card_abilities(
                        game.session_id.uid64, scid.uid.uid64,
                        json.dumps(merged), conn=_db)
                    # _card_full_data() is also called immediately before the
                    # battle state is persisted.  That save uses the separate
                    # game_session connection, so leave the shared connection
                    # transaction closed or it can deadlock on its own write
                    # (especially during the opening hand when socketed cards
                    # are re-pushed).
                    _db.commit()
        # Encounter scene statics are continuous effects. Apply them silently
        # when a card is materialized in the relevant zone, using the granted
        # ability's typed target filter. Beast Crossing therefore affects only
        # Wild troops in the warzone, including troops created later, without
        # ever placing those grants on the chain.
        if scid is not None and card_location == "warzone":
            from abilities.framework.targeting import evaluate_card_filter
            card_subtype = subtype or ""
            # Scene target filters can inspect dynamic IntAttr markers (for
            # example, Taming Dire Toad only grants Untamed to non-Tamed
            # Dire Toads).  Reconstruct those markers from the card's current
            # ability list before evaluating the scene filter.
            scene_int_attrs = {}
            scene_int_attrs.update(persisted_int_attrs)
            for _ag in ability_guids:
                for _eg, _et, _ep in db_ability_effect_rows(_ag, conn=_db):
                    if _et != "CardModifierAbilityEffectTemplate":
                        continue
                    try:
                        _pd = json.loads(_ep or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        _pd = {}
                    if str(_pd.get("property", "")).lower() != "intattr":
                        continue
                    _attr = str(_pd.get("attribute") or "")
                    if not _attr and "untamed" in str(_pd.get("text") or "").lower():
                        _attr = "Untamed"
                    if _attr:
                        scene_int_attrs[_attr] = 1
            if scene_int_attrs.get("Tamed", 0) > 0:
                scene_int_attrs.pop("Untamed", None)
            scene_added = []
            for scene_ag in ((getattr(self, "_scene_global_ability_guids", []) or [])
                             + (getattr(self, "_scene_targeted_ability_guids", []) or [])):
                target_ids_json = db_ability_target_template_ids(
                    scene_ag, conn=_db)
                try:
                    target_ids = json.loads(target_ids_json or "[]")
                except (TypeError, ValueError):
                    target_ids = []
                scene_ag = str(scene_ag).lower()
                # Targeted encounter passives retain the setup card's
                # controller.  This is important for filters such as
                # IsControlledBy (the Taming Dire Toad passive means the
                # controller's Dire Toads, not every Dire Toad in play).
                scene_card = {
                    "card_uid": scid.uid.uid64,
                    "card_type": ct_name,
                    "location": card_location,
                    "shards": shards,
                    "name": name,
                    "user_id": card_owner_id,
                    "subtype": card_subtype,
                    "attributes": int(attributes | inst_attrs | temp_attrs),
                    "int_attrs": scene_int_attrs,
                }
                if scene_ag in (getattr(self, "_scene_targeted_ability_owners", {}) or {}):
                    scene_card["src_owner_id"] = (
                        self._scene_targeted_ability_owners[scene_ag])
                qualifies = False
                for target_id in target_ids:
                    filter_json = db_target_template_filter(
                        target_id, conn=_db)
                    try:
                        filter_data = json.loads(filter_json or "{}")
                    except (TypeError, ValueError):
                        filter_data = {}
                    if evaluate_card_filter(scene_card, filter_data,
                                            scid.uid.uid64):
                        qualifies = True
                        break
                if qualifies and scene_ag not in ability_guids:
                    ability_guids.append(scene_ag)
                    abilities.append(game_engine.ResourceId.from_str(scene_ag))
                    scene_added.append(scene_ag)
            if scene_added:
                db_set_card_abilities(
                    game.session_id.uid64, scid.uid.uid64,
                    json.dumps(ability_guids), conn=_db)
                _db.commit()
        # Effective attributes = static template attributes + any attributes
        # granted by the card's CURRENT passives. Attributes render as icons on
        # the card and must be present the whole time (not computed only during
        # combat). E.g. Gemsoul Feeder's "This gets Lifedrain in all zones"
        # passive (44605164) grants SpiritDrain via a CardModifier
        # AttributeModifier.
        # Instance-persisted attributes (apply_attribute_grant writes the
        # template attrs + any granted keywords into game_cards.card_attributes,
        # e.g. Inner Conflict's permanent "can't attack or block"). Without
        # this OR the icons vanish on the next CardUpdated re-push.
        attributes |= inst_attrs | temp_attrs
        # Permanent + this-turn buffs (apply_card_stat_mod) also contribute.
        atk += atk_mod + perm_atk + temp_atk
        def_ += def_mod + perm_def + temp_def
        # Cost modifiers (static card_cost_mod + dynamic formula entries).
        cost = (int(native_cost) if native_cost is not None else
                max(0, cost + cost_mod))
        # Continuous static abilities (WhileCardInPlay / Permanent): dynamic
        # self-bonuses ("+2/+2 for each card in your hand"), auras ("Troops
        # you control have +2/+2") and zone-wide cost modifiers ("Your
        # artifacts in all zones have cost -1").  Computed from gamedata so the
        # displayed card and its playable cost always match the current board.
        if scid is not None and tpl_guid != "00000000-0000-0000-0000-000000000000":
            try:
                from rules_port.static_rules import effective_deltas
                bstate = getattr(self, "_current_bstate", None)
                if bstate is None:
                    try:
                        raw_state = db_session_turn_order(
                            game.session_id.uid64, conn=_db)
                        data = json.loads(raw_state) if raw_state else {}
                        bstate = data if isinstance(data, dict) else {}
                    except Exception:
                        bstate = {}
                    self._current_bstate = bstate
                static = effective_deltas(_db, game.session_id.uid64, bstate,
                                          scid.uid.uid64)
                atk += static["atk"]
                def_ += static["def"]
                if native_cost is None:
                    cost = max(0, cost + static["cost_mod"])
                attributes |= static["attrs"]
            except Exception:
                pass
        # TEMPORARY combat damage: show the defense reduced by damage taken
        # (the client colors values below the template base red). The troop
        # "heals" when card_damage is cleared at its controller's Prep.
        def_ = max(0, def_ - dmg)
        shards = [int(shard) for shard in shards if shard is not None]
        game.card_defs[scid] = game_engine.CardDef(
            name, ct, cost, atk, def_, shards, abilities, attributes, lethal,
            subtype=subtype)
        if scid is not None:
            from rules_port.ability_usage import remaining_uses_per_game
            ability_uses = remaining_uses_per_game(
                _db, game.session_id.uid64, scid.uid.uid64, ability_guids,
                getattr(self, "_current_bstate", None) or {},
                champion=ct == game_engine.ECardTypes.Champion)
            game.card_defs[scid].uses_per_game_counts = {
                game_engine.ResourceId.from_str(guid): int(value)
                for guid, value in ability_uses.items()
            }
        cooldowns = {}
        if scid is not None:
            try:
                from pvp_db import db_card_cooldown_counts
                cooldowns.update(db_card_cooldown_counts(
                    game.session_id.uid64, scid.uid.uid64, conn=_db))
            except Exception:
                pass
            state = getattr(self, "_current_bstate", None) or {}
            prefix = f"{int(scid.uid.uid64)}:"
            for key, value in (state.get("champion_ability_cooldowns") or {}).items():
                if str(key).startswith(prefix):
                    guid = str(key).split(":", 1)[1].lower()
                    try:
                        remaining = int(value or 0)
                    except (TypeError, ValueError):
                        continue
                    if remaining > 0:
                        cooldowns[guid] = remaining
        game.card_defs[scid].cooldown_counts = {
            game_engine.ResourceId.from_str(guid): int(value)
            for guid, value in cooldowns.items() if int(value or 0) > 0
        }
        # Tunneling is a card IntAttr in the template TAC, not an
        # ECardAttributes bit. Keep it on CardDef so every CardUpdated carries
        # the same value the original client reads from CardTemplate.tac.
        try:
            if ((getattr(getattr(game, "session", None),
                         "_rules_port_session", None) is not None) or
                    (getattr(self, "_current_bstate", None) or {}).get(
                        "_rules_port_attached")):
                from rules_port.tunneling import tunneling_value
            else:
                from abilities.framework.effects.counters import tunneling_value
            game.card_defs[scid].tunneling = tunneling_value(
                _db, tpl_guid, persisted_int_attrs)
        except Exception:
            game.card_defs[scid].tunneling = int(
                persisted_int_attrs.get("Tunneling", 0) or 0)
        game.card_defs[scid].damage_shield = instance_damage_shield
        # IntAttr modifiers are transmitted separately from keyword
        # attributes. Preserve metadata-defined Untamed on the final card
        # definition (the definition above replaces any earlier placeholder)
        # so the Taming Sphere's IntAttrFilter can select this Dire Toad.
        if scid is not None:
            untamed = False
            for _ag in ability_guids:
                _has_untamed = any(
                    _et == "CardModifierAbilityEffectTemplate" and
                    "untamed" in str(_ep or "").lower()
                    for _eg, _et, _ep in db_ability_effect_rows(
                        _ag, conn=_db))
                if _has_untamed:
                    untamed = True
                    break
            int_attrs = dict(persisted_int_attrs)
            if untamed:
                int_attrs.setdefault("Untamed", 1)
            # Taming is a persistent marker and overrides the scene's static
            # non-Tamed -> Untamed grant for this card instance.
            if int_attrs.get("Tamed", 0) > 0:
                int_attrs.pop("Untamed", None)
            game.card_defs[scid].int_attrs = int_attrs
        # Card.EscalationCount is per card instance (C# starts it at one and
        # Session.Escalate increments the selected card). Carry the persisted
        # count into CardUpdated for every card, regardless of its text.
        if scid is not None:
            from rules_port.statistics import card_escalation_count
            game.card_defs[scid].escalation = card_escalation_count(
                _db, game.session_id.uid64,
                getattr(self, "_current_bstate", None) or {},
                scid.uid.uid64)
        # Counters + voided-card relationships must survive every re-push: the
        # client renders counter badges and the exile's voided-card link from
        # CardUpdated, so carry them on the CardDef (push_card_updated reads
        # them by default).
        try:
            from abilities.framework.effects.counters import card_counters_full
            c_counts, c_guids = card_counters_full(
                _db, game.session_id.uid64, scid.uid.uid64)
            if c_counts:
                cc = {}
                for cname, cnum in c_counts.items():
                    g = c_guids.get(cname)
                    if g:
                        cc[str(g)] = int(cnum)
                game.card_defs[scid].counters = cc
        except Exception:
            pass
        try:
            cbstate = getattr(self, "_current_bstate", None) or {}
            vby = cbstate.get("voided_by") or {}
            linked = [int(u) for u in vby.get(str(scid.uid.uid64), [])]
            if linked:
                game.card_defs[scid].related_cards = [
                    game_engine.SessionCardId(game_engine.UID(u))
                    for u in linked]
        except Exception:
            pass
        # The card's ORIGINAL (pre-transform) template — read from the
        # game_cards row (per-instance, lasts the whole game), NOT derived here.
        # push_card_updated sends it as CardUpdated.OrigTemplate so the client's
        # examine panel can show the original card ("Transformed From <Name>").
        game.card_defs[scid].orig_template = orig_tpl
        # Carry the socketed gem on the CardDef so EVERY later push_card_updated
        # (warzone entry, trigger "shake", phase re-push) renders the gem even
        # when the caller omits gems= — e.g. Shamed Gladiator must keep its gem
        # once it resolves to the board.
        if scid is not None and game.card_defs.get(scid) is not None:
            game.card_defs[scid].gems = display_gems
            # Rage display: CardUpdated.rage drives the client's Rage icon, and
            # it was never populated (always 0).  Use the same authoritative
            # static-ability evaluation as combat (template rage_value + granted
            # "Rage 1 in all zones" gems, gated by the ability's threshold
            # condition) so the card shows the Rage it actually has.
            try:
                from rules_port.static_rules import effective_stats
                bstate = getattr(self, "_current_bstate", None) or {}
                _atk, _def, _attrs, _flags, rage = effective_stats(
                    _db, game.session_id.uid64, bstate, scid.uid.uid64)
                game.card_defs[scid].rage = int(rage or 0)
            except Exception:
                game.card_defs[scid].rage = 0
        return tpl_guid, ct, name, cost, atk, def_, display_gems

    def _champion_targets(self):
        """[(card_uid, user_id, name, health)] for both champions — the client
        can target champions (IsHero filters), so they join targeting pools."""
        out = []
        p = getattr(self, "_player_champ_scid", None)
        if p is not None:
            hp = getattr(self, "_player_starting_health", 20)
            if getattr(self, "_current_bstate", None):
                hp = self._current_bstate.get("player_health", hp)
            out.append((int(p.uid.uid64),
                        self.user_profile["id"] if self.user_profile else 0,
                        "Player", hp))
        a = getattr(self, "_ai_champ_scid", None)
        if a is not None:
            hp = getattr(self, "_ai_starting_health", 20)
            if getattr(self, "_current_bstate", None):
                hp = self._current_bstate.get("ai_health", hp)
            out.append((int(a.uid.uid64), 0, "AI", hp))
        return out

    def _revert_to_template(self, session, card_uid, template_guid, user_id=None):
        """Reversion: reset a card instance to its ORIGINAL template's canonical
        data.

        A transformed card (e.g. Spiritbound Spy -> Phantom) keeps the same
        card_uid but its template data changed; Reversion restores the instance
        to the template it was CREATED from (original_template_guid), resetting
        template_guid / card_type / card_attributes / card_abilities / stat mods
        / uses as though it were a fresh card. When `template_guid` is given and
        differs, it is used instead (an explicit Revert-to-X effect).
        """
        from pvp_db import db_card_original_template, db_card_template_full, db_card_revert_to_template
        # The instance's original identity wins; fall back to the given template.
        original = db_card_original_template(session.session_id, card_uid) or ""
        target = original if original else (template_guid or "")
        if not target:
            return False
        trow = db_card_template_full(target)
        if not trow:
            return False
        ctype, cost, atk, def_, thresh_json, abilities_json, attributes = trow
        abilities_json = abilities_json or "[]"
        try:
            import json as _rj
            ability_guids = [g.lower() for g in _rj.loads(abilities_json)]
        except Exception:
            ability_guids = []
        attributes = int(attributes or 0)
        db_card_revert_to_template(session.session_id, card_uid, attributes,
                                   abilities_json, target, ctype)
        log_req(f"    Revert {hex(card_uid)} to template {target[:8]} "
                f"(type={ctype} attrs={attributes} abilities={ability_guids})")
        return True

    def _card_ability_list(self, session, card_uid):
        """Current ability GUID list for a card instance (from card_abilities)."""
        return db_card_ability_list(session.session_id, card_uid)

    def _sync_instance_card_data(self, session, card_uid, template_guid,
                                 commit=True):
        """Populate a card instance's ability/attribute/uses data from its template.

        card_templates is the CANONICAL source; game_cards.card_abilities /
        card_attributes / card_uses are the per-instance working copy. This is
        called when a card instance is created (and by Reversion to reset it).
        Uses is reset to {} (a fresh instance has no usage history).
        """
        if not template_guid:
            return
        ab_json, attrs = db_get_card_abilities(template_guid)
        ab_json = ab_json or "[]"
        attrs = int(attrs or 0)
        try:
            ability_guids = [g.lower() for g in json.loads(ab_json)]
        except Exception:
            ability_guids = []
        db_card_sync_abilities(
            session.session_id, card_uid, ab_json, attrs, template_guid,
            commit=commit)
        return attrs

    def _battle_setup_template_data(self, template_guid, cache):
        """Return canonical setup data for a card template, using *cache*.

        Battle deck construction creates many copies of the same templates.  A
        fresh instance still needs the template's working ability/attribute
        snapshot, but resolving that snapshot once per copy causes a SELECT and
        an UPDATE for every card.  Keep the metadata local to one battle setup
        (rather than on the handler, which serves multiple sessions) and reuse
        it for every copy.

        Returns ``(abilities_json, effective_attributes, ability_guids,
        threshold_json)`` or ``None`` when the template is missing.
        """
        if not template_guid:
            return None
        key = str(template_guid).lower()
        if key in cache:
            return cache[key]
        row = db_card_template_thresholds(template_guid)
        if not row:
            cache[key] = None
            return None
        threshold_json, abilities_json, attributes = row
        abilities_json = abilities_json or "[]"
        try:
            ability_guids = [str(g).lower() for g in json.loads(abilities_json)]
        except Exception:
            ability_guids = []
        effective_attributes = int(attributes or 0)
        result = (abilities_json, effective_attributes, ability_guids,
                  threshold_json)
        cache[key] = result
        return result

    def _card_uses(self, session, card_uid):
        """Return the per-ability usage dict for a card instance."""
        return db_card_uses(session.session_id, card_uid)

    def _bump_card_use(self, session, card_uid, ability_guid):
        """Increment a card instance's usage of an ability (UsesPerGame/Turn)."""
        return db_bump_card_use(session.session_id, card_uid, ability_guid)

    def _champion_ability_uses(self, bstate):
        """Per-ability activation counts for the champion's synthetic powers."""
        uses = (bstate or {}).get("champion_ability_uses")
        return uses if isinstance(uses, dict) else {}

    def _champion_ability_available(self, bstate, ability_guid, graph=None,
                                    source_uid=None):
        """Whether an authored ``m_UsesPerGame`` champion power still has a use.

        ONE-SHOT talent powers (``uses_per_game=1``) belong to the synthetic
        champion SessionCardId, which has no ``game_cards`` row to hold
        ``card_uses``; the count lives in the shared battle state instead.
        """
        limit = int(getattr(getattr(graph, "costs", None),
                            "uses_per_game", 0) or 0)
        guid = str(ability_guid or "").lower()
        if source_uid is None:
            champion = getattr(self, "_player_champ_scid", None)
            if isinstance(champion, game_engine.SessionCardId):
                source_uid = int(champion.uid.uid64)
        from rules_port.cooldowns import champion_cooldown_key, champion_cooldown
        cooldown_key = champion_cooldown_key(source_uid, guid)
        if cooldown_key and champion_cooldown(bstate, source_uid, guid) > 0:
            return False
        uses = self._champion_ability_uses(bstate)
        used = int(uses.get(cooldown_key, uses.get(guid, 0)) or 0)
        if limit > 0 and used >= limit:
            return False
        per_turn = int(getattr(getattr(graph, "costs", None),
                               "uses_per_turn", 0) or 0)
        if per_turn > 0 and source_uid is not None:
            from rules_port.runtime_helpers import (
                champion_ability_use_key, champion_ability_uses_this_turn)
            turn_key = champion_ability_use_key(source_uid, guid)
            if champion_ability_uses_this_turn(bstate, turn_key) >= per_turn:
                return False
        return True

    def _consume_champion_ability_use(self, bstate, ability_guid, graph=None,
                                      source_uid=None):
        """Record one activation of a champion power in the battle state."""
        guid = str(ability_guid or "").lower()
        if source_uid is None:
            champion = getattr(self, "_player_champ_scid", None)
            if isinstance(champion, game_engine.SessionCardId):
                source_uid = int(champion.uid.uid64)
        from rules_port.cooldowns import (champion_cooldown_key,
                                          set_champion_cooldown)
        key = champion_cooldown_key(source_uid, guid) or guid
        if graph is None:
            # Callers may omit the graph; resolve the authored costs here so a
            # ONE-SHOT activation still consumes its per-game use.
            from gamedata import DEFAULT_RECORD_STORE, ability_graph
            graph = ability_graph(DEFAULT_RECORD_STORE, guid)
        costs = getattr(graph, "costs", None)
        per_game = int(getattr(costs, "uses_per_game", 0) or 0)
        uses = bstate.setdefault("champion_ability_uses", {})
        if per_game > 0:
            uses[key] = int(uses.get(key, uses.get(guid, 0)) or 0) + 1
        cooldown = int(getattr(getattr(graph, "costs", None),
                               "cooldown", 0) or 0)
        if cooldown > 0:
            set_champion_cooldown(bstate, source_uid, guid, cooldown)
        per_turn = int(getattr(getattr(graph, "costs", None),
                               "uses_per_turn", 0) or 0)
        if per_turn > 0 and source_uid is not None:
            from rules_port.runtime_helpers import (
                champion_ability_use_key,
                record_champion_ability_use_this_turn)
            record_champion_ability_use_this_turn(
                bstate, champion_ability_use_key(source_uid, guid))
        return int(uses.get(key, 0) or 0)

    def _remove_one_shot_ability(self, session, card_uid, ability_guid,
                                  game, pl_t, ai_t, bstate=None):
        """Consume a one-shot ability on this individual card instance.

        ``uses_per_game=1`` is the extracted representation of ONE-SHOT.
        Usage counters still gate activation, but the client also needs the
        current per-instance ability list updated or it will continue to show
        the used power on the card.  Recompute effective attributes and push a
        complete CardUpdated so both the server and client agree.
        """
        meta = db_ability_activation_metadata(
            ability_guid, conn=_db)
        if not meta or int(meta[1] or 0) != 1:
            return False
        row = db_card_mutation_snapshot(
            session.session_id, int(card_uid), conn=_db)
        if not row:
            return False
        abilities = self._card_ability_list(session, card_uid)
        ag = str(ability_guid).lower()
        if ag not in abilities:
            return False
        abilities.remove(ag)
        base_attrs = int(db_template_attributes(row[0], conn=_db) or 0)
        attributes = base_attrs
        db_set_card_abilities_and_attributes(
            session.session_id, int(card_uid), json.dumps(abilities),
            attributes, conn=_db)
        _db.commit()
        if game is not None:
            scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
            _tpl, ct, _name, cost, atk, defense, gems = self._card_full_data(
                game, scid, row[0])
            if (bstate or {}).get("pvp"):
                owner = game_engine.UID.make(244, int(row[1]))
            else:
                owner = pl_t if int(row[1] or 0) != 0 else ai_t
            from abilities.framework._shared import card_collection_for_location
            game.push_card_updated(
                scid, owner, card_collection_for_location(row[2]), ct,
                template_id=_tpl, cost=cost, attack=atk, defense=defense,
                gems=gems, attributes=attributes, state=int(row[3] or 0))
        log_req(f"    One-shot ability {ag[:8]} removed from {hex(int(card_uid))}")
        return True

    def _apply_power_shifted_triggers(self, session, target_uid, game,
                                      pl_t, ai_t, bstate):
        """Dispatch the authored PowerShiftedEvent through the shared BOM."""
        target_owner = db_card_owner_id(
            session.session_id, int(target_uid), conn=_db)
        if target_owner is None:
            return ""
        return self._dispatch_native_trigger(
            game, session, pl_t, ai_t, bstate, "PowerShiftedEvent",
            int(target_uid), target_owner, target_card_id=int(target_uid))

    def _shift_ability_between(self, session, pl_t, ai_t, source_uid, target_uid,
                               ability_guid, game, bstate=None):
        """Move a granted ability from one card instance to another (ShiftPower).

        Removes `ability_guid` from the source's card_abilities, adds it to the
        target's, then recomputes both cards' effective attributes (the shifted
        ability's BOM may grant an attribute, e.g. Lifedrain -> SpiritDrain).
        Persists to game_cards and pushes CardUpdateds for both cards. The
        template remains the canonical source; a Reversion resets either card.
        """
        # Load both instances' current ability lists.
        srow = db_card_mutation_info(
            session.session_id, int(source_uid), conn=_db)
        trow = db_card_mutation_info(
            session.session_id, int(target_uid), conn=_db)
        if not srow or not trow:
            return False
        src_ab = [a for a in self._card_ability_list(session, source_uid) if a != ability_guid]
        tgt_ab = self._card_ability_list(session, target_uid)
        if ability_guid not in tgt_ab:
            tgt_ab.append(ability_guid)
        # Recompute effective attributes from the (possibly changed) ability lists.
        # Include the template's static attributes.
        for row, ab, uid in ((srow, src_ab, source_uid), (trow, tgt_ab, target_uid)):
            base = int(db_template_attributes(row[0], conn=_db) or 0)
            eff = base
            db_set_card_abilities_and_attributes(
                session.session_id, int(uid), json.dumps(ab), eff, conn=_db)
        _db.commit()
        # A power was shifted ONTO the target: resolve any of its own
        # PowerShiftedEvent triggers (e.g. Deepgaze Acolyte +1/+1). Persists
        # the buff (card_attack_mod / card_defense_mod); _card_full_data below
        # folds it into the pushed stats.
        self._apply_power_shifted_triggers(
            session, target_uid, game, pl_t, ai_t, bstate or {})
        # Push CardUpdated for both cards so the client updates the ability
        # icons + attribute icons. Build the CardDef from the template via
        # _card_full_data so the pushed card keeps its threshold pips / text
        # (a bare CardDef with shards=[] wipes the color-cost pips), then
        # override the (shifted) ability list and effective attributes.
        for row, uid, ab in ((srow, source_uid, src_ab), (trow, target_uid, tgt_ab)):
            scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
            _tpl, ct, _name, _c, _a, _d, _gem = self._card_full_data(game, scid, row[0])
            cdef = game.card_defs[scid]
            cdef.abilities = [game_engine.ResourceId.from_str(a) for a in ab]
            base = 0
            st_val = db_template_attributes(row[0], conn=_db)
            if st_val:
                base = int(st_val)
            cdef.attributes = base
            owner = pl_t if row[1] != 0 else ai_t
            # NOTE: _card_full_data already folded in the persisted
            # card_attack_mod / card_defense_mod (written by
            # _apply_power_shifted_triggers), so we must NOT add atk_mod/def_mod
            # again here — that double-counts the PowerShifted +1/+1 (the client
            # showed 4/3, then the correct 3/2 at the next phase, looking like
            # the buff was lost).
            # Carry the card's persisted state (StartedATurnOnYourSide, Tapped,
            # Attacking, ...) so the CardUpdated doesn't wipe it on the client —
            # a bare state=None would make the troops look like they just came
            # into play (summoning sickness) and unattackable.
            cstate = db_card_state_value(
                session.session_id, int(uid), conn=_db)
            game.push_card_updated(scid, owner, game_engine.ECardCollections.Warzone, ct,
                                   template_id=row[0], attributes=cdef.attributes,
                                   state=cstate)
        log_req(f"    ShiftPower: {ability_guid[:8]} {hex(source_uid)} -> {hex(target_uid)} "
                f"(src_ab={src_ab} tgt_ab={tgt_ab})")
        return True

    def _activate_troop_ability(self, session, pl_t, ai_t, bstate, source_uid,
                                ability_guid, inner_bytes):
        """Activate a manual ability on a player-controlled card.

        Validates the cost/phase/uses gates, pays the resource cost, bumps the
        instance's usage, records the Shift source+target in battle state, then
        resolves the ability's BOM (ability_effects). The BOM's
        TACAbilityEffectTemplate leaf dispatches to ShiftPower generically via
        the serialized TAC (never hardcoded).
        """
        graph = _records_ability_graph(self, ability_guid)
        if graph is None:
            log_req(f"    Troop ability {ability_guid[:8]}: missing current "
                    "Records ability")
            return
        from rules_port.triggers import trigger_collection_allows
        source_location = db_card_location(
            session.session_id, int(source_uid), conn=_db)
        source_row = (source_location,) if source_location is not None else None
        if not source_row or not trigger_collection_allows(
                graph.trigger_collection_flags, source_row[0]):
            log_req(f"    Troop ability {ability_guid[:8]}: source collection "
                    f"{source_row[0] if source_row else None} is not allowed")
            return
        from gamedata import AbilityInstance, ActivationData
        ability = AbilityInstance.from_graph(
            graph, source_uid=int(source_uid), owner_id=self.user_profile["id"],
            responsible_player_id=self.user_profile["id"], store=self._play_plan_store)
        casting = 64 if graph.casting_behavior == "QuickAction" else 8
        cost = int(ability.costs.activation or 0)
        upg = int(ability.costs.uses_per_game or 0)
        upt = int(ability.costs.uses_per_turn or 0)
        exh = bool(ability.costs.exhausts_card_on_use)
        variable_x = int(ability.costs.variable_activation or 0)
        variable_min = int(ability.costs.variable_minimum or 0)
        resources = bstate.get("player_resources", 0)
        x_cost = 0
        if variable_x:
            x_cost = self._extract_int32_field(inner_bytes, "m_ResourceXCost")
            x_cost = max(0, int(x_cost or 0))
            if x_cost < variable_min:
                log_req(f"    Troop ability {ability_guid[:8]}: X={x_cost} below minimum {variable_min}")
                return
        from pvp_db import (db_card_ability_use_counts,
                            db_card_cooldown_counts,
                            db_record_card_ability_use,
                            db_set_card_cooldown)
        used_game, used_turn = db_card_ability_use_counts(
            session.session_id, source_uid, ability_guid,
            bstate.get("turn_number", 1), conn=_db)
        if cost + x_cost > resources:
            log_req(f"    Troop ability {ability_guid[:8]}: need {cost + x_cost} resources, have {resources}")
            return
        if upg and used_game >= upg:
            log_req(f"    Troop ability {ability_guid[:8]}: uses_per_game exhausted ({used_game})")
            return
        if upt and used_turn >= upt:
            log_req(f"    Troop ability {ability_guid[:8]}: uses_per_turn exhausted ({used_turn})")
            return
        if (graph.costs.cooldown and int(db_card_cooldown_counts(
                session.session_id, source_uid, conn=_db).get(
                    ability_guid, 0) or 0) > 0):
            log_req(f"    Troop ability {ability_guid[:8]}: cooldown active")
            return
        if exh:
            # Authoritative exhaust-as-cost gate (mirrors _affordable_troop_abilities).
            activation_info = db_card_activation_info(
                session.session_id, int(source_uid), conn=_db)
            cstate = int(activation_info[0] or 0) if activation_info else 0
            cattrs = int(activation_info[1] or 0) if activation_info else 0
            if (cstate & game_engine.ECardStates.Tapped
                    or (not (cstate & game_engine.ECardStates.StartedATurnOnYourSide)
                        and not (cattrs & game_engine.ECardAttributes.Speed))):
                log_req(f"    Troop ability {ability_guid[:8]}: cannot exhaust "
                        f"{hex(source_uid)} (summoning sick/tapped)")
                return
        # Ability conditions gate activation (client CanActivateAbilityBase:
        # TriggerCondition.IsValid). E.g. Droo's Colossal Walker only activates
        # while exhausted.
        if graph.ability_condition or graph.trigger_condition:
            if bstate.get("_rules_port_attached"):
                from rules_port.condition_context import ConditionContext
                from rules_port.conditions import trigger_condition_met
            else:
                from abilities.framework.condition_engine import (
                    ConditionContext,
                    trigger_condition_met,
                )
            cond_ctx = ConditionContext(
                _db, session, bstate,
                ability_source_uid=int(source_uid),
                ability_source_owner_id=self.user_profile["id"])
            if not trigger_condition_met(graph.source.to_dict(), cond_ctx):
                log_req(f"    Troop ability {ability_guid[:8]}: ability condition not met")
                return
        if bstate.get("_rules_port_attached"):
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        phase = _be.current_phase(bstate)
        if casting != 64 and (
                bstate.get("turn_player") != _be.PLAYER
                or phase not in (game_engine.ETurnPhases.FirstMainPhase,
                                  game_engine.ETurnPhases.SecondMainPhase)
                or not _be.stack_empty(bstate)):
            log_req(f"    Troop ability {ability_guid[:8]}: basic action not legal now")
            return
        # Extract every selected card from the transaction. A manual ability
        # can carry both payment targets (Concubunny's ready Shin'hare) and a
        # separate effect target, so the selected cards must be split by their
        # metadata target templates instead of treating the last UID as the
        # effect target.
        selected_uids = []
        if isinstance(inner_bytes, bytes):
            for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                try:
                    uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                    if (uid64 & 0xFF) == 1:
                        uid64 = int(uid64)
                        if uid64 != source_uid:
                            selected_uids.append(uid64)
                except Exception:
                    continue

        # Validate and split metadata-declared card payment targets first.
        # This is the same distinction used by the PvP activation path: an
        # ExhaustTarget is a cost, not an effect target, and must be paid
        # before the BOM resolves.  The live target filter is re-evaluated so
        # a stale transaction cannot exhaust a troop that became tapped after
        # the option list was sent.
        from rules_port.builder import AbilityBuilder
        from rules_port.costs import (
            AbilityCostTarget, validate_cost_target_selection,
        )
        builder = AbilityBuilder.from_graph(
            graph, store=getattr(self, "_play_plan_store", None))
        cost_selections = []
        cost_target_uids = []
        used_cost_uids = set()
        for cost_ref in builder.cost_targets:
            if cost_ref.target is None:
                log_req(f"    REJECTED troop ability {ability_guid[:8]}: "
                        f"missing payment target template {cost_ref.guid}")
                return
            minimum = cost_ref.minimum
            maximum = cost_ref.maximum
            auto_source = cost_ref.is_source_auto_target
            if auto_source:
                candidates = [int(source_uid)]
            else:
                candidates = builder.target_candidates(
                    _db, session.session_id, self.user_profile["id"],
                    cost_ref.target, int(source_uid), both_players=False,
                    champions=self._champion_targets(), battle_state=bstate)
            candidate_set = {int(uid) for uid in candidates}
            available = ([int(source_uid)] if auto_source else
                         [int(uid) for uid in selected_uids
                          if int(uid) in candidate_set and
                          int(uid) not in used_cost_uids])
            cost_target = AbilityCostTarget(
                index=cost_ref.index, kind=cost_ref.kind,
                guid=cost_ref.guid, minimum=minimum, maximum=maximum,
                is_source_auto_target=auto_source,
                candidates=tuple(candidates),
                allow_best_effort_minimum=cost_ref.allow_best_effort_minimum)
            if (len(available) < minimum and
                    not cost_target.allow_best_effort_minimum):
                log_req(f"    REJECTED troop ability {ability_guid[:8]}: "
                        f"missing/illegal payment target for {cost_ref.guid}")
                return
            chosen = available if maximum < 0 else available[:maximum]
            chosen = validate_cost_target_selection(
                _db, session.session_id, self.user_profile["id"],
                int(source_uid), cost_target, chosen,
                champions=self._champion_targets(), battle_state=bstate)
            if chosen is None:
                log_req(f"    REJECTED troop ability {ability_guid[:8]}: "
                        f"invalid metadata payment target {cost_ref.guid}")
                return
            used_cost_uids.update(chosen)
            cost_target_uids.extend(chosen)
            cost_selections.append(({"kind": cost_ref.kind}, tuple(chosen)))

        # Work out which target templates are effect targets (as opposed to
        # automatic source/player targets or card-cost targets), then validate
        # the transaction against the same gamedata filter used to build the
        # picker. This keeps activation data-driven and prevents an invalid or
        # missing target from silently resolving against the source card.
        explicit_templates = builder.targets(
            requires_input=True, include_costs=False)
        if explicit_templates:
            # Taming Sphere has one required explicit target. For abilities
            # with several templates, accept the selected card if it is legal
            # for any effect target, while excluding cards already consumed as
            # payment targets.
            target_uid = None
            for target in explicit_templates:
                candidates = builder.target_candidates(
                    _db, session.session_id, self.user_profile["id"],
                    target, int(source_uid),
                    champions=self._champion_targets(),
                    battle_state=bstate)
                for candidate in selected_uids:
                    if (int(candidate) not in used_cost_uids and
                            int(candidate) in {int(c) for c in candidates}):
                        target_uid = int(candidate)
                        break
                if target_uid is not None:
                    break
            valid_target = target_uid is not None
            if not valid_target:
                log_req(f"    REJECTED troop ability {ability_guid[:8]}: "
                        f"missing/illegal metadata target {target_uid}")
                return
        else:
            # Any card UIDs in a cost TargetMap (sacrifice/void/etc.) are not
            # effect targets consumed by the BOM resolver.
            target_uid = None

        # Expose payment selections to metadata-driven variables such as
        # Construction Plans, then pay them before resolving the ability.
        if cost_target_uids:
            bstate.setdefault("ability_lists", {})["ExhaustedCards"] = [
                int(uid) for uid in cost_target_uids
                if any(spec["kind"] == "exhaust" and int(uid) in selected
                       for spec, selected in cost_selections)]

        # Pay the resource cost only after target validation succeeds.
        if not self._card_costs_valid(
                session, cost_selections, self.user_profile["id"]):
            log_req(f"    REJECTED troop ability {ability_guid[:8]}: invalid card cost")
            return
        from rules_port.resources import apply_resource_change
        resource_change = apply_resource_change(
            bstate, "player", "currentresource", -(cost + x_cost))
        if variable_x:
            bstate["x_cost"] = x_cost
        # Leaves read the resolved target from bstate; self-targeting manual
        # abilities (Living Totem, Ascetic Aspirant) default to the source.
        bstate["player_mod_target"] = target_uid if target_uid else source_uid
        bstate["player_transform_target"] = target_uid if target_uid else source_uid
        bstate["player_spell_target"] = target_uid
        bstate["resolving_ability"] = ability_guid
        bstate["resolving_source_uid"] = source_uid
        bstate["resolving_owner_id"] = self.user_profile["id"]
        bstate["player_shift_source"] = source_uid
        bstate["player_shift_target"] = target_uid
        # Bump the instance's usage of this ability (UsesPerGame/Turn limits).
        if upg or upt:
            db_record_card_ability_use(
                session.session_id, source_uid, ability_guid,
                bstate.get("turn_number", 1), conn=_db,
                uses_per_game=bool(upg), uses_per_turn=bool(upt))
        if graph.costs.cooldown:
            db_set_card_cooldown(
                session.session_id, source_uid, ability_guid,
                graph.costs.cooldown, conn=_db)
        _be.save_state(session, bstate)
        # Resolve the typed ability through RulesPort.
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        if cost_selections:
            self._apply_card_play_costs(
                game, session, bstate, pl_t, ai_t, cost_selections,
                expected_owner_id=self.user_profile["id"])
        from rules_port.resolution import resolve_port_ability
        log = resolve_port_ability(
            self, game, session, _db, pl_t, ai_t, bstate, ability_guid,
            source_uid=source_uid,
            owner_id=bstate.get("resolving_owner_id", 0),
            target_map=({0: int(target_uid)}
                        if target_uid is not None else {}))
        self._remove_one_shot_ability(
            session, source_uid, ability_guid, game, pl_t, ai_t, bstate)
        # Manual troop abilities resolve directly rather than as stack items.
        # Apply state-based effects here as well, so a stat reduction that
        # lowers a troop's effective defense to zero sends it to the crypt
        # before the player receives another priority window.
        if _be.stack_empty(bstate):
            from rules_port.context import EffectContext
            from rules_port.death_effects import state_based_deaths
            state_based_deaths(EffectContext.from_rules_port(
                game, session, _db, self, pl_t, ai_t, bstate,
                "state_based_death", ability=None))
        _be.save_state(session, bstate)
        # Exhaust-as-cost: tap the source card now that the ability resolved
        # (e.g. Prairie Scout's [ACT] ... — the tap is part of the cost).
        if exh:
            db_update_card_state(
                session.session_id, int(source_uid),
                set_bits=game_engine.ECardStates.Tapped, conn=_db)
            _db.commit()
            scid_src = game_engine.SessionCardId(game_engine.UID(int(source_uid)))
            source_details = db_card_zone_details(
                session.session_id, int(source_uid), conn=_db)
            _tpl, ct2, _n, _c, atk, def_, _g = self._card_full_data(
                game, scid_src, source_details[0] if source_details else None)
            current_state = db_card_state_value(
                session.session_id, int(source_uid), conn=_db)
            game.push_card_updated(scid_src, pl_t, game_engine.ECardCollections.Warzone,
                                   ct2, template_id=_tpl, attack=atk, defense=def_,
                                   state=int(current_state or game_engine.ECardStates.Tapped))
            # Exhausting an activated troop emits the data-defined tap event.
            # Granted triggers such as Spider Nest listen for this event.
            self._dispatch_native_trigger(
                game, session, pl_t, ai_t, bstate, "CardTappedEvent",
                int(source_uid), self.user_profile["id"])
        # Push resource change + player update + greenlight so the player keeps
        # priority after activating the ability.
        ec = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
        ec.player_id = pl_t
        ec.operation = 2
        ec.delta = cost + x_cost
        ec.new_value = bstate.get("player_resources", 0)
        game._push(ec)
        game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        # A discard leaf is a client-side class-23 follow-up, not an ordinary
        # main-phase target.  Push the picker after the draw/resource events
        # and hold the normal GreenLight/options until the selected card has
        # actually moved to discard.
        discard_prompted = False
        if self._ability_requires_discard(ability_guid):
            self._push_discard_prompt(
                game, session, pl_t, ai_t, bstate, ability_guid)
            discard_prompted = bool(bstate.get("pending_discard_ability"))
        if (not discard_prompted and
                not bstate.get("pending_choice") and
                not bstate.get("pending_deck_search") and
                not bstate.get("pending_trigger") and
                not bstate.get("pending_conversation")):
            game.push_green_light(pl_t, self._priority_context_for(
                _be.current_phase(bstate), bstate))
        if game.events:
            self._send_battle_events(session, game, pl_t)
        if variable_x:
            bstate.pop("x_cost", None)
            _be.save_state(session, bstate)
        log_req(f"    Troop ability {ability_guid[:8]} activated on {hex(source_uid)} "
                f"(cost {cost}+{x_cost}, target={hex(target_uid) if target_uid else 'none'}): {log}")

    def _ai_draw_card(self, game, session, ai_t, battle_state):
        import ai
        if battle_state.get("_rules_port_attached"):
            from rules_port.context import EffectContext
            battle_state.pop("_rules_port_deck_out_owner", None)
            context = EffectContext.from_rules_port(
                game, session, _db, self, game.player_uid, ai_t,
                battle_state, "", ability=None)
            context.draw(1, owner=0)
            from rules_port.persistence import save_state
            save_state(session, battle_state)
            return bool(battle_state.pop("_rules_port_deck_out_owner", None))
        return ai.ai_draw_card(self, game, session, ai_t, battle_state)

    def _move_deck_to_hand(self, game, session, pl_t, card_uid, tpl_guid, instance_id=None):
        """Move a specific card from the deck to the player's hand (pushes into
        `game`). Used by !addcard."""
        db_move_card_to_location(
            session.session_id, int(card_uid), "hand", position=100, conn=_db)
        _db.commit()
        scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
        tpl_guid, ct, name, cost, atk, def_, gem_type = self._card_full_data(
            game, scid, tpl_guid, instance_id)
        game.push_card_drawn(scid, pl_t, 1)
        game.push_card_updated(scid, pl_t, game_engine.ECardCollections.Hand, ct,
                               attack=atk, defense=def_, cost=cost,
                               template_id=tpl_guid, gems=gem_type)
        log_req(f"    Added card {card_uid} ({name}) from deck to hand")

    def _player_draw_card(self, game, session, pl_t, owner_id=None):
        """Draw the top card of the human's deck into hand (pushes into `game`)."""
        _be = self._checkpoint_engine(session)
        is_pvp = (session and (session.session_name or "").startswith("tourney-"))
        if is_pvp:
            from services.tournament_game import pvp_load_state, pvp_save_state
            bstate = pvp_load_state(session)
            if not bstate:
                log_req("    PvP debug draw ignored: no PvP state")
                return False
        else:
            bstate = _be.load_state(session)
        self._current_bstate = bstate
        # Practice/campaign uses the local profile id; tournament PvP uses the
        # ServicePlayer/reckoning pid stored in game_cards.  The latter cannot
        # be inferred from user_profile (it is a different identifier).
        if owner_id is None:
            if bstate.get("pvp"):
                owner_id = int(pl_t.uid64) >> 8
            else:
                owner_id = self.user_profile["id"]
        owner_id = int(owner_id)
        if bstate.get("pvp"):
            pids = [int(p) for p in (bstate.get("pids") or [])]
            opponent_id = next((p for p in pids if p != owner_id), None)
            ai_t = (game_engine.UID.make(244, opponent_id)
                    if opponent_id is not None else game_engine.UID.make(244, owner_id))
            draw_pl_t = game_engine.UID.make(244, owner_id)
        else:
            ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
            draw_pl_t = pl_t
        if bstate.get("_rules_port_attached"):
            from rules_port.context import EffectContext
            bstate.pop("_rules_port_deck_out_owner", None)
            context = EffectContext.from_rules_port(
                game, session, _db, self, draw_pl_t, ai_t, bstate,
                "", ability=None)
            context.draw(1, owner=owner_id)
            if is_pvp:
                from services.tournament_game import pvp_save_state
                pvp_save_state(session, bstate)
            else:
                _be.save_state(session, bstate)
            return bool(bstate.pop("_rules_port_deck_out_owner", None))
        turn = bstate.get("turn_number", 1)
        if bstate.get("player_draws_turn") != turn:
            bstate["player_draws_this_turn"] = 0
            bstate["player_draws_turn"] = turn
        bstate["player_draws_this_turn"] = int(bstate.get("player_draws_this_turn", 0)) + 1
        # Replacement: "If you would draw a card..." (The Transcended) and
        # "If this would enter a hand..." (Booby Trap) — a resolved trigger
        # replaces the draw with its own effect.
        repl_draw = self._dispatch_native_trigger(
            game, session, draw_pl_t, ai_t, bstate,
            "CardWouldBeDrawnEvent", None, owner_id)
        row = db_deck_top_card_details(
            session.session_id, owner_id, conn=_db)
        if not row:
            if is_pvp:
                log_req(f"    PvP debug draw: pid {owner_id} deck empty")
                return False
            # Deck-out: a player who must draw with an empty deck loses.
            self._finish_battle_gameend(
                session, False, winners=[ai_t], losers=[pl_t])
            log_req("    Game over: player deck empty on draw (AI wins)")
            return True
        repl_zone = self._dispatch_native_trigger(
            game, session, draw_pl_t, ai_t, bstate,
            "CardWouldEnterZoneEvent", row[1], owner_id)
        if repl_draw or repl_zone:
            if is_pvp:
                from services.tournament_game import pvp_save_state
                pvp_save_state(session, bstate)
            else:
                _be.save_state(session, bstate)
            log_req(f"    Player draw replaced (would-draw/would-enter trigger)")
            return
        scid = game_engine.SessionCardId(game_engine.UID(row[1]))
        db_move_card_row_to_hand(session.session_id, row[0], conn=_db)
        _db.commit()
        tpl_guid, ct, name, cost, atk, def_, gem_type = self._card_full_data(game, scid, row[3], row[2])
        game.push_card_drawn(scid, draw_pl_t, 1)
        game.push_card_updated(scid, draw_pl_t, game_engine.ECardCollections.Hand, ct,
                               attack=atk, defense=def_, cost=cost,
                               template_id=tpl_guid, gems=gem_type)
        # Entering a hand is its own authoritative zone event.  This is
        # distinct from CardDrawnEvent: cards such as Reginald trigger from
        # entering Hand even when the card was put there by an effect rather
        # than a normal draw.  The source owner is the actual deck owner, so
        # the trigger follows the card if control of the deck changed.
        self._dispatch_native_trigger(
            game, session, draw_pl_t, ai_t, bstate,
            "CardEnteredZoneEvent", row[1], owner_id,
            event_source_collection="deck",
            event_destination_collection="hand")
        # Fire "when you draw" triggers.  The client's CardDrawnEvent carries
        # SourceCardId = the drawing CHAMPION and TargetCardId = the drawn
        # card, and the trigger conditions test them per m_TriggerTest (e.g.
        # Twisted Fate's "when you draw a card, bury..." checks IsHero on the
        # source and IsType on the target).
        champ_uid = None
        if bstate.get("pvp"):
            for pid, cuid in (bstate.get("champ_map") or {}).items():
                if int(pid) == owner_id:
                    champ_uid = int(cuid)
                    break
        else:
            champ_scid = (getattr(self, "_player_champ_scid", None)
                          if owner_id else getattr(self, "_ai_champ_scid", None))
            champ_uid = (int(champ_scid.uid.uid64)
                         if champ_scid is not None else None)
        self._dispatch_native_trigger(
            game, session, draw_pl_t, ai_t, bstate,
            "CardDrawnEvent", champ_uid, owner_id,
            target_card_id=row[1])
        if is_pvp:
            from services.tournament_game import pvp_save_state
            pvp_save_state(session, bstate)
        else:
            _be.save_state(session, bstate)
        log_req(f"    Player drew card {row[1]} ({name})")

    def _rules_port_deck_out(self, game, session, owner_id, pl_t, ai_t,
                             battle_state):
        """Publish the mode-specific result for a native deck-out rule."""
        owner_id = int(owner_id or 0)
        if battle_state.get("pvp"):
            from services.tournament_game import _pvp_end_game
            pids = [int(pid) for pid in (battle_state.get("pids") or ())]
            if len(pids) >= 2:
                loser = owner_id
                winner = next((pid for pid in pids if pid != loser), None)
                if winner is not None:
                    _pvp_end_game(session, battle_state, winner, loser,
                                  reason="deck-out")
            return True
        if owner_id:
            self._finish_battle_gameend(
                session, False, winners=[ai_t], losers=[pl_t])
            log_req("    Game over: player deck empty on native draw")
        else:
            import commands as _cmd
            _cmd.push_battle_game_end(
                handler=self, session=session,
                winners=[pl_t], losers=[ai_t])
            if hasattr(self, "_campaign_gameend"):
                self._campaign_gameend(session, won=True)
            log_req("    Game over: AI deck empty on native draw")
        return True

    def _ai_play_resource(self, game, session, ai_t, battle_state):
        import ai
        return ai.ai_play_resource(self, game, session, ai_t, battle_state)

    def _ai_play_troop(self, game, session, ai_t, battle_state):
        import ai
        return ai.ai_play_troop(self, game, session, ai_t, battle_state)

    def _resolve_ai_mulligan(self, session, game, ai_t):
        import ai
        return ai.resolve_ai_mulligan(self, session, game, ai_t)

    def _card_type_of(self, card_template_id):
        """Return the card_templates.card_type for a card_template_id."""
        s = str(card_template_id)
        try:
            if "-" in s:
                template = db_template_by_guid(s)
                return template[1] if template else None
            row = db_card_instance_display(
                self.user_profile["id"] if self.user_profile else 0,
                int(s), conn=_db)
            return row[1] if row else None
        except Exception:
            return None

    def handle(self):
        log(f"Connection from {self.addr}")
        try:
            while True:
                data = self.conn.recv(4096)
                if not data:
                    log(f"Client disconnected {self.addr}")
                    break
                self.buf += data
                while True:
                    try:
                        headers, body, consumed = parse_packet(self.buf)
                    except ValueError:
                        break
                    self.buf = self.buf[consumed:]
                    self.handle_message(headers, body)
        except ConnectionResetError:
            log(f"Connection reset {self.addr}")
        except Exception as e:
            import traceback
            log(f"Error {self.addr}: {e}\n{traceback.format_exc()}")
            # If a legacy helper left an uncommitted write transaction open on
            # the shared connection, roll it back to avoid pinning the database lock.
            try:
                if getattr(_db, "in_transaction", False):
                    log(f"Handler error left transaction active on shared DB; rolling back: {self.addr}")
                    _db.rollback()
            except Exception as rollback_error:
                log(f"Rollback after handler error failed: {rollback_error}")
        finally:
            self._handle_disconnect()
            self.conn.close()

    def _handle_disconnect(self):
        """Notify peers and remove this handler from all live registries."""
        if getattr(self, "_disconnect_handled", False):
            return
        self._disconnect_handled = True

        try:
            disconnected_pid = int(getattr(self, "client_reck_id", 0) or 0)
            if disconnected_pid:
                from services.tournament_game import notify_pvp_player_disconnected
                with player_handler_lock:
                    is_current_handler = (
                        player_handlers.get(disconnected_pid) is self)
                if is_current_handler:
                    notify_pvp_player_disconnected(disconnected_pid, self)
                    cleared = db_tournament_clear_player_searching(
                        disconnected_pid, conn=_db)
                    if cleared:
                        _db.commit()
                        log_req(f"    Cleared {cleared} tournament search(es) "
                                f"for disconnected player {disconnected_pid}")
                    with player_handler_lock:
                        if player_handlers.get(disconnected_pid) is self:
                            del player_handlers[disconnected_pid]
                else:
                    log_req(f"    Ignoring disconnect from stale handler for "
                            f"pid {disconnected_pid}")
        except Exception as exc:
            log_req(f"    PvP disconnect notification failed: {exc}")

        try:
            from services.chat import notify_chat_player_disconnected
            notify_chat_player_disconnected(self)
        except Exception as exc:
            log_req(f"    Chat disconnect notification failed: {exc}")

        # Remove this handler before the social offline broadcast so remaining
        # sessions are evaluated accurately.
        with _active_clients_lock:
            for uid in list(_active_clients.keys()):
                _active_clients[uid] = [
                    (h, t) for h, t in _active_clients[uid] if h is not self
                ]
                if not _active_clients[uid]:
                    del _active_clients[uid]
        if getattr(self, "user_profile", None):
            try:
                from services.social import broadcast_friend_offline
                broadcast_friend_offline(self, SERVICE_PROFILE_UID)
            except Exception:
                pass

    def handle_message(self, headers: dict, body: bytes):
        # Restore the handler's session context for every inbound packet.
        # ContextVar state is local to the worker thread, while the handler
        # may have returned from a service callback since the last packet.
        set_log_context(
            player_id=getattr(self, "_log_player_id", None),
            session_id=getattr(self, "_log_session_id", None),
            participant_ids=getattr(self, "_log_session_players", ()))
        target = headers.get("target", "")
        issuer = headers.get("issuer", "")
        instance = headers.get("instance", "")
        reqid = headers.get("reqid", 0)
        conh = headers.get("conh", 0)

        client_ccnt = headers.get("ccnt", self.ccnt)
        client_scnt = headers.get("scnt", self.scnt)
        if isinstance(client_ccnt, int) and client_ccnt > self.ccnt:
            self.ccnt = client_ccnt

        if instance != "ping":
            log_req(
                f"=== RECV === target={target} instance={instance} reqid={reqid} "
                f"conh={conh} ccnt={client_ccnt} scnt={client_scnt}",
                level="DEBUG")
            header_summary = {
                k: str(v)[:80] for k, v in headers.items()}
            log_req(
                f"  issuer={issuer} body_len={len(body)} "
                f"hdrs={header_summary}",
                level="DEBUG")

        # Try to dump body content
        if body:
            if instance == "auth:req":
                log_req(f"  body_json={body.decode('utf-8', errors='replace')}")
            else:
                log_req(f"  body_hex={hexdump(body)}", level="DEBUG")

        # Session creation
        if target == "newsession":
            self.sid = f"hcp-{time.time_ns()}"
            log_req(f">>> New session sid={self.sid}")
            self.scnt += 1
            self.send({"issuer": "Session", "target": "create", "sid": self.sid})

        # Auth
        elif instance == "auth:req":
            auth_data = json.loads(body.decode("utf-8"))
            username = auth_data.get("user", "TestPlayer")
            log_req(f">>> Auth: user={username}")

            # The auth proxy returns its login token in the /steam/login
            # response, and the client forwards it back to us verbatim.  We
            # use a "steam:"-prefixed token to carry the Steam ID so the
            # Steam account becomes the authoritative player key.
            token = auth_data.get("token") or ""
            steam_id = None
            if isinstance(token, str) and token.startswith("steam:"):
                steam_id = token[len("steam:"):]

            # Load or create user in DB
            profile = db_get_or_create_user(username, steam_id=steam_id)
            self.user_profile = profile
            # Derive client IDs from the hashed player ID. These must stay
            # within uint64 (the client parses SAuthID/SReckID as ulong) and
            # be stable across sessions, so fold the hash down to 48 bits and
            # keep them distinct (auth=+0, reck=+1 offsets).
            self._set_client_identity_from_profile(profile)
            # Bind the stable profile log before profile-stream responses so
            # authentication-time errors after this point are player-scoped.
            self._bind_log_session(None)

            # Read permission flags set by the auth proxy (Admin/Mod/Founder).
            flags = profile.get("flags", "{}")
            if isinstance(flags, str):
                try:
                    flags = json.loads(flags)
                except (ValueError, TypeError):
                    flags = {}
            is_admin = flags.get("admin") == "true"
            is_mod = flags.get("mod") == "true" or is_admin
            is_founder = flags.get("founder") == "true"

            auth_resp = {
                "Action": "Login",
                "Success": True,
                "SAuthID": self.client_auth_id,
                "SReckID": self.client_reck_id,
                # Display name without the hidden #discriminator suffix.
                "UserName": display_name_from_identity(username),
                "Admin": is_admin,
                "Moderator": is_mod,
                "Founder": is_founder,
                "ErrorMsg": "",
                "UserID": profile["id"],
            }
            self.authenticated = True
            self.client_uid = make_uid(UID_TYPE["ServicePlayer"], int(self.client_reck_id))
            # Register in active clients for chat broadcasting
            uid = profile["id"]
            with _active_clients_lock:
                if uid not in _active_clients:
                    _active_clients[uid] = []
                _active_clients[uid].append((self, time.time()))
            self.scnt += 1
            self.send(
                {"issuer": "Session", "target": "auth:res", "sid": self.sid},
                json.dumps(auth_resp, separators=(",", ":")).encode("utf-8"),
            )

            self.push_profile_stream()
            pending_tid = int(getattr(
                self, "_pending_tournament_reconnect", 0) or 0)
            if pending_tid:
                self._pending_tournament_reconnect = 0
                resume_corinth_deck_construction(self, pending_tid)

        # Ping/pong
        elif instance == "ping":
            self.scnt += 1
            self.send({"issuer": "Session", "target": "pong", "sid": self.sid})
        elif instance == "ping" and target == "ping":
            self.scnt += 1
            self.send({"issuer": "Session", "target": "pong", "sid": self.sid})

        elif target == "ping" and not instance:
            self.scnt += 1
            self.send({"issuer": "Session", "target": "pong", "sid": self.sid})

        elif target == "Session" and (instance == "chat" or instance == "battle"):
            from services.chat import handle_chat_message
            self._refresh_log_session_context()
            handle_chat_message(self, body)

        # === ROUTED SERVICE MESSAGES ===
        elif instance and instance != "ping":
            log_req(f">>> Routed msg target={target} instance={instance}",
                    level="DEBUG")

            # On first routed message after login, push inventory and existing cards
            if self._inventory_pending:
                self._inventory_pending = False
                if self.user_profile:
                    purchased = db_get_inventory(self.user_profile["id"])
                    for tguid, qty in purchased:
                        # Use stored client UID if available, else generate one
                        inventory_row = db_inventory_item(
                            self.user_profile["id"], tguid, conn=_db)
                        item_id = (inventory_row[2] if inventory_row and
                                   inventory_row[2] else
                                   db_next_inventory_client_uid(
                                       self.user_profile["id"], conn=_db))
                        self.push_inventory_to_client(qty=qty, template_guid=tguid, item_id=item_id)
                        if not inventory_row or not inventory_row[2]:
                            db_set_inventory_client_uid(
                                self.user_profile["id"], tguid, item_id,
                                conn=_db)
                    _db.commit()
                    log_req(f">>> Pushed {len(purchased)} inventory items on first request")
                    # Existing cards are not re-announced here: the login push
                    # already delivers the collection, and a CardsAdded event
                    # for a copy already in a deck makes the client re-template
                    # that deck card, which drops its socketed gems.

            # On first routed message, push social data (friend list etc.)
            if getattr(self, '_social_pending', False) and self.user_profile:
                self._social_pending = False
                log_req(f"    Social push triggered: _active_clients={list(get_active_clients_snapshot().keys())}")
                try:
                    from services.social import push_all_social_data
                    push_all_social_data(self, SERVICE_PROFILE_UID)
                except Exception as e:
                    log_req(f"    Social push failed: {e}")
                try:
                    from services.social import broadcast_friend_online
                    broadcast_friend_online(self, SERVICE_PROFILE_UID)
                except Exception as e:
                    log_req(f"    Friend online broadcast failed: {e}")

            dw = None
            try:
                dw = parse_datawrapper(body, preserve_complex=True)
                log_req(
                    f"    DW: dt={dw.get('DataType')} comp={dw.get('Comp')} "
                    f"sess={dw.get('RequestHandlerSessionId')}",
                    level="DEBUG")
            except Exception as e:
                import traceback
                log_req(f"    Failed to parse DataWrapper: {e}")
                log_req(f"    Body hex: {hexdump(body)}")
                log_req(traceback.format_exc())
                return

            orig_reqid = dw.get("RequestId", 0)
            data_type = dw.get("DataType", 0)
            comp = dw.get("Comp", 0)
            session_id = dw.get("RequestHandlerSessionId", "00000000-0000-0000-0000-000000000000")
            if session_id and session_id != "00000000-0000-0000-0000-000000000000":
                self.client_req_session_id = session_id
            raw_bytes = dw.get("Bytes", b"")

            inner_bytes = raw_bytes
            if comp == 1 and raw_bytes:
                try:
                    inner_bytes = decompress_gzip(raw_bytes)
                    log_req(
                        f"    Decompressed: {len(raw_bytes)}b -> {len(inner_bytes)}b",
                        level="DEBUG")
                except Exception as e:
                    log_req(f"    Decompress failed: {e}")
                    log_req(f"    raw[:60]={hexdump(raw_bytes[:60])}")
                    return
            # Downstream classifiers and the typed-UID decoder treat the
            # payload as ``bytes``; normalize buffer-like results so a
            # bytearray/memoryview cannot silently decode as an empty
            # transaction (which previously made choice answers look unselected).
            if isinstance(inner_bytes, (bytearray, memoryview)):
                inner_bytes = bytes(inner_bytes)

            inner_obj = {}
            inner_type = "?"
            try:
                if inner_bytes:
                    inner_obj = parse_datawrapper(
                        inner_bytes, preserve_complex=True)
                    # Keep the exact typed ObjFmt envelope available to the
                    # RulesPort ingress adapter.  The generic decoder may
                    # intentionally omit nested TargetMap/ActivationData;
                    # the adapter still needs the labelled wire fields to
                    # construct a continuation payload.
                    if isinstance(inner_obj, dict):
                        inner_obj.setdefault("__raw__", inner_bytes)
                    inner_type = inner_obj.get("__type__", "?")
                    inner_fields = {
                        k: str(v)[:80]
                        for k, v in inner_obj.items()
                        if k != "__type__"}
                    log_req(
                        f"    Inner: type={inner_type} fields={inner_fields}",
                        level="DEBUG")
                    log_req(f"    Inner raw: {hexdump(inner_bytes)}",
                            level="DEBUG")
            except Exception as e:
                log_req(f"    Inner parse: {e}")
                log_req(f"    Inner hex: {hexdump(inner_bytes, 120)}")
                inner_obj = {"__raw__": inner_bytes}

            self.handle_service_request(target, instance, data_type, orig_reqid,
                                        comp, session_id, conh, inner_obj,
                                        inner_bytes, body)

        else:
            log(f">>> Unknown: target={target} instance={instance}")


    def handle_service_request(self, target, instance, data_type, reqid,
                                comp, session_id, conh, inner_obj, inner_bytes,
                                raw_data_wrapper_bytes=b""):
        """Decode the protocol request into an application command envelope."""
        self._refresh_log_session_context()
        command = ServiceRequestCommand(
            target=target,
            instance=instance,
            data_type=data_type,
            request_id=reqid,
            compressed=comp,
            session_id=session_id,
            connection_handle=conh,
            inner_object=inner_obj,
            inner_bytes=inner_bytes,
            raw_data_wrapper_bytes=raw_data_wrapper_bytes,
        )
        # 22025 is ReadyToContinueGame.  The reconnect client sends it with
        # no response callback, so it must reach the PvP handler as a normal
        # fire-and-forget request.  Only 3027 is handled at the application
        # boundary as an end-session mutation.
        if data_type == 3027:
            return self._application.dispatch_request(
                command, self._handle_session_application_request)
        if data_type == 3029:
            with session_transaction_trace(reqid):
                return self._application.dispatch_request(
                    command,
                    lambda request: self._handle_service_request_legacy(
                        request.target, request.instance, request.data_type,
                        request.request_id, request.compressed,
                        request.session_id, request.connection_handle,
                        request.inner_object, request.inner_bytes,
                        request.raw_data_wrapper_bytes),
                )
        return self._application.dispatch_request(
            command,
            lambda request: self._handle_service_request_legacy(
                request.target, request.instance, request.data_type,
                request.request_id, request.compressed, request.session_id,
                request.connection_handle, request.inner_object,
                request.inner_bytes, request.raw_data_wrapper_bytes),
        )

    def _handle_session_application_request(self, command):
        """Handle session removal at the application boundary."""
        player_uid = make_uid(UID_TYPE["ServicePlayer"], int(self.client_reck_id))
        reason = "leave" if command.data_type == 22025 else "end"
        result = self._application.execute(RemoveSessionCommand(
            player_uid=player_uid,
            reason=reason,
        ))
        if result.value:
            log_req(f"    {reason.title()}ing session {result.value}")

        if command.data_type != 22025:
            return result

        # Response encoding remains a protocol concern, but it happens only
        # after the application transaction and committed event complete.
        resp_inner = encode_objfmt_response(
            ["Game.Shared.Network.LoadBalancer.LeaveSessionResponseArgs",
             "System.Boolean"],
            [("Success", "bool", True)]
        )
        resp_body = (compress_gzip(resp_inner)
                     if command.compressed else resp_inner)
        resp_reqid = command.request_id | 1
        dw_bytes = encode_datawrapper(
            resp_reqid, command.data_type, resp_body, command.compressed,
            command.session_id)
        issuer_str = (
            f"0.0.0.0.ServiceLoadBalancer.{SERVICE_MAIL_UID}."
            f"ServicePlayer.{self.client_uid}.{resp_reqid}")
        self.scnt += 1
        self.send({
            "issuer": issuer_str,
            "target": command.target,
            "instance": command.instance,
            "reqid": resp_reqid,
            "c": command.compressed,
            "conh": command.connection_handle,
            "sid": self.sid,
        }, dw_bytes)
        log_req(f"    Sent LeaveSession response ({len(dw_bytes)}b)")
        return result

    def _push_rules_port_chain_priority_sync(self, session, port):
        """Restore native chain priority after the client requests a resync.

        The legacy checkpoint can still describe a main phase while RulesPort
        owns a pending chain item. Replaying its ordinary phase GreenLight
        replaces the client's ResolveTopOfChain button context.
        """
        chain = getattr(port, "chain", None)
        if chain is None or chain.is_empty:
            return None

        priority = getattr(
            getattr(port, "action_stack", None), "priority_player_id", None)
        if priority is None:
            log_req("    RequestPrioritySync: native chain has no priority owner")
            return False

        if (session.session_name or "").startswith("tourney-"):
            from services.tournament_game import (
                _pvp_populate_game_state, _send_pvp_packet,
                player_handlers, pvp_load_state,
            )
            state = pvp_load_state(session) or {}
            pids = [int(value) for value in (state.get("pids") or ())]
            my_pid = int(self.client_reck_id)
            if len(pids) != 2 or my_pid not in pids:
                log_req(
                    "    RequestPrioritySync: no PvP participants for native chain")
                return False
            priority_pid = next((pid for pid in pids if _same_uid(
                priority, game_engine.UID.make(244, pid))), None)
            if priority_pid is None:
                log_req(
                    "    RequestPrioritySync: unknown PvP native chain owner")
                return False

            other_pid = next(pid for pid in pids if pid != my_pid)
            target_uid = game_engine.UID.make(244, my_pid)
            other_uid = game_engine.UID.make(244, other_pid)
            priority_uid = game_engine.UID.make(244, priority_pid)
            turn_pid = int(state.get("turn_pid") or priority_pid)
            turn_uid = game_engine.UID.make(244, turn_pid)
            phase = port.current_turn_phase
            window = game_engine.Game(
                int(session.session_id), target_uid, other_uid)
            _pvp_populate_game_state(window, state, my_pid, other_pid)
            window.push_green_light(
                priority_uid,
                game_engine.EPriorityContext.ResolveTopOfChain)
            window.push_turn_phase(phase, turn_uid, priority_uid)
            target_handler = player_handlers.get(my_pid) or self
            _send_pvp_packet(
                target_handler, session, window, target_uid,
                "rules-port-chain-priority-sync")
            log_req(
                "    RequestPrioritySync: restored PvP native chain window "
                f"priority={priority_pid} phase={phase}")
            return True

        player_uid = game_engine.UID.make(244, int(self.client_reck_id))
        ai_uid = game_engine.UID.make(
            game_engine.SERVICE_AI_UID_TYPE, 1000)
        priority_uid = (player_uid if _same_uid(priority, player_uid)
                        else ai_uid if _same_uid(priority, ai_uid) else None)
        if priority_uid is None:
            log_req(
                "    RequestPrioritySync: unknown Practice native chain owner")
            return False
        checkpoint = self._checkpoint_engine(session).load_state(session)
        self._current_bstate = checkpoint
        game = self._fresh_game(session, player_uid, ai_uid, checkpoint)
        active_owner = getattr(port, "active_player_id", None)
        active_uid = (player_uid if _same_uid(active_owner, player_uid)
                      else ai_uid if _same_uid(active_owner, ai_uid)
                      else player_uid)
        if _same_uid(priority_uid, player_uid):
            # The human is the response player; restore the legal quick actions
            # and champion powers alongside the ResolveTopOfChain button.
            self._push_phase_options_empty(session, player_uid, ai_uid)
        game.push_green_light(
            priority_uid, game_engine.EPriorityContext.ResolveTopOfChain)
        game.push_turn_phase(port.current_turn_phase, active_uid, priority_uid)
        self._send_battle_events(session, game, player_uid)
        log_req(
            "    RequestPrioritySync: restored Practice native chain window "
            f"priority={priority_uid!r} phase={port.current_turn_phase}")
        return True

    def _handle_priority_sync_transaction(self, session, transaction):
        """Reconcile a client's lost priority state without changing game state."""
        if not session:
            return False

        port = getattr(session, "_rules_port_session", None)
        if port is not None:
            chain_sync = self._push_rules_port_chain_priority_sync(
                session, port)
            if chain_sync is not None:
                if not chain_sync:
                    self._push_transaction_ack(session)
                return True

        # PvP tournament path.
        if (session.session_name or "").startswith("tourney-"):
            from services.tournament_game import pvp_load_state
            pvp_state = pvp_load_state(session)
            if pvp_state:
                my_pid = int(self.client_reck_id)
                my_uid = game_engine.UID.make(244, my_pid)
                pvp_pids = [int(k) for k in (pvp_state.get("pids") or [])
                            if int(k) != my_pid]
                opp_pid = pvp_pids[0] if pvp_pids else my_pid
                opp_uid = game_engine.UID.make(244, opp_pid)
                game_ps = game_engine.Game(int(session.session_id), my_uid, opp_uid)
                phase = pvp_state.get("phase", 3)
                turn_uid = game_engine.UID.make(244, pvp_state["turn_pid"])
                if phase == game_engine.ETurnPhases.Mulligan:
                    if pvp_state.get("mulligan_pid") == my_pid:
                        game_ps.push_green_light(
                            my_uid, game_engine.EPriorityContext.Normal)
                        pkt_ps = game_ps.make_network_packet(my_uid)
                        dw_ps = encode_datawrapper(
                            0, 3055,
                            compress_gzip(encode_sync_event(pkt_ps)), 1,
                            client_session_guid(self))
                        self.scnt += 1
                        self.send({
                            "issuer": f"0.0.0.0.ServiceGameSession.246."
                                      f"{session.session_id}.{self.scnt}",
                            "target": "ServiceGameSession",
                            "instance": str(session.server_id),
                            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                        }, dw_ps)
                        log_req(f"    RequestPrioritySync (PvP): Mulligan greenlight to actor {my_pid}")
                    else:
                        self._push_transaction_ack(session)
                        log_req(f"    RequestPrioritySync (PvP): Mulligan — {my_pid} not the actor, acked")
                elif phase == game_engine.ETurnPhases.PickGoesFirst:
                    winner_pid = pvp_state.get(
                        "goes_first_pid", pvp_state["turn_pid"])
                    if my_pid == winner_pid:
                        winner_uid = game_engine.UID.make(244, winner_pid)
                        game_ps.push_turn_phase(
                            game_engine.ETurnPhases.PickGoesFirst,
                            winner_uid, winner_uid)
                        game_ps.push_green_light(
                            my_uid, game_engine.EPriorityContext.Normal)
                        pkt_ps = game_ps.make_network_packet(my_uid)
                        dw_ps = encode_datawrapper(
                            0, 3055,
                            compress_gzip(encode_sync_event(pkt_ps)), 1,
                            client_session_guid(self))
                        self.scnt += 1
                        self.send({
                            "issuer": f"0.0.0.0.ServiceGameSession.246."
                                      f"{session.session_id}.{self.scnt}",
                            "target": "ServiceGameSession",
                            "instance": str(session.server_id),
                            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                        }, dw_ps)
                        log_req(f"    RequestPrioritySync (PvP): PickGoesFirst greenlight to winner {my_pid}")
                    else:
                        self._push_transaction_ack(session)
                        log_req(f"    RequestPrioritySync (PvP): PickGoesFirst — {my_pid} is not the winner, acked")
                else:
                    game_ps.push_turn_phase(phase, turn_uid, my_uid)
                    game_ps.push_green_light(
                        my_uid, game_engine.EPriorityContext.Normal)
                    pkt_ps = game_ps.make_network_packet(my_uid)
                    dw_ps = encode_datawrapper(
                        0, 3055,
                        compress_gzip(encode_sync_event(pkt_ps)), 1,
                        client_session_guid(self))
                    self.scnt += 1
                    self.send({
                        "issuer": f"0.0.0.0.ServiceGameSession.246."
                                  f"{session.session_id}.{self.scnt}",
                        "target": "ServiceGameSession",
                        "instance": str(session.server_id),
                        "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                    }, dw_ps)
                    log_req(f"    RequestPrioritySync (PvP): re-sent GreenLight for phase {phase}")
            return True

        _be = self._checkpoint_engine(session)
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        try:
            bstate = _be.load_state(session)
            self._current_bstate = bstate
            phase = _be.current_phase(bstate)
        except Exception:
            bstate, phase = None, None
        if bstate and phase is not None and bstate.get("turn_player") == _be.PLAYER:
            game = self._fresh_game(session, pl_t, ai_t, bstate)
            game.push_green_light(pl_t, self._priority_context_for(phase, bstate))
            game.push_player_updated(
                pl_t, champ_id=getattr(self, "_player_champ_scid", None))
            game.push_player_updated(
                ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
            self._send_battle_events(session, game, pl_t)
            if phase == game_engine.ETurnPhases.DeclareAttack:
                self._push_attack_options(session, pl_t, ai_t)
            elif phase in (game_engine.ETurnPhases.FirstMainPhase,
                           game_engine.ETurnPhases.SecondMainPhase):
                self._push_main_phase_options(session, pl_t, ai_t)
            else:
                self._push_phase_options_empty(session, pl_t, ai_t)
            log_req(f"    RequestPrioritySync: re-sent GreenLight for phase {phase}")
        elif (bstate and phase is not None
              and bstate.get("turn_player") == _be.AI
              and bstate.get("ai_turn_phase_idx") is not None):
            # During the AI's turn there is no human-side battle state in the
            # sense used by the old player-turn branch above: the AI drives the
            # phase loop server-side and only records ``ai_turn_phase_idx``
            # while waiting for the human at an opponent stop.  If the client
            # loses its local priority bit (for example after combat damage),
            # LocalPlayer requests a priority resync.  A bare 3055 ack leaves
            # the client with no Pass window and strands the AI turn.
            # Re-announce the held phase with the human as priority, then send
            # the same context/options contract as the normal AI-stop path.
            held = _be.ai_held_phase_context(bstate)
            if held is not None:
                held_phase, held_idx, held_phases = held
                # The resume cursor is written at the stop boundary and is
                # safer than a stale phase_idx left by a competing packet.
                bstate["turn_phases"] = list(held_phases)
                bstate["phase_idx"] = held_idx
                _be.save_state(session, bstate)
                phase = held_phase
            game = self._fresh_game(session, pl_t, ai_t, bstate)
            game.push_turn_phase(phase, ai_t, pl_t)
            context = (game_engine.EPriorityContext.Normal
                       if phase == game_engine.ETurnPhases.DeclareDefense
                       else game_engine.EPriorityContext.ResolveTopOfChain)
            game.push_green_light(pl_t, context)
            self._send_battle_events(session, game, pl_t)
            if phase == game_engine.ETurnPhases.DeclareDefense:
                self._push_blocker_options(session, pl_t, ai_t)
            else:
                self._push_phase_options_empty(session, pl_t, ai_t)
            log_req(f"    RequestPrioritySync: re-sent AI-stop GreenLight "
                    f"for phase {phase}")
        else:
            self._push_transaction_ack(session)
            log_req("    RequestPrioritySync: no active battle, acked")
        return True

    def _handle_choose_pick_transaction(self, session, transaction):
        """Resolve Play/Draw selection and enter the Mulligan phase."""
        inner_bytes = transaction.inner_bytes
        is_set_ability_data = transaction.is_set_ability_data
        is_ability_activate = transaction.is_ability_activate
        # PvP: chose Play or Draw — draw hands, then push Mulligan.
        if ((session.session_name or "").startswith("tourney-")
                and not is_set_ability_data
                and not is_ability_activate):
            from services.tournament_game import (pvp_load_state, pvp_save_state,
                                                  db_game_session_pids, _push_to_both_players)
            state = pvp_load_state(session)
            my_pid = int(self.client_reck_id)
            chose_draw = b"ChooseDrawTransaction" in inner_bytes
            pids = db_game_session_pids(session.session_id)
            if state and pids:
                winner_pid = state["goes_first_pid"]
                loser_pid = next(p for p in pids if p != winner_pid)
                # ONLY the coin-flip winner decides who plays/draws
                # first.  The loser's client never receives the
                # PickGoesFirst GreenLight (only the winner is the
                # active player), so it never shows the Play/Draw
                # dialog and never submits a choose transaction.
                # Requiring a pick from BOTH players therefore stalls
                # forever after the winner chooses ("chose to go first,
                # then nothing happened").  Ignore any non-winner pick
                # and resolve on the winner's first choose.
                if my_pid != winner_pid:
                    log_req(f"    PvP choose: {my_pid} (non-winner) "
                            f"pick ignored — waiting on winner "
                            f"{winner_pid}")
                    self._push_transaction_ack(session)
                    return True
                if state.get("pick_resolved"):
                    # Already resolved — this is a stale/re-sent choose
                    # from the winner; just ack to avoid double-drawing.
                    log_req(f"    PvP choose: already resolved "
                            f"(draws_first {state['draws_first_pid']}) "
                            f"— stale pick from {my_pid}")
                    self._push_transaction_ack(session)
                    return True
                # The winner picks Play -> winner plays first and the
                # loser draws first; Draw -> winner draws first and the
                # loser plays first.  The player who draws first goes
                # SECOND (the client reorders its player list so the
                # draws-first player is last / the other is "starting"),
                # so the actual first turn belongs to play_first_pid.
                from services.tournament_game import pvp_session_lock
                with pvp_session_lock(session):
                    draw_first_pid = winner_pid if chose_draw else loser_pid
                    play_first_pid = loser_pid if chose_draw else winner_pid
                    state["draws_first_pid"] = draw_first_pid
                    state["play_first_pid"] = play_first_pid
                    # The first turn goes to the play-first player, NOT the
                    # coin-flip winner — if the winner picked Draw, the
                    # loser opens the game.
                    state["turn_pid"] = play_first_pid
                    state["pids"] = [play_first_pid, draw_first_pid]
                    state["pick_resolved"] = True
                    # Advance the persisted PvP phase from PickGoesFirst(3)
                    # to Mulligan(4) BEFORE any events go out. Otherwise a
                    # client priority sync during the handoff can re-push
                    # PickGoesFirst and restore the wrong setup UI.
                    state["phase"] = 4
                    # Mulligan is an ACTIVE-player phase in C#. The player
                    # who plays first gets the first decision; the coin
                    # winner may have chosen Draw and therefore be second.
                    state["mulligan_pid"] = play_first_pid
                    state["mulligan_count"] = {}
                    pvp_save_state(session, state)

                    # Corinth starts with four shards; ordinary PvP starts
                    # with the constructed-game hand size.
                    opening_hand_size = int(
                        (getattr(session, "encounter_data", {}) or {}).get(
                            "starting_hand_size", 7) or 7)
                    for apid in pids:
                        db_game_draw_cards(session.session_id, apid,
                                           opening_hand_size)
                    log_req(f"    PvP choose: drew hands for both "
                            f"({opening_hand_size})")

                # Push hand + Mulligan to each player.
                hands = {
                    int(apid): db_game_get_hand(session.session_id, apid)
                    for apid in pids
                }
                hand_uids = [int(card_uid)
                             for hand in hands.values()
                             for card_uid, _template_guid in hand]
                hand_projections = db_card_instance_dynamic_projections(
                    session.session_id, hand_uids, conn=_db)
                for apid in pids:
                    h = player_handlers.get(apid)
                    if not h: continue
                    aopp = pids[0] if pids[1] == apid else pids[1]
                    pl = game_engine.UID.make(244, apid)
                    op = game_engine.UID.make(244, aopp)
                    g = game_engine.Game(int(session.session_id), pl, op)
                    for idx, (cu, tg) in enumerate(hands[int(apid)]):
                        scid = game_engine.SessionCardId(game_engine.UID(int(cu)))
                        # Populate the CardDef (cost/atk/def/thresholds/
                        # abilities/gems) so the client renders the hand
                        # and the Mulligan keep/redraw dialog correctly.
                        _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                            self._card_full_data(
                                g, scid, tg,
                                instance_projection=hand_projections.get(int(cu)))
                        g.push_card_updated(scid, pl, game_engine.ECardCollections.Hand,
                                            ct, template_id=tg, gems=gem_type)
                    for idx, (cu, tg) in enumerate(hands[int(aopp)]):
                        scid = game_engine.SessionCardId(game_engine.UID(int(cu)))
                        _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                            self._card_full_data(
                                g, scid, tg,
                                instance_projection=hand_projections.get(int(cu)))
                        g.push_card_updated(scid, op, game_engine.ECardCollections.Hand, ct,
                                            template_id=tg, nulling=True, gems=gem_type)
                    # Tell each client who draws first and who plays
                    # first.  Order matters: the Draw event first, then
                    # the Play event — the client derives the mulligan
                    # dialog line ("Your opponent chose to Play/Draw")
                    # from the LAST event that names the OPPONENT; each
                    # player's OWN pick resets it to Unknown.  With
                    # Draw-then-Play the picker's own client shows no
                    # opponent message and the opponent's client shows
                    # the correct "chose to Play/Draw".
                    g.push_player_wishes_to_draw_first(game_engine.UID.make(244, draw_first_pid))
                    g.push_player_wishes_to_play_first(game_engine.UID.make(244, play_first_pid))
                    # MulliganState uses the same active-player contract as
                    # C#: only play_first_pid starts with the dialog and
                    # priority. Both clients receive the same global phase
                    # owner; the waiting player does not get a temporary
                    # local priority that would open a second dialog.
                    mulligan_uid = game_engine.UID.make(
                        244, play_first_pid)
                    g.push_disable_interface(apid != play_first_pid)
                    g.push_green_light(
                        mulligan_uid, game_engine.EPriorityContext.Normal)
                    g.push_turn_phase(game_engine.ETurnPhases.Mulligan,
                                      mulligan_uid, mulligan_uid)
                    pkt = g.make_network_packet(pl)
                    dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                                            "00000000-0000-0000-0000-000000000000")
                    h.scnt += 1
                    h.send({"issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{h.scnt}",
                            "target": "ServiceGameSession", "instance": str(session.server_id),
                            "reqid": 0, "c": 0, "conh": 0, "sid": h.sid}, dw)
                    log_req(f"    PvP choose: hand to pid {apid}")
            else:
                _push_to_both_players(session, self, lambda g, pt:
                    [g.push_turn_phase(game_engine.ETurnPhases.Mulligan, pt, pt),
                     g.push_green_light(pt, game_engine.EPriorityContext.Normal)], log_req)
            return True
        else:
            # The coin-flip winner chose Play or Draw. FRA can resolve this
            # branch for the AI as well as accepting the player's transaction;
            # the latter is only legal when the player won the toss.
            ai_choice = bool(getattr(transaction, "ai_choice", False))
            coin_winner_is_player = getattr(
                self, "_pve_coin_winner_is_player", True)
            if not coin_winner_is_player and not ai_choice:
                log_req("    PvE choose: ignored player pick; AI won coin toss")
                self._push_transaction_ack(session)
                return True
            chose_draw = b"ChooseDrawTransaction" in inner_bytes
            if (coin_winner_is_player and
                    getattr(self, "_pve_forced_draw_first", False)):
                if not chose_draw:
                    log_req("    PvE choose: Weight rejected Play; forcing Draw")
                chose_draw = True
                self._pve_forced_draw_first = False
            # A draw-first player draws on turn 1. The play/draw choice is
            # made by the coin-toss winner, so invert the meaning when the AI
            # won: AI Draw means the human plays first, AI Play means the
            # human draws first.
            self._pending_player_draws_first = (
                coin_winner_is_player == chose_draw)
            self._pending_play_first_is_player = (
                coin_winner_is_player != chose_draw)
            encounter_data = dict(session.encounter_data or {})
            encounter_data["_pve_ai_mulligan_done"] = False
            encounter_data["_pve_player_mulliganed"] = False
            encounter_data["_pve_play_first_is_player"] = bool(
                self._pending_play_first_is_player)
            encounter_data["_pve_player_draws_first"] = bool(
                self._pending_player_draws_first)
            if (session.session_name or "").startswith("camp_"):
                encounter_data["_campaign_mulligan_redraws"] = 0
            session.encounter_data = encounter_data
            session._persist()
            pl_t = game_engine.UID.make(244, int(self.client_reck_id))
            ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
            game = game_engine.Game(session.session_id, pl_t, ai_t)
            # Draw the opening hands NOW — like PvP, PreGame and
            # PickGoesFirst show empty hands; each player draws after the
            # Play/Draw pick. Campaign uses the class/talent opening-hand
            # value for the player; the AI keeps the normal seven cards.
            # Campaign/PvE uses the campaign mulligan rule: the first redraw
            # replaces the opening hand at the same size.  Keep this counter
            # in the persisted session metadata so it survives the separate
            # transaction that carries the redraw request.  PvP retains its
            # existing one-fewer-card mulligan rule.
            if not db_game_get_hand(session.session_id,
                                    self.user_profile["id"]):
                db_game_draw_cards(session.session_id,
                                   self.user_profile["id"],
                                   self._starting_hand_size(session))
                db_game_draw_cards(session.session_id, 0, 7)
            player_hand = db_game_get_hand(session.session_id,
                                           self.user_profile["id"])
            ai_hand = db_game_get_hand(session.session_id, 0)
            opening_hand_projections = db_card_instance_dynamic_projections(
                session.session_id,
                [int(card_uid) for hand in (player_hand, ai_hand)
                 for card_uid, _template_guid in hand],
                conn=_db)
            for idx, (cu, tg) in enumerate(player_hand):
                scid = game_engine.SessionCardId(
                    game_engine.UID(int(cu)))
                _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                    self._card_full_data(
                        game, scid, tg,
                        instance_projection=opening_hand_projections.get(int(cu)))
                game.push_card_updated(
                    scid, pl_t, game_engine.ECardCollections.Hand,
                    ct, template_id=tg, gems=gem_type)
            for idx, (cu, tg) in enumerate(ai_hand):
                scid = game_engine.SessionCardId(
                    game_engine.UID(int(cu)))
                _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                    self._card_full_data(
                        game, scid, tg,
                        instance_projection=opening_hand_projections.get(int(cu)))
                game.push_card_updated(
                    scid, ai_t, game_engine.ECardCollections.Hand,
                    ct, template_id=tg, nulling=True, gems=gem_type)
            if ai_choice:
                if chose_draw:
                    game.push_player_wishes_to_draw_first(ai_t)
                else:
                    game.push_player_wishes_to_play_first(ai_t)
            play_first_is_player = bool(
                self._pending_play_first_is_player)
            ai_mulligan_done = _push_pve_mulligan_start(
                game, pl_t, ai_t, play_first_is_player,
                lambda: self._resolve_ai_mulligan(session, game, ai_t))
            if ai_mulligan_done:
                encounter_data = dict(session.encounter_data or {})
                encounter_data["_pve_ai_mulligan_done"] = True
                session.encounter_data = encounter_data
                session._persist()
            self._sync_rules_port_setup_checkpoint(
                session, game_engine.ETurnPhases.Mulligan, pl_t)
            if game.events:
                pkt = game.make_network_packet(pl_t)
                dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                                        "00000000-0000-0000-0000-000000000000")
                self._game_scnt = max(self._game_scnt, self.scnt) + 1
                self.scnt = self._game_scnt
                gs_inst = str(session.server_id)
                self.send({
                    "issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{gs_inst}.{self.scnt}",
                    "target": "ServiceGameSession", "instance": gs_inst,
                    "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                }, dw)
                log_req(f"    Pushed Mulligan phase ({len(dw)}b)")
            return True
    def _handle_mulligan_keep_transaction(self, session, transaction):
        """Resolve an accepted opening hand for PvP or Practice."""
        inner_bytes = transaction.inner_bytes
        handled = False
        if (session.session_name or "").startswith("tourney-"):
            from services.tournament_game import (
                pvp_load_state, pvp_save_state,
                pvp_mulligan_next, _pvp_push_mulligan_prompt,
                db_game_session_pids)
            # Track which player kept
            state = pvp_load_state(session)
            my_pid = int(self.client_reck_id)
            if state:
                from services.tournament_game import pvp_session_lock
                with pvp_session_lock(session):
                    # A keep can race with a retry or phase handoff. Reload
                    # under the session lock so a stale snapshot cannot roll
                    # the durable phase back after the first turn has begun.
                    state = pvp_load_state(session) or state
                    kept = state.get("kept") or []
                    mulligan_counts = state.get("mulligan_count") or {}
                    mulliganed = bool(int(mulligan_counts.get(
                        str(my_pid), mulligan_counts.get(my_pid, 0)) or 0))
                    # The client can resend AcceptStartingHand while
                    # the first response is still animating.  Treat a
                    # duplicate as an idempotent acknowledgement: an
                    # extra AcceptedStartingHand/priority packet can
                    # pop the visible Mulligan dialog on the opponent.
                    if my_pid in kept:
                        log_req(f"    PvP: duplicate keep from "
                                f"{my_pid}; ignored")
                    else:
                        kept.append(my_pid)
                        state["kept"] = kept
                        pvp_save_state(session, state)
                        # Tell BOTH clients the keeper's hand is
                        # accepted. The opponent also needs the event
                        # to recompute the keeper's deck count.
                        pl_t = game_engine.UID.make(244, my_pid)
                        pids = db_game_session_pids(session.session_id)
                        other_pid = pids[0] if pids[1] == my_pid else pids[1]
                        other_uid = game_engine.UID.make(244, other_pid)
                        for _keep_pid in pids:
                            _keep_h = player_handlers.get(_keep_pid)
                            if not _keep_h:
                                continue
                            g = game_engine.Game(int(session.session_id), pl_t, other_uid)
                            # The keeper has finished their decision.  Disable
                            # both clients while the other player's mulligan
                            # decision is pending; the next prompt enables
                            # only its asker.
                            g.push_disable_interface(True)
                            g.push_accepted_starting_hand(
                                pl_t, mulliganed=mulliganed)
                            pkt = g.make_network_packet(pl_t)
                            dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                                                     "00000000-0000-0000-0000-000000000000")
                            _keep_h.scnt += 1
                            _keep_h.send({
                                "issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{_keep_h.scnt}",
                                "target": "ServiceGameSession",
                                "instance": str(session.server_id),
                                "reqid": 0, "c": 0, "conh": 0, "sid": _keep_h.sid,
                            }, dw)
                        log_req(f"    PvP: player {my_pid} kept hand (pushed to both)")
                        from services.tournament_game import pvp_mulligan_next
                        pvp_mulligan_next(session, state, my_pid)
            return True
        else:
            pl_t = game_engine.UID.make(244, int(self.client_reck_id))
            ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
            # The selected Fortune is consumed when the human finishes the
            # opening-hand decision.  ReadyToStartGame has its own local
            # campaign-id variable, so derive it again here rather than
            # relying on that unrelated handler scope.
            try:
                camp_id = int((session.session_name or "camp_0").split("_")[-1]
                              or 0) if (session.session_name or "").startswith(
                                  "camp_") else 0
            except (TypeError, ValueError):
                camp_id = 0
        if not handled:
            game = game_engine.Game(session.session_id, pl_t, ai_t)
            game.player_resources = 0
            game.player_total_resources = 0
            game.max_hand_size = self._max_hand_size(session)
            setup_encounter = dict(session.encounter_data or {})
            ai_mulligan_done = bool(setup_encounter.get(
                "_pve_ai_mulligan_done", False))
            player_mulliganed = bool(setup_encounter.get(
                "_pve_player_mulliganed", False))
            play_first_is_player = bool(setup_encounter.get(
                "_pve_play_first_is_player",
                getattr(self, "_pending_play_first_is_player", True)))
            player_draws_first = setup_encounter.get(
                "_pve_player_draws_first",
                getattr(self, "_pending_player_draws_first", None))
            # Get cards in hand
            rows = db_hand_cards_raw(session.session_id, self.user_profile["id"])
            hand_cards = []  # list of (scid, cost)
            for r in rows:
                scid = game_engine.SessionCardId(game_engine.UID(r[0]))
                instance_id = r[1]
                # Resolve template data via the stored template_guid — one
                # path for both instance-based (FRA) and GUID (campaign/AI)
                # cards.
                t = self._template_by_guid(r[2])
                if t:
                    tpl_guid, ct_name, _name, cost, atk, def_ = t
                    ct = game_engine.card_type_from_db(ct_name)
                else:
                    tpl_guid = "00000000-0000-0000-0000-000000000000"
                    ct = game_engine.ECardTypes.Troop
                    cost, atk, def_ = 0, 0, 0
                hand_cards.append((scid, cost))
                # Full card data from the template via _card_full_data so
                # the re-pushed hand card keeps its thresholds, abilities,
                # ATTRIBUTES (e.g. Flight) and any stat mods — a bare
                # CardDef("Card", ...) without attributes wipes the Flight
                # keyword the client renders from the attributes bitmask.
                _tpl_g, ct, _nm, _c, _a, _d, gem_type = self._card_full_data(
                    game, scid, r[2], instance_id)
                game.push_card_updated(scid, pl_t, game_engine.ECardCollections.Hand, ct,
                    template_id=r[2], gems=gem_type)

            # The human kept: push the starting-hand acceptance + the AI's
            # mulligan decision, then enter the StartGame phase. The server
            # pushes ONE phase at a time; StartTurn/Ready/Prep/Draw/
            # FirstMainPhase are advanced one-by-one as the client (auto-)
            # passes, driven by the 3029 PassPriority handler.
            if (session.session_name or "").startswith("camp_"):
                campaign.apply_starting_hand_talents(
                    self, _db, session, game, pl_t,
                    getattr(self, "_campaign_starting_hand_effects", []))
            game.push_accepted_starting_hand(
                pl_t, mulliganed=player_mulliganed)
            # A human keep passes priority to the AI if it has not already
            # completed its active-first mulligan during setup.
            if not ai_mulligan_done:
                game.push_green_light(
                    ai_t, game_engine.EPriorityContext.Normal)
                game.push_turn_phase(
                    game_engine.ETurnPhases.Mulligan, ai_t, ai_t)
                self._resolve_ai_mulligan(session, game, ai_t)
            setup_encounter = dict(session.encounter_data or {})
            for transient_key in (
                    "_pve_ai_mulligan_done",
                    "_pve_player_mulliganed",
                    "_pve_play_first_is_player",
                    "_pve_player_draws_first"):
                setup_encounter.pop(transient_key, None)
            session.encounter_data = setup_encounter
            session._persist()
            first_turn_uid = pl_t if play_first_is_player else ai_t
            game.push_turn_phase(game_engine.ETurnPhases.StartGame,
                                 first_turn_uid, first_turn_uid)
            # Initialise the DB-backed battle state with the player who won the
            # subsequent Play/Draw choice. (The choice can belong to the AI.)
            # The client shows NO interaction during StartGame (no BattleState
            # is pushed for it), so a pass can never arrive — the server
            # advances StartGame -> StartTurn itself below.
            from rules_port import lifecycle as _be
            bstate = _be.default_state(
                turn_player=_be.PLAYER if play_first_is_player else _be.AI)
            setup_encounter = session.encounter_data or {}
            fra_start_state = (setup_encounter.get(
                "_fra_starting_battle_state", {})
                if isinstance(setup_encounter, dict) else {})
            bstate["player_resources"] = int(
                fra_start_state.get("player_resources", 0) or 0)
            bstate["player_total_resources"] = int(
                fra_start_state.get("player_total_resources", 0) or 0)
            bstate["ai_resources"] = int(
                fra_start_state.get("ai_resources", 0) or 0)
            bstate["ai_total_resources"] = int(
                fra_start_state.get("ai_total_resources", 0) or 0)
            for side in ("player", "ai"):
                thresholds = fra_start_state.get(f"{side}_threshold", {})
                bstate[f"{side}_threshold"] = {
                    int(color): int(value or 0)
                    for color, value in (thresholds or {}).items()
                }
            bstate["ai_resource_played_this_turn"] = False
            bstate["ai_resource_plays_this_turn"] = 0
            bstate["player_resource_plays_this_turn"] = 0
            bstate["player_charges"] = int(fra_start_state.get(
                "player_charges", getattr(self, "_player_starting_charges", 0)) or 0)
            bstate["ai_charges"] = int(fra_start_state.get(
                "ai_charges", getattr(self, "_ai_starting_charges", 0)) or 0)
            # Champion starting health (persisted for combat + reconnect).
            bstate["player_health"] = int(fra_start_state.get(
                "player_health", getattr(self, "_player_starting_health", 20)) or 20)
            bstate["ai_health"] = int(fra_start_state.get(
                "ai_health", getattr(self, "_ai_starting_health", 10)) or 10)
            # Attach before opening GameStarted triggers.  Otherwise the first
            # gameplay event would be resolved by the compatibility scanner,
            # leaving the session with a mixed native/legacy lifecycle from
            # the very first chain item.
            self._maybe_attach_rules_port(session, game, bstate)
            # A re-entrant attach refreshes this fresh seed with the shared
            # checkpoint created during setup, before PreGame modifiers ran.
            # That snapshot has no charge count, so re-apply the PreGame seed
            # (e.g. Fury's +2 charges) here or the starting charges are lost.
            bstate["player_charges"] = int(fra_start_state.get(
                "player_charges", getattr(self, "_player_starting_charges", 0)) or 0)
            bstate["ai_charges"] = int(fra_start_state.get(
                "ai_charges", getattr(self, "_ai_starting_charges", 0)) or 0)
            # Reapplying the challenge snapshot after RulesPort attachment
            # mirrors the charge restoration above and keeps the saved
            # round-zero resource/health mods from being overwritten by the
            # setup checkpoint created before they were applied.
            for key in ("player_resources", "player_total_resources",
                        "ai_resources", "ai_total_resources",
                        "player_health", "ai_health"):
                if key in fra_start_state:
                    bstate[key] = int(fra_start_state.get(key, 0) or 0)
            for side in ("player", "ai"):
                key = f"{side}_threshold"
                if key in fra_start_state:
                    bstate[key] = {
                        int(color): int(value or 0)
                        for color, value in
                        (fra_start_state.get(key) or {}).items()
                    }
            native_port = getattr(session, "_rules_port_session", None)
            if native_port is not None:
                # The setup packet contains the client's non-interactive
                # StartGame phase, but the cached native scheduler is still
                # sitting on its Mulligan priority action. Leave that action
                # before invoking either the human or AI turn driver.
                if native_port.begin_first_turn(first_turn_uid):
                    log_req(
                        "    RulesPort began first turn: "
                        f"active={native_port.active_player_id!r}")
            # Fortune readings are opponent-owned encounter effects. Resolve
            # the selected card through the normal metadata/BOM path so its
            # target templates choose the player's champion for
            # EachOpposingChampion, while EachChampion effects still affect
            # both champions. Sapphire's opening-hand modifier was applied
            # before the hand was dealt above.
            fortune_ability = getattr(
                self, "_campaign_fortune_ability_guid", None)
            fortune_guid = getattr(self, "_campaign_fortune_guid", None)
            if fortune_ability:
                self._current_bstate = bstate
                try:
                    from rules_port.resolution import resolve_port_ability
                    fortune_source = int(self._ai_champ_scid.uid.uid64)
                    fortune_log = resolve_port_ability(
                        self, game, session, _db, pl_t, ai_t, bstate,
                        fortune_ability, fortune_source, 0, target_map={})
                    log_req(f"    Fortune {fortune_guid}: {fortune_log}")
                except Exception as exc:
                    log_req(f"    Fortune {fortune_guid} error: {exc}")
                finally:
                    for key in ("player_mod_target", "player_spell_target",
                                "resolving_target_uid", "resolving_effect_guid"):
                        bstate.pop(key, None)
                    campaign.consume_fortune(_db, camp_id, fortune_guid)
                    self._campaign_fortune_guid = None
                    self._campaign_fortune_ability_guid = None
            game.player_threshold = dict(bstate.get("player_threshold", {}))
            game.player_charges = bstate.get("player_charges", 0)
            game.player_resources = bstate.get("player_resources", 0)
            game.player_total_resources = bstate.get("player_total_resources", 0)
            game.ai_threshold = dict(bstate.get("ai_threshold", {}))
            game.ai_charges = bstate.get("ai_charges", 0)
            game.ai_resources = bstate.get("ai_resources", 0)
            game.ai_total_resources = bstate.get("ai_total_resources", 0)
            game.player_health = bstate.get("player_health", 20)
            game.ai_health = bstate.get("ai_health", 10)
            # Apply any phase stops the client configured before the battle
            # state existed (SetTurnPhasesTransaction during setup), or the
            # player's saved preferences from previous battles.
            if getattr(self, "_pending_player_stops", None) is not None:
                pending_stops = self._pending_player_stops
                if (isinstance(pending_stops, (tuple, list)) and
                        len(pending_stops) >= 2):
                    bstate["player_self_stops"], bstate["player_opp_stops"] = pending_stops[:2]
                self._pending_player_stops = None
            else:
                saved_s, saved_o = self._load_player_stops(self.user_profile["id"])
                if saved_s is not None:
                    bstate["player_self_stops"] = saved_s
                if saved_o is not None:
                    bstate["player_opp_stops"] = saved_o
            # A draw-first player draws a card on their first turn.
            if player_draws_first is not None:
                bstate["player_draws_first_turn"] = bool(player_draws_first)
            self._pending_player_draws_first = None
            self._pending_play_first_is_player = None
            bstate["turn_player"] = (
                _be.PLAYER if play_first_is_player else _be.AI)
            _be.save_state(session, bstate)
            # "At the start of the game" triggers for both players'
            # opening-hand / warzone cards (e.g. Princess Victoria).
            game_started_event_start = len(game.events)
            for gs_owner in (self.user_profile["id"], 0):
                self._dispatch_native_trigger(
                    game, session, pl_t, ai_t, bstate,
                    "GameStartedEvent", None, gs_owner,
                    zones=("hand", "warzone"))
            # GameStarted triggers are mandatory setup work, not a priority
            # window.  Drain their normal chain in LIFO order before the
            # StartGame packet is sent; otherwise _advance_to_priority leaves
            # the client waiting for a manual pass during the opening flow.
            # A real choice (target, revealed card, or discard) still pauses
            # here and is sent to the client normally.
            self._autopass_game_started_chain(
                session, pl_t, ai_t, bstate, game)
            if (_be.stack_empty(bstate) and
                    not bstate.get("pending_choice") and
                    not bstate.get("pending_deck_search") and
                    not bstate.get("pending_trigger") and
                    not bstate.get("pending_discard_ability") and
                    not bstate.get("pending_conversation")):
                self._suppress_completed_game_started_chain_events(
                    game, game_started_event_start)
            # GameStarted abilities may change the DB-backed battle state.  In
            # particular, encounter-scene battleboard cards such as Savage
            # Lord's Resource Rich grant both current and permanent resources.
            # Persist after resolving them so reconnects and the next phase
            # load the same 1/1 state that was sent in the setup packet.
            _be.save_state(session, bstate)
            # Send the StartGame packet (one phase per packet).
            if game.events:
                pkt = game.make_network_packet(pl_t)
                dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1, "00000000-0000-0000-0000-000000000000")
                self._game_scnt = max(self._game_scnt, self.scnt) + 1
                self.scnt = self._game_scnt
                gs_inst = str(session.server_id)
                self.send({
                    "issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                    "target": "ServiceGameSession", "instance": gs_inst, "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                }, dw)
                self._event_q.append((self.scnt, dw, {}))
                log_req(f"    Pushed StartGame phase after keep ({len(dw)}b)")
            # Auto-pass non-stop phases (StartTurn -> Ready -> Prep -> Draw)
            # until the next stop phase (FirstMainPhase).  PvP sessions
            # advance via route_pvp_pass instead — calling the AI-turn
            # path here overwrites the PvP state with a human-vs-AI
            # battle state (turn_player etc.) and desyncs GreenLight.
            if not (session.session_name or "").startswith("tourney-"):
                if bstate.get("turn_player") == _be.AI:
                    self._run_ai_turn(session, pl_t, ai_t, bstate)
                else:
                    self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True

    def _handle_discard_transaction(self, session, transaction, *, card_uid=None):
        """Process a discard selection and acknowledge it."""
        inner_bytes = transaction.inner_bytes
        is_set_ability_data = transaction.is_set_ability_data
        is_ability_activate = transaction.is_ability_activate
        if ((session.session_name or "").startswith("tourney-")
                and not is_set_ability_data
                and not is_ability_activate):
            # Tournament PvP has its own persisted two-player phase
            # state.  Never let a discard transaction fall through to
            # the human-vs-AI battle_engine path: that would overwrite
            # turn_order_json and can jump the clients back to First
            # Main or even emit a false deck-out victory.
            from services.tournament_game import pvp_handle_discard
            handled = pvp_handle_discard(self, session, inner_bytes)
            if handled:
                self._push_transaction_ack(session)
                return True
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        if getattr(session, "_rules_port_session", None) is not None:
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        gd = game_engine.Game(session.session_id, pl_t, ai_t)
        card_uids = []
        if card_uid is not None:
            try:
                card_uids = [int(getattr(card_uid, "uid64", card_uid))]
            except (TypeError, ValueError):
                card_uids = []
        elif isinstance(inner_bytes, bytes):
            for m in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                try:
                    card_uid = struct.unpack('<Q', bytes.fromhex(m.group(1).decode()))[0]
                    # Only Card-type UIDs (the champion/player UIDs are
                    # type 0/3 and must NOT be discarded).
                    if (card_uid & 0xFF) != 1:
                        continue
                except Exception:
                    continue
                card_uids.append(int(card_uid))
        for card_uid in card_uids:
            try:
                # Typed RulesPort discard IDs and raw legacy IDs share the
                # same owner/hand validation in the domain helper.
                if (int(card_uid) & 0xFF) != 1:
                    continue
                gd_owner, _owner_uid = self._discard_card_to_owner(
                    session, pl_t, ai_t, int(card_uid))
                if gd_owner:
                    gd.events.extend(gd_owner.events)
                log_req(f"    Discarded card {card_uid}")
            except Exception:
                continue
        _db.commit()
        if gd.events:
            self._send_battle_events(session, gd, pl_t)
        # Discard down to the max hand size (7): each DiscardTransaction
        # moves ONE card, so stay in the Discard phase until the hand fits.
        # Only then advance to EndTurn.
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        hand_count = db_hand_card_count(session.session_id, self.user_profile["id"])
        if bstate.get("pending_discard_native"):
            # Stargazer-style discard is a temporary native discard window,
            # not an end-of-turn hand-limit discard. Restore the phase and
            # clear the continuation after exactly one DiscardTransaction.
            return_phase_idx = bstate.pop("pending_discard_return_phase_idx", bstate.get("phase_idx", 0))
            bstate.pop("pending_discard_native", None)
            bstate.pop("pending_discard_ability", None)
            bstate.pop("pending_discard_ability_instance_id", None)
            bstate.pop("pending_discard_target_template", None)
            bstate.pop("pending_discard_scid", None)
            bstate["phase_idx"] = return_phase_idx
            _be.save_state(session, bstate)
            self._advance_to_priority(session, pl_t, ai_t, bstate)
            return True
        if (hand_count > self._max_hand_size(session) and
                not self._hand_size_unlimited(session, bstate)):
            g2 = self._fresh_game(session, pl_t, ai_t, bstate)
            g2.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
            g2.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
            g2.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
            self._send_battle_events(session, g2, pl_t)
            log_req(f"    Still over hand limit ({hand_count} cards) — re-granted priority to discard more")
        else:
            _be.advance_phase(bstate)
            _be.save_state(session, bstate)
            self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True

    def _handle_mulligan_redraw_transaction(self, session, transaction):
        """Redraw the opening hand for PvP or Practice."""
        handled = False
        # PvP tournament: use reckoning id for game_cards.user_id.
        if (session.session_name or "").startswith("tourney-"):
            from services.tournament_game import (
                db_game_session_pids, pvp_load_state, pvp_save_state,
                pvp_session_lock)
            import random as _rp
            my_pid = int(self.client_reck_id)
            pl_t = game_engine.UID.make(244, my_pid)
            pids = db_game_session_pids(session.session_id)
            opp_pid = pids[0] if pids[1] == my_pid else pids[1]
            opp_t = game_engine.UID.make(244, opp_pid)
            from pvp_db import (db_rules_port_hand_rows,
                                db_rules_port_redraw_hand)
            hand_rows = db_rules_port_hand_rows(session.session_id, my_pid)
            redraw_count = len(hand_rows)
            draw_count = max(0, redraw_count - 1)
            log_req(f"    PvP mulligan redraw: {redraw_count} -> {draw_count} cards")
            # Save old card_uids so we can push them back to deck.
            old_scids = [game_engine.UID(row[1]) for row in hand_rows]
            _old_rows, new_rows = db_rules_port_redraw_hand(
                session.session_id, my_pid, draw_count)
            with pvp_session_lock(session):
                state = pvp_load_state(session)
                if state is not None:
                    mulligan_counts = dict(state.get("mulligan_count") or {})
                    key = str(my_pid)
                    mulligan_counts[key] = int(
                        mulligan_counts.get(key,
                                            mulligan_counts.get(my_pid, 0)) or 0) + 1
                    state["mulligan_count"] = mulligan_counts
                    pvp_save_state(session, state)
            # Push the updated hand to both clients. A priority handoff is
            # sent only after the player keeps or is auto-accepted at zero.
            for pid in pids:
                h = player_handlers.get(pid)
                if not h: continue
                is_me = (pid == my_pid)
                p_uid = game_engine.UID.make(244, pid)
                o_uid = game_engine.UID.make(244, opp_pid if is_me else my_pid)
                g = game_engine.Game(int(session.session_id), p_uid, o_uid)
                from rules_port.host_mutations import project_mulligan_cards
                project_mulligan_cards(
                    self, g, pl_t, _old_rows, new_rows,
                    new_player_uid=p_uid, reveal_new=is_me)
                # A C# MulliganTransaction redraw does not pass priority.
                # Keep the redrawer enabled and leave the opponent waiting;
                # only AcceptStartingHand hands priority to the other player.
                g.push_disable_interface(pid != my_pid)
                # The client uses this event to clear the local
                # BattleStateMulligan animation and refresh the hand
                # count.  CardMoved/CardUpdated alone can leave the
                # redraw state showing the old hand.
                g.push_player_mulliganed_hand(pl_t, draw_count)
                pkt = g.make_network_packet(p_uid)
                dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                                        "00000000-0000-0000-0000-000000000000")
                h.scnt += 1
                h.send({"issuer": f"0.0.0.0.ServiceGameSession.246.{session.session_id}.{h.scnt}",
                        "target": "ServiceGameSession", "instance": str(session.server_id),
                        "reqid": 0, "c": 0, "conh": 0, "sid": h.sid}, dw)
                log_req(f"    PvP mulligan: pushed redrawn hand to pid {pid}")
            if draw_count == 0:
                # C# auto-accepts a zero-card hand and passes priority.
                return self._handle_mulligan_keep_transaction(
                    session, transaction)
            # Redrawing retains the active player's priority and Mulligan UI.
            handled = True
        if not handled:
            pl_t = game_engine.UID.make(244, int(self.client_reck_id))
            ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
            import random as _random
            import json as _redraw_json
            game2 = game_engine.Game(session.session_id, pl_t, ai_t)

            # Fetch deck's active_gems ONCE (not per card)
            deck_gems = {}
            arena_lookup = db_get_arena_state(self.user_profile["id"])
            deck_lookup_id = self._resolve_fra_deck_id(
                arena_lookup["deck_id"]) or 0
            from pvp_db import db_deck_active_gems
            active_gems = db_deck_active_gems(deck_lookup_id)
            if active_gems:
                try:
                    import json as _gem_json
                    deck_gems = _gem_json.loads(active_gems)
                except Exception:
                    pass

            # Count cards in hand
            from pvp_db import db_rules_port_hand_rows
            hand_rows = db_rules_port_hand_rows(
                session.session_id, self.user_profile["id"])
            redraw_count = len(hand_rows)
            is_campaign = (session.session_name or "").startswith("camp_")
            if is_campaign:
                encounter_data = dict(session.encounter_data or {})
                try:
                    redraws = max(0, int(encounter_data.get(
                        "_campaign_mulligan_redraws", 0) or 0))
                except (TypeError, ValueError):
                    redraws = 0
                # The first campaign redraw is penalty-free.  Preserve the
                # normal shrinking-hand behavior if a client attempts a
                # second redraw before accepting its hand.
                draw_count = (redraw_count if redraws == 0 else
                              max(0, redraw_count - 1))
                encounter_data["_campaign_mulligan_redraws"] = redraws + 1
                session.encounter_data = encounter_data
                session._persist()
            else:
                draw_count = max(0, redraw_count - 1)

            # Persist the redraw atomically; this RulesPort domain operation
            # returns the old/new rows for the client event projection below.
            from pvp_db import db_rules_port_redraw_hand
            old_rows, new_cards = db_rules_port_redraw_hand(
                session.session_id, self.user_profile["id"], draw_count)
            encounter_data = dict(session.encounter_data or {})
            encounter_data["_pve_player_mulliganed"] = True
            session.encounter_data = encounter_data
            session._persist()
            from rules_port.host_mutations import project_mulligan_cards
            project_mulligan_cards(
                self, game2, pl_t, old_rows, new_cards,
                move_new=False, draw_new=True, update_old_first=False)

            game2.push_player_mulliganed_hand(pl_t, len(new_cards))

            if game2.events:
                pkt = game2.make_network_packet(pl_t)
                dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1, "00000000-0000-0000-0000-000000000000")
                self._game_scnt = max(self._game_scnt, self.scnt) + 1
                self.scnt = self._game_scnt
                gs_inst = str(session.server_id)
                self.send({
                    "issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                    "target": "ServiceGameSession", "instance": gs_inst, "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                }, dw)
                self._event_q.append((self.scnt, dw, {}))
                log_req(f"    Mulligan redraw: {redraw_count} back, {draw_count} drawn")
            if draw_count == 0:
                # AuthoritativeSessionBase.Mulligan accepts the hand and
                # passes priority immediately when the reduced size is zero.
                return self._handle_mulligan_keep_transaction(
                    session, transaction)
            handled = True
            # MulliganTransaction retains priority in C#. The AI is asked only
            # after the human accepts, if the AI has not already gone first.
        return True

    def _handle_generic_player_transaction(self, session, transaction,
                                      session_id, comp, conh,
                                      *, from_rules_port=False):
        """Handle a non-specialized player transaction after classification."""
        # A typed request that has entered RulesPort must never be silently
        # reinterpreted by this compatibility dispatcher.  The one permitted
        # call is the explicit domain-mutation adapter above, which keeps the
        # persistence/event projection separate from RulesPort validation and
        # ordering.
        if (not from_rules_port and
                getattr(session, "_rules_port_dispatch_command", None) is
                transaction):
            return False
        inner_bytes = transaction.inner_bytes
        typed_payload = getattr(transaction, "typed_payload", None)
        txn_info = transaction.fields
        is_quit = "m_QuitEntireSeries" in txn_info
        is_surr = "m_Surrendered" in txn_info
        is_pass_priority = transaction.is_pass_priority
        player_uid = make_uid(UID_TYPE["ServicePlayer"], int(self.client_reck_id))
        gs_instance = str(session.server_id)
        handled = False
        gs_instance = str(session.server_id)
        # Only end game on explicit quit/concede. The presence of the
        # m_QuitEntireSeries / m_Surrendered field marks a
        # QuitGameTransaction (the Withdraw/Concede button sends
        # m_QuitEntireSeries=false for a normal concede, so we must NOT
        # gate on the boolean value — the field's presence is the signal).
        is_quit = "m_QuitEntireSeries" in txn_info
        is_surr = "m_Surrendered" in txn_info
        # PvP tournament: route concessions through the shared end-game
        # path so both players receive the GameEnded banner.
        if not handled and session and (session.session_name or "").startswith("tourney-"):
            from services.tournament_game import (
                pvp_concede, pvp_handle_transaction,
            )
            try:
                if is_quit or is_surr:
                    handled = pvp_concede(self, session)
                else:
                    handled = pvp_handle_transaction(
                        self, session, inner_bytes,
                        typed_payload=getattr(transaction, "typed_payload", None))
            except Exception as _e:
                # Never let a PvP transaction exception kill this thread
                # (it would RST both clients).  Log, ack, and continue.
                import traceback
                log_req(f"    PvP txn exception: {_e}")
                traceback.print_exc()
                handled = True
        # PvP transactions are fully handled by tournament_game (cards,
        # combat, resources, deck search).  The human-vs-AI fallback
        # below must NOT run for them — it loads a battle_engine state
        # (turn_player etc.) into turn_order_json, clobbering the PvP
        # state machine and desyncing GreenLight.
        if handled and session and (session.session_name or "").startswith("tourney-"):
            if not is_pass_priority:
                self._push_transaction_ack(session)
                return True
        if not is_quit and not is_surr:
            log_req(f"    Normal transaction — processing card play/pass")

            # Try to identify which card was played from the transaction
            played_card_uid = None
            if isinstance(inner_bytes, bytes):
                scid_pos = inner_bytes.find(b"m_SessionCardId")
                if scid_pos >= 0:
                    # Find the UID value after m_SessionCardId struct
                    uid_pos = inner_bytes.find(b"m_UID64", scid_pos)
                    if uid_pos >= 0:
                        rest = inner_bytes[uid_pos + 7:]
                        parts = rest.split(b";", 6)
                        if len(parts) >= 4:
                            try:
                                hex_val = parts[4].decode("ascii", errors="replace")
                                played_card_uid = struct.unpack('<Q', bytes.fromhex(hex_val))[0]
                            except: pass

            # Find the card in DB to get its type and template GUID
            is_resource_play = False
            is_troop_play = False
            shard_color = None
            shard_ability = None
            shard_tpl = None
            resource_choice_ability = None
            played_card_type = None
            crow = None
            cur_grant = 0
            max_grant = 0
            if played_card_uid:
                from pvp_db import db_rules_port_card_projection
                crow = db_rules_port_card_projection(
                    session.session_id, int(played_card_uid))
                if crow:
                    played_card_type = crow[1]
                    cur_grant = int(crow[4] or 0)
                    max_grant = int(crow[5] or 0)
                    if crow[1] == 'Resource':
                        is_resource_play = True
                        # Validate the authored per-turn allowance before the
                        # compatibility projection moves the card out of the
                        # hand. This keeps an extra-resource card in hand
                        # when a stale client submits a now-illegal play.
                        import battle_engine as _resource_be
                        _resource_state = _resource_be.load_state(session)
                        from rules_port.resources import (
                            can_play_resource, resource_play_limit)
                        _resource_limit = resource_play_limit(
                            _db, session.session_id, _resource_state,
                            int(self.user_profile.get("id", 0)))
                        if not can_play_resource(
                                _resource_state, "player",
                                limit=_resource_limit):
                            log_req(
                                "    Rejected resource play: per-turn allowance used")
                            return True
                        shard_color = crow[2].split()[0]
                        # Shards of Fate ("Choose a Standard resource in
                        # your deck. Gain the thresholds it provides.")
                        # is data-driven: an ability chain targeting a
                        # Standard RESOURCE in the DECK.  It must NOT
                        # behave like a basic shard (+1 max/current
                        # resource, +1 charge, fixed threshold).
                        if crow[3]:
                            try:
                                _pl_ags = json.loads(crow[3])
                            except Exception:
                                _pl_ags = []
                            shard_ability, shard_tpl = \
                                self._shards_of_fate_template(_pl_ags)
                            if not shard_tpl:
                                from abilities.framework.resources import (
                                    printed_resource_choice_ability)
                                resource_choice_ability = \
                                    printed_resource_choice_ability(_pl_ags)
                    elif game_engine.card_type_from_db(crow[1]) & (
                            game_engine.ECardTypes.Troop |
                            game_engine.ECardTypes.Artifact |
                            game_engine.ECardTypes.Constant |
                            game_engine.ECardTypes.BasicAction |
                            game_engine.ECardTypes.QuickAction):
                        is_troop_play = True
                    # Remove this card from the hand by setting its position beyond the deck
                    from pvp_db import db_set_card_played_to_zone
                    db_set_card_played_to_zone(session.session_id, int(played_card_uid), 'PlayedResources')
                    log_req(f"    Played {crow[2]} (uid={played_card_uid}), moving to resource zone")

            # Track resources in the DB battle state. Playing a basic
            # threshold gives: +1 total, +1 current, +1 threshold colour,
            # +1 champion charge. All state lives in turn_order_json.
            import battle_engine as _be
            bstate = _be.load_state(session)
            self._current_bstate = bstate
            pl_t = game_engine.UID.make(244, int(self.client_reck_id))
            ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
            charge_delta = 0
            if is_resource_play:
                from rules_port.resources import resource_play_count
                old_charge = int(bstate.get("player_charges", 0) or 0)
                bstate["player_resource_plays_this_turn"] = (
                    resource_play_count(bstate, "player") + 1)
                bstate["player_resource_played_this_turn"] = True
                if shard_tpl:
                    # Shards of Fate: grants max/current resources from
                    # the template (m_MaxResourcesGranted=1,
                    # m_CurrentResourcesGranted=0 — it increases MAX
                    # mana only), plus one champion charge; the controller also
                    # chooses a Standard resource and gains its
                    # threshold.  The chosen shard stays in the deck.
                    _be.save_state(session, bstate)
                    g_tmp = self._fresh_game(session, pl_t, ai_t, bstate)
                    bstate["player_total_resources"] = (
                        bstate.get("player_total_resources", 0) + max_grant)
                    bstate["player_resources"] = (
                        bstate.get("player_resources", 0) + cur_grant)
                    bstate["player_charges"] = (
                        bstate.get("player_charges", 0) + 1)
                    g_tmp.player_total_resources = bstate.get(
                        "player_total_resources", 0)
                    g_tmp.player_resources = bstate.get(
                        "player_resources", 0)
                    self._send_battle_events(session, g_tmp, pl_t)
                    _be.save_state(session, bstate)
                    log_req(f"    Shards of Fate: resource play consumed; "
                            f"+{max_grant} max/+{cur_grant} current, "
                            f"printed threshold ability pending")
                else:
                    bstate["player_total_resources"] = (
                        bstate.get("player_total_resources", 0) + max_grant)
                    bstate["player_resources"] = (
                        bstate.get("player_resources", 0) + cur_grant)
                    bstate["player_charges"] = bstate.get("player_charges", 0) + 1
                    if shard_color and not resource_choice_ability:
                        color_map = {'Ruby': game_engine.ECardShards.Ruby,
                                     'Sapphire': game_engine.ECardShards.Sapphire,
                                     'Blood': game_engine.ECardShards.Blood,
                                     'Diamond': game_engine.ECardShards.Diamond,
                                     'Wild': game_engine.ECardShards.Wild}
                        color = color_map.get(shard_color)
                        if color is not None:
                            th = bstate.setdefault("player_threshold", {})
                            th[color] = th.get(color, 0) + 1
                charge_delta = max(
                    0, int(bstate.get("player_charges", 0) or 0) - old_charge)
                _be.save_state(session, bstate)
                log_req(f"    Resource: tot={bstate.get('player_total_resources',0)} th={dict(bstate.get('player_threshold',{}))} chg={bstate.get('player_charges',0)}")

            g3 = self._fresh_game(session, pl_t, ai_t, bstate)

            # Send ResourceCardPlayed + resource/threshold/charge updates
            if is_resource_play and played_card_uid and crow:
                scid_played = game_engine.SessionCardId(game_engine.UID(int(played_card_uid)))
                # Move card to PlayedResources zone first (CardUpdated then ResourceCardPlayed)
                g3.push_card_updated(scid_played, pl_t, game_engine.ECardCollections.PlayedResources,
                                    game_engine.ECardTypes.Resource, template_id=crow[0])
                g3.push_resource_card_played(scid_played, pl_t, free=False)
                if max_grant or cur_grant:
                    # Update current resources display
                    ev_cur = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
                    ev_cur.player_id = pl_t; ev_cur.operation = 1
                    ev_cur.delta = cur_grant
                    ev_cur.new_value = bstate.get("player_resources", 0)
                    g3._push(ev_cur)
                    # Update total resources display
                    ev_tot = game_engine.PlayerTotalResourcePoolChangedSessionEventArgs()
                    ev_tot.player_id = pl_t; ev_tot.operation = 1
                    ev_tot.delta = max_grant
                    ev_tot.new_value = bstate.get("player_total_resources", 0)
                    g3._push(ev_tot)
                if not shard_tpl and not resource_choice_ability:
                    # Update threshold display for the played shard's color
                    if shard_color:
                        col_map = {'Ruby': 8, 'Sapphire': 16, 'Blood': 4, 'Diamond': 64, 'Wild': 32}
                        shard_val = col_map.get(shard_color, 32)
                        thresh_count = bstate["player_threshold"].get(shard_val, 0)
                        ev_th = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                        ev_th.player_id = pl_t; ev_th.color = shard_val; ev_th.operation = 1; ev_th.delta = 1
                        ev_th.new_value = thresh_count
                        g3._push(ev_th)
                        from rules_port.triggers import dispatch_native_trigger
                        champion = getattr(self, "_player_champ_scid", None)
                        if champion is not None:
                            previous_color = bstate.get("gain_threshold_color")
                            bstate["gain_threshold_color"] = int(shard_val)
                            try:
                                dispatch_native_trigger(
                                    db=_db, handler=self, game=g3,
                                    session=session, player_uid=pl_t,
                                    ai_uid=ai_t, battle_state=bstate,
                                    event_type="GainThresholdEvent",
                                    source_card_id=int(champion.uid.uid64),
                                    source_player_id=(
                                        self.user_profile["id"]
                                        if self.user_profile else 0),
                                    data={"gain_threshold_color": int(shard_val)})
                            finally:
                                if previous_color is None:
                                    bstate.pop("gain_threshold_color", None)
                                else:
                                    bstate["gain_threshold_color"] = previous_color
                # Playing any resource, including Shards of Fate, grants a
                # champion charge point.
                if charge_delta:
                    ev_chg = game_engine.ChampionChargePointsChangedSessionEventArgs()
                    ev_chg.player_id = pl_t; ev_chg.operation = 1
                    ev_chg.delta = charge_delta
                    ev_chg.new_value = bstate.get("player_charges", 0)
                    g3._push(ev_chg)
                    from rules_port.triggers import dispatch_native_trigger
                    champion = getattr(self, "_player_champ_scid", None)
                    if champion is not None:
                        for _ in range(max(0, int(charge_delta))):
                            dispatch_native_trigger(
                                db=_db, handler=self, game=g3,
                                session=session, player_uid=pl_t,
                                ai_uid=ai_t, battle_state=bstate,
                                event_type="GainChargeEvent",
                                source_card_id=int(champion.uid.uid64),
                                source_player_id=(
                                    self.user_profile["id"]
                                    if self.user_profile else 0))
                # A resource can carry an instance-only, non-triggered
                # ability granted while it was in the deck (for example
                # Fruitful Foresight's Gain [L1][R1]).  Resolve those
                # metadata-defined additions after the normal resource grant.
                from abilities.framework.resources import (
                    resolve_granted_resource_abilities,
                    resolve_printed_resource_abilities)
                resource_owner = (self.user_profile["id"]
                                  if self.user_profile else 0)
                resource_logs = resolve_granted_resource_abilities(
                    g3, session, _db, self, pl_t, ai_t, bstate,
                    int(played_card_uid), resource_owner)
                if resource_logs:
                    log_req("    Resource granted abilities: " +
                            "; ".join(resource_logs))
                if resource_choice_ability:
                    # Choice resources carry their threshold effect in the
                    # printed BOM (rather than in the resource name).  Run it
                    # through the normal resolver so the client receives the
                    # generated Choosing cards and class-23 picker.
                    from rules_port.resolution import resolve_port_ability
                    resolve_port_ability(
                        self, g3, session, _db, pl_t, ai_t, bstate,
                        resource_choice_ability, int(played_card_uid),
                        resource_owner, target_map={})
                    log_req(f"    Resource choice ability: "
                            f"{resource_choice_ability[:8]}")
                printed_logs = resolve_printed_resource_abilities(
                    g3, session, _db, self, pl_t, ai_t, bstate,
                    int(played_card_uid), resource_owner,
                    skip_guids=({resource_choice_ability}
                                if resource_choice_ability else ()))
                if printed_logs:
                    log_req("    Resource printed abilities: " +
                            "; ".join(printed_logs))
                # Resource-triggered abilities are added to the authoritative
                # chain in ``bstate``. Persist that mutation before the later
                # option/packet helpers reload the session; otherwise the
                # client sees the trigger animation, but the next pass loads
                # an empty stack and advances FirstMain into combat.
                _be.save_state(session, bstate)

            if not is_troop_play:
                g3.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))

            # Troop/artifact play: push to the stack (CastSpells), then
            # auto-resolve since the AI opponent always passes.
            if is_troop_play and played_card_uid and crow:
                tid = int(played_card_uid)
                scid_played = game_engine.SessionCardId(game_engine.UID(tid))
                # Get instance id for gem lookup
                from pvp_db import db_rules_port_card_instance_id
                inst_id = db_rules_port_card_instance_id(session.session_id, tid)
                # _card_full_data fills g3.card_defs with cost/atk/def/
                # thresholds/abilities/gems and returns full stats.
                tpl_guid, ct_n, nm, cost, atk, def_, gem = self._card_full_data(
                    g3, scid_played, crow[0], inst_id)
                play_plan = self._card_play_plan(
                    crow[0], tid, self.user_profile["id"]
                    if self.user_profile else 0,
                    session=session)
                if play_plan is None:
                    self._push_transaction_ack(session)
                    return True
                targets = self._extract_transaction_targets(
                    inner_bytes, played_card_uid)
                cost_selection = self._card_play_cost_selections(
                    session, play_plan, tid, targets)
                if cost_selection is None:
                    log_req(f"    REJECTED play {crow[2]}: incomplete/illegal "
                            "card cost target")
                    g3 = game_engine.Game(session.session_id, pl_t, ai_t)
                    g3.push_player_updated(pl_t, champ_id=getattr(
                        self, "_player_champ_scid", None))
                    self._send_battle_events(session, g3, pl_t)
                    self._push_transaction_ack(session)
                    handled = True
                    return True
                cost_selections, cost_uids = cost_selection
                ability_data = ((typed_payload or {}).get("ability_data") or ()) \
                    if isinstance(typed_payload, dict) else ()
                x_cost = self._extract_int32_field(
                    inner_bytes, "m_ResourceXCost")
                x_cost = max(0, int(x_cost or 0))
                if play_plan.cost.variable:
                    if x_cost < play_plan.cost.variable_minimum:
                        log_req(f"    REJECTED play {crow[2]}: X={x_cost} "
                                f"below minimum {play_plan.cost.variable_minimum}")
                        self._push_transaction_ack(session)
                        handled = True
                        return True
                elif x_cost:
                    log_req(f"    REJECTED play {crow[2]}: non-variable card "
                            f"has X={x_cost}")
                    self._push_transaction_ack(session)
                    handled = True
                    return True
                x_payment = x_cost * play_plan.cost.variable_multiplier
                if cost + x_payment > bstate.get("player_resources", 0):
                    # Defense-in-depth: the client's playability is the
                    # normal gate, but a drag shouldn't be able to play
                    # an unaffordable card (e.g. an undiscounted Fury of
                    # the Mountain God) and push resources negative.
                    log_req(f"    REJECTED play {crow[2]}: cost {cost}+{x_payment} > "
                            f"resources {bstate.get('player_resources', 0)}")
                    g3 = game_engine.Game(session.session_id, pl_t, ai_t)
                    g3.push_player_updated(pl_t, champ_id=getattr(
                        self, "_player_champ_scid", None))
                    self._send_battle_events(session, g3, pl_t)
                    self._push_transaction_ack(session)
                    handled = True
                    return True
                # Build and validate the complete current-record activation
                # before mutating resources or moving the card.  This is the
                # single card-play boundary for target/option/X data; leaves
                # must never discover a missing target after payment.
                activations, _cost_target_map = play_plan.activation_bundle(
                    targets, x_cost=x_cost, ability_data=ability_data)
                ability_cost_selection = self._ability_card_play_cost_selections(
                    session, play_plan, tid, activations,
                    owner_id=self.user_profile["id"], battle_state=bstate)
                if ability_cost_selection is None:
                    log_req(f"    REJECTED play {crow[2]}: invalid ability cost target")
                    self._push_transaction_ack(session)
                    handled = True
                    return True
                ability_cost_selections, ability_cost_uids = ability_cost_selection
                cost_selections.extend(ability_cost_selections)
                cost_uids.update(ability_cost_uids)
                selected_cost_map = {
                    int(spec.get("index", index)): tuple(selected)
                    for index, (spec, selected) in enumerate(cost_selections)
                    if selected and not spec.get("ability_guid")
                }
                plan_errors = play_plan.validate(
                    variable_cost=x_cost,
                    activations=activations,
                    cost_target_map=selected_cost_map)
                if plan_errors:
                    log_req(f"    REJECTED play {crow[2]}: "
                            f"activation validation: {'; '.join(plan_errors)}")
                    g3 = game_engine.Game(session.session_id, pl_t, ai_t)
                    g3.push_player_updated(pl_t, champ_id=getattr(
                        self, "_player_champ_scid", None))
                    self._send_battle_events(session, g3, pl_t)
                    self._push_transaction_ack(session)
                    handled = True
                    return True
                if not self._card_costs_valid(
                        session, cost_selections, self.user_profile["id"]):
                    log_req(f"    REJECTED play {crow[2]}: invalid card cost")
                    self._push_transaction_ack(session)
                    handled = True
                    return True
                bstate["player_resources"] = (
                    bstate.get("player_resources", 0) - cost - x_payment)
                # Move from hand to CastSpells in DB
                from pvp_db import db_set_card_played_to_zone
                db_set_card_played_to_zone(session.session_id, tid, 'CastSpells')
                # Push chain events
                voided_cost_uids = self._apply_card_play_costs(
                    g3, session, bstate, pl_t, ai_t, cost_selections,
                    source_uid=tid,
                    expected_owner_id=self.user_profile["id"],
                    scrounge=any(getattr(ability, "is_scrounge", False)
                                 for ability in play_plan.cast_abilities),
                    defer_voided_cards=True)
                g3.push_card_updated(scid_played, pl_t, game_engine.ECardCollections.CastSpells,
                                    game_engine.card_type_from_db(played_card_type),
                                    template_id=crow[0], cost=cost, attack=atk, defense=def_, gems=gem)
                g3.push_troop_card_played(scid_played, pl_t)
                # Opponent (AI) gets priority after the card has been
                # registered on the client chain.  The chain animation must
                # precede GreenLight; otherwise the client can enter the
                # response window while ChainView is still empty.
                perm = game_engine.card_type_from_db(played_card_type) & (
                    game_engine.ECardTypes.Troop |
                    game_engine.ECardTypes.Artifact |
                    game_engine.ECardTypes.Constant)
                if perm:
                    # Permanents go on the STACK like spells (so they can
                    # be countered/interrupted) instead of resolving
                    # instantly.  The card stays in CastSpells; when BOTH
                    # players pass, _resolve_stack_item moves it to the
                    # warzone (CameOutThisTurn / summoning sick), re-pushes
                    # the full CardUpdated/CardMoved, and fires its
                    # enters-play + Deploy triggers.
                    effect_targets = [uid for uid in targets
                                      if int(uid) not in cost_uids]
                    target_uid = effect_targets[-1] if effect_targets else None
                    _be.stack_push(bstate, {
                        "kind": "troop", "source_uid": int(tid),
                        "ability_guids": [ability.ability_guid
                                          for ability in
                                          play_plan.cast_abilities],
                        "target_uid": target_uid,
                        "instance_id": 1, "x_cost": x_cost,
                        "voided_cost_uids": list(voided_cost_uids or ()),
                        "activations": {
                            guid: activation.as_dict()
                            for guid, activation in activations.items()
                        },
                        "played_from_hand": True,
                    })
                    # The troop's presence on the chain is driven by the
                    # CastSpells CardMove/UpdateCard of the actual card
                    # above. The chain event still needs a real client
                    # AbilityTemplate; a card-template GUID is not one and
                    # makes UIBattle silently skip the chain picture.
                    g3.push_ability_on_chain(
                        scid_played,
                        game_engine.ResourceId.from_str(
                            game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID))
                    _be.save_state(session, bstate)
                    log_req(f"    Troop {crow[2]} on the stack — "
                            f"resolves on both-pass (AI auto-passes)")
                else:
                    # BasicAction / QuickAction spell: cast and PUSH onto
                    # the chain (CastSpells = the visual stack). The BOM
                    # (draw / buff / etc.) executes when the chain resolves
                    # (both players pass) — ONE chain item, sub-effects
                    # bundled. Card-level costs were already paid and bound
                    # above; only the remaining IDs are effect targets.
                    effect_targets = [uid for uid in targets
                                      if int(uid) not in cost_uids]
                    target_uid = effect_targets[-1] if effect_targets else None
                    log_req(f"    Spell {played_card_uid}: targets={[hex(t) for t in targets]} "
                            f"costs={[hex(t) for t in cost_uids]} "
                            f"target={hex(target_uid) if target_uid else None}")
                    ability_guids = [ability.ability_guid
                                     for ability in play_plan.cast_abilities]
                    _be.stack_push(bstate, {
                        "kind": "spell", "source_uid": int(tid),
                        "ability_guids": ability_guids, "target_uid": target_uid,
                        "instance_id": 1, "x_cost": x_cost,
                        "voided_cost_uids": list(voided_cost_uids or ()),
                        "activations": {
                            guid: activation.as_dict()
                            for guid, activation in activations.items()
                        },
                    })
                    g3.push_ability_on_chain(
                        scid_played,
                        game_engine.ResourceId.from_str(
                            game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID),
                        target_card_ids=(
                            [game_engine.SessionCardId(
                                game_engine.UID(int(uid)))
                             for uid in effect_targets]))
                    _be.save_state(session, bstate)
                # Both card-play branches now have a visible chain item.
                g3.push_green_light(
                    ai_t, game_engine.EPriorityContext.ResolveTopOfChain)
                _be.save_state(session, bstate)
                g3.player_resources = bstate["player_resources"]
                ec = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
                ec.player_id = pl_t; ec.operation = 2; ec.delta = cost + x_payment
                ec.new_value = bstate["player_resources"]
                g3._push(ec)
                g3.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
                log_req(f"    Troop cost={cost}+{x_cost} — resources left={bstate['player_resources']}")
                if g3.events:
                    pkt3 = g3.make_network_packet(pl_t)
                    dw3 = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt3)), 1, "00000000-0000-0000-0000-000000000000")
                    self._game_scnt = max(self._game_scnt, self.scnt) + 1
                    self.scnt = self._game_scnt
                    self.send({
                        "issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                        "target": "ServiceGameSession", "instance": gs_instance,
                        "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                    }, dw3)
                    self._event_q.append((self.scnt, dw3, {}))
                    log_req(f"    Pushed permanent to chain ({len(dw3)}b); "
                            "waiting for both players to pass")
                handled = True

            # Get updated hand cards (skip the played one), resolving via
            # template_guid so it works for instance and GUID cards.
            rows3 = db_hand_cards_full(session.session_id, self.user_profile["id"])

            hand3 = []
            for r3 in rows3:
                scid3 = game_engine.SessionCardId(game_engine.UID(r3[0]))
                c3 = 0
                ct3 = r3[1] or ''
                thresh_json3 = None
                ab_guids3 = []
                t = self._template_by_guid(r3[2])
                if t:
                    c3 = t[3]
                    ct3 = t[1] or ''
                    srow = db_card_template_thresholds(r3[2])
                    if srow:
                        thresh_json3 = srow[0]
                        if srow[1]:
                            try:
                                ab_guids3 = [g.lower() for g in json.loads(srow[1])]
                            except Exception:
                                pass
                hand3.append((scid3, c3, ct3, thresh_json3, ab_guids3))

            # Filter: resources + threshold + valid troop target.
            fc3 = self._warzone_troop_count(session, self.user_profile["id"])
            ec3 = self._warzone_troop_count(session, 0)
            playable3 = []
            from rules_port.static_rules import effective_cost
            from rules_port.resources import can_play_resource, resource_play_limit
            resource_limit = resource_play_limit(
                _db, session.session_id, bstate,
                int(self.user_profile.get("id", 0)))
            resource_slots_used = not can_play_resource(
                bstate, "player", limit=resource_limit)
            for s, c, ct, thresh_json, ab_guids in hand3:
                # Recompute instance-level cost when rebuilding options after
                # a resource play; opening-hand discounts must remain active.
                try:
                    c = effective_cost(_db, session.session_id, bstate,
                                       s.uid.uid64)
                except Exception:
                    pass
                if self._hand_card_playable(session, s.uid.uid64, ct, c,
                                            thresh_json, ab_guids,
                                            bstate.get("player_resources", 0),
                                            bstate.get("player_threshold", {}),
                                            resource_slots_used,
                                            fc3, ec3):
                    playable3.append(s)
            if (not bstate.get("pending_choice") and
                    not bstate.get("pending_trigger") and
                    not bstate.get("pending_deck_search") and
                    not bstate.get("pending_conversation")):
                # A class-39 trigger target prompt is pending — the
                # prompt packet already carries the picker + priority;
                # pushing the normal post-play options would clobber it.
                chain_pending = not _be.stack_empty(bstate)
                if chain_pending:
                    # A permanent is still on CastSpells.  Do not publish the
                    # main-phase hand options here: that lets the player cast
                    # another troop/resource before the chain has resolved.
                    # Send the card/resource events first, then expose only
                    # quick responses while the chain is pending.
                    if g3.events:
                        pkt3 = g3.make_network_packet(pl_t)
                        dw3 = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt3)), 1, "00000000-0000-0000-0000-000000000000")
                        self.scnt += 1
                        self.send({
                            "issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                            "target": "ServiceGameSession", "instance": gs_instance,
                            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                        }, dw3)
                    self._push_phase_options_empty(session, pl_t, ai_t)
                    g_chain = game_engine.Game(session.session_id, pl_t, ai_t)
                    g_chain.push_green_light(
                        pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
                    self._send_battle_events(session, g_chain, pl_t)
                    log_req("    Chain pending after player play; "
                            "main-phase card options withheld")
                else:
                    g3.push_options(pl_t, playable3)
                    self._add_play_target_options(g3, session, pl_t, ai_t)
                    log_req(f"    Post-play options: {len(playable3)} playable, thresh={dict(bstate.get('player_threshold',{}))} resources={bstate.get('player_resources',0)}")
                    player_champ_cid3 = getattr(self, "_player_champ_scid", None)
                    if player_champ_cid3:
                        abilities3 = getattr(self, "_player_champ_abilities", [])
                        if abilities3:
                            champ_uid3 = (
                                player_champ_cid3.uid.to_uint64()
                                if hasattr(player_champ_cid3, "uid") else 0)
                            champ_targets3 = self._champion_ability_targets(
                                session, abilities3, champ_uid3)
                            champ_costs3 = self._champion_ability_costs(
                                session, abilities3, champ_uid3)
                            g3.add_champion_to_options(
                                pl_t, player_champ_cid3,
                                self._filter_affordable_abilities(
                                    abilities3, bstate,
                                    _be.current_phase(bstate),
                                    target_data=champ_targets3,
                                    cost_data=champ_costs3),
                                self._discard_costs_for(session, abilities3),
                                champ_targets3, champ_costs3)
                    # Warzone-troop manual abilities (e.g. Shift) also refresh
                    # after playing a card (a fresh troop just resolved).
                    affordable3 = self._affordable_troop_abilities(session, bstate)
                    if affordable3:
                        self._add_troop_ability_options(
                            g3, pl_t, session, affordable3, bstate)
                    # Re-grant priority so the player keeps the Pass Priority
                    # button visible after playing a card.
                    g3.push_green_light(pl_t, self._priority_context_for(_be.current_phase(bstate), bstate))
                    if g3.events:
                        pkt3 = g3.make_network_packet(pl_t)
                        dw3 = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt3)), 1, "00000000-0000-0000-0000-000000000000")
                        self.scnt += 1
                        self.send({
                            "issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                            "target": "ServiceGameSession", "instance": gs_instance,
                            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                        }, dw3)
                        log_req(f"    Pushed fresh GreenLight + options ({len(dw3)}b)")
            elif (bstate.get("pending_choice") or
                  bstate.get("pending_deck_search") or
                  bstate.get("pending_conversation")) and g3.events:
                # A resource picker (or Shards of Fate deck picker) is
                # pending: send only the resource
                # card played + PlayerUpdated events — the prompt packet
                # already carried the picker and priority.
                pkt3 = g3.make_network_packet(pl_t)
                dw3 = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt3)), 1, "00000000-0000-0000-0000-000000000000")
                self.scnt += 1
                self.send({
                    "issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                    "target": "ServiceGameSession", "instance": gs_instance,
                    "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                }, dw3)
                log_req(f"    Pushed resource-play events (choice pending) ({len(dw3)}b)")
        elif not handled:
            log_req(f"    === PlayerTransaction — ending game ===")
            self._finish_battle_gameend(
                session, False,
                [game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)],   # AI wins
                [game_engine.UID(player_uid)])     # player loses
            log_req(f"    Pushed GameEnded (3055) — marked as loss")
        return True

    def _handle_debug_cheat_transaction(self, session, transaction):
        """Execute a client DebugCheatTransaction and acknowledge it."""
        from debug_cheats import handle_debug_cheat
        log_req("    Debug cheat — dispatching server-side action")
        handled = handle_debug_cheat(self, session, transaction.inner_bytes)
        if handled:
            self._push_transaction_ack(session)
        return True

    def _handle_encounter_mod_dialog_transaction(self, session, transaction):
        """Route the class-55 conversation acknowledgement to its owner."""
        if (session.session_name or "").startswith("tourney-"):
            from services.tournament_game import pvp_handle_transaction
            handled = pvp_handle_transaction(self, session,
                                              transaction.inner_bytes,
                                              typed_payload=getattr(
                                                  transaction, "typed_payload", None))
            if not handled:
                self._push_transaction_ack(session)
            return True
        return self._resolve_pending_conversation(session, transaction)

    def _handle_set_ability_data_transaction(self, session, transaction):
        """Resolve target data supplied for a pending ability prompt."""
        inner_bytes = transaction.inner_bytes
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        if bstate.get("pending_trigger"):
            # The player answered a class-39 triggered-ability target
            # prompt (e.g. Solitary Exile's Deploy "Void another target
            # card").  Extract the chosen target (last Card-type UID in
            # the TargetMap), push the trigger onto the chain with that
            # target, and let the normal pass flow resolve it.
            chosen_uid = None
            if isinstance(inner_bytes, bytes):
                for m_du in re.finditer(
                        rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
                        inner_bytes):
                    try:
                        uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                        if (uid64 & 0xFF) == 1:
                            chosen_uid = int(uid64)
                    except Exception:
                        continue
            pend = bstate.pop("pending_trigger")
            ag = pend["ability_guid"]
            src = int(pend["source_uid"])
            inst_id = int(pend["instance_id"])
            _be.stack_push(bstate, {
                "kind": "trigger", "ability_guid": ag,
                "source_uid": src, "target_uid": chosen_uid,
                "instance_id": inst_id,
            })
            _be.save_state(session, bstate)
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            g.push_ability_on_chain(
                game_engine.SessionCardId(game_engine.UID(src)),
                game_engine.ResourceId.from_str(ag),
                ability_instance_id=inst_id)
            g.push_green_light(pl_t, game_engine.EPriorityContext.ResolveTopOfChain)
            self._send_battle_events(session, g, pl_t)
            self._push_transaction_ack(session)
            log_req(f"    Trigger target chosen: {ag[:8]} "
                    f"target={hex(chosen_uid) if chosen_uid else 'none'}")
            handled = True
        if bstate.get("pending_deck_search"):
            # The player answered a "search your deck" class-39 prompt
            # (e.g. Darkspire Priestess's Deathcry) — move the chosen
            # matching deck card into the controller's hand.
            handled = self._resolve_pending_deck_search(
                session, pl_t, ai_t, inner_bytes)
        if bstate.get("pending_discard_ability"):
            # The chosen card is the LAST Card-type UID in the transaction
            # (the TargetMap embeds it after the source card id).
            discard_card_uid = None
            if isinstance(inner_bytes, bytes):
                for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                    try:
                        uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                        if (uid64 & 0xFF) == 1:
                            discard_card_uid = int(uid64)
                    except Exception:
                        continue
            if not discard_card_uid:
                discard_card_uid = bstate.get("pending_discard_scid")
            row = None
            if discard_card_uid:
                from pvp_db import db_rules_port_hand_card
                row = db_rules_port_hand_card(
                    session.session_id, discard_card_uid,
                    self.user_profile["id"])
            if row and discard_card_uid is not None:
                # Discard to the card's OWNER (a Mind Grasp steal returns
                # to the AI's graveyard; a player card to the player's).
                gd_owner, owner_uid = self._discard_card_to_owner(
                    session, pl_t, ai_t, int(discard_card_uid))
                gd = gd_owner if gd_owner else game_engine.Game(session.session_id, pl_t, ai_t)
                tpl_d = "00000000-0000-0000-0000-000000000000"
                # Generic post-ability rule: re-grant priority to the
                # activating player after the discard resolves (the class-23
                # prompt consumed the priority window).
                gd.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
                self._send_battle_events(session, gd, pl_t)
                self._push_main_phase_options(session, pl_t, ai_t)
                log_req(f"    Discard (class-23 follow-up): {discard_card_uid} owner={owner_uid} ({tpl_d})")
            else:
                log_req(f"    Discard (class-23 follow-up): no hand card uid={discard_card_uid}")
            bstate.pop("pending_discard_ability", None)
            bstate.pop("pending_discard_ability_instance_id", None)
            bstate.pop("pending_discard_target_template", None)
            bstate.pop("pending_discard_scid", None)
            # The discard is the terminal continuation of Stargazer's
            # draw-then-discard BOM.  Clear the resume cursor and port
            # activation checkpoint so the next activation starts at effect
            # zero and cannot inherit the previous player's discard state.
            bstate.pop("rules_port_resume_effect_order", None)
            bstate.pop("resolution_paused", None)
            try:
                port = getattr(session, "_rules_port_session", None)
                if port is not None:
                    port.pending_activation = None
                    port.persist()
            except Exception:
                pass
            _be.save_state(session, bstate)
        return True

    def _handle_ability_activate_transaction(self, session, transaction):
        """Resolve a PvE card or champion ability activation."""
        inner_bytes = transaction.inner_bytes
        is_set_ability_data = transaction.is_set_ability_data
        is_ability_activate = transaction.is_ability_activate
        handled = False
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        # Extract AbilityTemplateId (GUID) from the transaction
        ability_guid = extract_ability_guid(inner_bytes)
        log_req(f"    Ability activation: guid={ability_guid}")

        # A DoubleChoice effect exposes the client's built-in
        # ChooseAndPlay ability. Consume that selection before normal
        # champion/troop ability routing can mistake the generated Choice
        # card for an ordinary activation.
        if ability_guid:
            _choice_be = self._checkpoint_engine(session)
            if _choice_be.load_state(session).get("pending_choice"):
                if self._resolve_pending_choice(
                        session, pl_t, ai_t, inner_bytes, ability_guid):
                    return True

        # A triggered-ability target response (e.g. Solitary Exile's
        # Deploy "Void another target card") arrives as an
        # AbilityActivationData transaction too — the client submits a
        # SetAbilityActivationDataTransaction for the class-39 prompt.
        # Route it to the pending-trigger follow-up BEFORE the manual
        # troop-ability path so the trigger resolves with the chosen
        # target instead of being rejected by the activation gates.
        if ability_guid and (
                self._resolve_pending_trigger_target(
                    session, pl_t, ai_t, inner_bytes, ability_guid)
                or self._resolve_pending_deck_search(
                    session, pl_t, ai_t, inner_bytes, ability_guid)):
            handled = True

        # Troop/artifact manual ability (e.g. Shift): the source is a
        # warzone troop the player controls, not the champion. Champion
        # abilities live in talent_abilities; card abilities live in
        # card_abilities_meta and are gated on the instance's ability
        # list. Route here BEFORE the champion path.
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        if ability_guid and not handled:
            # A GUID can be present in both the card-ability index and
            # champion metadata.  The transaction's SourceCardId is the
            # authoritative discriminator: when it identifies a matching
            # player-controlled warzone card carrying this ability, route it
            # through the card-ability resolver first.  Otherwise a card power
            # such as Crown of the Primals can enter the champion path, where
            # its selected source/target cards are mistaken for champion
            # payment targets and voided.
            from pvp_db import (db_rules_port_talent_ability_exists,
                                db_rules_port_cards_with_ability)
            champ_owned = db_rules_port_talent_ability_exists(ability_guid)
            # The same ability GUID can be present on multiple card instances
            # (for example, two Howling Braves).  The option list is per
            # source card, so route by SourceCardId rather than selecting the
            # first matching row.
            source_uid = None
            if isinstance(inner_bytes, bytes):
                scid_pos = inner_bytes.find(b"SourceCardId")
                if scid_pos >= 0:
                    uid_pos = inner_bytes.find(b"m_UID64", scid_pos)
                    if uid_pos >= 0:
                        rest = inner_bytes[uid_pos + 7:]
                        parts = rest.split(b";", 6)
                        if len(parts) >= 5:
                            try:
                                source_uid = struct.unpack(
                                    '<Q', bytes.fromhex(
                                        parts[4].decode("ascii")))[0]
                                if (source_uid & 0xFF) != 1:
                                    source_uid = None
                            except (ValueError, TypeError, struct.error):
                                source_uid = None
            src_row = None
            if source_uid is not None:
                rows = db_rules_port_cards_with_ability(
                    session.session_id, self.user_profile["id"], ability_guid,
                    source_uid)
                src_row = rows[0] if rows else None
            if src_row is None and source_uid is None:
                # Older clients may omit the source field. Preserve the
                # unambiguous single-copy case, but never guess between
                # multiple copies of the same ability.
                matches = db_rules_port_cards_with_ability(
                    session.session_id, self.user_profile["id"], ability_guid)
                if len(matches) == 1:
                    src_row = matches[0]
            if src_row or not champ_owned:
                if src_row:
                    self._activate_troop_ability(
                        session, pl_t, ai_t, bstate,
                        int(src_row[0]), ability_guid, inner_bytes)
                    if (not bstate.get("stack") and
                            not bstate.get("pending_choice") and
                            not bstate.get("pending_deck_search") and
                            not bstate.get("pending_trigger") and
                            not bstate.get("pending_discard_ability") and
                            _be.current_phase(bstate) in (
                                game_engine.ETurnPhases.FirstMainPhase,
                                game_engine.ETurnPhases.SecondMainPhase,
                                game_engine.ETurnPhases.DeclareCombatPriorityWindow,
                                game_engine.ETurnPhases.DeclareAttackPriorityWindow,
                                game_engine.ETurnPhases.DeclareDefensePriorityWindow,
                                game_engine.ETurnPhases.FirstStrikePriorityWindow)):
                        # A manual troop ability can change the resources or
                        # board targets available to a QuickAction.  Rebuild
                        # the option list in the same priority window; leaving
                        # the pre-activation list in place makes cards such as
                        # Strength of the Redwood remain hidden after Howling
                        # Brave generates a resource.
                        phase = _be.current_phase(bstate)
                        if phase in (game_engine.ETurnPhases.FirstMainPhase,
                                     game_engine.ETurnPhases.SecondMainPhase):
                            self._push_main_phase_options(session, pl_t, ai_t)
                        else:
                            self._push_phase_options_empty(session, pl_t, ai_t)
                    self._push_transaction_ack(session)
                    handled = True

        # If a class-23 discard prompt is pending and this activation is
        # the DiscardACard follow-up, this is the player's card choice —
        # discard it and finish, do NOT re-run the draw/class-23 flow.
        if not handled:
            bstate = _be.load_state(session)
            self._current_bstate = bstate
        if (bstate.get("pending_discard_ability") and
                ability_guid == bstate.get("pending_discard_ability")):
            # Extract the chosen card (last Card-type UID in the TargetMap).
            chosen_uid = None
            if isinstance(inner_bytes, bytes):
                for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                    try:
                        uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                        if (uid64 & 0xFF) == 1:
                            chosen_uid = int(uid64)
                    except Exception:
                        continue
            if not chosen_uid:
                chosen_uid = bstate.get("pending_discard_scid")
            row = None
            if chosen_uid:
                from pvp_db import db_rules_port_hand_card
                row = db_rules_port_hand_card(
                    session.session_id, chosen_uid, self.user_profile["id"])
            if row and chosen_uid is not None:
                # Discard to the card's OWNER (a Mind Grasp steal returns
                # to the AI's graveyard; a player card to the player's).
                gd_owner, owner_uid = self._discard_card_to_owner(
                    session, pl_t, ai_t, int(chosen_uid))
                tpl_d = "00000000-0000-0000-0000-000000000000"
                # _discard_card_to_owner returns a Game that carries only
                # the card events (no PlayerUpdated fields), so overlay the
                # live resources/health onto it for the PlayerUpdated below.
                # A bare Game would default them to 0/20, wiping the UI.
                if gd_owner:
                    gd = gd_owner
                    gd.player_resources = bstate.get("player_resources", 0)
                    gd.player_total_resources = bstate.get("player_total_resources", 0)
                    gd.player_threshold = dict(bstate.get("player_threshold", {}))
                    gd.player_charges = bstate.get("player_charges", 0)
                    gd.player_spell_points = bstate.get("player_spell_points", 0)
                    gd.player_health = bstate.get("player_health", 20)
                    gd.ai_health = bstate.get("ai_health", 10)
                else:
                    gd = self._fresh_game(session, pl_t, ai_t, bstate)
                gd.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
                # Re-grant priority so the player can keep playing after the
                # discard resolves (the class-23 prompt consumed the window).
                gd.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
                self._send_battle_events(session, gd, pl_t)
                self._push_main_phase_options(session, pl_t, ai_t)
                log_req(f"    Discard (class-23 follow-up): {chosen_uid} owner={owner_uid} ({tpl_d})")
            else:
                log_req(f"    Discard (class-23 follow-up): no hand card uid={chosen_uid}")
            bstate.pop("pending_discard_ability", None)
            bstate.pop("pending_discard_ability_instance_id", None)
            bstate.pop("pending_discard_target_template", None)
            bstate.pop("pending_discard_scid", None)
            _be.save_state(session, bstate)
            self._push_transaction_ack(session)
            handled = True
            return True  # skip the rest of this handler

        # If the ability's BOM includes a discard, the client prompted the
        # player to pick a hand card (TargetInstance attached to the
        # option); the chosen card arrives in the activation's TargetMap.
        # Extract its card_uid so we can move it to discard after the draw.
        discard_card_uid = None
        if ability_guid and not handled and isinstance(inner_bytes, bytes):
            needs_discard = self._bom_has_discard(ability_guid)
            if needs_discard:
                # The TargetMap embeds the chosen card's SessionCardId as a
                # m_UID64 with UID type 1 (Card). It appears after the
                # SourceCardId; take the LAST such UID in the transaction.
                for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                    try:
                        uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                        if (uid64 & 0xFF) == 1:
                            discard_card_uid = int(uid64)
                    except Exception:
                        continue
                # Fall back to auto-pick if no target was found in the
                # transaction (e.g. client didn't attach a TargetMap).
                if not discard_card_uid:
                    from pvp_db import db_rules_port_first_hand_card
                    discard_card_uid = db_rules_port_first_hand_card(
                        session.session_id, self.user_profile["id"])
                log_req(f"    Ability discard target: card_uid={discard_card_uid}")

        # Look up cost and deduct from battle state. Costs live on the
        # top-level granted ability in talent_abilities (the BOM's head);
        # ability_effects expands it into the leaf effect chain.
        # Guarded by `not handled`: a warzone-troop manual ability (e.g.
        # Shift) was already resolved by _activate_troop_ability (it pays
        # RESOURCE cost). Falling through here would re-resolve it and
        # push a second transaction ack.
        _be = self._checkpoint_engine(session)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        if ability_guid and not handled:
            from db import db_talent_ability_costs
            talent_cost = db_talent_ability_costs(ability_guid)
            trow = talent_cost[:2] if talent_cost else None
            if trow is None:
                # Champion signature charge powers aren't talents; their
                # costs come from champion_abilities (gamedata seed).
                from pvp_db import db_champion_ability_costs
                crow = db_champion_ability_costs(ability_guid)
                trow = (crow[0], crow[1]) if crow else None
            cc = trow[0] if trow else 0
            sc = trow[1] if trow else 0
            charges = bstate.get("player_charges", 0)
            sp = bstate.get("player_spell_points", 0)
            # Spell-power escalation: each use permanently bumps that
            # spell's SP cost by +1 (mirrors the client's
            # IncrementSpellPointCostModifier, Session.cs:1154).
            # Applies ONLY to spell powers (spell_cost > 0) — a charge
            # power must not accumulate an SP surcharge.
            sp_uses = bstate.get("player_sp_uses", {}) or {}
            used = int(sp_uses.get(ability_guid, 0))
            eff_sc = sc + (used if sc > 0 else 0)
            graph = _records_ability_graph(self, ability_guid)
            player_champ_scid = getattr(self, "_player_champ_scid", None)
            if not isinstance(player_champ_scid, game_engine.SessionCardId):
                log_req("    Champion ability rejected: player champion card id is unavailable")
                self._push_transaction_ack(session)
                return True
            champion_uid = int(player_champ_scid.uid.uid64)
            available = self._champion_ability_available(
                bstate, ability_guid, graph, source_uid=champion_uid)
            if (charges >= cc and sp >= eff_sc and available
                    and self._champion_thresholds_met(ability_guid, bstate)):
                # ONE-SHOT (m_UsesPerGame) powers are spent on activation;
                # the option list reads this count so the ability disappears.
                self._consume_champion_ability_use(
                    bstate, ability_guid, graph, source_uid=champion_uid)
                from rules_port.resources import apply_resource_change
                charge_change = apply_resource_change(
                    bstate, "player", "chargepoints", -cc)
                bstate["player_spell_points"] = sp - eff_sc
                if sc > 0:
                    sp_uses[ability_guid] = used + 1
                    bstate["player_sp_uses"] = sp_uses
                g = self._fresh_game(session, pl_t, ai_t, bstate)
                # Push charge/SP deduction events
                ev_chg = game_engine.ChampionChargePointsChangedSessionEventArgs()
                ev_chg.player_id = pl_t; ev_chg.operation = 2; ev_chg.delta = cc
                ev_chg.new_value = charge_change.new_value; g._push(ev_chg)
                if eff_sc:
                    ev_sp = game_engine.ChampionSpellPointsChangedSessionEventArgs()
                    ev_sp.player_id = pl_t; ev_sp.operation = 2; ev_sp.delta = eff_sc
                    ev_sp.new_value = bstate["player_spell_points"]; g._push(ev_sp)
                # Keep the escalated SP cost in the authoritative state and
                # CardDef.  Do not emit CardUpdated for the champion here:
                # the client treats a Champions collection update as a
                # location change and can leave a giant copy of the champion
                # behind when this ability is also pushed onto the chain.
                if player_champ_scid and sp_uses.get(ability_guid):
                    cdef = g.card_defs.get(player_champ_scid)
                    if cdef is not None:
                        for ag2, uses2 in sp_uses.items():
                            if int(uses2) > 0:
                                cdef.spell_point_cost_mods[game_engine.ResourceId.from_str(ag2)] = int(uses2)
                # Push the ability onto the CHAIN (the client's stack). The
                # BOM (draw / discard / etc.) executes when the chain
                # resolves (both players pass). One chain item per top-level
                # ability — its sub-effects (e.g. Soothsaying's draw +
                # discard) are bundled and resolve together, so a counterspell
                # has a single entry to cancel.
                champ_scid = player_champ_scid
                # The ability's chosen target (last Card-type UID in the
                # transaction, e.g. the troop for Dimmid's Lifedrain).
                target_uid = None
                all_target_uids = []
                if isinstance(inner_bytes, bytes):
                    for m_du in re.finditer(
                            rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});',
                            inner_bytes):
                        try:
                            uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                            if (uid64 & 0xFF) == 1:
                                all_target_uids.append(int(uid64))
                        except Exception:
                            continue
                selection = self._select_champion_activation_targets(
                    session, bstate, ability_guid,
                    champ_scid.uid.to_uint64(), all_target_uids)
                if selection is None:
                    log_req(f"    Champion ability {ability_guid[:8]}: "
                            "missing/illegal payment or effect target")
                    return True
                target_uid, sacrifice_uids = selection
                for sacrifice_uid in sacrifice_uids:
                    self._sacrifice_troop(
                        g, session, pl_t, ai_t, sacrifice_uid,
                        expected_owner_id=self.user_profile.get("id")
                        if isinstance(self.user_profile, dict) else None)
                if all_target_uids:
                    # Multi-target champion powers (e.g. Bun'jitsu's
                    # "Void two ready troops you control") carry every
                    # chosen card; the resolver voids them and reads
                    # their stats for the summoned-token buffs.
                    bstate["champion_void_uids"] = all_target_uids
                bstate["player_mod_target"] = target_uid
                # Clear the previous PlayerOptionList before the chain event
                # reaches the client. AbilityPushedOnChain renders the
                # champion; CanActivateAbility reads the cached options.
                g.push_options(pl_t, [])
                _be.stack_push(bstate, {
                    "kind": "ability", "ability_guid": str(ability_guid),
                    "source_uid": champ_scid.uid.to_uint64(),
                    "target_uid": target_uid,
                    "instance_id": 1,
                })
                g.push_ability_on_chain(champ_scid,
                                        game_engine.ResourceId.from_str(str(ability_guid)))
                _be.save_state(session, bstate)
                # Push PlayerUpdated for BOTH players so the client refreshes
                # hand AND deck counts (OnPlayerUpdated -> BattleAnimationSetCardCounts).
                g.player_resources = bstate.get("player_resources", 0)
                g.player_total_resources = bstate.get("player_total_resources", 0)
                g.player_threshold = dict(bstate.get("player_threshold", {}))
                g.player_charges = bstate.get("player_charges", 0)
                g.player_spell_points = bstate.get("player_spell_points", 0)
                g.ai_resources = bstate.get("ai_resources", 0)
                g.ai_total_resources = bstate.get("ai_total_resources", 0)
                g.ai_threshold = dict(bstate.get("ai_threshold", {}))
                g.ai_charges = bstate.get("ai_charges", 0)
                g.ai_spell_points = bstate.get("ai_spell_points", 0)
                g.push_player_updated(pl_t, champ_id=player_champ_scid)
                g.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
                # The chain card is added by a client animation queued from
                # AbilityPushedOnChain.  If ResolveTopOfChain is sent in this
                # same packet, UIBattle.CheckForMissingPriorityWindowState()
                # runs before that animation registers the card and pushes an
                # inactive priority state.  The client then displays Zuba on
                # the chain but has no usable priority.  Normal is still the
                # correct priority handoff; the server's pass handler resolves
                # the pending chain item, and avoids that client-side race.
                g.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
                self._send_battle_events(session, g, pl_t)
                # While the chain is non-empty expose only instant-speed
                # responses. Main-phase cards and BasicAction champion powers
                # return after the chain empties.
                self._push_phase_options_empty(session, pl_t, ai_t)
                log_req(f"    Ability activated on chain: charges {charges}->{bstate['player_charges']}, SP {sp}->{bstate['player_spell_points']}")
            elif not available:
                log_req(f"    Ability {str(ability_guid)[:8]} already used "
                        "(ONE-SHOT / UsesPerGame)")
            else:
                log_req(f"    Cannot afford ability: need {cc} charges/{sc} SP, have {charges}/{sp}")
        elif not handled:
            log_req("    Ability activation: could not parse GUID")
        if not handled:
            # Always respond so the client isn't blocked (champion
            # abilities; troop abilities ack inside _activate_troop_ability).
            self._push_transaction_ack(session)
        return True

    def _dispatch_blocker_events(self, game, session, bstate, runners):
        """Emit the authored block triggers for ``(attacker, blockers)`` pairs.

        ``Session.EmitBlockerEvents``/``AssignBlockers`` queue
        ``CardBlockedEvent`` and ``CardAttackedOrBlockedEvent`` with the blocker
        as their source AND the blocked attacker as their target, then one
        ``CardWasBlockedEvent`` for the attacker.  The target is what
        target-side abilities test ("When this becomes blocked", e.g. Nameless
        Citizen burying the top four cards of each opposing deck).  Only the
        AI's own blocking dispatched these, so a human block never fired the
        blocked attacker's authored abilities.
        """
        pl_t, ai_t = game.player_uid, game.ai_uid
        for attacker_uid, blocker_uids in runners:
            try:
                attacker_uid = int(attacker_uid)
                blocker_values = [int(value) for value in blocker_uids or ()]
            except (TypeError, ValueError):
                continue
            if not blocker_values:
                continue
            for blocker_uid in blocker_values:
                blocker_owner = db_card_owner_id(
                    session.session_id, blocker_uid, conn=_db)
                self._dispatch_native_trigger(
                    game, session, pl_t, ai_t, bstate, "CardBlockedEvent",
                    blocker_uid, blocker_owner, target_card_id=attacker_uid)
                self._dispatch_native_trigger(
                    game, session, pl_t, ai_t, bstate,
                    "CardAttackedOrBlockedEvent", blocker_uid, blocker_owner,
                    target_card_id=attacker_uid)
            self._dispatch_native_trigger(
                game, session, pl_t, ai_t, bstate, "CardWasBlockedEvent",
                attacker_uid,
                db_card_owner_id(session.session_id, attacker_uid, conn=_db))

    def _handle_commit_defense_transaction(self, session, transaction,
                                            *, declarations=None,
                                            authoritative_port=None):
        """Declare the player's blockers for the current combat."""
        inner_bytes = transaction.inner_bytes
        if authoritative_port is not None:
            from rules_port.persistence import load_state, save_state
            load_checkpoint = load_state
            save_checkpoint = save_state
        else:
            import battle_engine as _be
            load_checkpoint = _be.load_state
            save_checkpoint = _be.save_state
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        bstate = load_checkpoint(session)
        self._current_bstate = bstate
        attackers = {int(k): int(v) for k, v in (bstate.get("ai_attackers") or {}).items()}
        from pvp_db import db_warzone_card_uids, db_card_state_value as db_card_state_raw
        player_troops = set(db_warzone_card_uids(session.session_id, self.user_profile["id"]))
        all_uids = []
        if declarations is not None:
            for attacker, blockers in declarations:
                try:
                    all_uids.append(int(attacker))
                    all_uids.extend(int(blocker) for blocker in blockers)
                except (TypeError, ValueError):
                    continue
        elif isinstance(inner_bytes, bytes):
            for m in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                try:
                    u = struct.unpack('<Q', bytes.fromhex(m.group(1).decode()))[0]
                    if (u & 0xFF) == 1:
                        all_uids.append(int(u))
                except Exception:
                    continue
        blockers_map = {}
        if declarations is not None:
            for attacker, blockers in declarations:
                try:
                    blockers_map[int(attacker)] = [int(blocker)
                                                   for blocker in blockers]
                except (TypeError, ValueError):
                    continue
        cur_attacker = None
        for u in all_uids:
            if declarations is not None:
                continue
            if u in attackers:
                cur_attacker = u
                blockers_map.setdefault(cur_attacker, [])
            elif cur_attacker is not None and u in player_troops:
                blockers_map[cur_attacker].append(u)
        if authoritative_port is not None:
            # RulesPort has already validated and staged blocker assignment
            # against its shared Combat objects.  Project that accepted map
            # directly; a second legacy can_block pass could discard a valid
            # blocker when its SQLite-derived state lagged the port snapshot.
            port_blockers = {}
            port_attackers = {}
            current_ai_attackers = {
                int(value) for value in (bstate.get("ai_attackers") or {})
            }
            for combat in authoritative_port.combat_manager.combats:
                attacker = combat.attacker
                if attacker is None:
                    continue
                attacker_id = int(getattr(
                    getattr(attacker, "session_card_id", attacker),
                    "uid64", getattr(attacker, "session_card_id", attacker)))
                if current_ai_attackers and attacker_id not in current_ai_attackers:
                    continue
                port_attackers[attacker_id] = combat
                port_blockers[attacker_id] = []
                for blocker in combat.blockers:
                    value = getattr(blocker, "session_card_id", blocker)
                    value = getattr(value, "uid64", value)
                    port_blockers[attacker_id].append(int(value))
            if port_attackers:
                attackers = {
                    uid: int((bstate.get("ai_attackers") or {}).get(
                        str(uid), (bstate.get("ai_attackers") or {}).get(uid, 0)))
                    for uid in port_attackers
                }
                blockers_map = port_blockers
        else:
            # Legacy-only compatibility path: retain the metadata-driven
            # blocker legality checks for unclassified requests.
            from rules_port.combat_rules import can_block
            for u in list(blockers_map):
                blockers_map[u] = [b for b in blockers_map[u]
                                   if can_block(_db, session.session_id,
                                                bstate, u, b)]
        bstate["ai_blockers"] = {str(k): [str(b) for b in v]
                                 for k, v in blockers_map.items()}
        block_uids = {b for v in blockers_map.values() for b in v}
        if block_uids:
            db_bulk_blocker_state(session.session_id, list(block_uids))
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        player_champ_scid = getattr(self, "_player_champ_scid", None) or game_engine.SessionCardId(pl_t)
        combats = []
        for u in attackers:
            scid = game_engine.SessionCardId(game_engine.UID(u))
            combat_id = self._wire_combat_id_for_attacker(
                session, ai_t, u, u & 0xFFFF)
            blockers = [game_engine.SessionCardId(game_engine.UID(int(b)))
                        for b in blockers_map.get(u, [])]
            game.push_blockers_assigned(combat_id, scid, player_champ_scid, blockers)
            cs = game_engine.CombatSessionEventArgs()
            cs.player_id = ai_t
            cs.id = combat_id
            cs.attacker = scid
            cs.blockers = blockers
            combats.append(cs)
        self._dispatch_blocker_events(
            game, session, bstate,
            [(u, blockers_map.get(u, [])) for u in attackers])
        if combats:
            game.push_combat_listing(pl_t, combats)
        self._send_battle_events(session, game, pl_t)
        block_summary = ", ".join(
            hex(int(k)) + "->" + (hex(int(v[0])) if v else "none")
            for k, v in blockers_map.items())
        log_req(f"    CommitDefense: {len(attackers)} attacker(s), "
                f"blocks={block_summary}")
        if authoritative_port is not None:
            # The native defense transaction already staged the blockers and
            # consumed the defender's DeclareDefense priority before this
            # compatibility projection ran.  Drive that completed native
            # action into DeclareDefensePriorityWindow; re-entering the AI
            # turn loop here re-published DeclareDefense and left the client
            # submitting a second (often empty) defense transaction.
            save_checkpoint(session, bstate)
            self._advance_to_priority(session, pl_t, ai_t, bstate)
            return True
        start_idx = bstate.get("ai_turn_phase_idx", 0)
        bstate.pop("ai_turn_phase_idx", None)
        save_checkpoint(session, bstate)
        self._run_ai_turn(session, pl_t, ai_t, bstate, start_idx=start_idx)
        return True

    def _handle_commit_attack_transaction(self, session, transaction,
                                          *, declarations=None,
                                          authoritative_port=None):
        """Declare the player's attackers for the current combat."""
        inner_bytes = transaction.inner_bytes
        if authoritative_port is not None:
            from rules_port.persistence import load_state, save_state
            from rules_port.lifecycle import advance_checkpoint_phase
            load_checkpoint = load_state
            save_checkpoint = save_state
            advance_checkpoint = advance_checkpoint_phase
        else:
            import battle_engine as _be
            load_checkpoint = _be.load_state
            save_checkpoint = _be.save_state
            advance_checkpoint = _be.advance_phase
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        bstate = load_checkpoint(session)
        self._current_bstate = bstate
        # Extract the attacking card UIDs: every Card-type SessionCardId
        # in the transaction that is in the player's warzone. The
        # defender (AI champion) is not in the player's warzone.
        attacker_uids = []
        if declarations is not None:
            for _defender, attackers in declarations:
                try:
                    attacker_uids.extend(int(attacker) for attacker in attackers)
                except (TypeError, ValueError):
                    continue
        elif isinstance(inner_bytes, bytes):
            for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                try:
                    uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                    if (uid64 & 0xFF) == 1:  # Card type
                        attacker_uids.append(int(uid64))
                except Exception:
                    continue
        # RulesPort has already validated and staged these declarations.  In
        # that live path the host is only projecting the accepted decision;
        # re-running the old legality loop here used to silently discard
        # attackers when its legacy state view lagged the port view.
        from pvp_db import db_warzone_card_uids, db_card_state_value as db_card_state_raw
        from rules_port.static_rules import effective_attributes
        wz_uids = set(db_warzone_card_uids(
            session.session_id, self.user_profile["id"]))
        attackers = []
        if authoritative_port is not None:
            staged = {
                int(getattr(combat.attacker, "session_card_id",
                            getattr(combat.attacker, "uid", combat.attacker)))
                for combat in authoritative_port.combat_manager.combats
                if combat.attacker is not None
            }
            attackers = [uid for uid in attacker_uids if uid in staged]
        else:
            for uid in attacker_uids:
                if uid not in wz_uids:
                    continue
                state = int(db_card_state_raw(session.session_id, int(uid)) or 0)
                attrs = int(effective_attributes(
                    _db, session.session_id, bstate, int(uid)) or 0)
                if (state & (game_engine.ECardStates.Tapped |
                             game_engine.ECardStates.Attacking)):
                    continue
                if attrs & (game_engine.ECardAttributes.CantAttack |
                            game_engine.ECardAttributes.Defensive):
                    continue
                if not ((state & game_engine.ECardStates.StartedATurnOnYourSide)
                        or attrs & game_engine.ECardAttributes.Speed):
                    continue
                attackers.append(uid)
        # Persist declared attackers in battle state (DB) so combat
        # resolution and a reconnect can reconstruct them.
        ai_champ_uid = getattr(self, "_ai_champ_scid", None)
        ai_champ_uid64 = ai_champ_uid.uid.to_uint64() if ai_champ_uid else 0
        # Merge with any server-auto-declared ForceAttack ("Must
        # attack") attackers so a commit (or pass) never un-declares
        # them — e.g. Savage Raider is already attacking.
        existing = {int(k)
                    for k in (bstate.get("player_attackers") or {})}
        merged = existing | set(attackers)
        bstate["player_attackers"] = {
            str(u): str(ai_champ_uid64) for u in merged}
        save_checkpoint(session, bstate)
        log_req(f"    CommitTroopsToAttack: {len(attackers)} new attacker(s) -> "
                f"{[hex(u) for u in attackers]} (merged, total {len(merged)})")
        # Push the combat events: one AttackDeclared (class 27) per
        # attacker + a CombatListing (62) with a CombatSession (63) per
        # attacker (no blockers yet — the AI will pass DeclareDefense).
        # Also mark each attacker Attacking | HasAttacked | Tapped (the
        # client renders the tap + attack line from these states) and
        # persist in card_state for reconnect.
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        ai_champ_scid = ai_champ_uid if ai_champ_uid else game_engine.SessionCardId(ai_t)
        combats = []
        # Only the newly committed attackers get events here — the
        # auto-declared ForceAttack troops already got their
        # AttackDeclared / CombatListing when the phase opened.
        new_attackers = [u for u in attackers if u not in existing]
        for i, u in enumerate(new_attackers):
            cid = game_engine.SessionCardId(game_engine.UID(u))
            combat_id = self._wire_combat_id_for_attacker(
                session, pl_t, u, u & 0xFFFF)
            game.push_attack_declared(combat_id, pl_t, ai_champ_scid, cid)
            from pvp_db import db_card_state_value as db_card_state_raw
            trow = db_card_template_attrs_joined(session.session_id, int(u))
            tpl_guid = trow[0] if trow else None
            # Effective attributes = template static + instance-granted
            # (a SHIFTED Steadfast lives in card_attributes, not the
            # template — checking only ct.attributes would tap it).
            attrs = (trow[1] if trow and trow[1] else 0) | (trow[2] if trow and trow[2] else 0)
            # Mark the troop as attacking (and tapped UNLESS Steadfast —
            # Steadfast troops don't tap when attacking). Persist + push.
            state = (game_engine.ECardStates.Attacking |
                     game_engine.ECardStates.HasAttacked)
            if not (attrs & game_engine.ECardAttributes.Steadfast):
                state |= game_engine.ECardStates.Tapped
            from rules_port.host_mutations import apply_attacking_card_state
            apply_attacking_card_state(session, int(u), state)
            from pvp_db import db_card_state_value
            pushed_state = db_card_state_value(session.session_id, int(u))
            if not pushed_state:
                pushed_state = state
            from rules_port.host_mutations import project_attacking_card
            # Continuous attack-time keywords (for example Emberleaf
            # Duelist's "while this is attacking" Swiftstrike) are derived
            # from the authoritative state. Include them in the attack
            # CardUpdated so the client cache sees the same attributes used
            # by combat resolution.
            from rules_port.static_rules import effective_attributes
            attack_attributes = effective_attributes(
                _db, session.session_id, bstate, int(u))
            project_attacking_card(self, game, cid, pl_t, tpl_guid,
                                   pushed_state, attack_attributes)
            # Attacking normally exhausts the troop.  Emit the same typed tap
            # event used by activated and AI attacks so data-defined effects
            # such as Spider Nest's granted "when this exhausts" ability fire
            # for player-declared attackers too.  Steadfast attackers do not
            # become tapped and therefore must not emit this event.
            if state & game_engine.ECardStates.Tapped:
                self._dispatch_native_trigger(
                    game, session, pl_t, ai_t, bstate, "CardTappedEvent",
                    int(u), self.user_profile["id"])
            cs = game_engine.CombatSessionEventArgs()
            cs.player_id = pl_t
            cs.id = combat_id
            cs.attacker = cid
            cs.blockers = []
            combats.append(cs)
            # Fire "when this attacks" triggers (e.g. Chimera Guard
            # Outrider: +[ATK] equal to this troop's [DEF] this turn).
            for event_type in ("CardAttackedEvent",
                               "CardAttackedOrBlockedEvent"):
                self._dispatch_native_trigger(
                    game, session, pl_t, ai_t, bstate, event_type, u,
                    self.user_profile["id"])
            # Rage X: when this attacks it gets a permanent attack modifier.
            if authoritative_port is not None:
                from rules_port.context import EffectContext
                from rules_port.combat_effects import apply_rage
                apply_rage(EffectContext.from_rules_port(
                    game, session, _db, self, pl_t, ai_t, bstate,
                    "", ability=None), u)
            else:
                from abilities.framework.keywords.combat import apply_rage_keyword
                apply_rage_keyword(_db, session, self, game, pl_t, ai_t,
                                   bstate, u)
        if attackers:
            # The client raises one champion-scoped CardsAttackedEvent after
            # the complete attack declaration, carrying the total attacker
            # count for metadata conditions such as "three or more".
            player_champ = (getattr(self, "_player_champ_scid", None) or
                            game_engine.SessionCardId(pl_t))
            self._dispatch_native_trigger(
                game, session, pl_t, ai_t, bstate, "CardsAttackedEvent",
                int(player_champ.uid.uid64), self.user_profile["id"],
                attacker_count=len(attackers),
                attacker_uids=list(attackers))
        _db.commit()
        game.push_combat_listing(pl_t, combats)
        self._send_battle_events(session, game, pl_t)
        # Advance to DeclareAttackPriorityWindow (the next stop).
        advance_checkpoint(bstate)
        save_checkpoint(session, bstate)
        self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True

    def _handle_assign_damage_transaction(self, session, transaction,
                                          *, assignments=None,
                                          native_combat=False):
        """Resolve the client's combat damage assignment transaction."""
        inner_bytes = transaction.inner_bytes
        if native_combat:
            from rules_port.persistence import (current_phase, load_state,
                                                save_state)
            from rules_port.lifecycle import advance_checkpoint_phase
            load_checkpoint = load_state
            save_checkpoint = save_state
            current_checkpoint_phase = current_phase
            advance_checkpoint = advance_checkpoint_phase
        else:
            import battle_engine as _be
            load_checkpoint = _be.load_state
            save_checkpoint = _be.save_state
            current_checkpoint_phase = _be.current_phase
            advance_checkpoint = _be.advance_phase
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        bstate = load_checkpoint(session)
        self._current_bstate = bstate
        cur_phase = current_checkpoint_phase(bstate)
        native_phase_advanced = False
        if cur_phase == game_engine.ETurnPhases.AssignDamage:
            n_att = len(bstate.get("player_attackers") or {})
            # The AssignDamageOrderTransaction carries the player's
            # chosen blocker order (weakest-to-toughest) per combat:
            # m_AssignedDamageOrder -> DamageAssignment(CombatId,
            # ordered CardIds). Extract every card UID in stream order,
            # then order each attacker's blockers by their first
            # occurrence so resolve_combat assigns damage in that order.
            if assignments is not None:
                seq = []
                for _combat_id, card_ids in assignments:
                    try:
                        seq.extend(int(card_id) for card_id in card_ids)
                    except (TypeError, ValueError):
                        continue
            elif isinstance(inner_bytes, bytes):
                seq = []
                for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                    try:
                        v = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                        if (v & 0xFF) == 1:
                            seq.append(int(v))
                    except Exception:
                        continue
                blockers = {int(k): set(int(b) for b in (v or []))
                            for k, v in (bstate.get("ai_blockers") or {}).items()}
                order_map = {}
                for att, bset in blockers.items():
                    ordered = [u for u in seq if u in bset]
                    ordered += [b for b in bset if b not in ordered]
                    if ordered:
                        order_map[att] = ordered
                if order_map:
                    bstate["player_damage_order"] = {str(k): [str(b) for b in v]
                                                     for k, v in order_map.items()}
                    save_checkpoint(session, bstate)
            log_req(f"    AssignDamageOrder (AssignDamage step): resolving {n_att} attackers")
            if native_combat:
                bstate = self._resolve_combat_damage(
                    session, pl_t, ai_t, bstate)
                # Combat damage is resolved by the host projection, but the
                # native scheduler still owns the phase cursor.  Its priority
                # action is intentionally waiting for this transaction, so a
                # normal ``tick()`` cannot advance it.  Clear that consumed
                # window and perform the native transition before asking the
                # RulesPort driver to project the next input window;
                # advancing only the compatibility checkpoint leaves the
                # port at AssignDamage and makes Mono submit empty orders in
                # a loop.
                port = getattr(session, "_rules_port_session", None)
                if port is not None:
                    port.action_stack.clear()
                    port.advance_turn_phase()
                    phases = list(bstate.get("turn_phases") or ())
                    try:
                        bstate["phase_idx"] = phases.index(
                            port.current_turn_phase)
                    except ValueError:
                        pass
                    native_phase_advanced = True
            else:
                bstate = self._resolve_combat_damage(session, pl_t, ai_t, bstate)
            save_checkpoint(session, bstate)
            # If the AI's champion is dead, end the game (player wins).
            if bstate.get("ai_health", 10) <= 0:
                log_req(f"    AI defeated! ai_health={bstate['ai_health']}")
                self._finish_battle_gameend(
                    session, True, winners=[pl_t], losers=[ai_t])
                handled = True
                self._push_transaction_ack(session)
                return True
        elif cur_phase == game_engine.ETurnPhases.AssignFirstStrikeDamage:
            # Swiftstrike step: FirstStrike/DualStrike combatants deal
            # damage now; casualties are removed before the normal step.
            log_req("    AssignDamageOrder (Swiftstrike step): "
                    "resolving first-strike damage")
            if native_combat:
                bstate = self._resolve_combat_damage(
                    session, pl_t, ai_t, bstate, first_strike=True)
                port = getattr(session, "_rules_port_session", None)
                if port is not None:
                    port.action_stack.clear()
                    port.advance_turn_phase()
                    phases = list(bstate.get("turn_phases") or ())
                    try:
                        bstate["phase_idx"] = phases.index(
                            port.current_turn_phase)
                    except ValueError:
                        pass
                    native_phase_advanced = True
            else:
                bstate = self._resolve_combat_damage(
                    session, pl_t, ai_t, bstate, first_strike=True)
            save_checkpoint(session, bstate)
        else:
            log_req(f"    AssignDamageOrder ({cur_phase}): no damage yet, advancing")
        if bstate.get("pending_deck_search"):
            # A combat-death deck-search prompt is awaiting the player's
            # answer — do NOT advance to the next phase yet (the phase
            # packet would tear the client's target picker down).  The
            # SetAbilityActivationData answer advances the turn.
            save_checkpoint(session, bstate)
            self._push_transaction_ack(session)
            log_req("    AssignDamage: paused for deck-search answer")
            handled = True
            return True
        # Advance to the next phase (SecondMain after AssignDamage).  Native
        # RulesPort already performed that transition above; applying the
        # compatibility cursor a second time would skip the next phase.
        if not native_phase_advanced:
            advance_checkpoint(bstate)
        save_checkpoint(session, bstate)
        self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True

    def _handle_set_auto_pass_transaction(self, session, transaction):
        """Record and apply the client's one-turn auto-pass choice."""
        inner_bytes = transaction.inner_bytes
        if getattr(session, "_rules_port_session", None) is not None:
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        import re
        handled = False
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        # Tournament PvP has its own persisted two-player priority
        # state.  Do not feed its SetAutoPass through battle_engine:
        # that is the PvE/AI state machine and can advance a stale
        # turn_order, causing the other client to miss its resolve
        # priority.  Record F10 on the PvP state and let the normal
        # PvP pass route hand GreenLight between the clients.
        # m_PassingState: 1=Attack, 2=EndOfTurn, 3=EndPhase.
        st = 2
        if isinstance(inner_bytes, bytes):
            m = re.search(rb'm_PassingState;\d+;\d+;\d+;([0-9A-Fa-f]+);', inner_bytes)
            if m:
                try:
                    st = int(m.group(1), 16)
                except Exception:
                    pass
        if (session.session_name or "").startswith("tourney-"):
            try:
                from services.tournament_game import set_pvp_auto_pass
                if set_pvp_auto_pass(self, session, st):
                    self._push_transaction_ack(session)
                    handled = True
            except Exception as exc:
                log_req(f"    PvP SetAutoPass exception: {exc}")
        if handled:
            pass
        else:
            bstate = _be.load_state(session)
            self._current_bstate = bstate
            bstate["player_autopass"] = st
            _be.save_state(session, bstate)
            self._push_transaction_ack(session)
            log_req(f"    SetAutoPass: state={st} (player auto-passes to end of turn)")
            self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True


    def _handle_cancel_auto_pass_transaction(self, session, transaction):
        """Clear the client's one-turn auto-pass choice."""
        if getattr(session, "_rules_port_session", None) is not None:
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        handled = False
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        if (session.session_name or "").startswith("tourney-"):
            try:
                from services.tournament_game import pvp_load_state, pvp_save_state
                pstate = pvp_load_state(session)
                if pstate is not None:
                    if int(pstate.get("autopass_pid", 0)) == int(self.client_reck_id):
                        pstate.pop("autopass_pid", None)
                        pstate.pop("autopass_state", None)
                        pvp_save_state(session, pstate)
                    self._push_transaction_ack(session)
                    log_req("    PvP CancelAutoPass: auto-pass cleared")
                    handled = True
            except Exception as exc:
                log_req(f"    PvP CancelAutoPass exception: {exc}")
        if not handled:
            bstate = _be.load_state(session)
            self._current_bstate = bstate
            bstate.pop("player_autopass", None)
            _be.save_state(session, bstate)
            self._push_transaction_ack(session)
            log_req("    CancelAutoPass: auto-pass cleared")
        return True

    def _handle_set_turn_phases_transaction(self, session, transaction):
        """Persist client phase stops and acknowledge the transaction."""
        inner_bytes = transaction.inner_bytes
        self_stops = self._extract_enum_list(inner_bytes, "m_SelfTurnPhases", "m_OpponentTurnPhases")
        opp_stops = self._extract_enum_list(inner_bytes, "m_OpponentTurnPhases", None)
        # Persist for future battles.
        if self_stops is not None or opp_stops is not None:
            self._save_player_stops(self.user_profile["id"], self_stops, opp_stops)
        try:
            raw_to = session.turn_order
            if isinstance(raw_to, dict) and "turn_player" in raw_to:
                if getattr(session, "_rules_port_session", None) is not None:
                    from rules_port import lifecycle as _bse
                else:
                    import battle_engine as _bse
                bstate = _bse.load_state(session)
                if self_stops is not None:
                    bstate["player_self_stops"] = self_stops
                if opp_stops is not None:
                    bstate["player_opp_stops"] = opp_stops
                _bse.save_state(session, bstate)
                # If the AI turn is paused at an opponent-stop phase that
                # the player just removed from their stops, resume the AI
                # turn automatically instead of waiting for a pass.
                resume = bstate.get("ai_turn_phase_idx")
                if resume is not None:
                    paused_phase = _bse.TURN_PHASES[resume - 1]
                    if not _bse.is_opp_stop(bstate, paused_phase):
                        start_idx = resume
                        bstate.pop("ai_turn_phase_idx", None)
                        _bse.save_state(session, bstate)
                        log_req(f"    Stops updated — phase {paused_phase} no longer a stop; resuming AI turn")
                        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
                        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
                        self._run_ai_turn(session, pl_t, ai_t, bstate, start_idx=start_idx)
            elif (session.session_name or "").startswith("tourney-"):
                # PvP: persist each player's stops into the PvP state,
                # keyed by their reckoning pid, so the auto-advance can
                # respect "stop on Ready (self or opponent)" etc.
                from services.tournament_game import (pvp_load_state,
                                                      pvp_save_state,
                                                      pvp_session_lock)
                with pvp_session_lock(session):
                    _pstate = pvp_load_state(session)
                    if _pstate:
                        my_pid = int(self.client_reck_id)
                        if self_stops is not None:
                            _pstate[f"stops_self_{my_pid}"] = self_stops
                        if opp_stops is not None:
                            _pstate[f"stops_opp_{my_pid}"] = opp_stops
                        pvp_save_state(session, _pstate)
                self._pending_player_stops = (self_stops, opp_stops)
            else:
                self._pending_player_stops = (self_stops, opp_stops)
        except (ValueError, TypeError):
            self._pending_player_stops = (self_stops, opp_stops)
        log_req(f"    Captured phase stops: self={self_stops} opp={opp_stops}")
        # No 3055 events are pushed for SetTurnPhases — send an empty
        # sync packet so the client's one-at-a-time transaction pipeline
        # (m_HasPreviousTransactionBeenRespondedByServer) is unblocked;
        # otherwise the next transaction (incl. Withdraw) is dropped.
        self._push_transaction_ack(session)
        return True

    def _handle_pass_priority_transaction(self, session, transaction):
        """Resolve a priority pass through the gameplay service boundary."""
        inner_bytes = transaction.inner_bytes
        pass_turn_phase = transaction.pass_turn_phase
        handled = False
        if getattr(session, "_rules_port_session", None) is not None:
            from rules_port import lifecycle as _be
        else:
            import battle_engine as _be
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(game_engine.SERVICE_AI_UID_TYPE, 1000)
        # A pass arriving during setup (PreGame/PickGoesFirst/Mulligan,
        # before the player has kept their opening hand) has no turn-cycle
        # battle state yet — session.turn_order is still the default [].
        # Treat it as a no-op pass: acknowledge the transaction but do
        # NOT advance the turn, or it would corrupt the setup flow. The
        # client normally sends no transaction during PreGame (the
        # priority window auto-completes), but this is defensive.
        try:
            raw_to = session.turn_order
            setup_in_progress = not (isinstance(raw_to, dict) and "turn_player" in raw_to)
        except (ValueError, TypeError):
            setup_in_progress = True
        if setup_in_progress:
            from services.tournament_game import route_pvp_pass
            try:
                _rpv = route_pvp_pass(self, session)
            except Exception as _e:
                import traceback
                log_req(f"    PvP pass exception: {_e}")
                traceback.print_exc()
                _rpv = True
            if not _rpv:
                log_req("    Pass during setup phase — no-op (battle not started)")
            self._push_transaction_ack(session)
            handled = True
        else:
            bstate = _be.load_state(session)
            self._current_bstate = bstate
            log_req(f"    Pass priority: turn={bstate.get('turn_player')} priority=player "
                    f"phase={_be.current_phase(bstate)} (client={pass_turn_phase})")

            # The client also auto-passes non-stop phases, which race with
            # the server's own auto-advance. Ignore a stale pass whose
            # phase no longer matches the server's current phase.
            # A pending AI card can survive a phase-list rebuild (for example
            # when the AI gains an attack phase after its main phase). The
            # client still submits the phase it was shown when it received
            # priority; do not strand that chain item as a stale pass.
            pending_ai_chain = (
                not _be.stack_empty(bstate)
                and bstate.get("turn_player") == _be.AI
                and bstate.get("ai_turn_phase_idx") is not None)
            # A paused AI stop stores the resume cursor one phase past the
            # stop. If a late packet left phase_idx at an earlier AI phase,
            # reconcile it only when that cursor identifies the exact phase
            # shown by the client. This prevents a legitimate post-combat pass
            # from being discarded while still rejecting arbitrary stale
            # passes.
            if (pass_turn_phase is not None
                    and pass_turn_phase != _be.current_phase(bstate)
                    and bstate.get("turn_player") == _be.AI
                    and bstate.get("ai_turn_phase_idx") is not None):
                held = _be.ai_held_phase_context(bstate)
                if held is not None and held[0] == pass_turn_phase:
                    _held_phase, held_idx, held_phases = held
                    bstate["turn_phases"] = list(held_phases)
                    bstate["phase_idx"] = held_idx
                    _be.save_state(session, bstate)
                    log_req(f"    Reconciled stale AI phase_idx to held stop "
                            f"phase {pass_turn_phase}")
            if (pass_turn_phase is not None
                    and pass_turn_phase != _be.current_phase(bstate)
                    and not pending_ai_chain):
                log_req(f"    Ignored stale pass (client phase {pass_turn_phase} != {_be.current_phase(bstate)})")
                self._push_transaction_ack(session)
                handled = True
            elif (_be.stack_empty(bstate) and
                  bstate.get("turn_player") == _be.AI):
                # The human passed during the AI's turn (an opponent-stop
                # phase). Resume the AI turn from the next phase.
                start_idx = bstate.get("ai_turn_phase_idx", 0)
                bstate.pop("ai_turn_phase_idx", None)
                _be.save_state(session, bstate)
                log_req(f"    Human passed during AI turn — resuming AI at idx {start_idx}")
                self._run_ai_turn(session, pl_t, ai_t, bstate, start_idx=start_idx)
                handled = True
            elif _be.current_phase(bstate) in (game_engine.ETurnPhases.EndTurn,
                                               game_engine.ETurnPhases.Discard):
                # Defensive: end the human's turn and hand it to the AI.
                g_end2 = self._fresh_game(session, pl_t, ai_t, bstate)
                self._dispatch_native_trigger(
                    g_end2, session, pl_t, ai_t, bstate,
                    "TurnEndedEvent", None, self.user_profile["id"])
                self._clear_combat_damage(session, bstate)
                self._clear_expired_temporary_attributes(
                    session, bstate, self.user_profile["id"], "end_turn",
                    clear_stat_buffs=True)
                from pvp_db import db_rules_port_warzone_projection
                for cu, tpl, card_user_id, card_state in db_rules_port_warzone_projection(
                        session.session_id):
                    scid = game_engine.SessionCardId(game_engine.UID(cu))
                    _tpl, ct, _n, _c, _a, _d, _g = self._card_full_data(
                        g_end2, scid, tpl)
                    g_end2.push_card_updated(
                        scid, pl_t if card_user_id else ai_t,
                        game_engine.ECardCollections.Warzone, ct,
                        template_id=tpl,
                        state=int(card_state or 0))
                self._send_battle_events(session, g_end2, pl_t)
                if bstate.get("_rules_port_attached"):
                    from rules_port.lifecycle import complete_turn
                    next_player = complete_turn(bstate)
                else:
                    next_player = _be.next_turn_player(bstate)
                    bstate["turn_player"] = next_player
                    bstate["player_passed"] = False
                    bstate["ai_passed"] = False
                    bstate["phase_idx"] = 0
                    bstate["turn_phases"] = _be.BASE_TURN_PHASES
                    bstate[f"{next_player}_resource_played_this_turn"] = False
                    bstate[f"{next_player}_resource_plays_this_turn"] = 0
                _be.save_state(session, bstate)
                if next_player == _be.PLAYER:
                    self._advance_to_priority(session, pl_t, ai_t, bstate)
                    log_req("    Bonus turn kept player in control")
                else:
                    self._run_ai_turn(session, pl_t, ai_t, bstate)
                    log_req("    Turn passed to AI; AI turn played out")
            else:
                # A non-empty chain: passing resolves the top of the chain
                # instead of advancing the phase (troops/spells/abilities/
                # triggers). After resolving, if the chain is still non-empty
                # re-grant priority (the player can respond to the next
                # item); when it empties, push ChainEmpty and advance.
                if not _be.stack_empty(bstate):
                    _be.stack_set_pass(bstate, _be.PLAYER, True)
                    if not _be.stack_both_passed(bstate):
                        # The AI hasn't passed yet — it auto-passes, so
                        # count it now and resolve.
                        _be.stack_set_pass(bstate, _be.AI, True)
                    if _be.stack_both_passed(bstate):
                        item = _be.stack_pop(bstate)
                        _be.stack_reset_passes(bstate)
                        if item is None:
                            log_req("    RulesPort rejected empty chain item after both passes")
                            self._push_transaction_ack(session)
                            return True
                        # A FRESH game seeded from battle state so any
                        # PlayerUpdated pushed during resolution (e.g.
                        # after a heal) reports the real health/resources.
                        gs = self._fresh_game(session, pl_t, ai_t, bstate)
                        self._resolve_stack_item(session, pl_t, ai_t, bstate, item, gs)
                        if (_be.stack_empty(bstate) and
                                not bstate.get("pending_choice") and
                                not bstate.get("pending_trigger") and
                                not bstate.get("pending_deck_search") and
                                not bstate.get("pending_conversation")):
                            gs.push_chain_empty()
                        cur_phase = _be.current_phase(bstate)
                        pending_input = (bstate.get("pending_choice") or
                                         bstate.get("pending_trigger") or
                                         bstate.get("pending_deck_search") or
                                         bstate.get("pending_conversation"))
                        paused_ai_idx = (
                            bstate.get("ai_turn_phase_idx")
                            if (_be.stack_empty(bstate) and not pending_input and
                                bstate.get("turn_player") == _be.AI)
                            else None)
                        if paused_ai_idx is not None:
                            # The chain interrupted an AI phase. Once the
                            # human resolves the item, continue the AI from
                            # its saved cursor; granting another human
                            # greenlight here strands the turn between phases.
                            bstate.pop("ai_turn_phase_idx", None)
                            _be.save_state(session, bstate)
                            self._send_battle_events(session, gs, pl_t)
                            self._run_ai_turn(
                                session, pl_t, ai_t, bstate,
                                start_idx=int(paused_ai_idx))
                        else:
                            # Priority to the active player for the next item
                            # (or the next phase). ResolveTopOfChain keeps the
                            # pass button labelled "Resolve <Card>".
                            if (_be.stack_empty(bstate) and not pending_input and
                                    bstate.get("turn_player") == _be.PLAYER):
                                if cur_phase in (
                                        game_engine.ETurnPhases.FirstMainPhase,
                                        game_engine.ETurnPhases.SecondMainPhase):
                                    # A troop may have entered during this chain
                                    # (notably a Speed troop). Recompute before
                                    # rebuilding the green light so the next
                                    # prompt immediately offers ProceedToCombat.
                                    if cur_phase == game_engine.ETurnPhases.FirstMainPhase:
                                        bstate["player_has_ready_troop"] = self._player_can_attack_troops(session)
                                    bstate["turn_phases"] = _be.build_turn_phases(bstate)
                                    _be.save_state(session, bstate)
                                # The chain fully emptied on the player's turn:
                                # re-announce the current phase with the player as
                                # priority player so the client pushes a FRESH state.
                                # Without this the prior state's cached button tail
                                # (the resolved card's name, set only under
                                # ResolveTopOfChain) lingers — the pass button shows
                                # "Continue to Second Main Phase <CardName>".
                                if cur_phase is not None:
                                    gs.push_turn_phase(cur_phase, pl_t, pl_t)
                                    self._push_phase_options(
                                        session, pl_t, ai_t, cur_phase)
                            if not pending_input:
                                gs.push_green_light(pl_t, self._priority_context_for(
                                    cur_phase, bstate))
                            self._send_battle_events(session, gs, pl_t)
                        log_req(f"    Resolved chain item {item.get('kind')} "
                                f"({item.get('ability_guid', '')[:8] or hex(item.get('source_uid') or 0)})")
                    else:
                        self._push_transaction_ack(session)
                    handled = True
                else:
                    # The player passed a stop phase: advance one phase, then
                    # auto-pass non-stop phases until the next stop phase.
                    bstate["player_passed"] = True
                    bstate["ai_passed"] = True
                    # Passing the FirstMainPhase decides whether combat
                    # happens. Recompute AFTER any troop the player played
                    # this main phase has resolved to the warzone — a Speed
                    # (haste) troop can attack the turn it enters, so the
                    # decision is made here, not just at Prep.
                    if (_be.current_phase(bstate) ==
                            game_engine.ETurnPhases.FirstMainPhase and
                            bstate.get("turn_player") == _be.PLAYER):
                        bstate["player_has_ready_troop"] = self._player_can_attack_troops(session)
                        bstate["turn_phases"] = _be.build_turn_phases(bstate)
                        _be.save_state(session, bstate)
                        log_req(f"    FirstMain pass: combat={bstate['player_has_ready_troop']} "
                                f"phases={len(bstate['turn_phases'])}")
                    # If the pass ends the AssignDamage step, resolve the
                    # combat damage (unblocked attackers hit the champion)
                    # before moving on. AssignDamageOrderTransaction is the
                    # normal path; a plain pass here is the fallback.
                    if (_be.current_phase(bstate) in (
                            game_engine.ETurnPhases.AssignDamage,
                            game_engine.ETurnPhases.AssignFirstStrikeDamage)):
                        fs = (_be.current_phase(bstate) ==
                              game_engine.ETurnPhases.AssignFirstStrikeDamage)
                        bstate = self._resolve_combat_damage(
                            session, pl_t, ai_t, bstate, first_strike=fs)
                        _be.save_state(session, bstate)
                        if bstate.get("ai_health", 10) <= 0:
                            log_req(f"    AI defeated via pass at AssignDamage! ai_health={bstate['ai_health']}")
                            self._finish_battle_gameend(
                                session, True, winners=[pl_t], losers=[ai_t])
                            self._push_transaction_ack(session)
                            return True
                        if bstate.get("pending_deck_search"):
                            # Pause for the deck-search answer; the
                            # follow-up advances the phase.
                            self._push_transaction_ack(session)
                            log_req("    Pass at AssignDamage: paused for deck-search answer")
                            handled = True
                            return True
                    _be.advance_phase(bstate)
                    _be.save_state(session, bstate)
                    self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True

    # This legacy router covers many independently handled wire data types;
    # Pyright's control-flow complexity limit trips before analyzing its body.
    def _handle_service_request_legacy(self, target, instance, data_type, reqid,  # pyright: ignore[reportGeneralTypeIssues]
                                       comp, session_id, conh, inner_obj,
                                       inner_bytes, raw_data_wrapper_bytes=b""):
        log_req(f"    SR: target={target} dt={data_type}")
        # === Dispatch to service modules (Layer 2: extract logic) ===
        try:
            if _dispatch_service(self, data_type, target, instance, reqid, comp,
                                 session_id, conh, inner_obj, inner_bytes):
                return
        except Exception as e:
            log_req(f"    Dispatch error for dt={data_type}: {e}")
            if data_type == 6011:
                try:
                    from services.store import send_purchase_failure
                    send_purchase_failure(
                        self, target, instance, reqid, comp, session_id, conh,
                        SERVICE_MAIL_UID, inner_obj.get("Id", 1),
                        "Purchase could not be completed.")
                except Exception as response_error:
                    log_req(f"    PurchaseItem failure response error: "
                            f"{response_error}")
                return

        if data_type == 6011:
            # Registry misses must still use the transactional purchase path.
            # Never fall through to the legacy debit-and-grant implementation.
            try:
                from services.store import handle_purchase
                handle_purchase(
                    self, target, instance, reqid, comp, session_id, conh,
                    inner_obj, SERVICE_MAIL_UID, log_req=log_req)
            except Exception as e:
                log_req(f"    PurchaseItem handler unavailable: {e}")
                try:
                    from services.store import send_purchase_failure
                    send_purchase_failure(
                        self, target, instance, reqid, comp, session_id, conh,
                        SERVICE_MAIL_UID, inner_obj.get("Id", 1),
                        "Purchase could not be completed.")
                except Exception as response_error:
                    log_req(f"    PurchaseItem failure response error: "
                            f"{response_error}")
            return

        handler_fn = LEGACY_HANDLERS.get(data_type)
        if handler_fn is not None:
            return handler_fn(self, target, instance, data_type, reqid, comp,
                              session_id, conh, inner_obj, inner_bytes,
                              raw_data_wrapper_bytes)
        log_req(f">>> Unhandled DataType={data_type}")
        log_req(f"    target={target} instance={instance} inner_keys={list(inner_obj.keys()) if isinstance(inner_obj, dict) else '?'}")
        if inner_obj:
            log_req(f"    inner_obj={ {k: str(v)[:100] for k, v in inner_obj.items()} }")
    
    

from application.profile_stream import bind_runtime_globals
bind_runtime_globals(globals())


def _warm_runtime_records():
    """Load the immutable Records sections before accepting game traffic.

    The store is intentionally lazy for tools and tests, but a live trigger
    event must not pay the several-second cost of parsing the global ability
    index on the client's troop-resolution request.
    """
    started = time.perf_counter()
    from gamedata import DEFAULT_RECORD_STORE
    for section in ("AbilityTemplate", "AbilityTargetTemplate",
                    "AbilityEffectTemplate"):
        DEFAULT_RECORD_STORE.load(section)
    log(f"Warmed RulesPort Records in "
        f"{(time.perf_counter() - started) * 1000.0:.1f}ms")


def main():
    global _reload_requested
    enable_debugpy(log)
    try:
        from db import start_lock_watchdog
        start_lock_watchdog()
    except Exception as _exc:
        log(f"Could not start DB lock watchdog: {_exc}")
    _warm_runtime_records()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(5)
    # SIGUSR1 requests a runtime-module reload. The signal handler only sets a
    # flag; importlib work runs in the
    # main loop between accepts, keeping signal handling async-signal-safe and
    # leaving existing client sockets connected.
    if hasattr(signal, "SIGUSR1"):
        def _request_reload(_signum, _frame):
            global _reload_requested
            _reload_requested = True
        signal.signal(signal.SIGUSR1, _request_reload)
        log("HConnect reload hook ready (send SIGUSR1 to reload runtime modules)")
    srv.settimeout(1.0)
    log(f"HConnect server listening on port {port}")

    addr = ("0.0.0.0", 0)
    try:
        while True:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                conn = None
            if _reload_requested:
                _reload_requested = False
                try:
                    import commands as _commands
                    result = _commands.reload_runtime_modules()
                    log(f"Signal reload complete: {result}")
                except Exception as exc:
                    import traceback
                    log("Signal reload failed: " + "".join(
                        traceback.format_exception(exc)))
            if conn is None:
                continue
            handler = HCPHandler(conn, addr)
            t = threading.Thread(target=handler.handle, daemon=True)
            t.start()
    except KeyboardInterrupt:
        log("Shutting down...")
        srv.close()


if __name__ == "__main__":
    main()

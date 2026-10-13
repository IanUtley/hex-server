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
from services.chests import CLIENT_CHEST_UID_BASE, _CHEST_KEEP_COUNTS, ClientChest, _resolve_client_chest, _generate_chest_rewards, _grant_pack_inventory_rewards, _open_client_chests, _roll_pack_chest_rarities, _grant_reward_cards, _spin_client_chest, _encode_spin_response
from application.rules_port_timing import _rules_port_timing

from services.legacy_handlers import LEGACY_HANDLERS

from application.card_play import CardPlayMixin
from application.card_state import CardStateMixin
from application.combat import CombatMixin
from application.chain import ChainMixin
from application.turn_flow import TurnFlowMixin
from application.pending_choices import PendingChoicesMixin
from application.transactions import TransactionsMixin
from gamemodes.fra_battle import FraBattleMixin
from application.card_play import _NO_PERSISTED_GEM

DEFAULT_PORT = 9933
_reload_requested = False
DB_PATH = os.environ.get(
    "HEX_DB_PATH",
    os.path.join(os.path.dirname(__file__), "hconnect.db"),
)


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


class HCPHandler(ProfileStreamMixin, CardPlayMixin, CardStateMixin, CombatMixin, ChainMixin, TurnFlowMixin, PendingChoicesMixin, TransactionsMixin, FraBattleMixin):
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

        elif target == "Session" and instance == "whsp":
            from services.chat import handle_whisper_message
            self._refresh_log_session_context()
            handle_whisper_message(self, body)

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

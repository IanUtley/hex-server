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
import uuid
import hashlib
import signal
from binascii import hexlify, unhexlify
from datetime import datetime, timezone

# The live server is launched as ``__main__``, while service modules import
# ``hconnect_server`` for shared handler state.  Alias the running module so
# those imports do not create a second module with a separate client registry.
if __name__ == "__main__":
    sys.modules.setdefault("hconnect_server", sys.modules[__name__])
import game_session
import game_engine
import encoder
import campaign
from application import ApplicationCommandDispatcher
from application.commands import (JoinSessionCommand, RemoveSessionCommand,
                                  ServiceRequestCommand,
                                  SetSessionStateCommand,
                                  StartEncounterCommand, StartSessionCommand)
from application.player_transactions import classify_player_transaction
import gamemodes.tournament_server as tournament_server
from gamemodes.tournament_engine import (
    _encode_enter_tournament_error, _make_deck_data,
    _tournament_format_bitmask, _tournament_session_flags,
    _tournament_style_bitmask,
    build_tournament_desc_json, uid_instance,
    start_waiting_room_game,
    build_waiting_room_data, build_tournament_info_data,
    push_tournament_room_data, record_tournament_forfeit,
    player_handlers, player_handler_lock, player_decks,
)

DEFAULT_PORT = 9933
_reload_requested = False
DB_PATH = os.environ.get(
    "HEX_DB_PATH",
    os.path.join(os.path.dirname(__file__), "hconnect.db"),
)


def _profile_feature_flags():
    """Return profile-stream feature strings sent to every authenticated client.

    The historical server always enabled the developer console with
    ``allowcon``. Keep that behavior when the environment variable is absent;
    when it is present, its comma/space/semicolon-separated values replace the
    default list so deployments can choose an explicit feature set.
    """
    raw = os.environ.get("HEX_PROFILE_FLAGS")
    if raw is None:
        return ("allowcon",)
    flags = []
    for flag in re.split(r"[,;\s]+", raw.strip()):
        if flag and flag not in flags:
            flags.append(flag)
    return tuple(flags)


PROFILE_FEATURE_FLAGS = _profile_feature_flags()

# How long the AI "thinks" (seconds) before acting on its turn. Each client
# connection runs in its own thread, so this only delays that player's battle
# events; incoming packets just queue in the socket buffer meanwhile.
AI_PHASE_DELAY = 1.0  # pause between AI phase pushes so the client renders them

# All talent data (names, abilities, descriptions) is in the talent_data DB table,
# populated from gamedata during build.

def _target_count_from_text(game_text):
    """Parse how many targets a "X troops you control" cost/target template
    needs: "a troop" = 1, "two/three/... troops" = the number. Defaults to 1."""
    import re as _re
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    m = _re.search(r'\b(one|two|three|four|five|six|seven|eight|nine|ten)\b', (game_text or "").lower())
    return words[m.group(1)] if m else 1


def _talent_ability_guid(talent_guid: str) -> str | None:
    """Resolve a champion talent GUID to its actual ability GUID via DB."""
    row = _db.execute(
        "SELECT ability_guid FROM talent_data WHERE talent_guid=? AND has_ability=1",
        (talent_guid,)).fetchone()
    return row[0] if row else None
from db import _db, log, log_req, hexdump
from db import (db_template_by_guid, db_card_ability_list, db_card_uses,
                db_bump_card_use, db_card_state, db_warzone_troop_count,
                db_card_stat_mods)
from db import (player_id_from_name, player_id_from_steam, display_name_from_identity,
                db_tournament_by_id, db_tournament_signup_by_player,
                db_tournament_signups_by_tournament,
                db_game_session_pids,
                db_get_or_create_user, db_get_stardust, db_update_resources,
                db_get_user, db_get_user_by_client_auth_id,
                db_add_card, db_record_purchase, db_add_inventory, db_get_inventory,
                db_get_arena_state, db_get_fra_challengers,
                db_record_arena_fight, db_delete_game_session,
                db_get_player_champion_guid, db_get_charge_power, db_send_email, db_save_deck,
                db_update_deck, db_get_decks, db_redeem_code, db_get_store_items,
                db_save_session, db_find_session, db_store_chat, db_get_recent_chat,
                db_session_state_hash, db_record_session_transaction,
                db_complete_session_transaction, _record_session_events,
                STARDUST_TEMPLATES, CHEST_TEMPLATE)
from static import CRAYBURN_PACK_CARD_SEEDS

import domain.constants as _dc
_dc.event_logger = _record_session_events
# Game imported ``event_logger`` by value, so updating domain.constants alone
# does not update the callback used by Game.make_network_packet().  Bind it
# explicitly to the actual module containing Game.
import domain.game as _domain_game
_domain_game.event_logger = _record_session_events
game_engine.event_logger = _record_session_events

from ability import discover_abilities
discover_abilities()


def encode_inventory_item(buf, sizes, ft, template_guid, item_id, elem_idx, quantity=1, bound=True):
    return encoder.encode_inventory_item(buf, sizes, ft, template_guid, item_id, elem_idx, quantity, bound)


def encode_card_instance(buf, sizes, ft, guid, name, card_id, cost, atk, def_, idx):
    return encoder.encode_card_instance(buf, sizes, ft, guid, name, card_id, cost, atk, def_, idx)


def encode_deck_bits(buf, sizes, ft, did, dname, did_val, champ_did, card_guids, idx):
    return encoder.encode_deck_bits(buf, sizes, ft, did, dname, did_val, champ_did, card_guids, idx)


def encode_champion_bits_minimal(buf, sizes, ft, cu64, cname, cid, lvl, xp, cc, rc, gd, idx,
                                 last_campaign_id=0, last_deck_id=0, talents=None,
                                 pet_name=""):
    return encoder.encode_champion_bits_minimal(buf, sizes, ft, cu64, cname, cid, lvl, xp, cc, rc, gd, idx,
                                                last_campaign_id, last_deck_id, talents, pet_name)


def make_packet(headers: dict, body: bytes = b"") -> bytes:
    hdr_json = json.dumps(headers, separators=(",", ":")).encode("utf-8")
    rest_len = 4 + len(hdr_json) + 4 + len(body)
    return (
        IDENT
        + struct.pack("!I", rest_len)
        + struct.pack("!I", len(hdr_json))
        + hdr_json
        + struct.pack("!I", len(body))
        + body
    )

def parse_packet(data: bytes):
    if len(data) < 9:
        raise ValueError("Too short")
    if data[:5] != IDENT:
        raise ValueError(f"Bad ident: {data[:5]!r}")
    rest_len = struct.unpack("!I", data[5:9])[0]
    total = 5 + 4 + rest_len
    if len(data) < total:
        raise ValueError(f"Need {total} bytes, have {len(data)}")
    hdr_len = struct.unpack("!I", data[9:13])[0]
    hdr_end = 13 + hdr_len
    hdr_json = data[13:hdr_end]
    headers = json.loads(hdr_json.decode("utf-8"))
    body_len = struct.unpack("!I", data[hdr_end:hdr_end + 4])[0]
    body_start = hdr_end + 4
    body = data[body_start:body_start + body_len]
    return headers, body, total


# === DataWrapper ObjFmt parser (simplified, field-by-field extraction) ===

def parse_datawrapper(body):
    """
    Parse an ObjFmt-encoded DataWrapper.
    Returns dict with: request_id, data_type, raw_bytes, session_guid, comp, conh
    """
    sizes = []
    pos = len(body) - 1
    while pos >= 0:
        if body[pos] == 0x0a:
            size_part = body[pos+1:].decode("utf-8")
            sizes = [int(s) for s in size_part.split(";")]
            break
        pos -= 1
    if not sizes:
        raise ValueError("No size table found")

    type_table_start = sizes[0]
    type_end = body.index(0x0a, type_table_start)
    type_part = body[type_table_start:type_end].decode("utf-8")
    type_names = type_part.split(";")
    root_type = type_names[0] if type_names else "?"

    buf = memoryview(body)
    idx = 0

    def read_to_sep():
        nonlocal idx
        start = idx
        while idx < len(body) and body[idx] != 0x3b:
            idx += 1
        result = body[start:idx].decode("utf-8")
        if idx < len(body):
            idx += 1
        return result

    def parse_one(name_hint, f_num):
        nonlocal idx
        name = read_to_sep()
        f_size_idx = int(read_to_sep())
        f_type_idx = int(read_to_sep())
        num = int(read_to_sep())
        f_type = type_names[f_type_idx] if f_type_idx < len(type_names) else "?"

        if f_type == "System.Int64" and num == 0:
            return name, struct.unpack("<q", unhexlify(read_to_sep()))[0]
        elif f_type == "System.UInt64" and num == 0:
            return name, struct.unpack("<Q", unhexlify(read_to_sep()))[0]
        elif f_type == "System.Int32" and num == 0:
            return name, struct.unpack("<i", unhexlify(read_to_sep()))[0]
        elif f_type == "System.Byte" and num == 0:
            return name, int(read_to_sep(), 16)
        elif f_type == "System.Byte[]" and num == 0:
            raw_len = struct.unpack("!I", body[idx:idx+4])[0]
            idx += 4
            val = body[idx:idx+raw_len]
            idx += raw_len
            return name, val
        elif f_type == "System.Guid" and num == 0:
            guid_len = int(read_to_sep())
            val = body[idx:idx+guid_len].decode("utf-8")
            idx += guid_len
            return name, val
        elif f_type == "System.String" and num == 0:
            str_len = int(read_to_sep())
            val = body[idx:idx+str_len].decode("utf-8")
            idx += str_len
            return name, val
        elif f_type == "System.Boolean" and num == 0:
            val = (body[idx] == 0x31)
            idx += 1
            return name, val
        elif "ResourceId" in f_type or "UID" in f_type:
            # Complex types with sub-fields — skip gracefully
            for _ in range(num):
                parse_one("", 0)  # recursively skip sub-fields
            return name, {"__skipped__": f_type}
        elif f_type.startswith("System.Collections.Generic.List`1#") and num == 0:
            count = int(read_to_sep())
            elem_type = f_type.split("#", 1)[1] if "#" in f_type else ""
            vals = []
            for _ in range(count):
                ename = read_to_sep()  # element index (ignored)
                esize = int(read_to_sep())
                etype = int(read_to_sep())
                enum = int(read_to_sep())
                if elem_type == "System.UInt64":
                    v = struct.unpack("<Q", unhexlify(read_to_sep()))[0]
                    vals.append(v)
                elif elem_type == "System.Int32":
                    v = struct.unpack("<i", unhexlify(read_to_sep()))[0]
                    vals.append(v)
                elif elem_type == "System.String":
                    slen = int(read_to_sep())
                    v = body[idx:idx+slen].decode("utf-8")
                    idx += slen
                    vals.append(v)
                else:
                    read_to_sep()  # skip unknown value
            return name, vals
        elif "ResourceId" in f_type or "UID" in f_type:
            for _ in range(num):
                parse_one("", 0)
            return name, {"__skipped__": f_type}
        elif num > 0:
            sub = {}
            for _ in range(num):
                sn, sv = parse_one(name, 0)
                sub[sn] = sv
            return name, sub
        else:
            log(f"  Unhandled field {name}: type={f_type} num={num}")
            return name, f"<unhandled type={f_type} num={num}>"

    # Root field: name, size_ref, type_ref, num_props
    root_name = read_to_sep()
    size_idx = int(read_to_sep())
    type_idx = int(read_to_sep())
    num_props = int(read_to_sep())

    result = {"__type__": root_type}
    for _ in range(num_props):
        fn, fv = parse_one("", 0)
        result[fn] = fv

    return result


# === ObjFmt encoder ===

def encode_get_store_items_response():
    """Encode GetStoreItemsResponseArgs from DB store_items table."""
    items = db_get_store_items()
    return encode_store_response(items)

def encode_store_response(items):
    return encoder.encode_store_response(items)


def encode_store_item_set1_booster():
    return encode_get_store_items_response()  # deprecated, kept for compatibility


CARDS_DIR = "/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Hex_Data/Data/Sets"
_CARD_CACHE = {}  # set_id -> [(guid, name, rarity, cost, attack, defense)]

def _load_card_templates():
    global _CARD_CACHE
    if _CARD_CACHE:
        return _CARD_CACHE
    rows = _db.execute("SELECT guid, set_guid, name, rarity, cost, attack, defense, is_pve, no_pvp, card_type FROM card_templates").fetchall()
    if not rows:
        log("No card_templates in DB — run import_cards.py first")
        return _CARD_CACHE
    for guid, sid, name, rarity, cost, attack, defense, is_pve, no_pvp, card_type in rows:
        _CARD_CACHE.setdefault(sid, []).append((guid, name, rarity, cost, attack, defense, is_pve, no_pvp, card_type))
    log(f"Loaded cards from DB: {sum(len(v) for v in _CARD_CACHE.values())} cards across {len(_CARD_CACHE)} sets")
    return _CARD_CACHE

# PVP set GUIDs — sets that contain cards with non-Land/Epic/Promo rarities
_PVP_SET_GUIDS = None

def _get_pvp_sets():
    global _PVP_SET_GUIDS
    if _PVP_SET_GUIDS is not None:
        return _PVP_SET_GUIDS
    rows = _db.execute(
        "SELECT set_guid FROM card_templates "
        "WHERE is_pve=0 AND no_pvp=0 "
        "AND rarity IN ('Common','Uncommon','Rare','Legendary') "
        "GROUP BY set_guid"
    ).fetchall()
    _PVP_SET_GUIDS = set(r[0] for r in rows)
    return _PVP_SET_GUIDS

def _generate_booster(card_data, set_id):
    import random
    pool = card_data.get(set_id, [])
    # A mapped pack must never silently draw from another set.  Keep only
    # standard, directly collectible printings; generated Land templates such
    # as Bloodstone are not booster cards.
    pool = [
        c for c in pool
        if c[2] in ('Common', 'Uncommon', 'Rare', 'Legendary')
        and not c[6]
        and not c[7]
    ]
    if len(pool) < 17:
        return [(g, n, cost, atk, def_) for g, n, r, cost, atk, def_, _, _, _ in pool]
    
    commons = [x for x in pool if x[2] == 'Common']
    uncommons = [x for x in pool if x[2] == 'Uncommon']
    rares = [x for x in pool if x[2] == 'Rare']
    legendaries = [x for x in pool if x[2] in ('Legendary',)]
    
    if not commons: commons = list(pool)
    if not uncommons: uncommons = list(pool)
    if not rares: rares = list(pool)
    
    result = random.sample(commons, min(12, len(commons)))
    result += random.sample(uncommons, min(4, len(uncommons)))
    
    # ~11% chance of legendary
    if legendaries and random.random() < 0.11:
        result.append(random.choice(legendaries))
    else:
        result.append(random.choice(rares))
    
    random.shuffle(result)
    return [(g, n, cost, atk, def_) for g, n, r, cost, atk, def_, _, _, _ in result]


def _generate_crayburn_chest(card_data, chest_template_guid):
    """Return the authored five-card pool for a Crayburn reward chest.

    Unlike normal boosters, these cards are not selected by set or rarity.
    The pool is resolved through loaded card templates so the response uses
    the same cost/stat metadata as every other pack.
    """
    card_guids = CRAYBURN_PACK_CARD_SEEDS.get(chest_template_guid)
    if not card_guids:
        return None
    by_guid = {
        card[0]: card
        for cards in card_data.values()
        for card in cards
    }
    missing = [guid for guid in card_guids if guid not in by_guid]
    if missing:
        log(f"Crayburn chest {chest_template_guid} has missing card templates: {missing}")
    return [
        (card[0], card[1], card[3], card[4], card[5])
        for guid in card_guids
        if (card := by_guid.get(guid)) is not None
    ]


def _full_set_pool(pool):
    """Return the standard PvP printings used by a full-set grant.

    Epic and Promo templates are alternate-art or promotional printings, not
    part of the normal set collection.  Keep this filter metadata-driven so
    full-set grants stay aligned with the booster eligibility rules.
    """
    return [
        card for card in pool
        if card[2] in ('Common', 'Uncommon', 'Rare', 'Legendary')
        and not card[6]
        and not card[7]
    ]


def _roll_primal_upgrade(quantity, rng=None):
    """Roll a 2%-per-pack chance of upgrading a booster to its Primal pack.

    Returns (normal_qty, primal_qty) so mixed purchases grant the right number
    of each.  ``rng`` is injectable for tests (defaults to random.random).
    """
    if quantity <= 0:
        return (0, 0)
    import random as _random
    rand = rng or _random.random
    upgraded = sum(1 for _ in range(int(quantity)) if rand() < 0.02)
    return (int(quantity) - upgraded, upgraded)


def encode_objfmt_response(type_names, fields):
    return encoder.encode_objfmt_response(type_names, fields)


def encode_objfmt_string(s_value):
    return encoder.encode_objfmt_string(s_value)


def encode_session_state(session_id, session_name, min_players=2, max_players=2):
    return encoder.encode_session_state(session_id, session_name, min_players, max_players)


def encode_sync_event(packet):
    return encoder.encode_sync_event(packet)


def encode_challenger_list(challengers):
    return encoder.encode_challenger_list(challengers)


def encode_get_challengers_response(success, challengers):
    return encoder.encode_get_challengers_response(success, challengers)


def encode_login_stream_done():
    return encoder.encode_login_stream_done()


def encode_datawrapper(request_id, data_type, body_bytes, comp,
                       session_guid="00000000-0000-0000-0000-000000000000",
                       conh=0):
    return encoder.encode_datawrapper(request_id, data_type, body_bytes, comp, session_guid, conh)


def encode_get_unread_mail_count_response(unread_count=0):
    return encoder.encode_get_unread_mail_count_response(unread_count)


def encode_ping_mail_server_response(timestamp=None):
    return encoder.encode_ping_mail_server_response(timestamp)


def encode_profile_response(envelope_bytes):
    return encoder.encode_profile_response(envelope_bytes)


def compress_gzip(data):
    return encoder.compress_gzip(data)


def decompress_gzip(data):
    return encoder.decompress_gzip(data)


def make_uid(type_byte, instance_id):
    return encoder.make_uid(type_byte, instance_id)


def client_session_guid(handler):
    """Return the handler's cached RequestHandlerSessionId or the zero GUID."""
    return getattr(handler, 'client_req_session_id', None) or "00000000-0000-0000-0000-000000000000"


IDENT = b"~HCP~"

UID_TYPE = {
    "ServicePlayer": 244,
    "ServiceMail": 252,
    "ServiceProfile": 245,
    "ServiceGameSession": 246,
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
    for uid in list(_active_clients.keys()):
        _active_clients[uid] = [(h, t) for h, t in _active_clients[uid] if now - t < SESSION_TIMEOUT]
        if not _active_clients[uid]:
            del _active_clients[uid]
    # Also clean up ended game sessions from DB
    try:
        _db.execute("DELETE FROM game_sessions WHERE state='ended'")
        _db.execute("DELETE FROM game_cards WHERE session_id NOT IN (SELECT session_id FROM game_sessions)")
        _db.commit()
    except Exception:
        pass

def touch_session(handler):
    """Update the last-active time for a handler."""
    for uid, entries in _active_clients.items():
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

# ERace enum value -> deck name key
_RACE_DECK_MAP = {
    1: "Human", 2: "Elf", 3: "Coyotle", 4: "Orc",
    5: "Dwarf", 6: "ShinHare", 7: "Vennen", 8: "Necrotic",
}

_CHAMPION_CLASS_MAP = {
    1: "Mage", 2: "Warrior", 3: "Cleric", 4: "Rogue",
    5: "Warlock", 6: "Ranger", 7: "Boat",
}
_CHAMPION_GENDER_MAP = {1: "Male", 2: "Female", 3: "Other"}


def _default_talents_for_champion(race_val, class_val, gender_val):
    """Return the authored default talent list for a player template."""
    race_name = _RACE_DECK_MAP.get(int(race_val))
    class_name = _CHAMPION_CLASS_MAP.get(int(class_val))
    gender_name = _CHAMPION_GENDER_MAP.get(int(gender_val))
    if not race_name or not class_name or not gender_name:
        return []
    try:
        row = _db.execute(
            "SELECT default_talents FROM champion_templates "
            "WHERE race=? AND champion_class=? AND gender=? AND is_player=1 "
            "LIMIT 1",
            (race_name, class_name, gender_name),
        ).fetchone()
    except sqlite3.Error:
        return []
    if not row:
        return []
    try:
        values = json.loads(row[0] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(value).lower() for value in values] if isinstance(values, list) else []

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


# === Service dispatch registry (Layer 2: extract logic to services/gamemodes) ===
# Each entry maps a data_type to (module_name, function_name, extra_kwargs).
# At dispatch time the module is lazily imported and the handler called with
# (handler, target, instance, reqid, comp, session_id, conh,
#  inner_obj, inner_bytes, **extra_kwargs).
_SERVICE_DISPATCH = {
    # --- Mail service (1000 block) ---
    60001: ("services.mail", "handle_send_mail", {}),
    60002: ("services.mail", "handle_receive", {}),
    60003: ("services.mail", "handle_delete", {}),   # Claim attachment
    60004: ("services.mail", "handle_send", {}),      # MarkRead
    60005: ("services.mail", "handle_mark_read", {}),   # Delivered
    60006: ("services.mail", "handle_claim", {}),     # MarkDelete
    60007: ("services.mail", "handle_get_unread", {}),
    60008: ("services.mail", "handle_mark_sent_delete", {}),
    # --- Social service ---
    2149: ("services.social", "handle_add_friend", {}),
    2157: ("services.social", "handle_accept_friend_request", {}),
    2159: ("services.social", "handle_ignore_friend_request", {}),
    2161: ("services.social", "handle_remove_friend", {}),
    2163: ("services.social", "handle_ignore_player", {}),
    2165: ("services.social", "handle_unignore_player", {}),
    # --- Matchmaking ---
    4001: ("services.matchmaking", "handle_ping_matchmaking", {}),
    4013: ("services.matchmaking", "handle_send_quick_match_challenge", {}),
    4017: ("services.matchmaking", "handle_send_challenge_response", {}),
    # --- Tournament PvP (already extracted) ---
    22023: ("services.tournament_game", "handle_join_disconnected_game", {}),
    22025: ("services.tournament_game", "handle_ready_to_continue_game", {}),
    # --- Store / Escrow ---
    6009: ("services.store", "handle_get_items", {}),
    6011: ("services.store", "handle_purchase", {}),
    6013: ("services.store", "handle_redeem", {}),
    # --- Frost Ring Arena campaign service ---
    10001: ("services.arena", "handle_request", {"data_type": 10001}),
    10003: ("services.arena", "handle_request", {"data_type": 10003}),
    10005: ("services.arena", "handle_request", {"data_type": 10005}),
    10007: ("services.arena", "handle_request", {"data_type": 10007}),
    10009: ("services.arena", "handle_request", {"data_type": 10009}),
    10011: ("services.arena", "handle_request", {"data_type": 10011}),
    10013: ("services.arena", "handle_request", {"data_type": 10013}),
    10019: ("services.arena", "handle_request", {"data_type": 10019}),
    10027: ("services.arena", "handle_request", {"data_type": 10027}),
    10029: ("services.arena", "handle_request", {"data_type": 10029}),
    10033: ("services.arena", "handle_request", {"data_type": 10033}),
}

# Lazy dispatch: import and call the handler function.
def _dispatch_service(handler, data_type, target, instance, reqid, comp,
                       session_id, conh, inner_obj, inner_bytes):
    import importlib
    entry = _SERVICE_DISPATCH.get(data_type)
    if not entry:
        return False  # unhandled
    mod_name, fn_name, extra_kw = entry
    mod = importlib.import_module(mod_name)
    fn = getattr(mod, fn_name)
    # Common UID constants that service handlers expect
    kwargs = {
        "SERVICE_MAIL_UID": SERVICE_MAIL_UID,
        "SERVICE_PROFILE_UID": SERVICE_PROFILE_UID,
        "log_req": log_req,
    }
    kwargs.update(extra_kw)
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
    )
    handler._application.dispatch_request(
        command,
        lambda request: fn(
            handler, request.target, request.instance, request.request_id,
            request.compressed, request.session_id,
            request.connection_handle,
            inner_obj=request.inner_object,
            inner_bytes=request.inner_bytes, **kwargs),
    )
    return True


class HCPHandler:
    def __init__(self, conn, addr):
        self.conn = conn
        self.addr = addr
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
        self.user_profile = None
        self._inventory_pending = False
        self._tutorial_game = None
        self._application = ApplicationCommandDispatcher(
            event_publisher=self._publish_application_events)

    def _publish_application_events(self, events):
        """Publish events after an application command has committed.

        Session removal currently needs only an audit/log notification. Game
        commands will extend this callback to route committed domain events
        to the appropriate client event projection.
        """
        for event in events:
            log_req(f"    Committed application event: {event!r}")

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
        h = dict(headers)
        if self.sid:
            h.setdefault("sid", self.sid)
        h.setdefault("scnt", self.scnt)
        h.setdefault("ccnt", self.ccnt)
        pkt = make_packet(h, body)
        if h.get('target') != 'pong':
            log(f"SEND {h.get('target','')}/{h.get('instance','')}: body={len(body)}b")
        self.conn.sendall(pkt)

    def send_and_cache(self, headers, body, data_type, reqid, target, instance):
        self.send(headers, body)

    # ------------------------------------------------------------------
    # Battle turn engine (AI opponent)
    # ------------------------------------------------------------------

    def _send_battle_events(self, session, game, pl_t):
        """Send a Game's queued events as one 3055 sync packet."""
        if not game.events:
            return False
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
        from db import db_ability_meta_targets
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
                trow = _db.execute(
                    "SELECT filter_json, target_kind, collection_flags "
                    "FROM target_templates WHERE template_id=?", (tid,)).fetchone()
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

    def _card_target_requirements_met(self, session, ability_guids):
        """A hand card is only playable when every EXPLICIT target template of
        its non-manual abilities has at least one legal candidate.  This is
        what makes Countermagic ("Interrupt target card" — CollectionFlags
        CastSpells) unplayable with nothing on the chain: its only legal
        targets are cards in CastSpells, so with an empty chain the card is
        not offered.  Auto targets and player-champion targets never gate.
        """
        from db import db_ability_meta_targets
        from abilities.framework.targeting import legal_targets, ZONE_MAP
        for ag in (ability_guids or []):
            meta = db_ability_meta_targets(ag)
            if not meta or not meta[0]:
                continue
            if meta[4]:  # is_manual — a warzone activation, not a play cost
                continue
            try:
                tpl_ids = json.loads(meta[0])
            except Exception:
                continue
            for tid in tpl_ids:
                trow = _db.execute(
                    "SELECT filter_json, target_kind, is_auto_target, "
                    "collection_flags, min_target_count FROM target_templates "
                    "WHERE template_id=?", (tid,)).fetchone()
                if not trow:
                    continue
                kind = trow[1] or ""
                auto = int(trow[2] or 0)
                flags = trow[3] or ""
                minc = int(trow[4] or 1)
                if auto or minc < 1:
                    continue
                if kind == "PlayerTargetTemplate":
                    continue  # the controller's champion always exists
                if not flags or flags.strip().lower() in ("none", ""):
                    continue
                zones = [ZONE_MAP.get(z, z.lower())
                         for z in flags.split("|") if z]
                if not zones:
                    continue
                try:
                    candidates = legal_targets(
                        _db, session.session_id, self.user_profile["id"],
                        str(tid), 0, both_players=True,
                        champions=self._champion_targets())
                except Exception:
                    continue
                if not candidates:
                    log_req(f"    Playability: {str(tid)[:8]} zone "
                            f"{flags} has no legal target — card not playable")
                    return False
        return True

    def _warzone_troop_count(self, session, user_id):
        return db_warzone_troop_count(session.session_id, user_id)

    def _valid_targets_for_template(self, session, pl_t, ai_t, tid):
        """Compute the valid SessionCardId targets for an AbilityTargetTemplate,
        driven by the gamedata target template (kind + card filter): champions
        come from the filter's IsHero, troops from the filter, and
        PlayerTargetTemplate ('You') resolves to the controller's champion."""
        from abilities.framework.targeting import legal_targets
        trow = _db.execute(
            "SELECT filter_json, target_kind, is_auto_target FROM target_templates "
            "WHERE template_id=?", (tid,)).fetchone()
        if not trow:
            return None
        kind = trow[1] or ""
        auto = int(trow[2] or 0)
        fj = trow[0] or "{}"
        if auto:
            # Auto targets (e.g. a shard's 'You') resolve automatically server-
            # side — never attach a hand-play target picker for them.
            return None
        if kind == "PlayerTargetTemplate":
            champ = getattr(self, "_player_champ_scid", None)
            return [champ] if champ else []
        if not fj or fj.strip() == "{}":
            return None
        candidates = legal_targets(
            _db, session.session_id, self.user_profile["id"], tid, 0,
            both_players=True, champions=self._champion_targets())
        return [game_engine.SessionCardId(game_engine.UID(int(u)))
                for u in candidates]

    def _play_ability_targets(self, session, pl_t, ai_t, ability_guids):
        """For a hand card's abilities return [(ability_guid, index, template_id,
        [SessionCardId])] for each targeting template with computable targets."""
        from db import db_ability_meta_targets
        result = []
        for ag in (ability_guids or []):
            meta = db_ability_meta_targets(ag)
            if not meta or not meta[0]:
                continue
            try:
                tpl_ids = json.loads(meta[0])
            except Exception:
                continue
            for i, tid in enumerate(tpl_ids):
                targets = self._valid_targets_for_template(session, pl_t, ai_t, str(tid))
                if targets is not None and targets:
                    result.append((str(ag), i, str(tid), targets))
        return result

    def _add_play_target_options(self, game, session, pl_t, ai_t):
        """Attach targeting TargetInstances to the most recent PlayerOptionList.

        The client's play-card flow (BattleStatePlayCard.NextStep) only opens the
        target picker for a played card's ability when
        State.CanUseAbility(cardId, abilityId) is true, i.e. the ability has an
        OptionInstance + TargetInstance in PlayerOptions.m_Targets. Without this,
        a targeted spell (e.g. Bravery "+1/+1 target troop") is played with no
        target and no effect."""
        from db import db_get_card_abilities, db_card_template_field, db_target_template_text
        if not game.events:
            return
        last_ev = game.events[-1]
        if not isinstance(last_ev, game_engine.PlayerOptionListSessionEventArgs):
            return
        for opt in last_ev.options:
            card_uid = opt.card.uid.uid64
            row = _db.execute(
                "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
                (session.session_id, card_uid)).fetchone()
            if not row:
                continue
            ab_json, _attrs = db_get_card_abilities(row[0])
            ab = []
            if ab_json:
                try:
                    ab = [g.lower() for g in json.loads(ab_json)]
                except Exception:
                    pass
            for ag, idx, tid, targets in self._play_ability_targets(session, pl_t, ai_t, ab):
                # Troop abilities are NOT playable from hand.  Only the
                # card itself may be played (cost/threshold gate).
                # Manual abilities activate from the warzone, not hand.
                from db import db_ability_meta_targets
                meta = db_ability_meta_targets(ag)
                if meta and meta[4]:  # is_manual
                    continue
                inst = game._make_event(game_engine.OptionInstanceSessionEventArgs)
                inst.opt_id = game_engine.ResourceId.from_str(ag)
                inst.min_target_counts.append(1)
                inst.max_target_counts.append(1)
                inst.target_ids.append(game_engine.ResourceId.from_str(tid))
                tgt = game._make_event(game_engine.TargetInstanceSessionEventArgs)
                tgt.target_index = idx
                tgt.target_id = game_engine.ResourceId.from_str(tid)
                tgt.targets = list(targets)
                inst.target_instances.append(tgt)
                opt.instances.append(inst)
                # Also attach the picker to the PlayCard option itself so the
                # client's CanUseAbility finds the target whether it checks the
                # card's ability or the built-in PlayCard ability (the play-card
                # flow keys on BuiltInResources.PlayCardAbilityTemplateId).
                for inst2 in opt.instances:
                    if str(inst2.opt_id.guid) == game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID:
                        inst2.target_ids.append(game_engine.ResourceId.from_str(tid))
                        inst2.min_target_counts.append(1)
                        inst2.max_target_counts.append(1)
                        tgt2 = game._make_event(game_engine.TargetInstanceSessionEventArgs)
                        tgt2.target_index = len(inst2.target_instances)
                        tgt2.target_id = game_engine.ResourceId.from_str(tid)
                        tgt2.targets = list(targets)
                        inst2.target_instances.append(tgt2)
                        break
            # Additional-cost sacrifice (e.g. Abominate "sacrifice a troop you
            # control"): attach a SacrificeAbilityCostType CostInstance to the
            # PlayCard instance so the client's BattleStateAssignXCost prompts for
            # the sacrificed troop BEFORE the effect target. Without it the client
            # skips the cost and only asks for the single effect target.
            from db import db_card_template_field
            sac_target_guid = db_card_template_field(row[0], "sacrifice_target")
            if sac_target_guid and sac_target_guid != "00000000-0000-0000-0000-000000000000":
                sac_targets = self._valid_targets_for_template(session, pl_t, ai_t, sac_target_guid)
                if sac_targets:
                    from db import db_target_template_text
                    ttext = db_target_template_text(sac_target_guid)
                    count = _target_count_from_text(ttext)
                    # Find the PlayCard instance — its opt_id MUST match the
                    # client's BuiltInResources.PlayCardAbilityTemplateId, or
                    # GetCostsFor(cardId, PlayCard) can't see this CostInstance
                    # and the client never prompts for the sacrifice.
                    for inst in opt.instances:
                        if str(inst.opt_id.guid) == game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID:
                            ci = game._make_event(game_engine.CostInstanceSessionEventArgs)
                            HCPHandler._set_cost_instance_bounds(ci, count, count)
                            ci.cost_type = 2  # EAbilityCostType.SacrificeAbilityCostType
                            ci.target_template_id = game_engine.ResourceId.from_str(
                                sac_target_guid)
                            ci.targets = list(sac_targets)
                            inst.target_instances.append(ci)
                            break
            # Variable X cost (e.g. Burn to the Ground "Deal X damage"): attach
            # an XCostAbilityCostType CostInstance to the PlayCard instance so
            # the client's BattleStateAssignXCost pushes the X slider
            # (BattleStateResourceXCost).  Without this the cost list is empty
            # and the dialog auto-commits at X=0 without ever showing.
            if self._template_has_x_cost(row[0]):
                for inst in opt.instances:
                    if str(inst.opt_id.guid) == game_engine.PLAY_CARD_ABILITY_TEMPLATE_ID:
                        ci = game._make_event(game_engine.CostInstanceSessionEventArgs)
                        HCPHandler._set_cost_instance_bounds(ci, 0, 0)
                        ci.cost_type = 256  # EAbilityCostType.XCostAbilityCostType
                        ci.target_template_id = game_engine.ResourceId.invalid()
                        ci.targets = []
                        inst.target_instances.append(ci)
                        break

    def _cost_targets_available(self, session, tid, count):
        """True if the player controls at least `count` cards that can pay a
        cost targeting template `tid` (e.g. Abominate's "a troop you control").
        Artifacts/other kinds are resolved from the template's game text."""
        from db import db_target_template_text, db_warzone_troop_count
        text = db_target_template_text(tid)
        if "artifact" in text.lower():
            n = _db.execute(
                "SELECT COUNT(*) FROM game_cards WHERE session_id=? AND user_id=? "
                "AND location='warzone' AND card_type='Artifact'",
                (session.session_id, self.user_profile["id"])).fetchone()[0]
        else:
            n = db_warzone_troop_count(session.session_id, self.user_profile["id"])
        return n >= count

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
        """Read variable activation cost fields from the raw ability record."""
        row = _db.execute(
            "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
            (ability_guid,)).fetchone()
        if not row or not row[0]:
            return 0, 0
        try:
            raw = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0, 0
        return (int(raw.get("m_VariableActivationCost", 0) or 0),
                int(raw.get("m_VariableActivationCostMinimum", 0) or 0))

    @staticmethod
    def _template_has_x_cost(tpl_guid):
        """True if a card template has a variable X cost — data-driven from the
        gamedata card field m_VariableCost (card_templates.variable_cost), e.g.
        Burn to the Ground "1X" = 1 base + X.  These cards must show the
        client's X-cost dialog and the chosen X is paid as extra resources.
        """
        row = _db.execute(
            "SELECT variable_cost FROM card_templates WHERE guid=?",
            (tpl_guid,)).fetchone()
        return bool(row and int(row[0] or 0) > 0)

    def _resolve_gem_abilities(self, active_gems):
        """Bake a deck's socketed gems into per-instance ability lists at DECK
        SAVE time: {instance_id: [ability guids]} from gem_templates, e.g.
        Shamed Gladiator's Minor Blood Orb of Hatred (gem 5) -> "Rage 1 in all
        zones".  The game later copies these onto game_cards.card_abilities so
        the card is updated with the gem's ability."""
        import json as _j
        out = {}
        for inst, gem in (active_gems or {}).items():
            try:
                gem = int(gem)
            except (TypeError, ValueError):
                continue
            if gem <= 0:
                continue
            row = _db.execute(
                "SELECT abilities_json FROM gem_templates WHERE gem_type=?",
                (gem,)).fetchone()
            if row and row[0]:
                try:
                    abilities = _j.loads(row[0])
                except Exception:
                    abilities = []
                if abilities:
                    out[str(inst)] = [str(a).lower() for a in abilities]
        return out

    def _card_gem_type(self, game, scid, instance_id=None):
        """Resolve the authoritative socketed gem for one card instance.

        The deck stores FRA instance ids, while mirrored/AI cards retain the
        gem on their game_cards row.  Keeping this lookup in one place lets
        Practice and PvP derive abilities from the current socket instead of
        trusting a possibly stale decks.gem_abilities cache.
        """
        if instance_id is None and scid is not None:
            row = _db.execute(
                "SELECT card_template_id FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (game.session_id.uid64, scid.uid.uid64)).fetchone()
            if row and row[0]:
                try:
                    instance_id = int(row[0])
                except (TypeError, ValueError):
                    pass
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
                row = _db.execute(
                    "SELECT active_gems FROM decks WHERE id=?", (deck_id,)
                ).fetchone()
                if row and row[0]:
                    try:
                        gems = json.loads(row[0])
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
        if not gem_type and scid is not None:
            try:
                row = _db.execute(
                    "SELECT gems FROM game_cards WHERE session_id=? AND card_uid=?",
                    (game.session_id.uid64, scid.uid.uid64)).fetchone()
                gem_type = int(row[0] or 0) if row else 0
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
        import json as _j
        seen = set()
        stack = [ability_guid]
        while stack:
            ag = stack.pop()
            if ag in seen:
                continue
            seen.add(ag)
            trow = _db.execute(
                "SELECT target_template_ids FROM card_abilities_meta "
                "WHERE ability_guid=?", (ag,)).fetchone()
            if trow and trow[0]:
                try:
                    tids = _j.loads(trow[0])
                except Exception:
                    tids = []
                for tid in (tids or []):
                    tt = _db.execute(
                        "SELECT collection_flags FROM target_templates "
                        "WHERE template_id=?", (tid,)).fetchone()
                    if tt and "Choosing" in (tt[0] or "") and \
                            "deck" in (tt[0] or "").lower():
                        return ag, str(tid)
            for e in _db.execute(
                    "SELECT param FROM ability_effects WHERE ability_guid=? "
                    "AND effect_type='ActivateAbilityEffectTemplate'",
                    (ag,)).fetchall():
                if e and e[0]:
                    stack.append(e[0].lower())
        return None, None

    def _shards_of_fate_template(self, ability_guids):
        """Data-driven Shards of Fate detection: walk an ability's
        ActivateAbility chain and return (ability_guid, target_template_id)
        when a target template filters a Standard RESOURCE in the DECK
        ("Choose a Standard resource in your deck. Gain the thresholds it
        provides.").  Returns (None, None) for ordinary resources."""
        import json as _j
        seen = set()
        stack = [str(a).lower() for a in (ability_guids or [])]
        while stack:
            ag = stack.pop()
            if ag in seen:
                continue
            seen.add(ag)
            trow = _db.execute(
                "SELECT target_template_ids FROM card_abilities_meta "
                "WHERE ability_guid=?", (ag,)).fetchone()
            if trow and trow[0]:
                try:
                    tids = _j.loads(trow[0])
                except Exception:
                    tids = []
                for tid in (tids or []):
                    tt = _db.execute(
                        "SELECT filter_json FROM target_templates "
                        "WHERE template_id=?", (tid,)).fetchone()
                    fj = (tt[0] if tt else "") or ""
                    if ("IsSubType" in fj and "Standard" in fj
                            and "IsResource" in fj
                            and "InZone" in fj
                            and '"Deck"' in fj):
                        return ag, str(tid)
            for e in _db.execute(
                    "SELECT param FROM ability_effects WHERE ability_guid=? "
                    "AND effect_type='ActivateAbilityEffectTemplate'",
                    (ag,)).fetchall():
                if e and e[0]:
                    stack.append(e[0].lower())
        return None, None

    def _resolve_shards_of_fate(self, game, session, pl_t, ai_t, bstate,
                                played_card_uid, ability_guid, shard_tpl,
                                owner_id):
        """Shards of Fate: the controller chooses a Standard resource in their
        deck and gains its threshold.  A HUMAN gets the class-39 choosing
        prompt (the shard stays in the deck); the AI auto-picks a random one."""
        import battle_engine as _be
        from abilities.framework.targeting import legal_targets as _lt
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
        row = _db.execute(
            "SELECT ct.name FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?",
            (session.session_id, int(chosen))).fetchone()
        color = (row[0].split()[0] if row else "").lower()
        flag = game_engine.SHARD_TO_FLAG.get(color, 0)
        if flag:
            th = bstate.setdefault("ai_threshold", {})
            th[flag] = th.get(flag, 0) + 1
            _be.save_state(session, bstate)
            game.ai_threshold = dict(th)
            ev_th = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
            ev_th.player_id = ai_t
            ev_th.color = flag
            ev_th.operation = 1
            ev_th.delta = 1
            ev_th.new_value = th[flag]
            game._push(ev_th)
            game.push_player_updated(
                ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        log_req(f"    Shards of Fate (AI): gained {color} threshold")
        return f"shards of fate: AI gained {color} threshold"

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
            return False
        if not self._thresholds_met(thresh_json, threshold):
            return False
        reqs = self._card_troop_requirements(ability_guids)
        if reqs:
            if "friendly" in reqs and not friendly_count:
                return False
            if "enemy" in reqs and not enemy_count:
                return False
            if "any" in reqs and not (friendly_count or enemy_count):
                return False
        # Zone-bound explicit targets (e.g. Countermagic's CastSpells-only
        # "Interrupt target card") must have a legal candidate to be playable.
        if not self._card_target_requirements_met(session, ability_guids):
            return False
        # Additional sacrifice cost: the player must be able to pay it. The
        # gamedata card template's m_SacrificeTarget is an AbilityTargetTemplate
        # (e.g. "a troop you control" — sacrifice is restricted to your own).
        from db import db_card_template_field, db_target_template_text
        sac_target = db_card_template_field(
            _db.execute(
                "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
                (session.session_id, card_uid)).fetchone()[0],
            "sacrifice_target")
        if sac_target and sac_target != "00000000-0000-0000-0000-000000000000":
            if not self._cost_targets_available(session, sac_target,
                                                _target_count_from_text(db_target_template_text(sac_target))):
                return False
        return True

    def _push_main_phase_options(self, session, pl_t, ai_t):
        """Push the playable-card options (golden outlines) for a main phase.

        Computes which cards in the player's hand are affordable from the
        DB-backed battle state and pushes a PlayerOptionList on its own packet.
        Shards: playable if resource not yet played this turn.
        Troops: playable if cost <= available resources AND all threshold
        requirements are met.
        """
        import battle_engine as _be
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        resources = bstate.get("player_resources", 0)
        threshold = bstate.get("player_threshold", {})
        resource_played = bstate.get("player_resource_played_this_turn", False)
        friendly_count = self._warzone_troop_count(session, self.user_profile["id"])
        enemy_count = self._warzone_troop_count(session, 0)
        from db import db_hand_cards_with_templates
        playable = []
        rows = db_hand_cards_with_templates(session.session_id, self.user_profile["id"])
        # Enlightened Seeker: "You can't play cards." — a continuous static
        # ability; no hand card is playable while the controller has one in play.
        from abilities.framework.statics import controller_flags
        if "cant_play_cards" in controller_flags(
                _db, session.session_id, bstate, self.user_profile["id"]):
            rows = []
        for card_uid, cost, ct, thresh_json, abilities_json in rows:
            scid = game_engine.SessionCardId(game_engine.UID(card_uid))
            # Use the card's EFFECTIVE cost (base + permanent/triggered cost
            # modifiers, e.g. Fury of the Mountain God's "-1 per damage dealt"
            # while in hand) — the raw template cost would wrongly hide a
            # discounted card.
            from abilities.framework.statics import effective_cost
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
                log_req(f"    Playability: skip {card_uid} type {ct} "
                        f"(cost {cost} > resources {resources} / thresholds / no troop target)")
        game2 = self._fresh_game(session, pl_t, ai_t, bstate)
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
                game2.add_champion_to_options(
                    pl_t, player_champ_cid,
                    self._filter_affordable_abilities(abilities, bstate,
                                                      _be.current_phase(bstate)),
                    self._discard_costs_for(session, abilities),
                    self._champion_ability_targets(
                        session, abilities,
                        player_champ_cid.uid.to_uint64()
                        if hasattr(player_champ_cid, "uid") else 0),
                    self._champion_ability_costs(
                        session, abilities,
                        player_champ_cid.uid.to_uint64()
                        if hasattr(player_champ_cid, "uid") else 0))
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
        self._push_warzone_card_updates(game2, session, pl_t, ai_t)
        if self._send_battle_events(session, game2, pl_t):
            log_req(f"    Pushed main-phase options ({len(playable)} playable, champ_ab={len(abilities) if player_champ_cid and abilities else 0}, troop_ab={sum(len(v) for v in affordable.values())})")

    def _affordable_troop_abilities(self, session, bstate):
        """Return {(card_uid, scid): [ability_guid, ...]} for warzone troops the
        player controls whose MANUAL abilities are activatable.

        Gating (mirrors the client's CanActivateAbilityBase + the champion
        talent gating in _filter_affordable_abilities):
          - is_manual (player-activated, not a trigger)
          - casting_behavior matches the current phase (QuickAction=64 any
            window; BasicAction=8 only main phases)
          - activation_cost <= available resources
          - UsesPerGame / UsesPerTurn not exhausted for THIS instance
            (tracked in game_cards.card_uses)
        """
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
        rows = _db.execute(
            "SELECT gc.card_uid, gc.template_guid, gc.card_state, "
            "(ct.attributes | gc.card_attributes) "
            "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='warzone'",
            (session.session_id, self.user_profile["id"])).fetchall()
        result = {}
        for card_uid, tpl_guid, card_state, attrs in rows:
            ab_list = self._card_ability_list(session, card_uid)
            if not ab_list:
                continue
            uses = self._card_uses(session, card_uid)
            affordable = []
            for ag in ab_list:
                m = _db.execute(
                    "SELECT casting_behavior, is_manual, activation_cost, "
                    "uses_per_game, uses_per_turn, exhausts_on_use "
                    "FROM card_abilities_meta WHERE ability_guid=?",
                    (ag,)).fetchone()
                if not m:
                    continue
                casting, manual, cost, upg, upt, exh = m
                variable_x, variable_min = HCPHandler._ability_x_cost_metadata(ag)
                if not manual:
                    continue
                # Ability conditions gate manual activation (client's
                # CanActivateAbilityBase: TriggerCondition.IsValid). E.g.
                # Droo's Colossal Walker "While this is exhausted: ..." has a
                # RequiresSourcePassesFilterCondition(IsTapped) trigger
                # condition — it is only activatable while the troop is tapped.
                raw_row = _db.execute(
                    "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
                    (ag,)).fetchone()
                if raw_row and raw_row[0]:
                    from abilities.framework.condition_engine import (
                        ConditionContext,
                        trigger_condition_met,
                    )
                    cond_ctx = ConditionContext(
                        _db, session, bstate,
                        ability_source_uid=card_uid,
                        ability_source_owner_id=self.user_profile["id"])
                    if not trigger_condition_met(raw_row[0], cond_ctx):
                        continue
                # The ability must have at least one legal target for its
                # explicit target templates, and attacking-target abilities
                # (Prairie Scout's "target attacking troop") are only
                # activatable during combat steps — after attackers were
                # declared, until the second main phase.
                trow2 = _db.execute(
                    "SELECT target_template_ids FROM card_abilities_meta "
                    "WHERE ability_guid=?", (ag,)).fetchone()
                tids = []
                if trow2 and trow2[0]:
                    try:
                        tids = json.loads(trow2[0])
                    except Exception:
                        tids = []
                if tids:
                    from abilities.framework.targeting import (
                        legal_targets as _lt, target_uses_both_players,
                    )
                    wants_attacking = False
                    has_target = False
                    explicit_target_missing = False
                    for tid in tids:
                        tt = _db.execute(
                            "SELECT filter_json, target_kind, is_auto_target "
                            "FROM target_templates WHERE template_id=?",
                            (tid,)).fetchone()
                        if tt and tt[0] and "IsAttacking" in tt[0]:
                            wants_attacking = True
                        kind = (tt[1] if tt else "") or ""
                        auto = int(tt[2] or 0) if tt else 0
                        if auto or kind in ("PlayerTargetTemplate",
                                            "AbilitySourceCardTargetTemplate",
                                            "AbilityCreatedTargetTemplate"):
                            # Auto targets (e.g. Incubation Slave's 'You' —
                            # "Remove all egg counters from this and sacrifice
                            # it") resolve automatically; no picker, always
                            # available.
                            has_target = True
                            continue
                        cands = _lt(
                            _db, session.session_id, self.user_profile["id"],
                            tid, card_uid,
                            both_players=target_uses_both_players(_db, tid),
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
                if exh:
                    # Exhaust-as-cost (e.g. Prairie Scout): the card must be
                    # able to pay the tap — not already tapped, and not
                    # summoning sick (entered this turn without Speed).
                    cstate = card_state or 0
                    if cstate & game_engine.ECardStates.Tapped:
                        continue
                    if (not (cstate & game_engine.ECardStates.StartedATurnOnYourSide)
                            and not ((attrs or 0) & game_engine.ECardAttributes.Speed)):
                        continue
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
                if cost > resources:
                    continue
                if variable_x and resources < int(variable_min or 0):
                    continue
                used = int(uses.get(ag, 0))
                if upg and used >= upg:
                    continue
                if upt and used >= upt:
                    continue
                affordable.append(ag)
            if affordable:
                result[(card_uid, tpl_guid)] = affordable
        return result

    def _add_troop_ability_options(self, game, pl_t, session, affordable,
                                   bstate=None):
        """Append warzone-troop ability options (ECardUsage.Activate) to the
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
        last_ev = game.events[-1] if game.events else None
        if not isinstance(last_ev, game_engine.PlayerOptionListSessionEventArgs):
            return
        for (card_uid, tpl_guid), abilities in affordable.items():
            scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
            # Default candidate pool: the player's warzone troops.  Abilities
            # with explicit target templates get the pool filtered through the
            # gamedata card filter (e.g. Prairie Scout's IsAttacking+IsTroop).
            from abilities.framework.targeting import (
                legal_targets as _legal_targets, target_uses_both_players,
            )
            opt = game._make_event(game_engine.PlayerOptionSessionEventArgs)
            opt.card = scid
            opt.state = game_engine.ECardUsage.Activate
            for ag in abilities:
                inst = game._make_event(game_engine.OptionInstanceSessionEventArgs)
                inst.opt_id = game_engine.ResourceId.from_str(ag)
                # The ability's own target template ids (JSON list from
                # card_abilities_meta.target_template_ids).
                mrow = _db.execute(
                    "SELECT target_template_ids FROM card_abilities_meta "
                    "WHERE ability_guid=?",
                    (ag,)).fetchone()
                tpls = []
                if mrow and mrow[0]:
                    try:
                        tpls = json.loads(mrow[0])
                    except Exception:
                        tpls = []
                if tpls:
                    inst.min_target_counts = [1] * len(tpls)
                    inst.max_target_counts = [1] * len(tpls)
                    built = []
                    for i, tid in enumerate(tpls):
                        tt = _db.execute(
                            "SELECT target_kind, is_auto_target "
                            "FROM target_templates WHERE template_id=?",
                            (tid,)).fetchone()
                        kind = (tt[0] if tt else "") or ""
                        auto = int(tt[1] or 0) if tt else 0
                        if auto or kind in ("PlayerTargetTemplate",
                                            "AbilitySourceCardTargetTemplate",
                                            "AbilityCreatedTargetTemplate"):
                            # Auto targets resolve server-side — never attach a
                            # picker (the client's target cursor appears when a
                            # TargetInstance is present).
                            continue
                        built.append(i)
                        others = _legal_targets(
                            _db, session.session_id, self.user_profile["id"],
                            tid, int(card_uid),
                            both_players=target_uses_both_players(_db, tid),
                            champions=self._champion_targets(),
                            battle_state=bstate)
                        if not others:
                            # Auto/self templates with no real filter (e.g.
                            # Living Totem "this") fall back to the full
                            # friendly pool; restricting filters (e.g. Prairie
                            # Scout's IsAttacking) keep their empty pool so the
                            # ability is never offered without a valid target.
                            tt = _db.execute(
                                "SELECT filter_json FROM target_templates "
                                "WHERE template_id=?", (tid,)).fetchone()
                            filt = (tt[0] if tt else "") or ""
                            if filt.strip() in ("", "{}"):
                                others = [r[0] for r in _db.execute(
                                    "SELECT card_uid FROM game_cards WHERE session_id=? "
                                    "AND user_id=? AND location='warzone' ORDER BY position",
                                    (session.session_id, self.user_profile["id"])).fetchall()]
                        tgt = game._make_event(game_engine.TargetInstanceSessionEventArgs)
                        tgt.target_index = i
                        tgt.target_id = game_engine.ResourceId.from_str(tid)
                        tgt.targets = [game_engine.SessionCardId(game_engine.UID(int(u))) for u in others]
                        inst.target_instances.append(tgt)
                    if built:
                        inst.min_target_counts = [1] * len(built)
                        inst.max_target_counts = [1] * len(built)
                    else:
                        inst.min_target_counts = []
                        inst.max_target_counts = []
                variable_x, variable_min = HCPHandler._ability_x_cost_metadata(ag)
                if variable_x:
                    ci = game._make_event(game_engine.CostInstanceSessionEventArgs)
                    HCPHandler._set_cost_instance_bounds(ci, variable_min, 0)
                    ci.cost_type = 256  # EAbilityCostType.XCostAbilityCostType
                    ci.target_template_id = game_engine.ResourceId.invalid()
                    ci.targets = []
                    inst.target_instances.append(ci)
                opt.instances.append(inst)
                discard_prompt = self._discard_prompt_data(ag)
                if discard_prompt:
                    child_ability, discard_target = discard_prompt
                    hand = [game_engine.SessionCardId(game_engine.UID(int(uid)))
                            for (uid,) in _db.execute(
                                "SELECT card_uid FROM game_cards "
                                "WHERE session_id=? AND user_id=? "
                                "AND location='hand' ORDER BY position",
                                (session.session_id,
                                 self.user_profile["id"])).fetchall()]
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
            last_ev.options.append(opt)

    def _prompt_trigger_targets(self, game, pl_t, ai_t, session, bstate,
                                source_uid, ability_guid, target_template_ids,
                                candidates):
        """Ask the player to choose targets for a triggered ability with
        explicit targets (e.g. Solitary Exile's Deploy "Void another target
        card").  Mirrors the client's WaitForTriggeredAbilitiesAction: a
        PlayerOptionList carrying the legal candidates (filtered through the
        gamedata target template) plus the class-39
        TriggeredAbilityActivationDataRequired event.  The follow-up
        SetAbilityActivationDataTransaction resolves the trigger with the
        chosen target."""
        import battle_engine as _be
        inst_id = int(bstate.get("_next_instance_id", 1))
        bstate["_next_instance_id"] = inst_id + 1
        bstate["pending_trigger"] = {
            "ability_guid": ability_guid,
            "source_uid": int(source_uid),
            "owner_id": int(bstate.get("resolving_owner_id", 0)),
            "instance_id": inst_id,
            "target_template_id": (target_template_ids or [None])[0],
        }
        # PvP: the trigger choice lives in the PvP state (not the battle
        # state), and the prompt goes only to the choosing player via the
        # tournament packet path — same pattern as _prompt_deck_search.
        if bstate.get("pvp"):
            from services.tournament_game import (
                pvp_load_state, pvp_save_state, _send_pvp_packet)
            from gamemodes.tournament_engine import player_handlers
            from db import db_game_session_pids
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
                opt_inst.target_ids.append(
                    game_engine.ResourceId.from_str(target_template_ids[0]))
                tgt = g2._make_event(game_engine.TargetInstanceSessionEventArgs)
                # The first target in a triggered ability is often the source
                # card (an auto target such as "this").  The explicit target
                # therefore retains its original index in the ability's
                # target-template list.  ConfigureAbility uses that index
                # when it serializes the TargetMap; collapsing it to zero
                # makes the client immediately close the picker without
                # showing a cursor (Crazed Squirrel Titan is one example).
                try:
                    all_target_ids = json.loads(
                        (_db.execute(
                            "SELECT target_template_ids FROM card_abilities_meta "
                            "WHERE ability_guid=?", (ability_guid,)).fetchone()
                         or ["[]"])[0] or "[]")
                    tgt.target_index = next(
                        (i for i, tid in enumerate(all_target_ids)
                         if str(tid) == str(target_template_ids[0])), 0)
                except (TypeError, ValueError, json.JSONDecodeError):
                    tgt.target_index = 0
                tgt.target_id = game_engine.ResourceId.from_str(target_template_ids[0])
                tgt.targets = [game_engine.SessionCardId(game_engine.UID(int(u)))
                               for u in (candidates or [])]
                opt_inst.target_instances.append(tgt)
                opt_inst.min_target_counts = [1]
                opt_inst.max_target_counts = [1]
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
                    f"{chooser_pid} candidates={len(candidates or [])}")
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
        inst.target_ids.append(game_engine.ResourceId.from_str(target_template_ids[0]))
        tgt = game._make_event(game_engine.TargetInstanceSessionEventArgs)
        # The explicit target may follow an auto target such as "this".  The
        # client uses the ability's original target index when serializing the
        # answer, so do not collapse every triggered picker to index zero.
        try:
            all_target_ids = json.loads(
                (_db.execute(
                    "SELECT target_template_ids FROM card_abilities_meta "
                    "WHERE ability_guid=?", (ability_guid,)).fetchone() or
                 ["[]"])[0] or "[]")
            tgt.target_index = next(
                (i for i, tid in enumerate(all_target_ids)
                 if str(tid) == str(target_template_ids[0])), 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            tgt.target_index = 0
        tgt.target_id = game_engine.ResourceId.from_str(target_template_ids[0])
        tgt.targets = [game_engine.SessionCardId(game_engine.UID(int(u)))
                       for u in (candidates or [])]
        inst.target_instances.append(tgt)
        inst.min_target_counts = [1]
        inst.max_target_counts = [1]
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
                f"candidates={len(candidates or [])} inst={inst_id}")

    def _prompt_deck_search(self, game, session, pl_t, ai_t, bstate,
                            ability_guid, source_uid, owner_id, candidates,
                            kind="search"):
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
        import battle_engine as _be
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
                c_row = _db.execute(
                    "SELECT template_guid, card_template_id FROM game_cards "
                    "WHERE session_id=? AND card_uid=?",
                    (session.session_id, int(cu))).fetchone()
                if not c_row:
                    continue
                c_tpl = c_row[0]
                _tpl2, ct2, _n2, cost2, atk2, def2, _g2 = self._card_full_data(
                    g, c_scid, c_tpl, c_row[1])
                g.push_card_moved(c_scid, player_uid,
                                  game_engine.ECardCollections.Choosing,
                                  game_engine.ECardLocations.Top, 1)
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
            from db import db_game_session_pids
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
        import battle_engine as _be
        from abilities.framework.effects.choices import (
            CHOOSE_AND_PLAY_ABILITY, CHOICE_TARGET_TEMPLATE)

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
                CHOOSE_AND_PLAY_ABILITY)
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
                CHOOSE_AND_PLAY_ABILITY)
            req.effect_group_id = 1
            req.effect_instance_ids = []
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
            from db import db_game_session_pids
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
            if prompt_handler is not None:
                _send_pvp_packet(prompt_handler, session, private, chooser,
                                 "choice")
            return
        _be.save_state(session, bstate)
        build_prompt(game, pl_t)

    def _resolve_pending_choice(self, session, pl_t, ai_t, inner_bytes,
                                ability_guid=None):
        """Play a selected Choice token and resume its parent BOM."""
        import battle_engine as _be
        from abilities.framework.effects.choices import (
            CHOOSE_AND_PLAY_ABILITY, extract_card_uids, play_choice_card,
            resolve_choice_card_abilities)
        bstate = _be.load_state(session)
        pending = bstate.get("pending_choice")
        if not pending:
            return False
        if (ability_guid and str(ability_guid).lower() !=
                CHOOSE_AND_PLAY_ABILITY):
            return False
        selected = extract_card_uids(inner_bytes)
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
        if not play_choice_card(g, session, _db, self, pl_t, ai_t, bstate,
                                chosen_uid, owner_id):
            log_req(f"    Choice answer rejected: {hex(int(chosen_uid))}")
            bstate["pending_choice"] = pending
            bstate["resolution_paused"] = True
            _be.save_state(session, bstate)
            self._push_transaction_ack(session)
            return True
        resolve_choice_card_abilities(
            g, session, _db, self, pl_t, ai_t, bstate, chosen_uid,
            pending.get("source_uid"), owner_id)
        from abilities.framework.resolution import resolve_ability
        target_map = {int(key): value for key, value in
                      (pending.get("target_map") or {}).items()}
        resolve_ability(
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
                                candidates, revealed_cards):
        """Prompt for an explicit choice from cards just revealed by BOM data.

        Oakhenge's child target is a SourceRevealedTargetTemplate filtered by
        IsTroop.  The client already has a coverflow for CardsRevealed, but it
        still needs the normal PlayerOptionList + class-39 activation-data
        packet to turn that presentation into a selectable target.  PvP sends
        the reveal and picker only to the revealing player; the shared chain
        stream is stripped of the sensitive class-51 event.
        """
        import battle_engine as _be
        inst_id = int(bstate.get("_next_instance_id", 1))
        bstate["_next_instance_id"] = inst_id + 1
        pend = {
            "ability_guid": str(ability_guid),
            "source_uid": int(source_uid),
            "owner_id": int(owner_id),
            "instance_id": inst_id,
            "candidates": [int(u) for u in (candidates or [])],
            "revealed_cards": [int(u) for u in (revealed_cards or [])],
            "kind": "revealed_troop",
        }

        def build_prompt(g, player_uid):
            rows = _db.execute(
                "SELECT card_uid, position FROM game_cards "
                "WHERE session_id=? AND card_uid IN ({}) "
                "ORDER BY position".format(
                    ",".join("?" * len(pend["revealed_cards"]))),
                (session.session_id, *pend["revealed_cards"]) ).fetchall() \
                if pend["revealed_cards"] else []
            # In PvP the reveal leaf has already delivered the controller-only
            # event (with card definitions) before it knows whether a choice
            # will be needed.  Do not send a second coverflow event here.  The
            # Practice path still includes the reveal in this prompt packet.
            if not ((bstate or {}).get("pvp") and
                    (bstate or {}).get("private_reveal_sent")):
                ev = g._make_event(game_engine.CardsRevealedSessionEventArgs)
                ev.player_id = player_uid
                ev.session_card_ids = [
                    game_engine.SessionCardId(game_engine.UID(int(cu)))
                    for cu, _pos in rows]
                ev.collections = [game_engine.ECardCollections.Deck] * len(rows)
                ev.owning_players = [player_uid] * len(rows)
                ev.positions = [int(pos or 0) for _cu, pos in rows]
                g._push(ev)

            opt_list = g._make_event(
                game_engine.PlayerOptionListSessionEventArgs)
            opt_list.player_id = player_uid
            opt = g._make_event(game_engine.PlayerOptionSessionEventArgs)
            opt.card = game_engine.SessionCardId(
                game_engine.UID(int(source_uid)))
            opt.state = game_engine.ECardUsage.Activate
            inst = g._make_event(game_engine.OptionInstanceSessionEventArgs)
            inst.opt_id = game_engine.ResourceId.from_str(ability_guid)
            target_id = _db.execute(
                "SELECT target_template_ids FROM card_abilities_meta "
                "WHERE ability_guid=?", (ability_guid,)).fetchone()
            try:
                target_id = json.loads(target_id[0] or "[]")[0]
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
            inst.min_target_counts = [1]
            inst.max_target_counts = [1]
            opt.instances.append(inst)
            opt_list.options.append(opt)
            g._push(opt_list)

            ev39 = g._make_event(
                game_engine.TriggeredAbilityActivationDataRequiredSessionEventArgs)
            ev39.player_id = player_uid
            ev39.ability_instance_ids = [inst_id]
            ev39.ability_template_ids = [
                game_engine.ResourceId.from_str(ability_guid)]
            ev39.source_card_ids = [
                game_engine.SessionCardId(game_engine.UID(int(source_uid)))]
            g._push(ev39)
            g.push_green_light(player_uid, game_engine.EPriorityContext.Normal)

        if bstate.get("pvp"):
            from services.tournament_game import (
                pvp_load_state, pvp_save_state, _send_pvp_packet)
            from gamemodes.tournament_engine import player_handlers
            from db import db_game_session_pids
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
        import battle_engine as _be
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        pend = bstate.get("pending_trigger")
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
        bstate.pop("pending_trigger")
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
        return True

    def _resolve_pending_deck_search(self, session, pl_t, ai_t, inner_bytes,
                                     ability_guid=None):
        """Handle the player's card choice from a "search your deck" class-39
        prompt (e.g. Darkspire Priestess's Deathcry): move the chosen matching
        deck card into the controller's hand and re-grant priority.

        The client answers with a SetAbilityActivationDataTransaction that
        carries the deathcry's ability GUID — route it here BEFORE the manual
        troop / champion ability paths so it never activates a champion power
        instead (the bug that put Poca on the chain)."""
        import battle_engine as _be
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
        if pend.get("kind") == "revealed_troop":
            return self._resolve_pending_revealed_choice(
                session, pl_t, ai_t, bstate, pend, chosen_uid)
        bstate.pop("pending_deck_search")
        if pend.get("kind") == "shard":
            # Shards of Fate ("Choose a Standard resource in your deck. Gain
            # the thresholds it provides."): the chosen shard STAYS in the
            # deck — only its threshold is gained.
            return self._resolve_pending_shard_choice(
                session, pl_t, ai_t, bstate, pend, chosen_uid, inner_bytes)
        if chosen_uid and chosen_uid in pend["candidates"]:
            g = self._fresh_game(session, pl_t, ai_t, bstate)
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

    def _resolve_pending_revealed_choice(self, session, pl_t, ai_t, bstate,
                                         pend, chosen_uid):
        """Finish a SourceRevealed troop choice (Oakhenge-style BOM)."""
        import battle_engine as _be
        bstate.pop("pending_deck_search", None)
        bstate.pop("resolution_paused", None)
        candidates = [int(u) for u in (pend.get("candidates") or [])]
        revealed = [int(u) for u in (pend.get("revealed_cards") or [])]
        if chosen_uid and int(chosen_uid) in candidates:
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            from abilities.framework.effects.search import move_deck_card_to_hand
            move_deck_card_to_hand(g, session, _db, self, pl_t, ai_t,
                                   int(chosen_uid), pend["owner_id"], bstate)
            remaining = [cu for cu in revealed if cu != int(chosen_uid)]
            if remaining:
                from db import db_randomly_insert_deck_cards
                db_randomly_insert_deck_cards(
                    session.session_id, int(pend["owner_id"]), remaining)
            # Hide every other revealed card.  The cards were presented by
            # CardsRevealed, not moved in the database, so they all remain in
            # the deck until this continuation resolves.
            self._hide_candidates_to_deck(
                g, session, pl_t, ai_t,
                remaining)
            self._send_battle_events(session, g, pl_t)
            log_req(f"    Revealed choice: {hex(int(chosen_uid))} -> hand "
                    f"(owner {pend['owner_id']})")
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

    def _resolve_pending_shard_choice(self, session, pl_t, ai_t, bstate, pend,
                                      chosen_uid, inner_bytes):
        """Resolve Shards of Fate's pick: gain the chosen Standard resource's
        threshold (the shard remains in the deck) and hide the unchosen
        candidates again."""
        import battle_engine as _be
        if chosen_uid and chosen_uid in pend["candidates"]:
            g = self._fresh_game(session, pl_t, ai_t, bstate)
            row = _db.execute(
                "SELECT ct.name FROM game_cards gc "
                "JOIN card_templates ct ON ct.guid = gc.template_guid "
                "WHERE gc.session_id=? AND gc.card_uid=?",
                (session.session_id, int(chosen_uid))).fetchone()
            color = (row[0].split()[0] if row else "").lower()
            from db import db_randomly_insert_deck_cards
            db_randomly_insert_deck_cards(
                session.session_id, int(pend["owner_id"]), pend["candidates"])
            flag = game_engine.SHARD_TO_FLAG.get(color, 0)
            if flag:
                th = bstate.setdefault("player_threshold", {})
                th[flag] = th.get(flag, 0) + 1
                _be.save_state(session, bstate)
                g.player_threshold = dict(th)
                ev_th = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                ev_th.player_id = pl_t
                ev_th.color = flag
                ev_th.operation = 1
                ev_th.delta = 1
                ev_th.new_value = th[flag]
                g._push(ev_th)
                g.push_player_updated(
                    pl_t, champ_id=getattr(self, "_player_champ_scid", None))
            # Hide EVERY candidate again (back to the face-down deck) — the
            # chosen shard stays in the deck, and all of them must be rendered
            # nullable so nothing is left face-up on top after the Choosing
            # window closes.
            self._hide_candidates_to_deck(g, session, pl_t, ai_t,
                                          pend["candidates"])
            self._send_battle_events(session, g, pl_t)
            log_req(f"    Shards of Fate: gained {color} threshold "
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
            c_row = _db.execute(
                "SELECT template_guid, card_template_id, user_id FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, int(cu))).fetchone()
            if not c_row:
                continue
            _db.execute(
                "UPDATE game_cards SET location='deck', card_state=? "
                "WHERE session_id=? AND card_uid=?",
                (state_after_zone_exit(0), session.session_id, int(cu)))
            _db.commit()
            owner = pl_t if (c_row[2] or 0) != 0 else ai_t
            _tpl2, ct2, _n2, _c2, _a2, _d2, _g2 = self._card_full_data(
                game, c_scid, c_row[0], c_row[1])
            game.push_card_updated(c_scid, owner,
                                   game_engine.ECardCollections.Deck, ct2,
                                   template_id=c_row[0], cost=_c2, attack=_a2,
                                   defense=_d2, state=0, nulling=True)
            game.push_card_moved(c_scid, owner, game_engine.ECardCollections.Deck,
                                 game_engine.ECardLocations.Unknown, 0)

    @staticmethod
    def _champion_health_by_guid(guid):
        """Look up a champion template's starting health by its GUID."""
        if not guid:
            return 20
        from db import db_champion_template_health
        return db_champion_template_health(guid)

    @staticmethod
    def _champion_starting_health(race_name, cls_name):
        """Look up a champion's starting health from champion_class_data."""
        if not race_name or not cls_name:
            return 20
        from db import db_champion_template_health_by_class
        return db_champion_template_health_by_class(race_name, cls_name)

    def _resolve_fra_deck_id(self, arena_deck_id):
        """The practice/FRA deck: the arena-assigned deck when it is valid for
        this player, otherwise the player's most recently saved deck.  NEVER a
        hardcoded id — each player's own collection is used."""
        uid = self.user_profile["id"] if self.user_profile else None
        if uid and arena_deck_id:
            row = _db.execute(
                "SELECT 1 FROM decks WHERE id=? AND user_id=?",
                (arena_deck_id, uid)).fetchone()
            if row:
                return arena_deck_id
        if uid:
            row = _db.execute(
                "SELECT id FROM decks WHERE user_id=? "
                "ORDER BY COALESCE(last_saved, created_at, 0) DESC, id DESC LIMIT 1",
                (uid,)).fetchone()
            if row:
                return row[0]
        return None

    def _filter_affordable_abilities(self, abilities, bstate, phase=None):
        """Filter champion abilities to only those activatable in the current phase
        and affordable (charges/SP >= cost). Pass `phase` (an ETurnPhases value) to
        also gate on activatable_phases (a bitmask where bit N = 1<<N).

        QuickAction abilities (casting_behavior=64) ignore the phase gate — they
        may be activated in ANY priority window (mirrors the client's
        CanActivateAbilityBase, which skips the main-phase check for QuickAction).
        """
        import battle_engine as _be
        charges = bstate.get("player_charges", 0)
        sp = bstate.get("player_spell_points", 0)
        phase_bit = (1 << phase) if phase is not None else 0
        # Spell-power escalation: each use permanently bumps that spell's SP cost
        # by +1 (mirrors Session.cs:1154 IncrementSpellPointCostModifier).
        # Only spell powers (spell_cost > 0) escalate; charge powers don't.
        sp_uses = bstate.get("player_sp_uses", {}) or {}
        from db import db_talent_ability_costs
        afford = []
        for aid in (abilities or []):
            row = db_talent_ability_costs(str(aid.guid))
            if row is None:
                # Champion signature charge powers (champion_abilities) aren't
                # talents — their costs/casting come from the gamedata seed.
                from db import db_champion_ability_costs
                row = db_champion_ability_costs(str(aid.guid))
            if row:
                cc, sc, aphases, casting = row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0
                if casting != 64:
                    # BasicAction etc.: only on the PLAYER's own main phases.
                    # During the opponent's turn there is no player main phase,
                    # so a BasicAction charge power must not be offered even if
                    # the AI happens to be in FirstMain.
                    if bstate.get("turn_player") != _be.PLAYER:
                        continue
                    if phase is not None and not (aphases & phase_bit):
                        continue  # not activatable this phase (QuickAction exempt)
                eff_sc = sc + (int(sp_uses.get(str(aid.guid), 0)) if sc > 0 else 0)
                if (charges >= cc and sp >= eff_sc
                        and self._champion_thresholds_met(str(aid.guid), bstate)):
                    afford.append(aid)
            else:
                # Unknown cost — include only if phase gating passes (or no
                # phase given) and any gamedata threshold is met.
                if ((phase is None or phase_bit)
                        and self._champion_thresholds_met(str(aid.guid), bstate)):
                    afford.append(aid)
        return afford

    def _champion_thresholds_met(self, ability_guid, bstate):
        """True if the champion's threshold requirements (from gamedata
        champion_abilities.thresholds_json, e.g. Dimmid's [DIAMOND][DIAMOND])
        are met by the player's current threshold counts."""
        from db import db_champion_ability_thresholds
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

    def _bom_has_discard(self, ability_guid):
        """True if the ability's BOM (recursively through ActivateAbility leaves)
        contains a DiscardCardAbilityEffectTemplate. Soothsaying's discard is
        including nested ActivateAbility -> DiscardCard chains."""
        import ability
        return ability.bom_has_discard(_db, ability_guid)

    def _discard_prompt_data(self, ability_guid):
        """Return ``(ability_guid, hand_target_template)`` for a discard.

        Most abilities expose a dedicated discard BOM leaf.  Older extracted
        records such as Wretched Wrangler retain the discard in the typed
        ability contract but not as a materialized leaf, so select the generic
        authored ``a card from your hand`` target as the protocol contract.
        """
        from abilities import bom_leaf_prompt_data
        prompt = bom_leaf_prompt_data(
            _db, str(ability_guid).lower(),
            "DiscardCardAbilityEffectTemplate")
        if prompt and prompt[1]:
            return prompt
        row = _db.execute(
            "SELECT game_text FROM card_abilities_meta "
            "WHERE ability_guid=?", (str(ability_guid).lower(),)).fetchone()
        import re as _re
        if not (row and _re.match(
                r"^\s*(?:\[[^]]+\]\s*)*discard\s+(?:a|one)\s+card\b",
                row[0] or "", _re.IGNORECASE)):
            return None
        target = _db.execute(
            "SELECT template_id FROM target_templates "
            "WHERE lower(game_text)=? AND lower(filter_json) LIKE ? "
            "ORDER BY template_id LIMIT 1",
            ("a card from your hand", "%hand%"),).fetchone()
        return (str(ability_guid).lower(), target[0]) if target else None

    def _ability_requires_discard(self, ability_guid):
        """Return whether activation requires the controller to discard.

        Most discard effects have a typed BOM leaf.  A few older extracted
        abilities (including Wretched Wrangler) encode the activation cost in
        the serialized ability contract but expose only the authored opening
        sentence in the materialized effect rows.  Restrict the fallback to
        an opening ``Discard a card`` cost so opponent-discard effects are not
        mistaken for a controller payment.
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
        from db import db_game_cards_at_location_scalar
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
        from abilities.framework.targeting import (
            legal_targets as _lt, target_uses_both_players)
        out = {}
        for aid in ability_ids:
            ag = str(aid.guid)
            row = _db.execute(
                "SELECT target_template_ids FROM card_abilities_meta "
                "WHERE ability_guid=?", (ag,)).fetchone()
            if not row or not row[0]:
                row = _db.execute(
                    "SELECT target_template_ids FROM champion_abilities "
                    "WHERE ability_guid=?", (ag,)).fetchone()
            if not row or not row[0]:
                # PvE champion powers are granted by talents. Their target
                # contracts come from the source AbilityTemplate and are
                # materialized in talent_abilities.
                row = _db.execute(
                    "SELECT target_template_ids FROM talent_abilities "
                    "WHERE ability_guid=? LIMIT 1", (ag,)).fetchone()
            if not row or not row[0]:
                continue
            try:
                tpls = json.loads(row[0])
            except Exception:
                continue
            cost_tids = {t for t, _ in self._ability_cost_templates(ag)}
            for target_index, tid in enumerate(tpls):
                # Card costs (void/sacrifice/exhaust...) are paid via the
                # client's X-cost dialog (CostInstance events) — never as
                # effect targets.
                if tid in cost_tids:
                    continue
                trow = _db.execute(
                    "SELECT target_kind, is_auto_target, min_target_count, "
                    "max_target_count FROM target_templates WHERE template_id=?",
                    (tid,)).fetchone()
                if not trow:
                    continue
                kind = trow[0] or ""
                auto = int(trow[1] or 0)
                if auto or kind == "PlayerTargetTemplate":
                    # Auto targets (e.g. Poca's 'You' — "Summon a Blaze
                    # Elemental") resolve automatically: never attach a picker.
                    continue
                if kind in ("AbilitySourceCardTargetTemplate",
                            "AbilityCreatedTargetTemplate"):
                    continue
                cands = _lt(_db, session.session_id, self.user_profile["id"],
                            tid, int(champ_uid or 0),
                            both_players=target_uses_both_players(_db, tid),
                            champions=self._champion_targets())
                mn = int(trow[2] or 1)
                mx = int(trow[3] or 1)
                out.setdefault(ag, []).append(
                    (tid, cands, mn, mx, target_index))
        return out

    _ABILITY_COST_FIELD_TYPES = {
        "m_VoidTarget": 16,               # EAbilityCostType.VoidAbilityCostType
        "m_SacrificeTarget": 2,           # SacrificeAbilityCostType
        "m_ExhaustTarget": 1,             # ExhaustAbilityCostType
        "m_DiscardTarget": 8,             # DiscardAbilityCostType
        "m_RevealTarget": 64,             # RevealAbilityCostType
        "m_PutIntoDeckTarget": 32,        # PutIntoDeckAbilityCostType
        "m_PutIntoDeckTarget2": 32,       # PutIntoDeckAbilityCostType
        "m_PutIntoHandTarget": 128,       # PutIntoHandAbilityCostType
        "m_ShuffleIntoDeckTarget": 4,     # ShuffleIntoDeckAbilityCostType
    }

    def _ability_cost_templates(self, ability_guid):
        """[(target_template_id, EAbilityCostType)] parsed from the ability's
        gamedata raw_json cost fields (m_VoidTarget / m_SacrificeTarget /
        m_ExhaustTargets ...).  These are card costs the player pays when
        activating the ability, not effect targets."""
        import re as _re
        row = _db.execute(
            "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
            (ability_guid,)).fetchone()
        if not row or not row[0]:
            return []
        raw = row[0]
        out = []
        for field, ctype in self._ABILITY_COST_FIELD_TYPES.items():
            m = _re.search(
                rf'"{field}"\s*:\s*\{{[^}}]*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"',
                raw)
            if m:
                g = m.group(1).lower()
                if g != "00000000-0000-0000-0000-000000000000":
                    out.append((g, ctype))
                continue
            # Plural array variants (m_ExhaustTargets / m_DiscardTargets).
            m_arr = _re.search(rf'"{field}"\s*:\s*\[(.*?)\]', raw)
            if not m_arr:
                continue
            for g in _re.findall(r'"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"',
                                 m_arr.group(1)):
                g = g.lower()
                if g != "00000000-0000-0000-0000-000000000000":
                    out.append((g, ctype))
        return out

    def _champion_ability_costs(self, session, ability_ids, champ_uid=0):
        """{ability_guid: [(target_template_id, EAbilityCostType, [candidate
        card uids], min, max)]} for the champion ability's CARD costs — the
        client's BattleStateAssignXCost prompts for these (e.g. Bun'jitsu's
        "Void two ready troops you control")."""
        from abilities.framework.targeting import legal_targets as _lt
        out = {}
        for aid in (ability_ids or []):
            ag = str(aid.guid)
            for tid, ctype in self._ability_cost_templates(ag):
                trow = _db.execute(
                    "SELECT min_target_count, max_target_count "
                    "FROM target_templates WHERE template_id=?", (tid,)).fetchone()
                mn = int(trow[0] or 1) if trow else 1
                mx = int(trow[1] or 1) if trow else 1
                cands = _lt(_db, session.session_id, self.user_profile["id"],
                            tid, int(champ_uid or 0), both_players=False,
                            champions=self._champion_targets())
                out.setdefault(ag, []).append((tid, ctype, cands, mn, mx))
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
        import json as _json
        from abilities.framework.targeting import (
            legal_targets as _lt, target_uses_both_players)
        selected_uids = [int(uid) for uid in (selected_uids or [])]
        used = set()
        sacrifices = []
        cost_templates = self._ability_cost_templates(ability_guid)
        for tid, cost_type in cost_templates:
            trow = _db.execute(
                "SELECT target_kind, is_auto_target, min_target_count, "
                "max_target_count FROM target_templates "
                "WHERE template_id=?", (tid,)).fetchone()
            kind = (trow[0] if trow else "") or ""
            auto = bool(int(trow[1] or 0)) if trow else False
            minimum = int(trow[2] or 1) if trow else 1
            maximum = int(trow[3] or 1) if trow else 1
            if auto and kind == "AbilitySourceCardTargetTemplate":
                candidates = [int(champ_uid)]
                available = candidates
            else:
                candidates = _lt(
                    _db, session.session_id, self.user_profile["id"], tid,
                    int(champ_uid), both_players=False,
                    champions=self._champion_targets(), battle_state=bstate)
                available = [uid for uid in selected_uids
                             if uid in {int(c) for c in candidates}
                             and uid not in used]
            if len(available) < minimum:
                return None
            chosen = available[:maximum]
            used.update(chosen)
            if int(cost_type) == 2:
                sacrifices.extend(chosen)

        row = _db.execute(
            "SELECT target_template_ids FROM card_abilities_meta "
            "WHERE ability_guid=?", (ability_guid,)).fetchone()
        if not row or not row[0]:
            row = _db.execute(
                "SELECT target_template_ids FROM champion_abilities "
                "WHERE ability_guid=?", (ability_guid,)).fetchone()
        if not row or not row[0]:
            row = _db.execute(
                "SELECT target_template_ids FROM talent_abilities "
                "WHERE ability_guid=? LIMIT 1", (ability_guid,)).fetchone()
        try:
            target_templates = _json.loads(row[0]) if row and row[0] else []
        except (TypeError, ValueError, _json.JSONDecodeError):
            target_templates = []
        effect_candidates = []
        explicit_required = False
        for tid in target_templates:
            tid = str(tid).lower()
            if any(tid == cost_tid for cost_tid, _ in cost_templates):
                continue
            trow = _db.execute(
                "SELECT target_kind, is_auto_target, min_target_count "
                "FROM target_templates WHERE template_id=?", (tid,)
            ).fetchone()
            kind = (trow[0] if trow else "") or ""
            auto = int(trow[1] or 0) if trow else 0
            if auto or kind in ("PlayerTargetTemplate",
                                "AbilitySourceCardTargetTemplate",
                                "AbilityCreatedTargetTemplate"):
                continue
            explicit_required = explicit_required or bool(
                int(trow[2] or 1) if trow else 1)
            effect_candidates.extend(int(uid) for uid in _lt(
                _db, session.session_id, self.user_profile["id"], tid,
                int(champ_uid),
                both_players=target_uses_both_players(_db, tid),
                champions=self._champion_targets(), battle_state=bstate))
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

    def _push_phase_options_empty(self, session, pl_t, ai_t):
        """Push a PlayerOptionList with champion abilities + playable hand
        QuickActions (instant-speed — castable in ANY priority window).

        Called for stop phases that are NOT main phases so the champion's
        charge/spell power buttons and QuickActions are always interactive when
        the player has priority. Always carries a fresh PlayerUpdated so the
        client's resource / charge / SP display reflects the real battle state.
        """
        import battle_engine as _be
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        # QuickActions may be played in ANY priority window (your turn or the
        # opponent's turn when a stop hands you priority).
        from db import db_hand_quick_actions
        playable = []
        rows = db_hand_quick_actions(session.session_id, self.user_profile["id"])
        resources = bstate.get("player_resources", 0)
        threshold = bstate.get("player_threshold", {})
        fc = self._warzone_troop_count(session, self.user_profile["id"])
        ec = self._warzone_troop_count(session, 0)
        from abilities.framework.statics import effective_cost
        for card_uid, cost, ct, thresh_json, abilities_json in rows:
            ab = []
            if abilities_json:
                try:
                    ab = [g.lower() for g in json.loads(abilities_json)]
                except Exception:
                    pass
            # Quick-action refreshes also need the current instance cost,
            # including opening-hand discounts.
            try:
                cost = effective_cost(_db, session.session_id, bstate,
                                      card_uid)
            except Exception:
                pass
            if self._hand_card_playable(session, card_uid, ct, cost, thresh_json,
                                        ab, resources, threshold, True, fc, ec):
                playable.append(game_engine.SessionCardId(game_engine.UID(card_uid)))
        game.push_options(pl_t, playable)
        # Attach targeting TargetInstances for played spells (target picker).
        self._add_play_target_options(game, session, pl_t, ai_t)
        player_champ_cid = getattr(self, "_player_champ_scid", None)
        if player_champ_cid:
            abilities = getattr(self, "_player_champ_abilities", [])
            if abilities:
                game.add_champion_to_options(
                    pl_t, player_champ_cid,
                    self._filter_affordable_abilities(abilities, bstate,
                                                      _be.current_phase(bstate)),
                    self._discard_costs_for(session, abilities),
                    self._champion_ability_targets(
                        session, abilities,
                        player_champ_cid.uid.to_uint64()
                        if hasattr(player_champ_cid, "uid") else 0),
                    self._champion_ability_costs(
                        session, abilities,
                        player_champ_cid.uid.to_uint64()
                        if hasattr(player_champ_cid, "uid") else 0))
        # Warzone-troop QuickAction abilities (e.g. Living Totem's 3-cost activations)
        # are castable in ANY priority window — include them here for non-main phases.
        affordable = self._affordable_troop_abilities(session, bstate)
        if affordable:
            self._add_troop_ability_options(game, pl_t, session, affordable,
                                             bstate)
        game.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))
        game.push_player_updated(ai_t, champ_id=getattr(self, "_ai_champ_scid", None))
        self._push_warzone_card_updates(game, session, pl_t, ai_t)
        self._send_battle_events(session, game, pl_t)
        log_req(f"    Phase options (priority window): {len(playable)} playable "
                f"quick actions, champ_ab={len(self._filter_affordable_abilities(getattr(self, '_player_champ_abilities', []), bstate, _be.current_phase(bstate))) if getattr(self, '_player_champ_abilities', None) else 0}")

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
        import battle_engine as _be
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        self._auto_declare_force_attackers(session, pl_t, ai_t, bstate, game)
        from db import db_warzone_troops_basic
        rows = db_warzone_troops_basic(session.session_id, self.user_profile["id"])
        ready = []
        for uid, ct, state, attrs in rows:
            state = state or 0
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
        import battle_engine as _be
        from db import (
            db_warzone_troops_basic, db_card_set_attacking_state,
            db_card_state_raw)
        rows = db_warzone_troops_basic(session.session_id,
                                       self.user_profile["id"])
        ai_champ_uid = getattr(self, "_ai_champ_scid", None)
        ai_champ_uid64 = ai_champ_uid.uid.to_uint64() if ai_champ_uid else 0
        attackers = {int(k): int(v)
                     for k, v in (bstate.get("player_attackers") or {}).items()}
        combats = []
        for uid, ct, state, attrs in rows:
            state = state or 0
            attrs = attrs or 0
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
            game.push_attack_declared(
                combat_id, pl_t,
                ai_champ_uid or game_engine.SessionCardId(ai_t), scid)
            state_bits = (game_engine.ECardStates.Attacking |
                          game_engine.ECardStates.HasAttacked)
            if not (attrs & game_engine.ECardAttributes.Steadfast):
                state_bits |= game_engine.ECardStates.Tapped
            db_card_set_attacking_state(session.session_id, u, state_bits)
            trow = _db.execute(
                "SELECT template_guid FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, u)).fetchone()
            tpl_guid = trow[0] if trow else None
            self._card_full_data(game, scid, tpl_guid)
            pushed_state = db_card_state_raw(session.session_id, u) or state_bits
            game.push_card_updated(
                scid, pl_t, game_engine.ECardCollections.Warzone,
                game_engine.ECardTypes.Troop, template_id=tpl_guid,
                state=pushed_state)
            if state_bits & game_engine.ECardStates.Tapped:
                import ability as _abil
                _abil.resolve_triggers(
                    _db, self, game, session, pl_t, ai_t, bstate,
                    "CardTappedEvent", u, self.user_profile["id"])
            cs = game_engine.CombatSessionEventArgs()
            cs.player_id = pl_t
            cs.id = combat_id
            cs.attacker = scid
            cs.blockers = []
            combats.append(cs)
            # Fire "when this attacks" triggers (e.g. Chimera Guard Outrider).
            import ability as _abil
            _abil.resolve_triggers(
                _db, self, game, session, pl_t, ai_t, bstate,
                "CardAttackedEvent", u, self.user_profile["id"])
            _abil.resolve_triggers(
                _db, self, game, session, pl_t, ai_t, bstate,
                "CardAttackedOrBlockedEvent", u, self.user_profile["id"])
            from abilities.framework.keywords.combat import apply_rage_keyword
            apply_rage_keyword(_db, session, self, game, pl_t, ai_t, bstate, u)
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
        import battle_engine as _be
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        attackers = bstate.get("ai_attackers") or {}
        attacker_scids = [game_engine.SessionCardId(game_engine.UID(int(u)))
                          for u in attackers]
        # Flight: an attacker with Flight can only be blocked by a blocker that
        # itself has Flight or SkyGuard — build a per-attacker "flyer" map.
        attacker_attrs = {}
        for u in attackers:
            r = _db.execute(
                "SELECT (ct.attributes | gc.card_attributes | "
                "COALESCE(gc.temporary_attributes, 0)) FROM game_cards gc "
                "JOIN card_templates ct ON ct.guid = gc.template_guid "
                "WHERE gc.session_id=? AND gc.card_uid=?",
                (session.session_id, int(u))).fetchone()
            attacker_attrs[int(u)] = r[0] if r else 0
        rows = _db.execute(
            "SELECT card_uid, "
            "(ct.attributes | gc.card_attributes | COALESCE(gc.temporary_attributes, 0)) "
            "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND user_id=? AND location='warzone' "
            "AND gc.card_type LIKE '%Troop%' AND (gc.card_state & ?) = 0 "
            "AND (ct.attributes | gc.card_attributes | "
            "COALESCE(gc.temporary_attributes, 0)) & ? = 0",
            (session.session_id, self.user_profile["id"],
             game_engine.ECardStates.Tapped,
             game_engine.ECardAttributes.CantBlock)).fetchall()
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        ev = game._make_event(game_engine.PlayerOptionListSessionEventArgs)
        ev.player_id = pl_t
        blocking_id = game_engine.ResourceId.from_str(
            "83659505-152d-4ddc-89df-7c29bdfba16d")
        for uid, battrs in rows:
            can_block_flyers = bool(
                int(battrs or 0) & (game_engine.ECardAttributes.Flight |
                                    game_engine.ECardAttributes.SkyGuard))
            # This blocker's blockable targets: every attacker it may block.
            blockable = []
            for scid, u in zip(attacker_scids, attackers):
                if (int(attacker_attrs.get(int(u)) or 0)
                        & game_engine.ECardAttributes.CantBeBlocked):
                    continue  # Unblockable attacker (e.g. Infiltrator Bot)
                if (attacker_attrs[int(u)] & game_engine.ECardAttributes.Flight
                        and not can_block_flyers):
                    continue
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
        bstate = __import__("battle_engine").load_state(session)
        attackers = [int(uid) for uid in (bstate.get("ai_attackers") or {})]
        if not attackers:
            # Keep the broad board check for callers that ask before attacker
            # declarations are persisted; the DeclareDefense path itself has
            # the attacker list and applies the full pairwise legality test.
            attackers = None
        rows = _db.execute(
            "SELECT gc.card_uid FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.user_id=? "
            "AND gc.location='warzone' AND gc.card_type LIKE '%Troop%' "
            "AND (gc.card_state & ?) = 0 "
            "AND (ct.attributes | gc.card_attributes | "
            "COALESCE(gc.temporary_attributes, 0)) & ? = 0",
            (session.session_id, self.user_profile["id"],
             _ge.ECardStates.Tapped, _ge.ECardAttributes.CantBlock)).fetchall()
        from abilities.framework.statics import can_block
        if attackers is None:
            return bool(rows)
        return any(can_block(_db, session.session_id, bstate, attacker_uid,
                             int(row[0]))
                   for row in rows for attacker_uid in attackers)

    def _check_champion_health(self, session, pl_t, ai_t, bstate):
        """End the game immediately when a champion's health is 0 or less.
        State checks run at EVERY phase (and after chain resolutions), not
        only when combat damage happens — e.g. the AI killing itself with its
        own Fang of the Mountain God must not leave a 0-health champion alive
        until the player's next combat."""
        import commands as _cmd
        ph = int(bstate.get("player_health", 20))
        ah = int(bstate.get("ai_health", 20))
        if ph <= 0:
            _cmd.push_battle_game_end(handler=self, session=session,
                                      winners=[ai_t], losers=[pl_t])
            campaign.handle_battle_gameend(self, _db, session, False, SERVICE_MAIL_UID, UID_TYPE["ServiceCampaign"])
            log_req(f"    Game over: player health {ph} <= 0 (AI wins)")
            return True
        if ah <= 0:
            _cmd.push_battle_game_end(handler=self, session=session,
                                      winners=[pl_t], losers=[ai_t])
            campaign.handle_battle_gameend(self, _db, session, True, SERVICE_MAIL_UID, UID_TYPE["ServiceCampaign"])
            log_req(f"    Game over: AI health {ah} <= 0 (player wins)")
            return True
        return False

    def _push_phase_options(self, session, pl_t, ai_t, phase):
        """Push the PlayerOptionList appropriate for `phase` (used when re-
        granting priority after a chain resolves / at a stop): playable cards for
        main phases, attack options for DeclareAttack, the combat listing at
        damage steps, otherwise just champion abilities + QuickActions."""
        import battle_engine as _be
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

    def _push_combat_listing_phase(self, session, pl_t, ai_t, bstate):
        """Push the current combat listing (no blockers yet) at a damage step
        so the client's BattleStateAssignDamage can auto-commit and send
        AssignDamageOrderTransaction."""
        attackers = bstate.get("player_attackers") or {}
        combats = []
        for i, u in enumerate(attackers):
            scid = game_engine.SessionCardId(game_engine.UID(int(u)))
            combat_id = game_engine.CombatId(pl_t, i + 1)
            cs = game_engine.CombatSessionEventArgs()
            cs.player_id = pl_t
            cs.id = combat_id
            cs.attacker = scid
            cs.blockers = []
            combats.append(cs)
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        if combats:
            game.push_combat_listing(pl_t, combats)
        self._send_battle_events(session, game, pl_t)

    def _player_can_attack_troops(self, session, user_id=None):
        import ai
        return ai.player_can_attack_troops(self, session, user_id)

    def _ai_can_attack_troops(self, session):
        import ai
        return ai.ai_can_attack_troops(self, session)

    def _ai_declare_attackers(self, game, session, ai_t, pl_t, battle_state):
        import ai
        return ai.ai_declare_attackers(self, game, session, ai_t, pl_t, battle_state)

    def _resolve_ai_combat_damage(self, session, pl_t, ai_t, bstate):
        import ai
        return ai.resolve_ai_combat_damage(self, session, pl_t, ai_t, bstate)

    def _resolve_combat_damage(self, session, pl_t, ai_t, bstate,
                               first_strike=False):
        """Resolve the player's combat damage (the player attacks the AI). Thin
        wrapper over the shared ai.resolve_combat with the player as the
        attacker — mirrors the AI-attack path. Returns the (updated) bstate;
        callers check bstate['ai_health'] <= 0 for a player win.
        """
        import ai
        attackers = {int(k): int(v) for k, v in (bstate.get("player_attackers") or {}).items()}
        blockers = {int(k): [int(b) for b in (v or [])]
                    for k, v in (bstate.get("ai_blockers") or {}).items()}
        # The player (attacker) ordered its combat damage among the blockers
        # via AssignDamageOrderTransaction.
        order_map = {int(k): [int(b) for b in (v or [])]
                     for k, v in (bstate.get("player_damage_order") or {}).items()}
        return ai.resolve_combat(self, session, pl_t, ai_t, bstate,
                                 attackers, blockers, pl_t, ai_t, "player_attackers",
                                 order_map=order_map, first_strike=first_strike)

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
            game = game_engine.Game(session.session_id, pl_t, game_engine.UID.make(3, 1000))
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
        from db import db_card_owner, db_set_card_owner_and_discard
        row = db_card_owner(session.session_id, card_uid)
        if not row:
            return None, None
        row_id, owner_uid, tpl_guid = row
        owner_uid = owner_uid or 0
        db_set_card_owner_and_discard(session.session_id, card_uid, owner_uid)
        # The owner's UID: AI owns user_id 0, the human owns their profile id.
        owner_player_uid = ai_t if owner_uid == 0 else pl_t
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
        tpl_d, ct_d, name_d, cost_d, atk_d, def_d, gem_d = self._card_full_data(
            game, scid, tpl_guid, None)
        game.push_card_discarded(scid, owner_player_uid)
        game.push_card_updated(scid, owner_player_uid, game_engine.ECardCollections.Discard,
                               game_engine.card_type_from_db(ct_d) if ct_d else game_engine.ECardTypes.Troop,
                               attack=atk_d, defense=def_d, cost=cost_d,
                               template_id=tpl_d, gems=gem_d)
        game.push_card_moved(scid, owner_player_uid, game_engine.ECardCollections.Discard,
                              game_engine.ECardLocations.Top, 0)
        # The card entered the crypt — "when a card enters an opposing crypt"
        # triggers (Incantation of Fear) fire here.
        import ability as _abil
        import battle_engine as _be
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        _abil.resolve_triggers(_db, self, game, session, pl_t, ai_t, bstate,
                               "CardEnteredZoneEvent", int(card_uid),
                               source_owner_uid=owner_uid)
        return game, owner_player_uid

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

    def _sacrifice_troop(self, game, session, pl_t, ai_t, card_uid):
        """Pay a sacrifice cost (e.g. Abominate's "sacrifice a troop you
        control"): move the troop to its owner's graveyard (no Dead state — a
        sacrifice isn't combat damage). The troop still DIES, so its Deathcry
        triggers (mirrors the client; e.g. Spiritbound Spy -> Phantom)."""
        from db import db_card_basic, db_card_set_sacrifice_state
        row = db_card_basic(session.session_id, card_uid)
        if not row:
            return
        tpl_guid, owner_id = row
        db_card_set_sacrifice_state(session.session_id, card_uid)
        scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
        _tpl, ct, _n, _c, atk, def_, _g = self._card_full_data(game, scid, tpl_guid)
        owner = pl_t if (owner_id or 0) != 0 else ai_t
        game.push_card_updated(scid, owner, game_engine.ECardCollections.Discard, ct,
                               template_id=tpl_guid, attack=atk, defense=def_,
                               attributes=game.card_defs[scid].attributes)
        game.push_card_moved(scid, owner, game_engine.ECardCollections.Discard,
                             game_engine.ECardLocations.Top, 0)
        log_req(f"    Sacrificed troop {hex(card_uid)} to graveyard")
        import ability as _abil
        import battle_engine as _be
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        _abil.resolve_triggers(_db, self, game, session, pl_t, ai_t, bstate,
                               "CardExitedZoneEvent", int(card_uid),
                               source_owner_uid=owner_id)
        _abil.resolve_triggers(_db, self, game, session, pl_t, ai_t, bstate,
                               "CardEnteredZoneEvent", int(card_uid),
                               source_owner_uid=owner_id)
        _abil.resolve_deathcry(game, session, _db, self, pl_t, ai_t,
                               int(card_uid), tpl_guid,
                               bstate)

    def _push_discard_prompt(self, game, session, pl_t, ai_t, bstate,
                             ability_guid=None):
        """Push a class-23 AbilityActivationDataRequired prompt so the PLAYER
        picks a hand card to discard (e.g. a Deathcry that forces each opposing
        champion to discard, like Bloatcap). The follow-up
        SetAbilityActivationDataTransaction discards the chosen card."""
        import battle_engine as _be
        player_champ_scid = getattr(self, "_player_champ_scid", None)
        resolving_ability = ability_guid or bstate.get("resolving_ability", "")
        prompt = self._discard_prompt_data(resolving_ability)
        if not prompt or not prompt[1]:
            log_req("    Deathcry discard: missing metadata prompt target")
            return
        child_ability, target_template = prompt
        req = game_engine.AbilityActivationDataRequiredSessionEventArgs()
        req.player_id = pl_t
        req.ability_instance_id = 1
        req.ability_parent_id = 0
        source_uid = bstate.get("resolving_source_uid")
        source_card_id = (game_engine.SessionCardId(game_engine.UID(
            int(source_uid))) if source_uid else
            (player_champ_scid if player_champ_scid else pl_t))
        req.source_card_id = source_card_id
        req.ability_template_id = game_engine.ResourceId.from_str(child_ability)
        req.effect_group_id = 1
        req.effect_instance_ids = [0]
        req.resolve_chain = False
        game._push(req)
        bstate["pending_discard_ability"] = child_ability
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
        raw_row = _db.execute(
            "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
            (ability_guid,)).fetchone()
        if not raw_row or not raw_row[0]:
            return
        try:
            rec = _j.loads(raw_row[0])
        except Exception:
            return
        vt = (rec.get("m_VoidTarget") or {})
        if not (vt.get("m_Guid") or ""):
            return
        voided = 0
        voided_uids = []
        stats = {"atk": 0, "def": 0}
        for uid in uids:
            row = _db.execute(
                "SELECT gc.card_uid, gc.user_id, gc.location, "
                "COALESCE(ct.attack,0)+COALESCE(gc.card_attack_mod,0), "
                "COALESCE(ct.defense,0)+COALESCE(gc.card_defense_mod,0) "
                "FROM game_cards gc JOIN card_templates ct "
                "ON ct.guid = gc.template_guid "
                "WHERE gc.session_id=? AND gc.card_uid=?",
                (session.session_id, int(uid))).fetchone()
            if not row or row[2] != "warzone":
                continue
            # Bun'jitsu: "+[ATK] equal to the VOIDED TROOPS' [ATK] plus 3" —
            # with two voided troops the summoned token's buff sums BOTH
            # troops' attack/defense, then adds the fixed +3.
            stats["atk"] += int(row[3] or 0)
            stats["def"] += int(row[4] or 0)
            _db.execute(
                "UPDATE game_cards SET location='void', position=0 "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, int(uid)))
            _db.commit()
            scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
            trow = _db.execute(
                "SELECT template_guid FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, int(uid))).fetchone()
            tpl_guid = trow[0] if trow else None
            _tpl, ct, _n, _c, atk, def_, _g = self._card_full_data(
                game, scid, tpl_guid)
            owner = pl_t if (row[1] or 0) != 0 else ai_t
            game.push_card_updated(scid, owner,
                                   game_engine.ECardCollections.Void, ct,
                                   template_id=tpl_guid, attack=atk,
                                   defense=def_)
            game.push_card_moved(scid, owner,
                                 game_engine.ECardCollections.Void,
                                 game_engine.ECardLocations.Top, 0)
            voided += 1
            voided_uids.append(int(uid))
        if stats:
            bstate["champion_voided_stats"] = stats
        if voided_uids:
            bstate.setdefault("ability_lists", {})["VoidedCards"] = \
                list(voided_uids)
        log_req(f"    Champion void targets: {voided} voided "
                f"(stats={stats})")

    def _resolve_stack_item(self, session, pl_t, ai_t, bstate, item, game):
        """Execute the top of the chain (`item`, already popped) and push the
        resolve chain events onto `game` (TopOfChainResolved + RemovedTopOfChain).

        Item kinds:
          - troop:  CastSpells -> Warzone (came out this turn, summoning sick).
          - trigger: a Deathcry (ability.resolve_stack_trigger).
          - spell:   execute the spell BOM then CastSpells -> Discard.
          - ability: a champion ability — resolve_effect BOM + class-23 discard
                     prompt if its BOM has a discard.
        """
        import ability as _abil
        import battle_engine as _be
        kind = item.get("kind")
        instance_id = int(item.get("instance_id", 1))
        game.push_top_of_chain_resolved(instance_id)
        game.push_removed_top_of_chain(instance_id)
        if kind == "troop":
            uid = int(item.get("source_uid") or 0)
            if uid:
                loc_row = _db.execute(
                    "SELECT location FROM game_cards "
                    "WHERE session_id=? AND card_uid=?",
                    (session.session_id, uid)).fetchone()
                if not loc_row or loc_row[0] != "CastSpells":
                    log_req(f"    Troop {uid} already left the chain "
                            f"(loc={loc_row[0] if loc_row else None}) — skipped")
                    _be.save_state(session, bstate)
                    return game
                scid = game_engine.SessionCardId(game_engine.UID(uid))
                from db import db_card_set_warzone_arrival, db_card_with_template, db_set_card_resolved_at
                db_card_set_warzone_arrival(session.session_id, uid)
                db_set_card_resolved_at(session.session_id, uid, self._next_resolve_counter(session))
                crow2 = db_card_with_template(session.session_id, uid)
                if crow2:
                    orow = _db.execute(
                        "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
                        (session.session_id, uid)).fetchone()
                    owner_id = orow[0] if orow else 0
                    owner_sid = pl_t if (owner_id or 0) != 0 else ai_t
                    # Populate the CardDef (cost/atk/def/thresholds/abilities)
                    # so the re-pushed CardUpdated renders the full card — without
                    # it a troop played mid-game (e.g. via a spell) shows zeros.
                    self._card_full_data(game, scid, crow2[0], None)
                    game.push_card_updated(scid, owner_sid, game_engine.ECardCollections.Warzone,
                                           game_engine.card_type_from_db(crow2[1]),
                                           template_id=crow2[0])
                    game.push_card_moved(scid, owner_sid, game_engine.ECardCollections.Warzone,
                                         game_engine.ECardLocations.Top, 0)
                    game.push_troop_card_played(scid, owner_sid)
                    # A troop resolving to the warzone from the stack fires its
                    # own CardEnteredZone triggers plus those of other warzone
                    # troops under the same controller (Adamanthian Scrivener).
                    _abil.resolve_enters_play_triggers(
                        _db, self, game, session, pl_t, ai_t, bstate,
                        uid, owner_id)
                    # CardCastEvent is emitted for every non-resource card,
                    # including permanents.  This is distinct from the
                    # enters-play event above: cards such as Jadiim react to
                    # the act of playing a troop and use that card's cost.
                    bstate["card_cast_copy_target"] = uid
                    _abil.resolve_triggers(
                        _db, self, game, session, pl_t, ai_t, bstate,
                        "CardCastEvent", uid, owner_id)
                    bstate.pop("card_cast_copy_target", None)
        elif kind == "trigger":
            _abil.resolve_stack_trigger(self, game, session, _db, pl_t, ai_t, bstate, item)
        elif kind == "spell":
            uid = int(item.get("source_uid") or 0)
            # The spell must still be on the chain (CastSpells) to resolve.  A
            # Countermagic/interrupt already moved it to the graveyard while it
            # waited for the stack — skip its BOM so a countered spell never
            # also draws/buffs/damages (the client's interrupted card fizzles).
            loc_row = _db.execute(
                "SELECT location, user_id FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, uid)).fetchone()
            loc = loc_row[0] if loc_row else "discard"
            if loc != "CastSpells":
                log_req(f"    Spell {uid} already left the chain "
                        f"(loc={loc}) — countered/interrupted, BOM skipped")
                _be.save_state(session, bstate)
                return game
            # Track actions cast this turn (for ChampionActionsCastThisTurn).
            turn_now = int(bstate.get("turn_number", 1))
            cast_side = "ai" if (loc_row[1] or 0) == 0 else "player"
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
                loc_row[1] if loc_row else self.user_profile["id"])
            bstate["x_cost"] = int(item.get("x_cost") or 0)
            esc_before = int(bstate.get("player_escalation_uses", 0))
            _abil.resolve_played_spell(game, session, _db, self, pl_t, ai_t, bstate,
                                       item.get("ability_guids", []))
            # "When you play an action/... " triggers (e.g. Chimes of the
            # Zodiac's "copy it") fire against the played spell.
            if item.get("source_uid"):
                bstate["card_cast_copy_target"] = item.get("source_uid")
                _abil.resolve_triggers(_db, self, game, session, pl_t, ai_t,
                                       bstate, "CardCastEvent",
                                       item.get("source_uid"),
                                       self.user_profile["id"])
                bstate.pop("card_cast_copy_target", None)
            bstate.pop("player_spell_target", None)
            bstate.pop("resolving_source_uid", None)
            bstate.pop("x_cost", None)
            if uid:
                from db import db_card_with_template
                scid = game_engine.SessionCardId(game_engine.UID(uid))
                # Re-read the card's zone AFTER its BOM resolved: a leaf may
                # have moved it into the deck (Eternal Youth / Chronic Madness
                # "Put this into your deck") — only a spell still in CastSpells
                # goes to the graveyard.
                loc_row2 = _db.execute(
                    "SELECT location FROM game_cards "
                    "WHERE session_id=? AND card_uid=?",
                    (session.session_id, uid)).fetchone()
                loc = loc_row2[0] if loc_row2 else "discard"
                if loc != "deck":
                    # Default: the spent spell goes to the graveyard. A spell
                    # that moved itself into the deck (Eternal Youth's
                    # Escalation "PutThisIntoYourDeck") was already pushed by
                    # the leaf — skip the discard.
                    from db import db_card_discard_spell
                    db_card_discard_spell(session.session_id, uid)
                    # The spell entered the crypt — "when a card enters an
                    # opposing crypt" triggers (Incantation of Fear) fire here.
                    _abil.resolve_triggers(
                        _db, self, game, session, pl_t, ai_t, bstate,
                        "CardEnteredZoneEvent", uid,
                        (loc_row[1] if loc_row else 0))
                    crow2 = db_card_with_template(session.session_id, uid)
                    if crow2:
                        card_owner = (loc_row[1] if loc_row else 0)
                        owner_sid = pl_t if (card_owner or 0) != 0 else ai_t
                        # Populate the CardDef so the graveyard card renders its
                        # full data (cost/atk/def/thresholds/abilities) — a bare
                        # push_card_updated leaves the grave card blank.
                        self._card_full_data(game, scid, crow2[0], None)
                        game.push_card_updated(scid, owner_sid, game_engine.ECardCollections.Discard,
                                               game_engine.card_type_from_db(crow2[1]),
                                               template_id=crow2[0])
                        game.push_card_moved(scid, owner_sid, game_engine.ECardCollections.Discard,
                                             game_engine.ECardLocations.Top, 0)
                # Escalation: push the new multiplier on every copy of this
                # spell the caster owns (any zone) so the client re-renders the
                # escalated text (e.g. Eternal Youth "Gain 8 health").
                esc_after = int(bstate.get("player_escalation_uses", 0))
                if esc_after > esc_before:
                    trow = _db.execute(
                        "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
                        (session.session_id, uid)).fetchone()
                    if trow and trow[0]:
                        copies = _db.execute(
                            "SELECT card_uid, user_id, location FROM game_cards "
                            "WHERE session_id=? AND template_guid=? AND user_id=?",
                            (session.session_id, trow[0],
                             self.user_profile["id"])).fetchall()
                        for cu, owner_id, loc2 in copies:
                            sc = game_engine.SessionCardId(game_engine.UID(int(cu)))
                            _tpl, ct2, _n2, cost2, atk2, def2, _g2 = self._card_full_data(
                                game, sc, trow[0])
                            owner = pl_t if (owner_id or 0) != 0 else ai_t
                            coll = {"deck": game_engine.ECardCollections.Deck,
                                    "hand": game_engine.ECardCollections.Hand,
                                    "discard": game_engine.ECardCollections.Discard,
                                    "void": game_engine.ECardCollections.Void,
                                    "warzone": game_engine.ECardCollections.Warzone,
                                    "CastSpells": game_engine.ECardCollections.CastSpells,
                                    }.get(loc2, game_engine.ECardCollections.Warzone)
                            game.push_card_updated(sc, owner, coll, ct2,
                                                   template_id=trow[0], attack=atk2,
                                                   defense=def2, cost=cost2,
                                                   escalation=esc_after + 1,
                                                   nulling=(loc2 == "deck"))
        elif kind == "ability":
            ag = item.get("ability_guid", "")
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
                    bstate["resolving_owner_id"] = (
                        self.user_profile["id"] if self.user_profile else 0)
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
                fn = _abil.resolve_effect(ag)
                ability_log = ""
                if fn:
                    ability_log = fn(
                        game, session, _db, self, pl_t, ai_t, bstate, ag, None)
                else:
                    # Keep the authoritative BOM path available even when the
                    # compatibility resolver was loaded before a runtime
                    # metadata refresh. Champion powers must not silently
                    # consume their charge and resolve to no effect merely
                    # because the registry lookup missed the GUID.
                    from abilities.framework.resolution import resolve_ability
                    ability_log = resolve_ability(
                        self, game, session, _db, pl_t, ai_t, bstate, ag,
                        bstate.get("resolving_source_uid"),
                        bstate.get("resolving_owner_id", 0), {})
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
                if self._bom_has_discard(ag):
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
            _abil.state_based_deaths(game, session, _db, self, pl_t, ai_t, bstate)
        # Champion state check: a spell/trigger that brought a champion to 0
        # health ends the game right here.
        self._check_champion_health(session, pl_t, ai_t, bstate)
        return game

    def _max_hand_size(self, session):
        """Return the effective maximum hand size for the active battle."""
        try:
            if (session.session_name or "").startswith("camp_"):
                return int(getattr(self, "_campaign_max_hand_size", 7))
        except Exception:
            pass
        return 7

    def _starting_hand_size(self, session):
        """Return the effective opening-hand count for the active battle."""
        try:
            if (session.session_name or "").startswith("camp_"):
                return int(getattr(self, "_campaign_starting_hand_size", 7))
        except Exception:
            pass
        return 7

    def _next_resolve_counter(self, session):
        """Increment and return the resolve counter from battle state.
        Used to stamp resolved_at on cards entering the warzone."""
        import battle_engine as _be
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
        return game

    def _push_warzone_card_updates(self, game, session, pl_t, ai_t):
        """Re-push CardUpdateds for ALL warzone cards (both players) so the
        client's card icons/abilities/state always match the DB (MVC: model ->
        view). Called at the end of every phase/options push, mirroring the
        PlayerUpdated refresh — a shifted ability/attribute that changed the
        DB row is reflected on the board immediately.
        """
        import battle_engine as _be
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
        rows = _db.execute(
            "SELECT card_uid, template_guid, user_id, card_state FROM game_cards "
            "WHERE session_id=? AND location='warzone'",
            (session.session_id,)).fetchall()
        for card_uid, tpl_guid, user_id, cstate in rows:
            if int(card_uid) in chain_sources:
                continue
            scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
            _tpl, ct, _n, _c, _a, _d, _g = self._card_full_data(game, scid, tpl_guid)
            cdef = game.card_defs.get(scid)
            attrs = cdef.attributes if cdef else 0
            owner = pl_t if (user_id or 0) != 0 else ai_t
            related = [game_engine.SessionCardId(game_engine.UID(int(v)))
                       for v in (voided_by.get(str(int(card_uid))) or [])]
            game.push_card_updated(scid, owner, game_engine.ECardCollections.Warzone, ct,
                                   template_id=tpl_guid, attributes=attrs,
                                   state=int(cstate or 0), related_cards=related)

    def _push_champions_warm(self, session, pl_t, ai_t, bstate, game):
        """Re-push both champion CardUpdateds onto `game` so the client's
        State.Cards cache has both ChampionSessionCardIds warm.

        UIBattle.OnTurnPhaseUpdated reads State.Cards[State.Players[p].ChampionSessionCardId]
        for the OPPONENT at StartTurn (and for combat phases). If the AI champion
        isn't in the cache, the client throws KeyNotFoundException, desyncs
        priority and sends RequestPrioritySyncTransaction. Must re-register the
        CardDefs on this fresh Game first, or the CardUpdated carries zero
        abilities and wipes the champion charge/spell buttons. Also carry the
        persisted health into the Game so any PlayerUpdated from this object
        reports the live health (not the 20 default).
        """
        game.ai_health = bstate.get("ai_health", 10)
        game.player_health = bstate.get("player_health", 20)
        ai_champ = getattr(self, "_ai_champ_scid", None)
        if ai_champ:
            game.card_defs[ai_champ] = game_engine.CardDef(
                "AI", game_engine.ECardTypes.Champion, 0,
                bstate.get("ai_health", 10), bstate.get("ai_health", 10),
                [], [game_engine.ResourceId.from_str(g) for g in getattr(self, "_ai_champ_ability_guids", [])])
            game.push_card_updated(ai_champ, ai_t, game_engine.ECardCollections.Champions,
                                   game_engine.ECardTypes.Champion,
                                   template_id=getattr(self, "_ai_champ_guid", None))
        pl_champ = getattr(self, "_player_champ_scid", None)
        if pl_champ:
            pl_abilities = getattr(self, "_player_champ_abilities", [])
            cdef = game_engine.CardDef(
                "Player", game_engine.ECardTypes.Champion, 0,
                bstate.get("player_health", 17), bstate.get("player_health", 17),
                [], list(pl_abilities))
            # Carry the persisted spell-power escalation (player_sp_uses) onto
            # the champion CardDef so the client's button shows the INCREASED
            # SP cost (e.g. Soothsaying 4->5 after one use) — a re-pushed
            # champion without these mods wipes the escalation display.
            for ag, uses in (bstate.get("player_sp_uses", {}) or {}).items():
                if int(uses) > 0:
                    cdef.spell_point_cost_mods[game_engine.ResourceId.from_str(ag)] = int(uses)
            game.card_defs[pl_champ] = cdef
            game.push_card_updated(pl_champ, pl_t, game_engine.ECardCollections.Champions,
                                   game_engine.ECardTypes.Champion,
                                   template_id=getattr(self, "_player_champ_guid", None))

    def _advance_to_priority(self, session, pl_t, ai_t, bstate):
        """Auto-advance the human's turn through non-stop phases.

        The server pushes ONE phase per packet. If the current phase is NOT a
        stop for the turn player (and not PickGoesFirst/Mulligan, which can
        never be skipped), the server auto-passes it — pushes the phase and
        immediately advances. It stops when it reaches a stop phase (player gets
        priority + GreenLight) or the turn ends (handed to the AI). Returns
        True if the player now holds priority.
        """
        import battle_engine as _be
        while True:
            phase = _be.current_phase(bstate)
            if self._check_champion_health(session, pl_t, ai_t, bstate):
                return False
            # Start of the player's turn: reset the combat decision. The actual
            # ready-troop check happens at Prep (StartedATurnOnYourSide is only
            # assigned there, so a troop that survived to this turn is flagged
            # mid-turn, not at StartTurn). Also re-push both champions so the
            # client's OnTurnPhaseUpdated (State.Cards[ChampionSessionCardId])
            # doesn't KeyNotFound at StartTurn / combat phases.
            if phase == game_engine.ETurnPhases.StartTurn and bstate.get("turn_player") == _be.PLAYER:
                bstate["player_has_ready_troop"] = False
                bstate.pop("player_attackers", None)
                bstate["turn_phases"] = _be.BASE_TURN_PHASES
                _be.save_state(session, bstate)
                warm = game_engine.Game(session.session_id, pl_t, ai_t)
                # "At the start of your turn" triggers (e.g. Fang of the
                # Mountain God: "This deals 1 damage to you.").  The AI side
                # fires these in ai.run_ai_turn; the player side was missing.
                import ability as _abil_start
                _abil_start.resolve_triggers(
                    _db, self, warm, session, pl_t, ai_t, bstate,
                    "TurnStartedEvent", None, self.user_profile["id"])
                self._push_champions_warm(session, pl_t, ai_t, bstate, warm)
                self._send_battle_events(session, warm, pl_t)
                log_req("    Player turn start: combat decision deferred to Prep; champions re-pushed")
            # End of turn: hand over to the AI.
            if phase == game_engine.ETurnPhases.EndTurn:
                # "At the end of your turn" triggers for the player's cards.
                import ability as _abil_end
                g_end = self._fresh_game(session, pl_t, ai_t, bstate)
                _abil_end.resolve_triggers(
                    _db, self, g_end, session, pl_t, ai_t, bstate,
                    "TurnEndedEvent", None, self.user_profile["id"])
                # "Until end of turn" attribute grants (e.g. Dimmid's
                # Lifedrain) expire NOW — the controller's turn is ending —
                # and the warzone cards are re-pushed so the client drops the
                # badges immediately instead of at the next Ready step.
                from abilities.framework._shared import (
                    clear_combat_damage, clear_expired_temporary_attributes)
                # Cleanup order matters: remove combat damage while the
                # outgoing turn's temporary defense bonuses still apply.
                clear_combat_damage(_db, session.session_id)
                clear_expired_temporary_attributes(
                    _db, session.session_id, self.user_profile["id"],
                    "end_turn", clear_stat_buffs=True)
                for wzr in _db.execute(
                        "SELECT card_uid, template_guid, user_id FROM game_cards "
                        "WHERE session_id=? AND location='warzone'",
                        (session.session_id,)).fetchall():
                    cu, tpl, card_user_id = wzr
                    scid = game_engine.SessionCardId(game_engine.UID(cu))
                    _tpl, ct, _n, _c, _a, _d, _g = self._card_full_data(
                        g_end, scid, tpl)
                    crow = _db.execute(
                        "SELECT card_state FROM game_cards WHERE session_id=? "
                        "AND card_uid=?", (session.session_id, cu)).fetchone()
                    g_end.push_card_updated(
                        scid, pl_t if card_user_id else ai_t,
                        game_engine.ECardCollections.Warzone,
                        ct, template_id=tpl,
                        state=int(crow[0]) if crow else 0)
                self._send_battle_events(session, g_end, pl_t)
                if getattr(self, "_autoplay_drive_ai_turn", False):
                    # Headless harness drives the AI turn itself (with proper
                    # resume at opponent stops).  Leave turn_player as PLAYER
                    # and let the harness hand over.
                    _be.save_state(session, bstate)
                    return False
                next_player = _be.next_turn_player(bstate)
                bstate["turn_player"] = next_player
                bstate["player_passed"] = False
                bstate["ai_passed"] = False
                bstate["phase_idx"] = 0
                bstate["turn_phases"] = _be.BASE_TURN_PHASES
                if next_player == _be.AI:
                    bstate["ai_resource_played_this_turn"] = False
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
                from db import db_hand_card_count
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
            if phase == game_engine.ETurnPhases.Draw:
                # The human draws a card at the Draw phase — except on their very
                # first turn when they chose to play first.
                if bstate.get("turn_number", 1) > 1 or bstate.get("player_draws_first_turn"):
                    if self._player_draw_card(game, session, pl_t):
                        # Deck-out: the player drew from an empty deck and lost.
                        return False
            elif phase == game_engine.ETurnPhases.Prep:
                # Prep: refill current resources to max for the turn.
                bstate["player_resources"] = bstate.get("player_total_resources", 0)
                from abilities.framework._shared import (
                    clear_expired_temporary_attributes)
                clear_expired_temporary_attributes(
                    _db, session.session_id, self.user_profile["id"],
                    "start_turn", clear_stat_buffs=True)
                # Clear summoning sickness from warzone troops: they now have
                # StartedATurnOnYourSide (survived to this turn). Persist the
                # state in game_cards.card_state (DB) so a reconnect and the
                # combat-phase decision can reconstruct it.
                wz_rows = _db.execute(
                    "SELECT card_uid, template_guid FROM game_cards "
                    "WHERE session_id=? AND user_id=? AND location='warzone'",
                    (session.session_id, self.user_profile["id"])).fetchall()
                for wzr in wz_rows:
                    scid = game_engine.SessionCardId(game_engine.UID(wzr[0]))
                    # Ready/untap: clear combat states (Tapped, Attacking,
                    # HasAttacked, Blocking, HasBlocked) and CameOutThisTurn;
                    # set StartedATurnOnYourSide.
                    import game_engine as _ge
                    attrs_row = _db.execute(
                        "SELECT temporary_attributes FROM game_cards "
                        "WHERE session_id=? AND card_uid=?",
                        (session.session_id, int(wzr[0]))).fetchone()
                    clear_mask = (_ge.ECardStates.CameOutThisTurn |
                                  _ge.ECardStates.Tapped |
                                  _ge.ECardStates.Attacking |
                                  _ge.ECardStates.HasAttacked |
                                  _ge.ECardStates.Blocking |
                                  _ge.ECardStates.HasBlocked)
                    if (attrs_row and int(attrs_row[0] or 0)
                            & _ge.ECardAttributes.CantReadyAutomatically):
                        clear_mask &= ~_ge.ECardStates.Tapped
                    _db.execute(
                        "UPDATE game_cards SET card_state = (card_state | ?) & ~?, "
                        "card_damage = 0 "
                        "WHERE session_id=? AND card_uid=?",
                        (_ge.ECardStates.StartedATurnOnYourSide,
                         clear_mask,
                         session.session_id, int(wzr[0])))
                    self._card_full_data(game, scid, wzr[1])
                    tpl = self._template_by_guid(wzr[1])
                    ct = game_engine.card_type_from_db(tpl[1]) if tpl else game_engine.ECardTypes.Troop
                    from db import db_card_state_raw
                    pstate = db_card_state_raw(session.session_id, int(wzr[0]))
                    if not pstate:
                        pstate = _ge.ECardStates.StartedATurnOnYourSide
                    game.push_card_updated(scid, pl_t, game_engine.ECardCollections.Warzone, ct,
                                          template_id=wzr[1], state=pstate)
                _db.commit()
                # "AfterCardsReadyOnPlayersTurn" effects (e.g. Nazhk's
                # CantReadyAutomatically) must survive through this ready
                # step, then expire for the rest of the turn.
                clear_expired_temporary_attributes(
                    _db, session.session_id, self.user_profile["id"],
                    "prep", clear_stat_buffs=True)
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

    def _run_ai_turn(self, session, pl_t, ai_t, battle_state, start_idx=0):
        import ai
        return ai.run_ai_turn(self, session, pl_t, ai_t, battle_state, start_idx)

    def _save_player_stops(self, user_id, self_stops, opp_stops):
        """Persist a player's phase-stop preferences (survive across battles)."""
        import json as _j
        from db import db_card_save_player_stops
        db_card_save_player_stops(user_id, _j.dumps(self_stops), _j.dumps(opp_stops))

    def _load_player_stops(self, user_id):
        """Load a player's saved phase-stop preferences, or (None, None)."""
        from db import db_card_load_player_stops
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

    def _resolve_card_ref(self, card_ref):
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
        row = _db.execute(
            "SELECT ci.template_guid, ct.card_type, ct.name, ct.cost, ct.attack, ct.defense "
            "FROM card_instances ci JOIN card_templates ct ON ci.template_guid=ct.guid "
            "WHERE ci.instance_id=?", (card_ref,)).fetchone()
        if row:
            return row[0], row[1], row[2], row[3] or 0, row[4] or 0, row[5] or 0
        return None, None, None, 0, 0, 0

    # Attribute-grant effect templates -> ECardAttributes bit they add.
    # (CardModifierAbilityEffectTemplate with an AttributeModifier, from
    # gamedata AbilityEffectTemplate). Used to compute a card's EFFECTIVE
    # attributes = static template attributes + attributes its passives grant
    # (e.g. Gemsoul Feeder's "This gets Lifedrain in all zones" passive grants
    # SpiritDrain). Attributes render as icons and are persistent, so this is
    # resolved at card setup, not during combat.
    _ATTRIBUTE_GRANT_EFFECTS = {
        "809ef966-c285-a302-1d9d-31ac3aa739c3": "SkyGuard",
        "c890fa6a-5cef-c3fa-9d3d-08731bb43cf5": "Immortal",
        "25f1ee9b-e0a7-82e1-1c68-58b7ba74a13e": "CantBlock",
        "1ab49be9-c3bd-9968-09ac-7005ddc7d235": "Speed",
        "ff72caf6-fa76-3771-da4f-e930f63bb0a5": "SpiritDrain",
        "6ddc0f5f-9caa-96e8-a48d-f3d6cd212050": "Defensive",
        "ba28b843-8e94-1c2f-65ed-0f3568ae0b4d": "Steadfast",
        "153d83c1-c8bd-3939-d6e2-209b93f21ae4": "FirstStrike",
        "4fbd9fa8-e730-2081-cde0-fe93ccb80e1b": "Flight",
        "bf00d4e4-e081-5ab3-9a3b-1ec0101a8b31": "Juggernaught",
        "3787c5ec-3604-3b64-fb20-6daf3d17d139": "SpellShield",
        "5a4e0185-35a9-8580-c1ab-0c85fbfb1e3e": "QuickAction",
        "26a9acaa-2713-e582-f9b6-b01997a20f59": "CantBeBlocked",
        "3d6f6852-7895-21b8-12fc-50a786ca405c": "CantBlock",
        "2923f5de-b0ce-776e-a7d6-df8a9f483bed": "CantReadyAutomatically",
        "cfd94daa-26c4-b97e-c370-b43b6461c88c": "CantAttack",
        "0ed9bb83-458b-e0b7-b8f7-3574128b2140": "Boon",
        "26d35928-e617-007d-1910-1c5fae022684": "QuickAction",
        "1e82cc24-4ce9-93fa-1c38-4d5993dcec26": "ForceAttack",
        "72f51755-13b1-b31b-c9c2-eb0322671153": "EntersPlayExhausted",
        "b201ce32-1a98-cfe8-ff78-cf47829c1515": "CantReadyAutomatically",
        "47f6567e-8d6e-e73b-01f0-1b72e4ab0e13": "CantReadyAutomatically",
        "1e4592ab-6e90-8011-f443-0cc0f5df3a9a": "CantAttack",  # CantAttack|CantBlock
        "fb7bafd5-c6c5-6573-c2dc-b2b387cd26dd": "FirstStrike",
        "adcdba20-2141-3862-79e0-d8dffe1e97de": "Flight",
        "9b68061f-79da-c09c-5fdd-dc0c120156a8": "SpiritDrain",
        "a0542356-97f6-8df5-2e92-16e589cd911e": "Steadfast",
        "8a569e75-0ef5-3619-437a-ee48f7b2e84c": "Immortal",
        "b90767f8-0bd4-df1e-6ae2-f3018bae68bd": "SpellShield",
        "c952fbce-42a4-0d81-f1f0-07a0efb2765f": "CantBlock",
    }

    def _granted_attributes(self, ability_guids):
        """Effective attribute bits granted by a card's ability passives.

        Walks each ability's BOM (ability_effects) and ORs in the attribute any
        AttributeModifier grant effect adds. Static template attributes are
        separate (card_templates.attributes).

        **Skip manual abilities** (is_manual=1): their effects only apply when
        the player pays the cost and activates the ability — they are NOT passive
        keywords. Only triggered/passive abilities (no manual flag) and abilities
        with a trigger event type get their BOM effects counted as passive
        attribute grants.
        """
        from db import db_ability_effects, db_ability_meta_targets
        bits = 0
        for ag in (ability_guids or []):
            meta = db_ability_meta_targets(ag)
            if meta and meta[4]:  # is_manual — skip manual activations
                continue
            # Triggered effects modify their resolved target when the trigger
            # fires; they are not persistent keywords of the source card.
            # Giant Butterfly is the important example: its ability grants
            # Defensive to opposing troops after it damages a champion.  The
            # old scanner treated that target-side effect as a keyword on the
            # Butterfly itself, making the transformed card unable to attack.
            # CardCreatedEvent abilities that explicitly apply in all zones
            # are the exception: those are effectively static card data.
            if meta and meta[1]:
                if "CardCreatedEvent" not in str(meta[1]) or \
                        "all zones" not in str(meta[2] or "").lower():
                    continue
            for eg in db_ability_effects(ag):
                name = self._ATTRIBUTE_GRANT_EFFECTS.get(eg)
                if name:
                    bits |= getattr(game_engine.ECardAttributes, name, 0)
        return bits

    def _card_full_data(self, game, scid, template_guid, instance_id=None):
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
        t = self._template_by_guid(template_guid)
        if t:
            tpl_guid, ct_name, name, cost, atk, def_ = t
            ct = game_engine.card_type_from_db(ct_name)
        else:
            # Champion templates — PvP champs in champion_templates_extended,
            # PvE champs (and some PvP) in champion_templates.  Try both.
            from db import db_is_champion_template, db_champion_ability_guids
            if db_is_champion_template(template_guid):
                tpl_guid, ct_name, name, cost, atk, def_ = (
                    template_guid, "Champion", "Champion", 0, 0, 20)
                ct = game_engine.ECardTypes.Champion
            else:
                tpl_guid = "00000000-0000-0000-0000-000000000000"
                ct, name, cost, atk, def_ = game_engine.ECardTypes.Troop, "Card", 0, 0, 0
        shards = []
        attributes = game_engine.ECardAttributes.Unknown
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
        orig_tpl = None
        card_location = None
        card_owner_id = None
        from db import db_card_instance_full
        if scid is not None:
            irow = db_card_instance_full(game.session_id.uid64, scid.uid.uid64)
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
                except Exception:
                    pass
                try:
                    tb = json.loads(irow[6] or "{}")
                    temp_atk = int(tb.get("atk", 0) or 0)
                    temp_def = int(tb.get("def", 0) or 0)
                except Exception:
                    pass
            lrow = _db.execute(
                "SELECT user_id, location FROM game_cards WHERE session_id=? "
                "AND card_uid=?", (game.session_id.uid64,
                                    scid.uid.uid64)).fetchone()
            card_owner_id = lrow[0] if lrow else None
            card_location = lrow[1] if lrow else None
        cost_mod = int(irow[7] or 0) if irow else 0
        cost_mod_json = irow[8] if irow else None
        if cost_mod_json and str(cost_mod_json).strip() not in ("[]", "{}", ""):
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
        from db import db_card_template_thresholds
        if tpl_guid != "00000000-0000-0000-0000-000000000000":
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
                from db import db_champion_ability_guids
                carow = db_champion_ability_guids(tpl_guid)
                if carow:
                    srow = (None, json.dumps(carow), None)
        # Ability list: the instance's persisted list wins (Shift moves
        # abilities between cards); empty instance list falls back to the
        # template's printed list.  NOTE: `'[]'` (an empty JSON array) must
        # ALSO count as empty — champions' game_cards.card_abilities is seeded
        # as '[]', and treating it as authoritative would wipe the champion's
        # signature charge powers (db_champion_ability_guids) on every re-push,
        # leaving PvP champions with no charge buttons.
        ab_src = inst_abilities_json
        if not ab_src or str(ab_src).strip() in ("[]", "null", "{}"):
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
        gem_type = HCPHandler._card_gem_type(self, game, scid, instance_id)
        if gem_type:
            try:
                gem_row = _db.execute(
                    "SELECT abilities_json FROM gem_templates WHERE gem_type=?",
                    (gem_type,)).fetchone()
                gem_guids = json.loads(gem_row[0]) if gem_row and gem_row[0] else []
            except Exception:
                gem_guids = []
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
                merged = [str(g).lower() for g in current]
                if not merged:
                    merged = list(ability_guids)
                else:
                    for gem_guid in gem_guids:
                        if str(gem_guid).lower() not in merged:
                            merged.append(str(gem_guid).lower())
                _db.execute(
                    "UPDATE game_cards SET card_abilities=? "
                    "WHERE session_id=? AND card_uid=?",
                    (json.dumps(merged), game.session_id.uid64, scid.uid.uid64))
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
            subtype_row = _db.execute(
                "SELECT subtype FROM card_templates WHERE guid=?",
                (tpl_guid,)).fetchone()
            card_subtype = (subtype_row[0] if subtype_row else "") or ""
            # Scene target filters can inspect dynamic IntAttr markers (for
            # example, Taming Dire Toad only grants Untamed to non-Tamed
            # Dire Toads).  Reconstruct those markers from the card's current
            # ability list before evaluating the scene filter.
            scene_int_attrs = {}
            scene_int_attrs.update(persisted_int_attrs)
            for _ag in ability_guids:
                for _eg, _et, _ep in _db.execute(
                        "SELECT effect_guid,effect_type,param FROM ability_effects "
                        "WHERE ability_guid=?", (_ag,)).fetchall():
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
                meta_row = _db.execute(
                    "SELECT target_template_ids FROM card_abilities_meta WHERE ability_guid=?",
                    (scene_ag,)).fetchone()
                try:
                    target_ids = json.loads(meta_row[0] or "[]") if meta_row else []
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
                    filter_row = _db.execute(
                        "SELECT filter_json FROM target_templates WHERE template_id=?",
                        (target_id,)).fetchone()
                    try:
                        filter_data = json.loads(filter_row[0] or "{}") if filter_row else {}
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
                _db.execute(
                    "UPDATE game_cards SET card_abilities=? WHERE session_id=? AND card_uid=?",
                    (json.dumps(ability_guids), game.session_id.uid64,
                     scid.uid.uid64))
                _db.commit()
        # Effective attributes = static template attributes + any attributes
        # granted by the card's CURRENT passives. Attributes render as icons on
        # the card and must be present the whole time (not computed only during
        # combat). E.g. Gemsoul Feeder's "This gets Lifedrain in all zones"
        # passive (44605164) grants SpiritDrain via a CardModifier
        # AttributeModifier.
        attributes |= self._granted_attributes(ability_guids)
        # Instance-persisted attributes (apply_attribute_grant writes the
        # template attrs + any granted keywords into game_cards.card_attributes,
        # e.g. Inner Conflict's permanent "can't attack or block"). Without
        # this OR the icons vanish on the next CardUpdated re-push.
        attributes |= inst_attrs | temp_attrs
        # Permanent + this-turn buffs (apply_card_stat_mod) also contribute.
        atk += atk_mod + perm_atk + temp_atk
        def_ += def_mod + perm_def + temp_def
        # Cost modifiers (static card_cost_mod + dynamic formula entries).
        cost = max(0, cost + cost_mod)
        # Continuous static abilities (WhileCardInPlay / Permanent): dynamic
        # self-bonuses ("+2/+2 for each card in your hand"), auras ("Troops
        # you control have +2/+2") and zone-wide cost modifiers ("Your
        # artifacts in all zones have cost -1").  Computed from gamedata so the
        # displayed card and its playable cost always match the current board.
        if scid is not None and tpl_guid != "00000000-0000-0000-0000-000000000000":
            try:
                from abilities.framework.statics import effective_deltas
                bstate = getattr(self, "_current_bstate", None)
                if bstate is None:
                    try:
                        row = _db.execute(
                            "SELECT turn_order_json FROM game_sessions "
                            "WHERE session_id=?",
                            (str(game.session_id.uid64),)).fetchone()
                        data = json.loads(row[0]) if row and row[0] else {}
                        bstate = data if isinstance(data, dict) else {}
                    except Exception:
                        bstate = {}
                    self._current_bstate = bstate
                static = effective_deltas(_db, game.session_id.uid64, bstate,
                                          scid.uid.uid64)
                atk += static["atk"]
                def_ += static["def"]
                cost = max(0, cost + static["cost_mod"])
                attributes |= static["attrs"]
            except Exception:
                pass
        # TEMPORARY combat damage: show the defense reduced by damage taken
        # (the client colors values below the template base red). The troop
        # "heals" when card_damage is cleared at its controller's Prep.
        def_ = max(0, def_ - dmg)
        game.card_defs[scid] = game_engine.CardDef(name, ct, cost, atk, def_, shards, abilities, attributes)
        # IntAttr modifiers are transmitted separately from keyword
        # attributes. Preserve metadata-defined Untamed on the final card
        # definition (the definition above replaces any earlier placeholder)
        # so the Taming Sphere's IntAttrFilter can select this Dire Toad.
        if scid is not None:
            untamed = False
            for _ag in ability_guids:
                _has_untamed = _db.execute(
                    "SELECT 1 FROM ability_effects WHERE ability_guid=? "
                    "AND effect_type='CardModifierAbilityEffectTemplate' "
                    "AND LOWER(param) LIKE '%untamed%' LIMIT 1", (_ag,)).fetchone()
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
        # Escalation: the client renders "Gain ESC:4 health" as "Gain N health"
        # from CardUpdated.escalation — seed it with the caster's next
        # escalation multiplier (uses + 1) so the preview is 4, then 8, ...
        for ag in ability_guids:
            gt = _db.execute(
                "SELECT game_text FROM card_abilities_meta WHERE ability_guid=?",
                (ag,)).fetchone()
            if gt and gt[0] and "esc:" in gt[0].lower():
                esc_bstate = getattr(self, "_current_bstate", None) or {}
                game.card_defs[scid].escalation = int(
                    esc_bstate.get("player_escalation_uses", 0)) + 1
                break
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
            game.card_defs[scid].gems = gem_type
            # Rage display: CardUpdated.rage drives the client's Rage icon, and
            # it was never populated (always 0).  Use the same authoritative
            # static-ability evaluation as combat (template rage_value + granted
            # "Rage 1 in all zones" gems, gated by the ability's threshold
            # condition) so the card shows the Rage it actually has.
            try:
                from abilities.framework.statics import effective_stats
                bstate = getattr(self, "_current_bstate", None) or {}
                _atk, _def, _attrs, _flags, rage = effective_stats(
                    _db, game.session_id.uid64, bstate, scid.uid.uid64)
                game.card_defs[scid].rage = int(rage or 0)
            except Exception:
                game.card_defs[scid].rage = 0
        return tpl_guid, ct, name, cost, atk, def_, gem_type

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
        from db import db_card_original_template, db_card_template_full, db_card_revert_to_template
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
        attributes |= self._granted_attributes(ability_guids)
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
        from db import db_get_card_abilities, db_card_sync_abilities
        ab_json, attrs = db_get_card_abilities(template_guid)
        ab_json = ab_json or "[]"
        attrs = int(attrs or 0)
        try:
            ability_guids = [g.lower() for g in json.loads(ab_json)]
        except Exception:
            ability_guids = []
        attrs |= self._granted_attributes(ability_guids)
        db_card_sync_abilities(
            session.session_id, card_uid, ab_json, attrs, template_guid,
            commit=commit)
        return attrs

    def _card_uses(self, session, card_uid):
        """Return the per-ability usage dict for a card instance."""
        return db_card_uses(session.session_id, card_uid)

    def _bump_card_use(self, session, card_uid, ability_guid):
        """Increment a card instance's usage of an ability (UsesPerGame/Turn)."""
        return db_bump_card_use(session.session_id, card_uid, ability_guid)

    def _remove_one_shot_ability(self, session, card_uid, ability_guid,
                                  game, pl_t, ai_t, bstate=None):
        """Consume a one-shot ability on this individual card instance.

        ``uses_per_game=1`` is the extracted representation of ONE-SHOT.
        Usage counters still gate activation, but the client also needs the
        current per-instance ability list updated or it will continue to show
        the used power on the card.  Recompute effective attributes and push a
        complete CardUpdated so both the server and client agree.
        """
        meta = _db.execute(
            "SELECT uses_per_game FROM card_abilities_meta "
            "WHERE ability_guid=?", (str(ability_guid).lower(),)).fetchone()
        if not meta or int(meta[0] or 0) != 1:
            return False
        row = _db.execute(
            "SELECT template_guid, user_id, location, card_state "
            "FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(card_uid))).fetchone()
        if not row:
            return False
        abilities = self._card_ability_list(session, card_uid)
        ag = str(ability_guid).lower()
        if ag not in abilities:
            return False
        abilities.remove(ag)
        base_row = _db.execute(
            "SELECT attributes FROM card_templates WHERE guid=?", (row[0],)
        ).fetchone()
        base_attrs = int(base_row[0] or 0) if base_row else 0
        attributes = base_attrs | self._granted_attributes(abilities)
        _db.execute(
            "UPDATE game_cards SET card_abilities=?, card_attributes=? "
            "WHERE session_id=? AND card_uid=?",
            (json.dumps(abilities), attributes, session.session_id,
             int(card_uid)))
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

    def _apply_power_shifted_triggers(self, session, target_uid, game):
        """Resolve PowerShiftedEvent triggers on a card a power was shifted onto.

        A shifted-on target may carry abilities that fire when a power is
        shifted onto it (e.g. Deepgaze Acolyte's "gets +1[ATK]/+1[DEF]"). Each
        such ability's game text is parsed for "+N[ATK]" / "+M[DEF]" and the
        buff is persisted on game_cards.card_attack_mod / card_defense_mod (so
        reconnects, the Prep re-push and combat all see it) and reflected on
        the pushed CardUpdated. Returns (atk_mod, def_mod).
        """
        import re as _re
        ability_guids = self._card_ability_list(session, target_uid)
        atk_mod = 0
        def_mod = 0
        if ability_guids:
            ph = ",".join("?" * len(ability_guids))
            rows = _db.execute(
                f"SELECT ability_guid, game_text FROM card_abilities_meta "
                f"WHERE trigger_event_type=? AND ability_guid IN ({ph})",
                ("Game.Shared.Mechanics.PowerShiftedEvent",) + tuple(ability_guids)).fetchall()
            for ag, text in rows:
                ma = _re.search(r'\+(\d+)\s*\[ATK\]', text or "")
                md = _re.search(r'\+(\d+)\s*\[DEF\]', text or "")
                if ma:
                    atk_mod += int(ma.group(1))
                if md:
                    def_mod += int(md.group(1))
                log_req(f"    PowerShifted trigger {ag[:8]}: +{atk_mod}ATK/+{def_mod}DEF")
            if atk_mod or def_mod:
                _db.execute(
                    "UPDATE game_cards SET card_attack_mod=card_attack_mod+?, card_defense_mod=card_defense_mod+? "
                    "WHERE session_id=? AND card_uid=?",
                    (atk_mod, def_mod, session.session_id, int(target_uid)))
                _db.commit()
        return atk_mod, def_mod

    def _shift_ability_between(self, session, pl_t, ai_t, source_uid, target_uid,
                               ability_guid, game):
        """Move a granted ability from one card instance to another (ShiftPower).

        Removes `ability_guid` from the source's card_abilities, adds it to the
        target's, then recomputes both cards' effective attributes (the shifted
        ability's BOM may grant an attribute, e.g. Lifedrain -> SpiritDrain).
        Persists to game_cards and pushes CardUpdateds for both cards. The
        template remains the canonical source; a Reversion resets either card.
        """
        # Load both instances' current ability lists.
        srow = _db.execute(
            "SELECT template_guid, user_id, card_type FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(source_uid))).fetchone()
        trow = _db.execute(
            "SELECT template_guid, user_id, card_type FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(target_uid))).fetchone()
        if not srow or not trow:
            return False
        src_ab = [a for a in self._card_ability_list(session, source_uid) if a != ability_guid]
        tgt_ab = self._card_ability_list(session, target_uid)
        if ability_guid not in tgt_ab:
            tgt_ab.append(ability_guid)
        # Recompute effective attributes from the (possibly changed) ability lists.
        src_attrs = self._granted_attributes(src_ab)
        tgt_attrs = self._granted_attributes(tgt_ab)
        # Include the template's static attributes.
        for row, ab, uid in ((srow, src_ab, source_uid), (trow, tgt_ab, target_uid)):
            st = _db.execute(
                "SELECT attributes FROM card_templates WHERE guid=?", (row[0],)).fetchone()
            base = int(st[0]) if st and st[0] else 0
            eff = base | self._granted_attributes(ab)
            _db.execute(
                "UPDATE game_cards SET card_abilities=?, card_attributes=? "
                "WHERE session_id=? AND card_uid=?",
                (json.dumps(ab), eff, session.session_id, int(uid)))
        _db.commit()
        # A power was shifted ONTO the target: resolve any of its own
        # PowerShiftedEvent triggers (e.g. Deepgaze Acolyte +1/+1). Persists
        # the buff (card_attack_mod / card_defense_mod); _card_full_data below
        # folds it into the pushed stats.
        self._apply_power_shifted_triggers(session, target_uid, game)
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
            from db import db_card_template_field
            st_val = db_card_template_field(row[0], "attributes")
            if st_val:
                base = int(st_val)
            cdef.attributes = base | self._granted_attributes(ab)
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
            crow = _db.execute(
                "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
                (session.session_id, int(uid))).fetchone()
            cstate = int(crow[0]) if crow and crow[0] else 0
            game.push_card_updated(scid, owner, game_engine.ECardCollections.Warzone, ct,
                                   template_id=row[0], attributes=cdef.attributes,
                                   state=cstate)
        log_req(f"    ShiftPower: {ability_guid[:8]} {hex(source_uid)} -> {hex(target_uid)} "
                f"(src_ab={src_ab} tgt_ab={tgt_ab})")
        return True

    def _activate_troop_ability(self, session, pl_t, ai_t, bstate, source_uid,
                                ability_guid, inner_bytes):
        """Activate a manual ability on a warzone troop (e.g. Gemsoul Feeder's Shift).

        Validates the cost/phase/uses gates, pays the resource cost, bumps the
        instance's usage, records the Shift source+target in battle state, then
        resolves the ability's BOM (ability_effects). The BOM's
        TACAbilityEffectTemplate leaf dispatches to ShiftPower generically via
        the serialized TAC (never hardcoded).
        """
        m = _db.execute(
            "SELECT casting_behavior, activation_cost, uses_per_game, uses_per_turn, "
            "exhausts_on_use "
            "FROM card_abilities_meta WHERE ability_guid=?", (ability_guid,)).fetchone()
        casting = m[0] if m else 64
        cost = m[1] if m else 0
        upg = m[2] if m else 0
        upt = m[3] if m else 0
        exh = m[4] if m else 0
        variable_x, variable_min = HCPHandler._ability_x_cost_metadata(ability_guid)
        resources = bstate.get("player_resources", 0)
        x_cost = 0
        if variable_x:
            x_cost = self._extract_int32_field(inner_bytes, "m_ResourceXCost")
            x_cost = max(0, int(x_cost or 0))
            if x_cost < variable_min:
                log_req(f"    Troop ability {ability_guid[:8]}: X={x_cost} below minimum {variable_min}")
                return
        uses = self._card_uses(session, source_uid)
        used = int(uses.get(ability_guid, 0))
        if cost + x_cost > resources:
            log_req(f"    Troop ability {ability_guid[:8]}: need {cost + x_cost} resources, have {resources}")
            return
        if upg and used >= upg:
            log_req(f"    Troop ability {ability_guid[:8]}: uses_per_game exhausted ({used})")
            return
        if upt and used >= upt:
            log_req(f"    Troop ability {ability_guid[:8]}: uses_per_turn exhausted ({used})")
            return
        if exh:
            # Authoritative exhaust-as-cost gate (mirrors _affordable_troop_abilities).
            crow = _db.execute(
                "SELECT gc.card_state, (ct.attributes | gc.card_attributes) "
                "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
                "WHERE gc.session_id=? AND gc.card_uid=?",
                (session.session_id, int(source_uid))).fetchone()
            cstate = int(crow[0]) if crow else 0
            cattrs = int(crow[1]) if crow else 0
            if (cstate & game_engine.ECardStates.Tapped
                    or (not (cstate & game_engine.ECardStates.StartedATurnOnYourSide)
                        and not (cattrs & game_engine.ECardAttributes.Speed))):
                log_req(f"    Troop ability {ability_guid[:8]}: cannot exhaust "
                        f"{hex(source_uid)} (summoning sick/tapped)")
                return
        # Ability conditions gate activation (client CanActivateAbilityBase:
        # TriggerCondition.IsValid). E.g. Droo's Colossal Walker only activates
        # while exhausted.
        raw_row = _db.execute(
            "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
            (ability_guid,)).fetchone()
        if raw_row and raw_row[0]:
            from abilities.framework.condition_engine import (
                ConditionContext,
                trigger_condition_met,
            )
            cond_ctx = ConditionContext(
                _db, session, bstate,
                ability_source_uid=int(source_uid),
                ability_source_owner_id=self.user_profile["id"])
            if not trigger_condition_met(raw_row[0], cond_ctx):
                log_req(f"    Troop ability {ability_guid[:8]}: ability condition not met")
                return
        import battle_engine as _be
        phase = _be.current_phase(bstate)
        if casting != 64 and (
                bstate.get("turn_player") != _be.PLAYER
                or phase not in (game_engine.ETurnPhases.FirstMainPhase,
                                  game_engine.ETurnPhases.SecondMainPhase)
                or not _be.stack_empty(bstate)):
            log_req(f"    Troop ability {ability_guid[:8]}: basic action not legal now")
            return
        # Extract the selected card target from the transaction.  Do not assume
        # it is friendly: metadata such as Taming Sphere's ``SinglePlayer``
        # target deliberately permits an Untamed troop on either side.
        target_uid = None
        if isinstance(inner_bytes, bytes):
            for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                try:
                    uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                    if (uid64 & 0xFF) == 1:
                        uid64 = int(uid64)
                        if uid64 != source_uid:
                            target_uid = uid64
                except Exception:
                    continue
        # Work out which target templates are effect targets (as opposed to
        # automatic source/player targets or card-cost targets), then validate
        # the transaction against the same gamedata filter used to build the
        # picker.  This keeps activation data-driven and prevents an invalid or
        # missing target from silently resolving against the source card.
        target_row = _db.execute(
            "SELECT target_template_ids FROM card_abilities_meta "
            "WHERE ability_guid=?", (ability_guid,)).fetchone()
        try:
            target_templates = json.loads(target_row[0]) if target_row and target_row[0] else []
        except (TypeError, ValueError, json.JSONDecodeError):
            target_templates = []
        cost_templates = {tid for tid, _ctype in self._ability_cost_templates(ability_guid)}
        explicit_templates = []
        for tid in target_templates:
            if tid in cost_templates:
                continue
            tt = _db.execute(
                "SELECT target_kind, is_auto_target, min_target_count "
                "FROM target_templates WHERE template_id=?", (tid,)).fetchone()
            kind = (tt[0] if tt else "") or ""
            auto = int(tt[1] or 0) if tt else 0
            if auto or kind in ("PlayerTargetTemplate",
                                "AbilitySourceCardTargetTemplate",
                                "AbilityCreatedTargetTemplate"):
                continue
            explicit_templates.append((str(tid), int(tt[2] or 1) if tt else 1))
        if explicit_templates:
            from abilities.framework.targeting import (
                legal_targets as _legal_targets, target_uses_both_players,
            )
            # Taming Sphere has one required explicit target.  For abilities
            # with several templates, accepting the selected card if it is
            # legal for any effect target preserves the existing last-target
            # transaction convention while still enforcing every filter.
            valid_target = False
            if target_uid is not None:
                for tid, _minimum in explicit_templates:
                    candidates = _legal_targets(
                        _db, session.session_id, self.user_profile["id"],
                        tid, int(source_uid),
                        both_players=target_uses_both_players(_db, tid),
                        champions=self._champion_targets(),
                        battle_state=bstate)
                    if int(target_uid) in {int(c) for c in candidates}:
                        valid_target = True
                        break
            if not valid_target:
                log_req(f"    REJECTED troop ability {ability_guid[:8]}: "
                        f"missing/illegal metadata target {target_uid}")
                return
        else:
            # Any card UIDs in a cost TargetMap (sacrifice/void/etc.) are not
            # effect targets consumed by the BOM resolver.
            target_uid = None

        # Pay the resource cost only after target validation succeeds.
        bstate["player_resources"] = resources - cost - x_cost
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
        self._bump_card_use(session, source_uid, ability_guid)
        _be.save_state(session, bstate)
        # Resolve the BOM.
        import ability as _ability_mod
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        fn = _ability_mod.resolve_effect(ability_guid)
        log = ""
        if fn:
            log = fn(game, session, _db, self, pl_t, ai_t, bstate, ability_guid, None)
        self._remove_one_shot_ability(
            session, source_uid, ability_guid, game, pl_t, ai_t, bstate)
        # Manual troop abilities resolve directly rather than as stack items.
        # Apply state-based effects here as well, so a stat reduction that
        # lowers a troop's effective defense to zero sends it to the crypt
        # before the player receives another priority window.
        if _be.stack_empty(bstate):
            _ability_mod.state_based_deaths(
                game, session, _db, self, pl_t, ai_t, bstate)
        _be.save_state(session, bstate)
        # Exhaust-as-cost: tap the source card now that the ability resolved
        # (e.g. Prairie Scout's [ACT] ... — the tap is part of the cost).
        if exh:
            _db.execute(
                "UPDATE game_cards SET card_state = card_state | ? "
                "WHERE session_id=? AND card_uid=?",
                (game_engine.ECardStates.Tapped, session.session_id, int(source_uid)))
            _db.commit()
            scid_src = game_engine.SessionCardId(game_engine.UID(int(source_uid)))
            trow = _db.execute(
                "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
                (session.session_id, int(source_uid))).fetchone()
            _tpl, ct2, _n, _c, atk, def_, _g = self._card_full_data(
                game, scid_src, trow[0] if trow else None)
            crow = _db.execute(
                "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
                (session.session_id, int(source_uid))).fetchone()
            game.push_card_updated(scid_src, pl_t, game_engine.ECardCollections.Warzone,
                                   ct2, template_id=_tpl, attack=atk, defense=def_,
                                   state=int(crow[0]) if crow else game_engine.ECardStates.Tapped)
            # Exhausting an activated troop emits the data-defined tap event.
            # Granted triggers such as Spider Nest listen for this event.
            _ability_mod.resolve_triggers(
                _db, self, game, session, pl_t, ai_t, bstate,
                "CardTappedEvent", int(source_uid), self.user_profile["id"])
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
                not bstate.get("pending_trigger")):
            game.push_green_light(pl_t, self._priority_context_for(
                __import__("battle_engine").current_phase(bstate), bstate))
        if game.events:
            self._send_battle_events(session, game, pl_t)
        if variable_x:
            bstate.pop("x_cost", None)
            _be.save_state(session, bstate)
        log_req(f"    Troop ability {ability_guid[:8]} activated on {hex(source_uid)} "
                f"(cost {cost}+{x_cost}, target={hex(target_uid) if target_uid else 'none'}): {log}")

    def _ai_draw_card(self, game, session, ai_t, battle_state):
        import ai
        return ai.ai_draw_card(self, game, session, ai_t, battle_state)

    def _move_deck_to_hand(self, game, session, pl_t, card_uid, tpl_guid, instance_id=None):
        """Move a specific card from the deck to the player's hand (pushes into
        `game`). Used by !addcard."""
        _db.execute(
            "UPDATE game_cards SET location='hand', position=100 WHERE session_id=? AND card_uid=?",
            (session.session_id, int(card_uid)))
        _db.commit()
        scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
        tpl_guid, ct, name, cost, atk, def_, gem_type = self._card_full_data(
            game, scid, tpl_guid, instance_id)
        game.push_card_moved(scid, pl_t, game_engine.ECardCollections.Hand,
                             game_engine.ECardLocations.Top, 1)
        game.push_card_drawn(scid, pl_t, 1)
        game.push_card_updated(scid, pl_t, game_engine.ECardCollections.Hand, ct,
                               attack=atk, defense=def_, cost=cost,
                               template_id=tpl_guid, gems=gem_type)
        log_req(f"    Added card {card_uid} ({name}) from deck to hand")

    def _player_draw_card(self, game, session, pl_t, owner_id=None):
        """Draw the top card of the human's deck into hand (pushes into `game`)."""
        import battle_engine as _be
        import ability as _abil
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
            ai_t = game_engine.UID.make(3, 1000)
            draw_pl_t = pl_t
        turn = bstate.get("turn_number", 1)
        if bstate.get("player_draws_turn") != turn:
            bstate["player_draws_this_turn"] = 0
            bstate["player_draws_turn"] = turn
        bstate["player_draws_this_turn"] = int(bstate.get("player_draws_this_turn", 0)) + 1
        # Replacement: "If you would draw a card..." (The Transcended) and
        # "If this would enter a hand..." (Booby Trap) — a resolved trigger
        # replaces the draw with its own effect.
        import ability as _abil_repl
        repl_draw = _abil_repl.resolve_triggers(
            _db, self, game, session, draw_pl_t, ai_t, bstate,
            "CardWouldBeDrawnEvent", None, owner_id)
        rows = _db.execute(
            "SELECT id, card_uid, card_template_id, template_guid FROM game_cards "
            "WHERE session_id=? AND user_id=? AND location='deck' ORDER BY position LIMIT 1",
            (session.session_id, owner_id)).fetchall()
        if not rows:
            if is_pvp:
                log_req(f"    PvP debug draw: pid {owner_id} deck empty")
                return False
            # Deck-out: a player who must draw with an empty deck loses.
            import commands as _cmd
            _cmd.push_battle_game_end(handler=self, session=session,
                                      winners=[ai_t], losers=[pl_t])
            campaign.handle_battle_gameend(self, _db, session, False, SERVICE_MAIL_UID, UID_TYPE["ServiceCampaign"])
            log_req("    Game over: player deck empty on draw (AI wins)")
            return True
        row = rows[0]
        repl_zone = _abil_repl.resolve_triggers(
            _db, self, game, session, draw_pl_t, ai_t, bstate,
            "CardWouldEnterZoneEvent", row[1], owner_id)
        if repl_draw or repl_zone:
            if is_pvp:
                pvp_save_state(session, bstate)
            else:
                _be.save_state(session, bstate)
            log_req(f"    Player draw replaced (would-draw/would-enter trigger)")
            return
        scid = game_engine.SessionCardId(game_engine.UID(row[1]))
        _db.execute("UPDATE game_cards SET location='hand', position=100 WHERE id=?", (row[0],))
        _db.commit()
        tpl_guid, ct, name, cost, atk, def_, gem_type = self._card_full_data(game, scid, row[3], row[2])
        game.push_card_moved(scid, draw_pl_t, game_engine.ECardCollections.Hand,
                             game_engine.ECardLocations.Top, 1)
        game.push_card_drawn(scid, draw_pl_t, 1)
        game.push_card_updated(scid, draw_pl_t, game_engine.ECardCollections.Hand, ct,
                               attack=atk, defense=def_, cost=cost,
                               template_id=tpl_guid, gems=gem_type)
        # Entering a hand is its own authoritative zone event.  This is
        # distinct from CardDrawnEvent: cards such as Reginald trigger from
        # entering Hand even when the card was put there by an effect rather
        # than a normal draw.  The source owner is the actual deck owner, so
        # the trigger follows the card if control of the deck changed.
        _abil.resolve_triggers(
            _db, self, game, session, draw_pl_t, ai_t, bstate,
            "CardEnteredZoneEvent", row[1], owner_id)
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
        _abil.resolve_triggers(
            _db, self, game, session, draw_pl_t, ai_t, bstate,
            "CardDrawnEvent", champ_uid, owner_id,
            extra_target=row[1])
        if is_pvp:
            pvp_save_state(session, bstate)
        else:
            _be.save_state(session, bstate)
        log_req(f"    Player drew card {row[1]} ({name})")

    def _ai_play_resource(self, game, session, ai_t, battle_state):
        import ai
        return ai.ai_play_resource(self, game, session, ai_t, battle_state)

    def _ai_play_troop(self, game, session, ai_t, battle_state):
        import ai
        return ai.ai_play_troop(self, game, session, ai_t, battle_state)

    def _resolve_ai_mulligan(self, session, game, ai_t):
        import ai
        return ai.resolve_ai_mulligan(self, session, game, ai_t)

    def _card_type_of(card_template_id):
        """Return the card_templates.card_type for a card_template_id."""
        s = str(card_template_id)
        try:
            if "-" in s:
                row = _db.execute(
                    "SELECT card_type FROM card_templates WHERE guid=?", (s,)).fetchone()
                return row[0] if row else None
            row = _db.execute(
                "SELECT ct.card_type FROM card_instances ci "
                "JOIN card_templates ct ON ci.template_guid=ct.guid WHERE ci.instance_id=?",
                (int(s),)).fetchone()
            return row[0] if row else None
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
            # A handler can fail after a legacy helper has opened a write
            # transaction on the shared connection (for example, when a
            # game-card sync exhausts its SQLite lock retries).  Leaving that
            # transaction open blocks every other process using the database,
            # including the auth proxy.  Roll it back before closing this
            # client connection so a failed request cannot poison the server.
            try:
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
                notify_pvp_player_disconnected(disconnected_pid, self)
                with player_handler_lock:
                    if player_handlers.get(disconnected_pid) is self:
                        del player_handlers[disconnected_pid]
        except Exception as exc:
            log_req(f"    PvP disconnect notification failed: {exc}")

        try:
            from services.chat import notify_chat_player_disconnected
            notify_chat_player_disconnected(self)
        except Exception as exc:
            log_req(f"    Chat disconnect notification failed: {exc}")

        # Remove this handler before the social offline broadcast so remaining
        # sessions are evaluated accurately.
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
            log_req(f"=== RECV === target={target} instance={instance} reqid={reqid} conh={conh} ccnt={client_ccnt} scnt={client_scnt}")
            log_req(f"  issuer={issuer} body_len={len(body)} hdrs={ {k:str(v)[:80] for k,v in headers.items()} }")

        # Try to dump body content
        if body:
            if instance == "auth:req":
                log_req(f"  body_json={body.decode('utf-8', errors='replace')}")
            else:
                log_req(f"  body_hex={hexdump(body)}")

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
            if uid not in _active_clients:
                _active_clients[uid] = []
            _active_clients[uid].append((self, time.time()))
            self.scnt += 1
            self.send(
                {"issuer": "Session", "target": "auth:res", "sid": self.sid},
                json.dumps(auth_resp, separators=(",", ":")).encode("utf-8"),
            )

            self.push_profile_stream()

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
            handle_chat_message(self, body)

        # === ROUTED SERVICE MESSAGES ===
        elif instance and instance != "ping":
            log_req(f">>> Routed msg target={target} instance={instance}")

            # On first routed message after login, push inventory and existing cards
            if self._inventory_pending:
                self._inventory_pending = False
                if self.user_profile:
                    purchased = db_get_inventory(self.user_profile["id"])
                    for tguid, qty in purchased:
                        # Use stored client UID if available, else generate one
                        uid_row = _db.execute("SELECT client_item_uid FROM player_inventory WHERE user_id=? AND template_guid=?",
                                              (self.user_profile["id"], tguid)).fetchone()
                        item_id = uid_row[0] if uid_row and uid_row[0] else (2000 + int(qty))
                        self.push_inventory_to_client(qty=qty, template_guid=tguid, item_id=item_id)
                        if not uid_row or not uid_row[0]:
                            _db.execute("UPDATE player_inventory SET client_item_uid=? WHERE user_id=? AND template_guid=? AND client_item_uid=0",
                                        (item_id, self.user_profile["id"], tguid))
                    _db.commit()
                    log_req(f">>> Pushed {len(purchased)} inventory items on first request")
                    # Push existing cards (no "New" banner)
                    self.push_cards_to_client()

            # On first routed message, push social data (friend list etc.)
            if getattr(self, '_social_pending', False) and self.user_profile:
                self._social_pending = False
                log_req(f"    Social push triggered: _active_clients={list(_active_clients.keys())}")
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
                dw = parse_datawrapper(body)
                log_req(f"    DW: dt={dw.get('DataType')} comp={dw.get('Comp')} sess={dw.get('RequestHandlerSessionId')}")
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
                    log_req(f"    Decompressed: {len(raw_bytes)}b -> {len(inner_bytes)}b")
                except Exception as e:
                    log_req(f"    Decompress failed: {e}")
                    log_req(f"    raw[:60]={hexdump(raw_bytes[:60])}")
                    return

            inner_obj = {}
            inner_type = "?"
            try:
                if inner_bytes:
                    inner_obj = parse_datawrapper(inner_bytes)
                    inner_type = inner_obj.get("__type__", "?")
                    log_req(f"    Inner: type={inner_type} fields={ {k: str(v)[:80] for k, v in inner_obj.items() if k != '__type__'} }")
                    log_req(f"    Inner raw: {hexdump(inner_bytes)}")
            except Exception as e:
                log_req(f"    Inner parse: {e}")
                log_req(f"    Inner hex: {hexdump(inner_bytes, 120)}")
                inner_obj = {"__raw__": inner_bytes}

            self.handle_service_request(target, instance, data_type, orig_reqid,
                                        comp, session_id, conh, inner_obj, inner_bytes)

        else:
            log(f">>> Unknown: target={target} instance={instance}")


    def handle_service_request(self, target, instance, data_type, reqid,
                                comp, session_id, conh, inner_obj, inner_bytes):
        """Decode the protocol request into an application command envelope."""
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
        )
        # 22025 is ReadyToContinueGame.  The reconnect client sends it with
        # no response callback, so it must reach the PvP handler as a normal
        # fire-and-forget request.  Only 3027 is handled at the application
        # boundary as an end-session mutation.
        if data_type == 3027:
            return self._application.dispatch_request(
                command, self._handle_session_application_request)
        return self._application.dispatch_request(
            command,
            lambda request: self._handle_service_request_legacy(
                request.target, request.instance, request.data_type,
                request.request_id, request.compressed, request.session_id,
                request.connection_handle, request.inner_object,
                request.inner_bytes),
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

    def _handle_priority_sync_transaction(self, session, transaction):
        """Reconcile a client's lost priority state without changing game state."""
        if not session:
            return False

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

        import battle_engine as _be
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(3, 1000)
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
            import db as _draw_db
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
                    # to Mulligan(4) BEFORE any events go out — the loser's
                    # client opens its Mulligan dialog as soon as it gets
                    # the phase push and immediately issues a priority sync;
                    # if the state still says phase 3 at that moment the
                    # sync response re-pushes PickGoesFirst and kills the
                    # loser's dialog.  (pvp_default_state stores phase as
                    # an int, so keep it an int here.)
                    state["phase"] = 4
                    state["mulligan_pid"] = winner_pid
                    state["mulligan_count"] = {}
                    pvp_save_state(session, state)

                    # Draw 7 cards for BOTH players now.
                    for apid in pids:
                        _draw_db.db_game_draw_cards(session.session_id, apid, 7)
                    log_req(f"    PvP choose: drew hands for both")

                # Push hand + Mulligan to each player.
                for apid in pids:
                    h = player_handlers.get(apid)
                    if not h: continue
                    aopp = pids[0] if pids[1] == apid else pids[1]
                    pl = game_engine.UID.make(244, apid)
                    op = game_engine.UID.make(244, aopp)
                    g = game_engine.Game(int(session.session_id), pl, op)
                    for idx, (cu, tg) in enumerate(_draw_db.db_game_get_hand(session.session_id, apid)):
                        scid = game_engine.SessionCardId(game_engine.UID(int(cu)))
                        # Populate the CardDef (cost/atk/def/thresholds/
                        # abilities/gems) so the client renders the hand
                        # and the Mulligan keep/redraw dialog correctly.
                        _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                            self._card_full_data(g, scid, tg)
                        g.push_card_moved(scid, pl, game_engine.ECardCollections.Hand,
                                          game_engine.ECardLocations.Top, idx)
                        g.push_card_updated(scid, pl, game_engine.ECardCollections.Hand,
                                            ct, template_id=tg, gems=gem_type)
                    for idx, (cu, tg) in enumerate(_draw_db.db_game_get_hand(session.session_id, aopp)):
                        scid = game_engine.SessionCardId(game_engine.UID(int(cu)))
                        _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                            self._card_full_data(g, scid, tg)
                        g.push_card_moved(scid, op, game_engine.ECardCollections.Hand,
                                          game_engine.ECardLocations.Top, idx)
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
                    # Each client opens the keep/redraw dialog when it
                    # receives the Mulligan phase AS the priority
                    # player (UIBattle.PushStateForPhase only fires for
                    # the priority player).  Push a temporary local
                    # GreenLight before that phase for the non-actor so
                    # it does not request a resync, then send the real
                    # actor GreenLight after the phase.  The final
                    # event in either packet gives both clients the
                    # same authoritative priority owner.
                    winner_uid = game_engine.UID.make(244, winner_pid)
                    # Only the current mulligan actor may submit a decision;
                    # keep the waiting client's dialog visible but disable
                    # its input until the handoff packet arrives.
                    g.push_disable_interface(apid != winner_pid)
                    if apid == winner_pid:
                        # The actor already has the authoritative
                        # GreenLight before the phase is processed.
                        g.push_green_light(winner_uid,
                                           game_engine.EPriorityContext.Normal)
                    else:
                        # Give the waiting client a local GreenLight
                        # only long enough for it to open its dialog;
                        # the authoritative handoff follows the phase.
                        g.push_green_light(
                            pl, game_engine.EPriorityContext.Normal)
                    g.push_turn_phase(game_engine.ETurnPhases.Mulligan,
                                      pl, pl)
                    if apid != winner_pid:
                        g.push_green_light(winner_uid,
                                           game_engine.EPriorityContext.Normal)
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
            pl_t = game_engine.UID.make(244, int(self.client_reck_id))
            ai_t = game_engine.UID.make(3, 1000)
            game = game_engine.Game(session.session_id, pl_t, ai_t)
            # Draw the opening hands NOW — like PvP, PreGame and
            # PickGoesFirst show empty hands; each player draws after the
            # Play/Draw pick. Campaign uses the class/talent opening-hand
            # value for the player; the AI keeps the normal seven cards.
            from db import (db_game_draw_cards, db_game_get_hand,
                            db_game_card_type)
            # Campaign/PvE uses the campaign mulligan rule: the first redraw
            # replaces the opening hand at the same size.  Keep this counter
            # in the persisted session metadata so it survives the separate
            # transaction that carries the redraw request.  PvP retains its
            # existing one-fewer-card mulligan rule.
            if (session.session_name or "").startswith("camp_"):
                encounter_data = dict(session.encounter_data or {})
                encounter_data["_campaign_mulligan_redraws"] = 0
                session.encounter_data = encounter_data
                session._persist()
            if not db_game_get_hand(session.session_id,
                                    self.user_profile["id"]):
                db_game_draw_cards(session.session_id,
                                   self.user_profile["id"],
                                   self._starting_hand_size(session))
                db_game_draw_cards(session.session_id, 0, 7)
            player_hand = db_game_get_hand(session.session_id,
                                           self.user_profile["id"])
            ai_hand = db_game_get_hand(session.session_id, 0)
            for idx, (cu, tg) in enumerate(player_hand):
                scid = game_engine.SessionCardId(
                    game_engine.UID(int(cu)))
                _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                    self._card_full_data(game, scid, tg)
                game.push_card_moved(
                    scid, pl_t, game_engine.ECardCollections.Hand,
                    game_engine.ECardLocations.Top, idx)
                game.push_card_updated(
                    scid, pl_t, game_engine.ECardCollections.Hand,
                    ct, template_id=tg, gems=gem_type)
            for idx, (cu, tg) in enumerate(ai_hand):
                scid = game_engine.SessionCardId(
                    game_engine.UID(int(cu)))
                _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                    self._card_full_data(game, scid, tg)
                game.push_card_moved(
                    scid, ai_t, game_engine.ECardCollections.Hand,
                    game_engine.ECardLocations.Top, idx)
                game.push_card_updated(
                    scid, ai_t, game_engine.ECardCollections.Hand,
                    ct, template_id=tg, nulling=True, gems=gem_type)
            if ai_choice:
                if chose_draw:
                    game.push_player_wishes_to_draw_first(ai_t)
                else:
                    game.push_player_wishes_to_play_first(ai_t)
            game.push_turn_phase(game_engine.ETurnPhases.Mulligan, pl_t, pl_t)
            game.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
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
                    kept = state.get("kept") or []
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
                            g.push_accepted_starting_hand(pl_t, mulliganed=False)
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
            ai_t = game_engine.UID.make(3, 1000)
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
            # Get cards in hand
            from db import db_hand_cards_raw
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
            game.push_accepted_starting_hand(pl_t, mulliganed=False)
            # The human kept. Now the opposing player (AI) gets to decide:
            # it keeps if it has a shard, otherwise it mulligans. This
            # pushes PlayerMulliganedHand/AcceptedStartingHand for the AI.
            self._resolve_ai_mulligan(session, game, ai_t)
            play_first_is_player = getattr(
                self, "_pending_play_first_is_player", True)
            first_turn_uid = pl_t if play_first_is_player else ai_t
            game.push_turn_phase(game_engine.ETurnPhases.StartGame,
                                 first_turn_uid, first_turn_uid)
            # Initialise the DB-backed battle state with the player who won the
            # subsequent Play/Draw choice. (The choice can belong to the AI.)
            # The client shows NO interaction during StartGame (no BattleState
            # is pushed for it), so a pass can never arrive — the server
            # advances StartGame -> StartTurn itself below.
            import battle_engine as _be
            bstate = _be.default_state(
                turn_player=_be.PLAYER if play_first_is_player else _be.AI)
            bstate["player_resources"] = 0
            bstate["player_total_resources"] = 0
            bstate["ai_resource_played_this_turn"] = False
            bstate["player_charges"] = getattr(self, "_player_starting_charges", 0)
            bstate["ai_charges"] = getattr(self, "_ai_starting_charges", 0)
            # Champion starting health (persisted for combat + reconnect).
            bstate["player_health"] = getattr(self, "_player_starting_health", 20)
            bstate["ai_health"] = getattr(self, "_ai_starting_health", 10)
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
                    from abilities.framework.resolution import resolve_ability
                    fortune_source = int(self._ai_champ_scid.uid.uid64)
                    fortune_log = resolve_ability(
                        self, game, session, _db, pl_t, ai_t, bstate,
                        fortune_ability, fortune_source, 0, {})
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
            if getattr(self, "_pending_player_stops", None):
                bstate["player_self_stops"], bstate["player_opp_stops"] = self._pending_player_stops
                self._pending_player_stops = None
            else:
                saved_s, saved_o = self._load_player_stops(self.user_profile["id"])
                if saved_s is not None:
                    bstate["player_self_stops"] = saved_s
                if saved_o is not None:
                    bstate["player_opp_stops"] = saved_o
            # A draw-first player draws a card on their first turn.
            if getattr(self, "_pending_player_draws_first", None) is not None:
                bstate["player_draws_first_turn"] = self._pending_player_draws_first
                self._pending_player_draws_first = None
            self._pending_play_first_is_player = None
            bstate["turn_player"] = (
                _be.PLAYER if play_first_is_player else _be.AI)
            _be.save_state(session, bstate)
            # "At the start of the game" triggers for both players'
            # opening-hand / warzone cards (e.g. Princess Victoria).
            import ability as _abil_gs
            for gs_owner in (self.user_profile["id"], 0):
                _abil_gs.resolve_triggers(
                    _db, self, game, session, pl_t, ai_t, bstate,
                    "GameStartedEvent", None, gs_owner,
                    zones=("hand", "warzone"))
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

    def _handle_discard_transaction(self, session, transaction):
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
        ai_t = game_engine.UID.make(3, 1000)
        import battle_engine as _be
        gd = game_engine.Game(session.session_id, pl_t, ai_t)
        if isinstance(inner_bytes, bytes):
            for m in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                try:
                    card_uid = struct.unpack('<Q', bytes.fromhex(m.group(1).decode()))[0]
                    # Only Card-type UIDs (the champion/player UIDs are
                    # type 0/3 and must NOT be discarded).
                    if (card_uid & 0xFF) != 1:
                        continue
                    gd_owner, _owner_uid = self._discard_card_to_owner(
                        session, pl_t, ai_t, int(card_uid))
                    if gd_owner:
                        gd.events.extend(gd_owner.events)
                    log_req(f"    Discarded card {card_uid}")
                except Exception:
                    pass
        _db.commit()
        if gd.events:
            self._send_battle_events(session, gd, pl_t)
        # Discard down to the max hand size (7): each DiscardTransaction
        # moves ONE card, so stay in the Discard phase until the hand fits.
        # Only then advance to EndTurn.
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        from db import db_hand_card_count
        hand_count = db_hand_card_count(session.session_id, self.user_profile["id"])
        if hand_count > self._max_hand_size(session):
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
            from services.tournament_game import db_game_session_pids
            import random as _rp
            my_pid = int(self.client_reck_id)
            pl_t = game_engine.UID.make(244, my_pid)
            pids = db_game_session_pids(session.session_id)
            opp_pid = pids[0] if pids[1] == my_pid else pids[1]
            opp_t = game_engine.UID.make(244, opp_pid)
            hand_rows = _db.execute(
                "SELECT id, card_uid FROM game_cards WHERE session_id=? AND user_id=? AND location='hand'",
                (session.session_id, my_pid)).fetchall()
            redraw_count = len(hand_rows)
            draw_count = max(1, redraw_count - 1)
            log_req(f"    PvP mulligan redraw: {redraw_count} -> {draw_count} cards")
            # Save old card_uids so we can push them back to deck.
            old_scids = [game_engine.UID(row[1]) for row in hand_rows]
            if hand_rows:
                _db.executemany(
                    "UPDATE game_cards SET location='deck', card_state=0 "
                    "WHERE id=?",
                                [(r[0],) for r in hand_rows])
                _db.commit()
            # Reshuffle this player's deck.
            deck_rows = _db.execute(
                "SELECT id FROM game_cards WHERE session_id=? AND user_id=?",
                (session.session_id, my_pid)).fetchall()
            deck_ids = [r[0] for r in deck_rows]
            _rp.shuffle(deck_ids)
            _db.executemany("UPDATE game_cards SET position=? WHERE id=?",
                            [(pos, gid) for pos, gid in enumerate(deck_ids)])
            _db.commit()
            new_rows = _db.execute(
                "SELECT card_uid, template_guid FROM game_cards "
                "WHERE session_id=? AND user_id=? AND location='deck' ORDER BY position LIMIT ?",
                (session.session_id, my_pid, draw_count)).fetchall()
            _db.executemany("UPDATE game_cards SET location='hand' WHERE session_id=? AND card_uid=?",
                            [(session.session_id, r[0]) for r in new_rows])
            _db.commit()
            # Push hand cards to both (the next player's Mulligan
            # prompt is pushed separately by pvp_mulligan_next).
            for pid in pids:
                h = player_handlers.get(pid)
                if not h: continue
                is_me = (pid == my_pid)
                p_uid = game_engine.UID.make(244, pid)
                o_uid = game_engine.UID.make(244, opp_pid if is_me else my_pid)
                g = game_engine.Game(int(session.session_id), p_uid, o_uid)
                # Push old hand cards back to deck (face-down).
                for cu in old_scids:
                    scid = game_engine.SessionCardId(cu)
                    g.push_card_moved(scid, pl_t, game_engine.ECardCollections.Deck,
                                     game_engine.ECardLocations.Top, 0)
                    g.push_card_updated(scid, pl_t, game_engine.ECardCollections.Deck,
                                        game_engine.ECardTypes.Unknown, nulling=True)
                # Push new hand.
                if is_me:
                    for idx, (cu, tg) in enumerate(new_rows):
                        scid = game_engine.SessionCardId(game_engine.UID(int(cu)))
                        _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                            self._card_full_data(g, scid, tg)
                        g.push_card_moved(scid, p_uid, game_engine.ECardCollections.Hand,
                                          game_engine.ECardLocations.Top, idx)
                        g.push_card_updated(scid, p_uid, game_engine.ECardCollections.Hand,
                                            ct, template_id=tg, gems=gem_type)
                else:
                    for idx, (cu, tg) in enumerate(new_rows):
                        scid = game_engine.SessionCardId(game_engine.UID(int(cu)))
                        _tpl_g, ct, _nm, _c, _a, _d, gem_type = \
                            self._card_full_data(g, scid, tg)
                        g.push_card_moved(scid, pl_t, game_engine.ECardCollections.Hand,
                                          game_engine.ECardLocations.Top, idx)
                        g.push_card_updated(scid, pl_t, game_engine.ECardCollections.Hand,
                                            ct, template_id=tg, nulling=True, gems=gem_type)
                # The redrawer's decision is complete.  Disable input for
                # both clients while pvp_mulligan_next hands priority to the
                # opponent (or re-asks this player after the opponent keeps).
                g.push_disable_interface(True)
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
            # Hand the decision to the next player (or re-ask the
            # redrawer when their opponent already kept).
            from services.tournament_game import (
                pvp_load_state, pvp_mulligan_next, pvp_session_lock)
            with pvp_session_lock(session):
                state = pvp_load_state(session)
                pvp_mulligan_next(session, state, my_pid)
            handled = True
        if not handled:
            pl_t = game_engine.UID.make(244, int(self.client_reck_id))
            ai_t = game_engine.UID.make(3, 1000)
            import random as _random
            import json as _redraw_json
            game2 = game_engine.Game(session.session_id, pl_t, ai_t)

            # Fetch deck's active_gems ONCE (not per card)
            deck_gems = {}
            arena_lookup = db_get_arena_state(self.user_profile["id"])
            deck_lookup_id = self._resolve_fra_deck_id(
                arena_lookup["deck_id"]) or 0
            gem_row = _db.execute("SELECT active_gems FROM decks WHERE id=?", (deck_lookup_id,)).fetchone()
            if gem_row and gem_row[0]:
                try:
                    import json as _gem_json
                    deck_gems = _gem_json.loads(gem_row[0])
                except Exception:
                    pass

            # Count cards in hand
            hand_rows = _db.execute(
                "SELECT id, card_uid FROM game_cards WHERE session_id=? AND user_id=? AND location='hand' ORDER BY position",
                (session.session_id, self.user_profile["id"])).fetchall()
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
                              max(1, redraw_count - 1))
                encounter_data["_campaign_mulligan_redraws"] = redraws + 1
                session.encounter_data = encounter_data
                session._persist()
            else:
                draw_count = max(1, redraw_count - 1)

            # Move current hand cards back to deck (one batch UPDATE)
            if hand_rows:
                _db.executemany(
                    "UPDATE game_cards SET location='deck', card_state=0 "
                    "WHERE id=?",
                    [(row[0],) for row in hand_rows])
                _db.commit()
                for i, row in enumerate(hand_rows):
                    scid = game_engine.SessionCardId(game_engine.UID(row[1]))
                    # Return to deck as face-down (nulling=True) so the
                    # client clears any playable/golden outline — cards
                    # in the deck are never playable.
                    game2.push_card_updated(scid, pl_t, game_engine.ECardCollections.Deck, game_engine.ECardTypes.Unknown,
                                            nulling=True)
                    game2.push_card_moved(scid, pl_t, game_engine.ECardCollections.Deck,
                                          game_engine.ECardLocations.Top, i)

            # Shuffle all positions in one pass (read ids, shuffle, batch update)
            all_ids = [r[0] for r in _db.execute(
                "SELECT id FROM game_cards WHERE session_id=? AND user_id=?",
                (session.session_id, self.user_profile["id"])).fetchall()]
            _random.shuffle(all_ids)
            if all_ids:
                _db.executemany(
                    "UPDATE game_cards SET position=? WHERE id=?",
                    [(pos, gid) for pos, gid in enumerate(all_ids)])
                _db.commit()

            # Draw back less than was sent (join template data in ONE query)
            new_cards = _db.execute(
                "SELECT gc.card_uid, gc.card_template_id, gc.template_guid, ct.name, ct.card_type, "
                "ct.cost, ct.attack, ct.defense, ct.threshold_json, ct.abilities_json "
                "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
                "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='deck' ORDER BY gc.position LIMIT ?",
                (session.session_id, self.user_profile["id"], draw_count)).fetchall()

            for i, row in enumerate(new_cards):
                scid = game_engine.SessionCardId(game_engine.UID(row[0]))
                # Full card data (thresholds, abilities, attributes, gems)
                # so the redrawn hand card shows its keywords (e.g. Flight).
                _tpl_g, ct, _nm, _c, _a, _d, gem_type = self._card_full_data(
                    game2, scid, row[2], row[1])
                game2.push_card_drawn(scid, pl_t, i + 1)
                game2.push_card_updated(scid, pl_t, game_engine.ECardCollections.Hand, ct,
                                        template_id=row[2], gems=gem_type)

            # Batch update hand locations
            if new_cards:
                _db.executemany(
                    "UPDATE game_cards SET location='hand' WHERE card_uid=?",
                    [(row[0],) for row in new_cards])
                _db.commit()

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
            handled = True
            # The human mulliganed. The opposing player (AI) is then asked:
            # keep if it has a shard, otherwise mulligan. Pushed as a second
            # packet so the client shows the AI's decision after the human's.
            gai = game_engine.Game(session.session_id, pl_t, ai_t)
            self._resolve_ai_mulligan(session, gai, ai_t)
            self._send_battle_events(session, gai, pl_t)
            log_req("    AI mulligan resolved after human redraw")
        return True

    def _handle_generic_player_transaction(self, session, transaction,
                                      session_id, comp, conh):
        """Handle a non-specialized player transaction after classification."""
        inner_bytes = transaction.inner_bytes
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
                        self, session, inner_bytes)
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
            played_card_type = None
            if played_card_uid:
                crow = _db.execute(
                    "SELECT gc.template_guid, ct.card_type, ct.name, "
                    "ct.abilities_json, ct.current_resources_granted, "
                    "ct.max_resources_granted FROM game_cards gc "
                    "JOIN card_templates ct ON ct.guid = gc.template_guid "
                    "WHERE gc.session_id=? AND gc.card_uid=?",
                    (session.session_id, int(played_card_uid))).fetchone()
                if crow:
                    played_card_type = crow[1]
                    cur_grant = int(crow[4] or 0)
                    max_grant = int(crow[5] or 0)
                    if crow[1] == 'Resource':
                        is_resource_play = True
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
                    elif game_engine.card_type_from_db(crow[1]) & (
                            game_engine.ECardTypes.Troop |
                            game_engine.ECardTypes.Artifact |
                            game_engine.ECardTypes.Constant |
                            game_engine.ECardTypes.BasicAction |
                            game_engine.ECardTypes.QuickAction):
                        is_troop_play = True
                    # Remove this card from the hand by setting its position beyond the deck
                    from db import db_set_card_played_to_zone
                    db_set_card_played_to_zone(session.session_id, int(played_card_uid), 'PlayedResources')
                    log_req(f"    Played {crow[2]} (uid={played_card_uid}), moving to resource zone")

            # Track resources in the DB battle state. Playing a basic
            # threshold gives: +1 total, +1 current, +1 threshold colour,
            # +1 champion charge. All state lives in turn_order_json.
            import battle_engine as _be
            bstate = _be.load_state(session)
            self._current_bstate = bstate
            pl_t = game_engine.UID.make(244, int(self.client_reck_id))
            ai_t = game_engine.UID.make(3, 1000)
            if is_resource_play and not bstate.get("player_resource_played_this_turn"):
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
                    self._resolve_shards_of_fate(
                        g_tmp, session, pl_t, ai_t, bstate,
                        played_card_uid, shard_ability, shard_tpl,
                        self.user_profile["id"])
                    g_tmp.player_total_resources = bstate.get(
                        "player_total_resources", 0)
                    g_tmp.player_resources = bstate.get(
                        "player_resources", 0)
                    self._send_battle_events(session, g_tmp, pl_t)
                    _be.save_state(session, bstate)
                    log_req(f"    Shards of Fate: resource play consumed; "
                            f"+{max_grant} max/+{cur_grant} current, "
                            f"threshold chosen")
                else:
                    bstate["player_total_resources"] = (
                        bstate.get("player_total_resources", 0) + max_grant)
                    bstate["player_resources"] = (
                        bstate.get("player_resources", 0) + cur_grant)
                    bstate["player_charges"] = bstate.get("player_charges", 0) + 1
                    if shard_color:
                        color_map = {'Ruby': game_engine.ECardShards.Ruby,
                                     'Sapphire': game_engine.ECardShards.Sapphire,
                                     'Blood': game_engine.ECardShards.Blood,
                                     'Diamond': game_engine.ECardShards.Diamond,
                                     'Wild': game_engine.ECardShards.Wild}
                        color = color_map.get(shard_color, game_engine.ECardShards.Wild)
                        th = bstate.setdefault("player_threshold", {})
                        th[color] = th.get(color, 0) + 1
                _be.save_state(session, bstate)
                log_req(f"    Resource: tot={bstate.get('player_total_resources',0)} th={dict(bstate.get('player_threshold',{}))} chg={bstate.get('player_charges',0)}")

            g3 = self._fresh_game(session, pl_t, ai_t, bstate)

            # Send ResourceCardPlayed + resource/threshold/charge updates
            if is_resource_play and played_card_uid:
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
                if not shard_tpl:
                    # Update threshold display for the played shard's color
                    if shard_color:
                        col_map = {'Ruby': 8, 'Sapphire': 16, 'Blood': 4, 'Diamond': 64, 'Wild': 32}
                        shard_val = col_map.get(shard_color, 32)
                        thresh_count = bstate["player_threshold"].get(shard_val, 0)
                        ev_th = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                        ev_th.player_id = pl_t; ev_th.color = shard_val; ev_th.operation = 1; ev_th.delta = 1
                        ev_th.new_value = thresh_count
                        g3._push(ev_th)
                # Playing any resource, including Shards of Fate, grants a
                # champion charge point.
                ev_chg = game_engine.ChampionChargePointsChangedSessionEventArgs()
                ev_chg.player_id = pl_t; ev_chg.operation = 1; ev_chg.delta = 1
                ev_chg.new_value = bstate.get("player_charges", 0)
                g3._push(ev_chg)
                from abilities.framework.triggers import resolve_gain_charge_triggers
                resolve_gain_charge_triggers(
                    _db, self, g3, session, pl_t, ai_t, bstate,
                    self.user_profile["id"] if self.user_profile else 0)

            if not is_troop_play:
                g3.push_player_updated(pl_t, champ_id=getattr(self, "_player_champ_scid", None))

            # Troop/artifact play: push to the stack (CastSpells), then
            # auto-resolve since the AI opponent always passes.
            if is_troop_play and played_card_uid:
                tid = int(played_card_uid)
                scid_played = game_engine.SessionCardId(game_engine.UID(tid))
                # Get instance id for gem lookup
                gc_row = _db.execute(
                    "SELECT card_template_id FROM game_cards WHERE session_id=? AND card_uid=?",
                    (session.session_id, tid)).fetchone()
                inst_id = gc_row[0] if gc_row else None
                # _card_full_data fills g3.card_defs with cost/atk/def/
                # thresholds/abilities/gems and returns full stats.
                tpl_guid, ct_n, nm, cost, atk, def_, gem = self._card_full_data(
                    g3, scid_played, crow[0], inst_id)
                x_cost = 0  # set below for variable-X spells
                if cost > bstate.get("player_resources", 0):
                    # Defense-in-depth: the client's playability is the
                    # normal gate, but a drag shouldn't be able to play
                    # an unaffordable card (e.g. an undiscounted Fury of
                    # the Mountain God) and push resources negative.
                    log_req(f"    REJECTED play {crow[2]}: cost {cost} > "
                            f"resources {bstate.get('player_resources', 0)}")
                    g3 = game_engine.Game(session.session_id, pl_t, ai_t)
                    g3.push_player_updated(pl_t, champ_id=getattr(
                        self, "_player_champ_scid", None))
                    self._send_battle_events(session, g3, pl_t)
                    self._push_transaction_ack(session)
                    handled = True
                    return True
                bstate["player_resources"] = bstate.get("player_resources", 0) - cost
                # Move from hand to CastSpells in DB
                from db import db_set_card_played_to_zone, db_card_set_warzone_arrival, db_set_card_resolved_at
                db_set_card_played_to_zone(session.session_id, tid, 'CastSpells')
                # Push chain events
                g3.push_card_updated(scid_played, pl_t, game_engine.ECardCollections.CastSpells,
                                    game_engine.card_type_from_db(played_card_type),
                                    template_id=crow[0], cost=cost, attack=atk, defense=def_, gems=gem)
                g3.push_card_moved(scid_played, pl_t, game_engine.ECardCollections.CastSpells,
                                  game_engine.ECardLocations.Top, 0)
                g3.push_troop_card_played(scid_played, pl_t)
                # Opponent (AI) gets priority — it auto-passes, then the
                # human's Resolve pass completes the both-pass and the
                # item resolves via _resolve_stack_item.
                g3.push_green_light(ai_t, game_engine.EPriorityContext.ResolveTopOfChain)
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
                    _be.stack_push(bstate, {
                        "kind": "troop", "source_uid": int(tid),
                        "ability_guids": [], "target_uid": None,
                        "instance_id": 1, "x_cost": 0,
                    })
                    # The troop's presence on the chain is driven by the
                    # CastSpells CardMove/UpdateCard of the actual card
                    # above; AbilityTemplateId is only a targeting/cost
                    # hint and does not gate whether the card is shown.
                    g3.push_ability_on_chain(
                        scid_played,
                        game_engine.ResourceId.from_str(crow[0]))
                    _be.save_state(session, bstate)
                    log_req(f"    Troop {crow[2]} on the stack — "
                            f"resolves on both-pass (AI auto-passes)")
                else:
                    # BasicAction / QuickAction spell: cast and PUSH onto
                    # the chain (CastSpells = the visual stack). The BOM
                    # (draw / buff / etc.) executes when the chain resolves
                    # (both players pass) — ONE chain item, sub-effects
                    # bundled. The sacrifice cost (if any) is paid now.
                    import ability as _abil
                    targets = self._extract_transaction_targets(inner_bytes, played_card_uid)
                    # Sacrifice cost (e.g. Abominate "sacrifice a troop you
                    # control"): card_templates.sacrifice_target is set.
                    # The cost is paid first, so the sacrificed troop is
                    # the FIRST non-source UID; the effect target is last.
                    from db import db_card_template_field
                    sac_row_val = db_card_template_field(crow[0], "sacrifice_target")
                    sacrifice_uid = None
                    if sac_row_val and sac_row_val != "00000000-0000-0000-0000-000000000000":
                        sacrifice_uid = targets[0] if targets else None
                        target_uid = targets[-1] if len(targets) > 1 else None
                    else:
                        target_uid = targets[-1] if targets else None
                    log_req(f"    Spell {played_card_uid}: targets={[hex(t) for t in targets]} "
                            f"sacrifice={hex(sacrifice_uid) if sacrifice_uid else None} "
                            f"target={hex(target_uid) if target_uid else None}")
                    if sacrifice_uid:
                        self._sacrifice_troop(g3, session, pl_t, ai_t, sacrifice_uid)
                    # Variable X cost: the X the player chose in the
                    # client's X-cost dialog travels as
                    # xCostData.m_ResourceXCost on the play transaction.
                    x_cost = self._extract_int32_field(
                        inner_bytes, "m_ResourceXCost")
                    x_cost = max(0, int(x_cost or 0))
                    from db import db_get_card_abilities
                    ab_json, _ = db_get_card_abilities(crow[0])
                    try:
                        ability_guids = [g.lower() for g in json.loads(ab_json)] if ab_json else []
                    except Exception:
                        ability_guids = []
                    _be.stack_push(bstate, {
                        "kind": "spell", "source_uid": int(tid),
                        "ability_guids": ability_guids, "target_uid": target_uid,
                        "instance_id": 1, "x_cost": x_cost,
                    })
                    if x_cost:
                        bstate["player_resources"] = max(
                            0, bstate.get("player_resources", 0) - x_cost)
                        log_req(f"    Spell X cost: {x_cost} paid "
                                f"(resources left {bstate['player_resources']})")
                    g3.push_ability_on_chain(
                        scid_played,
                        game_engine.ResourceId.from_str(crow[0]))
                    _be.save_state(session, bstate)
                _be.save_state(session, bstate)
                g3.player_resources = bstate["player_resources"]
                ec = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
                ec.player_id = pl_t; ec.operation = 2; ec.delta = cost + x_cost
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
            from db import db_hand_cards_full
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
                    from db import db_card_template_thresholds
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
            from abilities.framework.statics import effective_cost
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
                                            bstate.get("player_resource_played_this_turn", False),
                                            fc3, ec3):
                    playable3.append(s)
            if not bstate.get("pending_trigger") and \
                    not bstate.get("pending_deck_search"):
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
                            g3.add_champion_to_options(
                                pl_t, player_champ_cid3,
                                self._filter_affordable_abilities(
                                    abilities3, bstate,
                                    _be.current_phase(bstate)),
                                self._discard_costs_for(session, abilities3),
                                self._champion_ability_targets(
                                    session, abilities3,
                                    player_champ_cid3.uid.to_uint64()
                                    if hasattr(player_champ_cid3, "uid")
                                    else 0),
                                self._champion_ability_costs(
                                    session, abilities3,
                                    player_champ_cid3.uid.to_uint64()
                                    if hasattr(player_champ_cid3, "uid")
                                    else 0))
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
            elif bstate.get("pending_deck_search") and g3.events:
                # Shards of Fate prompt pending: send only the resource
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
                log_req(f"    Pushed resource-play events (Shards of Fate) ({len(dw3)}b)")
        elif not handled:
            log_req(f"    === PlayerTransaction — ending game ===")
            import commands as _cmd
            _cmd.push_battle_game_end(
                self, session,
                [game_engine.UID.make(3, 1000)],   # AI wins
                [game_engine.UID(player_uid)])     # player loses
            log_req(f"    Pushed GameEnded (3055) — marked as loss")
            # Campaign gameendnotify — updates campaign state after the
            # battle so the client doesn't re-queue the encounter.
            campaign.handle_battle_gameend(self, _db, session, False, SERVICE_MAIL_UID, UID_TYPE["ServiceCampaign"])
        return True

    def _handle_debug_cheat_transaction(self, session, transaction):
        """Execute a client DebugCheatTransaction and acknowledge it."""
        from debug_cheats import handle_debug_cheat
        log_req("    Debug cheat — dispatching server-side action")
        handled = handle_debug_cheat(self, session, transaction.inner_bytes)
        if handled:
            self._push_transaction_ack(session)
        return True

    def _handle_set_ability_data_transaction(self, session, transaction):
        """Resolve target data supplied for a pending ability prompt."""
        inner_bytes = transaction.inner_bytes
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(3, 1000)
        import battle_engine as _be
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
                row = _db.execute(
                    "SELECT id, template_guid FROM game_cards WHERE session_id=? AND card_uid=? "
                    "AND user_id=? AND location='hand'",
                    (session.session_id, discard_card_uid,
                     self.user_profile["id"])).fetchone()
            if row:
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
            bstate.pop("pending_discard_target_template", None)
            bstate.pop("pending_discard_scid", None)
            _be.save_state(session, bstate)
        return True

    def _handle_ability_activate_transaction(self, session, transaction):
        """Resolve a PvE card or champion ability activation."""
        inner_bytes = transaction.inner_bytes
        is_set_ability_data = transaction.is_set_ability_data
        is_ability_activate = transaction.is_ability_activate
        handled = False
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(3, 1000)
        # Extract AbilityTemplateId (GUID) from the transaction
        ability_guid = None
        if isinstance(inner_bytes, bytes):
            import re as _rar
            m = _rar.search(rb'AbilityTemplateId;[^;]*;[^;]*;[^;]*;[^;]*;[^;]*;[^;]*;([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', inner_bytes)
            if not m:
                # Try simpler: find a GUID near AbilityTemplateId
                aidx = inner_bytes.find(b"AbilityTemplateId")
                if aidx >= 0:
                    m2 = _rar.search(rb'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', inner_bytes[aidx:aidx+300])
                    if m2:
                        m = m2
            if m:
                ability_guid = m.group(1).decode().lower()
        log_req(f"    Ability activation: guid={ability_guid}")

        # A DoubleChoice effect exposes the client's built-in
        # ChooseAndPlay ability. Consume that selection before normal
        # champion/troop ability routing can mistake the generated Choice
        # card for an ordinary activation.
        if ability_guid:
            import battle_engine as _choice_be
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
        import battle_engine as _be
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        if ability_guid and not handled:
            champ_owned = _db.execute(
                "SELECT 1 FROM talent_abilities WHERE ability_guid=? LIMIT 1",
                (ability_guid,)).fetchone()
            if not champ_owned:
                # The same ability GUID can be present on multiple card
                # instances (for example, two Howling Braves).  The option
                # list is per source card, so route by the transaction's
                # SessionCardId rather than selecting the first matching row.
                source_uid = None
                if isinstance(inner_bytes, bytes):
                    scid_pos = inner_bytes.find(b"m_SessionCardId")
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
                    src_row = _db.execute(
                        "SELECT card_uid, card_uses FROM game_cards "
                        "WHERE session_id=? AND user_id=? AND location='warzone' "
                        "AND card_uid=? AND card_abilities LIKE ?",
                        (session.session_id, self.user_profile["id"],
                         int(source_uid), f'%"{ability_guid}"%')).fetchone()
                if src_row is None and source_uid is None:
                    # Older clients may omit the source field. Preserve the
                    # unambiguous single-copy case, but never guess between
                    # multiple copies of the same ability.
                    matches = _db.execute(
                        "SELECT card_uid, card_uses FROM game_cards "
                        "WHERE session_id=? AND user_id=? AND location='warzone' "
                        "AND card_abilities LIKE ?",
                        (session.session_id, self.user_profile["id"],
                         f'%"{ability_guid}"%')).fetchall()
                    if len(matches) == 1:
                        src_row = matches[0]
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
                                game_engine.ETurnPhases.SecondMainPhase)):
                        self._push_main_phase_options(session, pl_t, ai_t)
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
                row = _db.execute(
                    "SELECT id, template_guid FROM game_cards WHERE session_id=? AND card_uid=? "
                    "AND user_id=? AND location='hand'",
                    (session.session_id, chosen_uid,
                     self.user_profile["id"])).fetchone()
            if row:
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
                    row = _db.execute(
                        "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
                        "AND location='hand' ORDER BY position LIMIT 1",
                        (session.session_id, self.user_profile["id"])).fetchone()
                    if row:
                        discard_card_uid = int(row[0])
                log_req(f"    Ability discard target: card_uid={discard_card_uid}")

        # Look up cost and deduct from battle state. Costs live on the
        # top-level granted ability in talent_abilities (the BOM's head);
        # ability_effects expands it into the leaf effect chain.
        # Guarded by `not handled`: a warzone-troop manual ability (e.g.
        # Shift) was already resolved by _activate_troop_ability (it pays
        # RESOURCE cost). Falling through here would re-resolve it and
        # push a second transaction ack.
        import battle_engine as _be
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        if ability_guid and not handled:
            trow = _db.execute(
                "SELECT charge_cost, spell_cost FROM talent_abilities WHERE ability_guid=? LIMIT 1",
                (ability_guid,)).fetchone()
            if trow is None:
                # Champion signature charge powers aren't talents; their
                # costs come from champion_abilities (gamedata seed).
                from db import db_champion_ability_costs
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
            if (charges >= cc and sp >= eff_sc
                    and self._champion_thresholds_met(ability_guid, bstate)):
                bstate["player_charges"] = charges - cc
                bstate["player_spell_points"] = sp - eff_sc
                if sc > 0:
                    sp_uses[ability_guid] = used + 1
                    bstate["player_sp_uses"] = sp_uses
                g = self._fresh_game(session, pl_t, ai_t, bstate)
                # Push charge/SP deduction events
                ev_chg = game_engine.ChampionChargePointsChangedSessionEventArgs()
                ev_chg.player_id = pl_t; ev_chg.operation = 2; ev_chg.delta = cc
                ev_chg.new_value = bstate["player_charges"]; g._push(ev_chg)
                if eff_sc:
                    ev_sp = game_engine.ChampionSpellPointsChangedSessionEventArgs()
                    ev_sp.player_id = pl_t; ev_sp.operation = 2; ev_sp.delta = eff_sc
                    ev_sp.new_value = bstate["player_spell_points"]; g._push(ev_sp)
                # Reflect the escalated SP cost on the champion card so the
                # client's button cost display updates (CardUpdated
                # SpellPointCostModifiers).
                player_champ_scid = getattr(self, "_player_champ_scid", None)
                if player_champ_scid and sp_uses.get(ability_guid):
                    cdef = g.card_defs.get(player_champ_scid)
                    if cdef is not None:
                        for ag2, uses2 in sp_uses.items():
                            if int(uses2) > 0:
                                cdef.spell_point_cost_mods[game_engine.ResourceId.from_str(ag2)] = int(uses2)
                        g.push_card_updated(player_champ_scid, pl_t,
                                            game_engine.ECardCollections.Champions,
                                            game_engine.ECardTypes.Champion,
                                            template_id=getattr(self, "_player_champ_guid", None))
                # Push the ability onto the CHAIN (the client's stack). The
                # BOM (draw / discard / etc.) executes when the chain
                # resolves (both players pass). One chain item per top-level
                # ability — its sub-effects (e.g. Soothsaying's draw +
                # discard) are bundled and resolve together, so a counterspell
                # has a single entry to cancel.
                champ_scid = player_champ_scid if player_champ_scid else pl_t
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
                    champ_scid.uid.to_uint64() if hasattr(champ_scid, "uid")
                    else 0, all_target_uids)
                if selection is None:
                    log_req(f"    Champion ability {ability_guid[:8]}: "
                            "missing/illegal payment or effect target")
                    return True
                target_uid, sacrifice_uids = selection
                for sacrifice_uid in sacrifice_uids:
                    self._sacrifice_troop(
                        g, session, pl_t, ai_t, sacrifice_uid)
                if all_target_uids:
                    # Multi-target champion powers (e.g. Bun'jitsu's
                    # "Void two ready troops you control") carry every
                    # chosen card; the resolver voids them and reads
                    # their stats for the summoned-token buffs.
                    bstate["champion_void_uids"] = all_target_uids
                bstate["player_mod_target"] = target_uid
                _be.stack_push(bstate, {
                    "kind": "ability", "ability_guid": str(ability_guid),
                    "source_uid": champ_scid.uid.to_uint64() if hasattr(champ_scid, 'uid') else 0,
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
                # The chain is non-empty -> ResolveTopOfChain makes the client
                # show "Resolve <Card>" as the pass button.
                g.push_green_light(pl_t, self._priority_context_for(
                    _be.current_phase(bstate), bstate))
                self._send_battle_events(session, g, pl_t)
                # Push fresh playability so used ability un-lights
                self._push_main_phase_options(session, pl_t, ai_t)
                log_req(f"    Ability activated on chain: charges {charges}->{bstate['player_charges']}, SP {sp}->{bstate['player_spell_points']}")
            else:
                log_req(f"    Cannot afford ability: need {cc} charges/{sc} SP, have {charges}/{sp}")
        elif not handled:
            log_req("    Ability activation: could not parse GUID")
        if not handled:
            # Always respond so the client isn't blocked (champion
            # abilities; troop abilities ack inside _activate_troop_ability).
            self._push_transaction_ack(session)
        return True

    def _handle_commit_defense_transaction(self, session, transaction):
        """Declare the player's blockers for the current combat."""
        inner_bytes = transaction.inner_bytes
        import battle_engine as _be
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(3, 1000)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        attackers = {int(k): int(v) for k, v in (bstate.get("ai_attackers") or {}).items()}
        from db import db_warzone_card_uids
        player_troops = set(db_warzone_card_uids(session.session_id, self.user_profile["id"]))
        all_uids = []
        if isinstance(inner_bytes, bytes):
            for m in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                try:
                    u = struct.unpack('<Q', bytes.fromhex(m.group(1).decode()))[0]
                    if (u & 0xFF) == 1:
                        all_uids.append(int(u))
                except Exception:
                    continue
        blockers_map = {}
        cur_attacker = None
        for u in all_uids:
            if u in attackers:
                cur_attacker = u
                blockers_map.setdefault(cur_attacker, [])
            elif cur_attacker is not None and u in player_troops:
                blockers_map[cur_attacker].append(u)
        # Authoritative block legality: Flight needs a Flight/SkyGuard
        # blocker, and "can't be blocked except by artifact troops
        # and/or blood troops" (Corrupt Harvester, Wailing Banshee)
        # only allows qualifying blockers.  Drop illegal declarations.
        from abilities.framework.statics import can_block
        for u in list(blockers_map):
            blockers_map[u] = [b for b in blockers_map[u]
                               if can_block(_db, session.session_id,
                                            bstate, u, b)]
        bstate["ai_blockers"] = {str(k): [str(b) for b in v]
                                 for k, v in blockers_map.items()}
        block_uids = {b for v in blockers_map.values() for b in v}
        if block_uids:
            from db import db_bulk_blocker_state
            db_bulk_blocker_state(session.session_id, list(block_uids))
        game = self._fresh_game(session, pl_t, ai_t, bstate)
        player_champ_scid = getattr(self, "_player_champ_scid", None) or game_engine.SessionCardId(pl_t)
        combats = []
        for u in attackers:
            scid = game_engine.SessionCardId(game_engine.UID(u))
            combat_id = game_engine.CombatId(ai_t, u & 0xFFFF)
            blockers = [game_engine.SessionCardId(game_engine.UID(int(b)))
                        for b in blockers_map.get(u, [])]
            game.push_blockers_assigned(combat_id, scid, player_champ_scid, blockers)
            cs = game_engine.CombatSessionEventArgs()
            cs.player_id = ai_t
            cs.id = combat_id
            cs.attacker = scid
            cs.blockers = blockers
            combats.append(cs)
        if combats:
            game.push_combat_listing(pl_t, combats)
        self._send_battle_events(session, game, pl_t)
        log_req(f"    CommitDefense: {len(attackers)} attacker(s), "
                f"blocks={{ {', '.join(hex(int(k)) + '->' + (hex(int(v[0])) if v else 'none') for k, v in blockers_map.items())} }}")
        start_idx = bstate.get("ai_turn_phase_idx", 0)
        bstate.pop("ai_turn_phase_idx", None)
        _be.save_state(session, bstate)
        self._run_ai_turn(session, pl_t, ai_t, bstate, start_idx=start_idx)
        return True

    def _handle_commit_attack_transaction(self, session, transaction):
        """Declare the player's attackers for the current combat."""
        inner_bytes = transaction.inner_bytes
        import battle_engine as _be
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(3, 1000)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        # Extract the attacking card UIDs: every Card-type SessionCardId
        # in the transaction that is in the player's warzone. The
        # defender (AI champion) is not in the player's warzone.
        attacker_uids = []
        if isinstance(inner_bytes, bytes):
            for m_du in re.finditer(rb'm_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});', inner_bytes):
                try:
                    uid64 = struct.unpack('<Q', bytes.fromhex(m_du.group(1).decode()))[0]
                    if (uid64 & 0xFF) == 1:  # Card type
                        attacker_uids.append(int(uid64))
                except Exception:
                    continue
        # Only cards actually in the player's warzone are attackers.
        from db import db_warzone_card_uids
        wz_uids = set(db_warzone_card_uids(session.session_id, self.user_profile["id"]))
        attackers = [u for u in attacker_uids if u in wz_uids]
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
        _be.save_state(session, bstate)
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
            combat_id = game_engine.CombatId(pl_t, u & 0xFFFF)
            game.push_attack_declared(combat_id, pl_t, ai_champ_scid, cid)
            from db import db_card_template_attrs_joined, db_card_set_attacking_state, db_card_state_raw
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
            db_card_set_attacking_state(session.session_id, int(u), state)
            from db import db_card_state_raw
            pushed_state = db_card_state_raw(session.session_id, int(u))
            if not pushed_state:
                pushed_state = state
            self._card_full_data(game, cid, tpl_guid)
            game.push_card_updated(cid, pl_t, game_engine.ECardCollections.Warzone,
                                   game_engine.ECardTypes.Troop,
                                   template_id=tpl_guid,
                                   state=pushed_state)
            # Attacking normally exhausts the troop.  Emit the same typed tap
            # event used by activated and AI attacks so data-defined effects
            # such as Spider Nest's granted "when this exhausts" ability fire
            # for player-declared attackers too.  Steadfast attackers do not
            # become tapped and therefore must not emit this event.
            if state & game_engine.ECardStates.Tapped:
                import ability as _abil
                _abil.resolve_triggers(
                    _db, self, game, session, pl_t, ai_t, bstate,
                    "CardTappedEvent", int(u), self.user_profile["id"])
            cs = game_engine.CombatSessionEventArgs()
            cs.player_id = pl_t
            cs.id = combat_id
            cs.attacker = cid
            cs.blockers = []
            combats.append(cs)
            # Fire "when this attacks" triggers (e.g. Chimera Guard
            # Outrider: +[ATK] equal to this troop's [DEF] this turn).
            import ability as _abil
            _abil.resolve_triggers(
                _db, self, game, session, pl_t, ai_t, bstate,
                "CardAttackedEvent", u, self.user_profile["id"])
            _abil.resolve_triggers(
                _db, self, game, session, pl_t, ai_t, bstate,
                "CardAttackedOrBlockedEvent", u, self.user_profile["id"])
            # Rage X: when this attacks it gets +X ATK this turn.
            from abilities.framework.keywords.combat import apply_rage_keyword
            apply_rage_keyword(_db, session, self, game, pl_t, ai_t, bstate, u)
        _db.commit()
        game.push_combat_listing(pl_t, combats)
        self._send_battle_events(session, game, pl_t)
        # Advance to DeclareAttackPriorityWindow (the next stop).
        _be.advance_phase(bstate)
        _be.save_state(session, bstate)
        self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True

    def _handle_assign_damage_transaction(self, session, transaction):
        """Resolve the client's combat damage assignment transaction."""
        inner_bytes = transaction.inner_bytes
        import battle_engine as _be
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(3, 1000)
        bstate = _be.load_state(session)
        self._current_bstate = bstate
        cur_phase = _be.current_phase(bstate)
        if cur_phase == game_engine.ETurnPhases.AssignDamage:
            n_att = len(bstate.get("player_attackers") or {})
            # The AssignDamageOrderTransaction carries the player's
            # chosen blocker order (weakest-to-toughest) per combat:
            # m_AssignedDamageOrder -> DamageAssignment(CombatId,
            # ordered CardIds). Extract every card UID in stream order,
            # then order each attacker's blockers by their first
            # occurrence so resolve_combat assigns damage in that order.
            if isinstance(inner_bytes, bytes):
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
                    _be.save_state(session, bstate)
            log_req(f"    AssignDamageOrder (AssignDamage step): resolving {n_att} attackers")
            bstate = self._resolve_combat_damage(session, pl_t, ai_t, bstate)
            _be.save_state(session, bstate)
            # If the AI's champion is dead, end the game (player wins).
            if bstate.get("ai_health", 10) <= 0:
                log_req(f"    AI defeated! ai_health={bstate['ai_health']}")
                import commands as _cmd
                _cmd.push_battle_game_end(handler=self, session=session,
                                          winners=[pl_t], losers=[ai_t])
                campaign.handle_battle_gameend(self, _db, session, True, SERVICE_MAIL_UID, UID_TYPE["ServiceCampaign"])
                handled = True
                self._push_transaction_ack(session)
                return True
        elif cur_phase == game_engine.ETurnPhases.AssignFirstStrikeDamage:
            # Swiftstrike step: FirstStrike/DualStrike combatants deal
            # damage now; casualties are removed before the normal step.
            log_req("    AssignDamageOrder (Swiftstrike step): "
                    "resolving first-strike damage")
            bstate = self._resolve_combat_damage(
                session, pl_t, ai_t, bstate, first_strike=True)
            _be.save_state(session, bstate)
        else:
            log_req(f"    AssignDamageOrder ({cur_phase}): no damage yet, advancing")
        if bstate.get("pending_deck_search"):
            # A combat-death deck-search prompt is awaiting the player's
            # answer — do NOT advance to the next phase yet (the phase
            # packet would tear the client's target picker down).  The
            # SetAbilityActivationData answer advances the turn.
            _be.save_state(session, bstate)
            self._push_transaction_ack(session)
            log_req("    AssignDamage: paused for deck-search answer")
            handled = True
            return True
        # Advance to the next phase (SecondMain after AssignDamage).
        _be.advance_phase(bstate)
        _be.save_state(session, bstate)
        self._advance_to_priority(session, pl_t, ai_t, bstate)
        return True

    def _handle_set_auto_pass_transaction(self, session, transaction):
        """Record and apply the client's one-turn auto-pass choice."""
        inner_bytes = transaction.inner_bytes
        import battle_engine as _be
        import re
        handled = False
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(3, 1000)
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
        import battle_engine as _be
        handled = False
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(3, 1000)
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
                        ai_t = game_engine.UID.make(3, 1000)
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
        import battle_engine as _be
        pl_t = game_engine.UID.make(244, int(self.client_reck_id))
        ai_t = game_engine.UID.make(3, 1000)
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
            elif bstate.get("turn_player") == _be.AI or bstate.get("ai_turn_phase_idx") is not None:
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
                import ability as _abil_end2
                g_end2 = self._fresh_game(session, pl_t, ai_t, bstate)
                _abil_end2.resolve_triggers(
                    _db, self, g_end2, session, pl_t, ai_t, bstate,
                    "TurnEndedEvent", None, self.user_profile["id"])
                from abilities.framework._shared import (
                    clear_combat_damage, clear_expired_temporary_attributes)
                clear_combat_damage(_db, session.session_id)
                clear_expired_temporary_attributes(
                    _db, session.session_id, self.user_profile["id"],
                    "end_turn", clear_stat_buffs=True)
                for cu, tpl, card_user_id in _db.execute(
                        "SELECT card_uid, template_guid, user_id FROM game_cards "
                        "WHERE session_id=? AND location='warzone'",
                        (session.session_id,)).fetchall():
                    scid = game_engine.SessionCardId(game_engine.UID(cu))
                    _tpl, ct, _n, _c, _a, _d, _g = self._card_full_data(
                        g_end2, scid, tpl)
                    crow = _db.execute(
                        "SELECT card_state FROM game_cards WHERE session_id=? "
                        "AND card_uid=?", (session.session_id, cu)).fetchone()
                    g_end2.push_card_updated(
                        scid, pl_t if card_user_id else ai_t,
                        game_engine.ECardCollections.Warzone, ct,
                        template_id=tpl,
                        state=int(crow[0]) if crow else 0)
                self._send_battle_events(session, g_end2, pl_t)
                next_player = _be.next_turn_player(bstate)
                bstate["turn_player"] = next_player
                bstate["player_passed"] = False
                bstate["ai_passed"] = False
                bstate["phase_idx"] = 0
                bstate["turn_phases"] = _be.BASE_TURN_PHASES
                if next_player == _be.AI:
                    bstate["ai_resource_played_this_turn"] = False
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
                        # A FRESH game seeded from battle state so any
                        # PlayerUpdated pushed during resolution (e.g.
                        # after a heal) reports the real health/resources.
                        gs = self._fresh_game(session, pl_t, ai_t, bstate)
                        self._resolve_stack_item(session, pl_t, ai_t, bstate, item, gs)
                        if (_be.stack_empty(bstate) and
                                not bstate.get("pending_choice") and
                                not bstate.get("pending_trigger") and
                                not bstate.get("pending_deck_search")):
                            gs.push_chain_empty()
                        # Priority to the active player for the next item
                        # (or the next phase). ResolveTopOfChain keeps the
                        # pass button labelled "Resolve <Card>".
                        cur_phase = _be.current_phase(bstate)
                        if (_be.stack_empty(bstate) and
                                not bstate.get("pending_choice") and
                                not bstate.get("pending_trigger") and
                                not bstate.get("pending_deck_search") and
                                bstate.get("turn_player") == _be.PLAYER):
                            if cur_phase == game_engine.ETurnPhases.FirstMainPhase:
                                # A troop may have entered during this chain
                                # (notably a Speed troop). Recompute before
                                # rebuilding the green light so the next
                                # prompt immediately offers ProceedToCombat.
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
                            gs.push_turn_phase(cur_phase, pl_t, pl_t)
                            self._push_phase_options(session, pl_t, ai_t, cur_phase)
                        if not (bstate.get("pending_choice") or
                                bstate.get("pending_trigger") or
                                bstate.get("pending_deck_search")):
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
                            import commands as _cmd
                            _cmd.push_battle_game_end(handler=self, session=session,
                                                      winners=[pl_t], losers=[ai_t])
                            campaign.handle_battle_gameend(self, _db, session, True, SERVICE_MAIL_UID, UID_TYPE["ServiceCampaign"])
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

    def _handle_service_request_legacy(self, target, instance, data_type, reqid,
                                       comp, session_id, conh, inner_obj,
                                       inner_bytes):
        log_req(f"    SR: target={target} dt={data_type}")
        # This method has several branch-local imports of battle_engine as
        # `_be`; Python therefore treats `_be` as a local for the entire
        # function.  Import it before the F10/auto-pass branches use it.
        import battle_engine as _be

        # === Dispatch to service modules (Layer 2: extract logic) ===
        try:
            if _dispatch_service(self, data_type, target, instance, reqid, comp,
                                 session_id, conh, inner_obj, inner_bytes):
                return
        except Exception as e:
            log_req(f"    Dispatch error for dt={data_type}: {e}")

        # GetUnreadMailCount (60007)
        if data_type == 60007:
            from db import db_get_unread_mail_count
            count = db_get_unread_mail_count(self.user_profile["id"]) if self.user_profile else 0
            
            # First mail check after login = client is ready
            if self._inventory_pending and self.user_profile:
                self._inventory_pending = False
                log_req(">>> Client ready (mail check received)")
            
            log_req(f">>> Respond GetUnreadMailCount -> {count}")
            resp_inner = encode_get_unread_mail_count_response(count)
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceMail.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str,
                "target": target,
                "instance": instance,
                "reqid": resp_reqid,
                "c": comp,
                "conh": conh,
                "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent GetUnreadMailCount response ({len(dw_bytes)}b)")
    
        # Mail.Receive (60002) — get mail list
        elif data_type == 60002:
            from db import db_get_mail_list
            db_emails = db_get_mail_list(self.user_profile["id"]) if self.user_profile else []
            log_req(f">>> Respond Mail.Receive -> {len(db_emails)} emails")
            now = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())
    
            # Include envelope type + UID type
            type_names = [
                "Game.Shared.Mail.Messages.Mail+Receive+Response",
                "Game.Shared.Mail.Messages.Mail+PagingResponse",
                "System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+Envelope",
                "Game.Shared.Mail.Messages.Mail+Envelope",
                "Game.Shared.UID",
                "System.UInt64",
                "System.String",
                "System.UInt32",
                "System.Int32",
                "System.DateTime",
            ]
            def ft(tn):
                if tn not in type_names:
                    type_names.append(tn)
                return type_names.index(tn)
    
            sizes = []
            buf = io.BytesIO()
            w = lambda s: buf.write(s.encode("utf-8"))
            sep = lambda: buf.write(b";")
            lf = lambda: buf.write(b"\n")
    
            sizes.append(0)
            w(""); sep(); w("0"); sep(); w(str(ft(type_names[0]))); sep(); w("1"); sep()
    
            f1 = buf.tell(); sizes.append(0)
            w("PagingResp"); sep(); w("1"); sep(); w(str(ft(type_names[1]))); sep(); w("5"); sep()
    
            # Envelopes collection
            f2 = buf.tell(); sizes.append(0)
            w("Envelopes"); sep(); w("2"); sep(); w(str(ft(type_names[2]))); sep(); w("0"); sep()
            w(str(len(db_emails))); sep()
            
            for i, (eid, sender, subject, body, sent, gold_dlv, plat_dlv, claimed) in enumerate(db_emails):
                fe = buf.tell(); sizes.append(0)
                eidx = len(sizes) - 1
                w(str(i)); sep(); w(str(eidx)); sep(); w(str(ft(type_names[3]))); sep(); w("11"); sep()
                
                def write_email_field(ftype, name, val):
                    f = buf.tell(); sizes.append(0)
                    w(name); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(ftype))); sep(); w("0"); sep()
                    if ftype == "System.UInt64":
                        w(hexlify(struct.pack("<Q", val)).decode("ascii")); sep()
                    elif ftype == "System.String":
                        enc = val.encode("utf-8")
                        w(str(len(enc))); sep(); buf.write(enc)
                    elif ftype == "System.UInt32":
                        w(hexlify(struct.pack("<I", val)).decode("ascii")); sep()
                    elif ftype == "System.Int32":
                        w(hexlify(struct.pack("<i", val)).decode("ascii")); sep()
                    elif ftype == "System.DateTime":
                        enc = str(val).encode("utf-8")
                        w(str(len(enc))); sep(); buf.write(enc)
                    sizes[-1] = buf.tell() - f
                
                def write_uid_field(name, val):
                    f = buf.tell(); sizes.append(0)
                    w(name); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.UID"))); sep(); w("1"); sep()
                    fsub = buf.tell(); sizes.append(0)
                    w("m_UID64"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
                    w(hexlify(struct.pack("<Q", val)).decode("ascii")); sep()
                    sizes[-1] = buf.tell() - fsub
                    sizes[-2] = buf.tell() - f
                
                write_uid_field("MailID", 1000 + eid)
                write_uid_field("SenderID", 0)
                write_email_field("System.String", "SenderName", sender)
                write_uid_field("ReceiverID", int(self.client_reck_id))
                write_email_field("System.String", "ReceiverName", self.user_profile["name"])
                write_email_field("System.String", "Template", "")
                write_email_field("System.String", "Subject", subject)
                write_email_field("System.String", "Body", body or "")
                write_email_field("System.UInt32", "Platinum", plat_dlv or 0)
                write_email_field("System.UInt32", "Gold", gold_dlv or 0)
                write_email_field("System.DateTime", "Created", now)
    
                sizes[eidx] = buf.tell() - fe
            
            sizes[2] = buf.tell() - f2
    
            # MinTime
            f3 = buf.tell(); sizes.append(0)
            w("MinTime"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(type_names[9]))); sep(); w("0"); sep()
            enc = now.encode("utf-8")
            w(str(len(enc))); sep(); buf.write(enc)
            sizes[-1] = buf.tell() - f3
    
            # MaxTime
            f4 = buf.tell(); sizes.append(0)
            w("MaxTime"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(type_names[9]))); sep(); w("0"); sep()
            w(str(len(enc))); sep(); buf.write(enc)
            sizes[-1] = buf.tell() - f4
    
            # Offset
            f5 = buf.tell(); sizes.append(0)
            w("Offset"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(type_names[7]))); sep(); w("0"); sep()
            w(hexlify(struct.pack("<I", 0)).decode("ascii")); sep()
            sizes[-1] = buf.tell() - f5
    
            # Total
            f6 = buf.tell(); sizes.append(0)
            w("Total"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(type_names[7]))); sep(); w("0"); sep()
            w(hexlify(struct.pack("<I", len(db_emails))).decode("ascii")); sep()
            sizes[-1] = buf.tell() - f6
    
            sizes[1] = buf.tell() - f1
            sizes[0] = buf.tell()
    
            w(";".join(type_names))
            lf()
            for i, s in enumerate(sizes):
                if i > 0: w(";")
                w(str(s))
    
            resp_inner = buf.getvalue()
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceMail.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent Mail.Receive response ({len(dw_bytes)}b)")
    
        # Mail.Delivered (60005) — sent mail
        elif data_type == 60005:
            log_req(">>> Respond Mail.Delivered -> 0 (empty)")
            now = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())
            type_names = [
                "Game.Shared.Mail.Messages.Mail+Delivered+Response",
                "Game.Shared.Mail.Messages.Mail+PagingResponse",
                "System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+Envelope",
                "System.DateTime", "System.UInt32",
            ]
            def ft(tn):
                if tn not in type_names:
                    type_names.append(tn)
                return type_names.index(tn)
            sizes = []
            buf = io.BytesIO()
            w = lambda s: buf.write(s.encode("utf-8"))
            sep = lambda: buf.write(b";")
            lf = lambda: buf.write(b"\n")
            sizes.append(0)
            w(""); sep(); w("0"); sep(); w(str(ft(type_names[0]))); sep(); w("1"); sep()
            f1 = buf.tell(); sizes.append(0)
            w("PagingResp"); sep(); w("1"); sep(); w(str(ft(type_names[1]))); sep(); w("5"); sep()
            f2 = buf.tell(); sizes.append(0)
            w("Envelopes"); sep(); w("2"); sep(); w(str(ft(type_names[2]))); sep(); w("0"); sep()
            w("0"); sep()
            sizes[2] = buf.tell() - f2
            f3 = buf.tell(); sizes.append(0)
            w("MinTime"); sep(); w("3"); sep(); w(str(ft(type_names[3]))); sep(); w("0"); sep()
            enc = now.encode("utf-8"); w(str(len(enc))); sep(); buf.write(enc)
            sizes[3] = buf.tell() - f3
            f4 = buf.tell(); sizes.append(0)
            w("MaxTime"); sep(); w("4"); sep(); w(str(ft(type_names[3]))); sep(); w("0"); sep()
            w(str(len(enc))); sep(); buf.write(enc)
            sizes[4] = buf.tell() - f4
            f5 = buf.tell(); sizes.append(0)
            w("Offset"); sep(); w("5"); sep(); w(str(ft(type_names[4]))); sep(); w("0"); sep()
            w(hexlify(struct.pack("<I", 0)).decode("ascii")); sep()
            sizes[5] = buf.tell() - f5
            f6 = buf.tell(); sizes.append(0)
            w("Total"); sep(); w("6"); sep(); w(str(ft(type_names[4]))); sep(); w("0"); sep()
            w(hexlify(struct.pack("<I", 0)).decode("ascii")); sep()
            sizes[6] = buf.tell() - f6
            sizes[1] = buf.tell() - f1
            sizes[0] = buf.tell()
            w(";".join(type_names)); lf()
            for i, s in enumerate(sizes):
                if i > 0: w(";")
                w(str(s))
            resp_inner = buf.getvalue()
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceMail.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent Mail.Delivered response ({len(dw_bytes)}b)")
    
        # Mail.MarkRead (60004)
        elif data_type == 60004:
            log_req(">>> Respond Mail.MarkRead")
            from db import db_mark_all_mail_read
            if self.user_profile:
                db_mark_all_mail_read(self.user_profile["id"])
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Mail.Messages.Mail+MarkRead+Response"], []
            )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceMail.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent Mail.MarkRead response ({len(dw_bytes)}b)")
    
        # Mail.MarkDelete (60006)
        elif data_type == 60006:
            log_req(">>> Respond Mail.MarkDelete")
            # Mark all user's emails as deleted in DB
            from db import db_delete_all_mail
            if self.user_profile:
                db_delete_all_mail(self.user_profile["id"])
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Mail.Messages.Mail+MarkDelete+Response"], []
            )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceMail.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent Mail.MarkDelete response ({len(dw_bytes)}b)")
    
        # Mail.Claim (60003) — claim attachments from a mail
        elif data_type == 60003:
            log_req(">>> Mail.Claim (dt=60003)")
            p = self.user_profile
            mail_id_obj = inner_obj.get("MailID", {})
            mail_id_64 = 0
            if isinstance(mail_id_obj, dict):
                mail_id_64 = mail_id_obj.get("m_UID64", 0)
            elif isinstance(mail_id_obj, int):
                mail_id_64 = mail_id_obj
            eid = mail_id_64 - 1000 if mail_id_64 else 0
    
            gold_granted = 0
            plat_granted = 0
            if eid > 0 and p:
                from db import db_get_mail_by_id, db_claim_mail
                row = db_get_mail_by_id(eid, p["id"])
                if row and not row[3]:
                    gold_granted = row[1] or 0
                    plat_granted = row[2] or 0
                    if gold_granted or plat_granted:
                        new_gold = p["gold"] + gold_granted
                        new_plat = p["platinum"] + plat_granted
                        db_update_resources(p["id"], gold=new_gold, platinum=new_plat)
                        p["gold"] = new_gold
                        p["platinum"] = new_plat
                        log_req(f"    Claimed mail #{eid}: gold+{gold_granted} plat+{plat_granted}")
                    db_claim_mail(eid)
                else:
                    log_req(f"    Mail #{eid} already claimed or not found")
    
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Mail.Messages.Mail+Claim+Response",
                 "System.UInt64",
                 "System.UInt32",
                 "System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+AttachmentCard",
                 "System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+AttachmentInventory"],
                [("EnvCLID", "ulong", mail_id_64),
                 ("CardC", "uint", 0),
                 ("InvenC", "uint", 0),
                 ("Cards", "coll", ("System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+AttachmentCard", 0)),
                 ("Inven", "coll", ("System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+AttachmentInventory", 0))]
            )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceMail.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send_and_cache({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes, data_type, reqid, target, instance)
            log_req(f"    Sent Mail.Claim response ({len(dw_bytes)}b)")
    
        # PingMailServer (9001)
        elif data_type == 9001:
            log_req(">>> Respond PingMailServer (dt=9001)")
            timestamp = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())
            resp_inner = encode_ping_mail_server_response(timestamp)
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceMail.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str,
                "target": target,
                "instance": instance,
                "reqid": resp_reqid,
                "c": comp,
                "conh": conh,
                "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent PingMailServer response ({len(dw_bytes)}b)")
    
        # ServiceProfile (80000)
        elif data_type == 80000:
            envelope = inner_obj.get("Envelope", b"{}")
            log_req(f">>> ServiceProfile (dt=80000) env={len(envelope)}b")
            try:
                env_json = json.loads(envelope.decode("utf-8"))
                log_req(f"    Envelope action: {env_json.get('action', '?')}")
                for k, v in env_json.items():
                    log_req(f"      {k}={str(v)[:80]}")
            except:
                log_req(f"    Envelope raw: {hexdump(envelope)}")
            try:
                env_json = json.loads(envelope.decode("utf-8"))
            except (TypeError, ValueError, UnicodeDecodeError):
                env_json = {}
            if isinstance(env_json, dict) and env_json.get("action") == "qreplaylst":
                from services.replay import replay_list
                resp_envelope = json.dumps(
                    replay_list(env_json), separators=(",", ":")
                ).encode("utf-8")
                log_req("    Replay list returned from game_replays")
            else:
                resp_envelope = b"{}"
            resp_inner = encode_profile_response(resp_envelope)
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str,
                "target": target,
                "instance": instance,
                "reqid": resp_reqid,
                "c": comp,
                "conh": conh,
                "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent ServiceProfile response ({len(dw_bytes)}b)")

        # ServiceGameSession ReplayFetch (160000)
        elif data_type == 160000:
            envelope = inner_obj.get("Envelope", b"{}")
            try:
                env_json = json.loads(envelope.decode("utf-8"))
            except (TypeError, ValueError, UnicodeDecodeError):
                env_json = {}
            if isinstance(env_json, dict) and env_json.get("action") == "replayfetch":
                from services.replay import replay_fetch
                resp_envelope = replay_fetch(env_json)
                log_req(
                    f">>> ReplayFetch session={env_json.get('Session', '')} "
                    f"offset={env_json.get('Offset', 0)} size={len(resp_envelope)}"
                )
            else:
                resp_envelope = b"\0\0\0\0\0"
                log_req(">>> ReplayFetch invalid request")
            resp_inner = encode_objfmt_response(
                ["Game.Shared.GameReq.GameReq+Response", "System.Byte[]"],
                [("Envelope", "bytes", resp_envelope)],
            )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = (
                f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}."
                f"{instance}.{resp_reqid}"
            )
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent ReplayFetch response ({len(dw_bytes)}b)")
    
        # ServiceLoadBalancer — QuitOnReconnectionGame (22011)
        elif data_type == 22011:
            # The landing page sends this when the player declines the
            # reconnect dialog.  It is deliberately fire-and-forget (the
            # client supplies no response callback), but the server still
            # has to close/forfeit the PvP session or the same session will
            # be offered again on the next login.
            reconnect_pid = int(self.client_reck_id or 0)
            reconnect_session = game_session.find_session_by_player(reconnect_pid)
            if reconnect_session:
                try:
                    from services.tournament_game import (
                        pvp_load_state, _pvp_end_game, pvp_session_lock,
                    )
                    from db import db_game_session_pids
                    reconnect_state = pvp_load_state(reconnect_session) or {}
                    pids = db_game_session_pids(
                        reconnect_session.session_id)
                    if (reconnect_state.get("pvp") and len(pids) >= 2
                            and reconnect_session.state != "ended"):
                        winner_pid = pids[1] if pids[0] == reconnect_pid else pids[0]
                        with pvp_session_lock(reconnect_session):
                            _pvp_end_game(
                                reconnect_session, reconnect_state,
                                winner_pid, reconnect_pid,
                                "player declined reconnect")
                        log_req(
                            f">>> QuitOnReconnectionGame: PvP session "
                            f"{reconnect_session.session_name} forfeited by "
                            f"pid {reconnect_pid}")
                    else:
                        log_req(
                            f">>> QuitOnReconnectionGame: ignored non-PvP or "
                            f"ended session for pid {reconnect_pid}")
                except Exception as exc:
                    log_req(f">>> QuitOnReconnectionGame failed: {exc}")
            else:
                log_req(">>> QuitOnReconnectionGame: no session")
            # No 22011 response: this transaction is sent without a client
            # callback and the fixed client does not expect one.

        # ServiceLoadBalancer — TryReconnectionToDisconnectedGame (22013)
        elif data_type == 22013:
            reconnect_pid = int(self.client_reck_id or 0)
            reconnect_session = game_session.find_session_by_player(reconnect_pid)
            reconnect_state = {}
            if reconnect_session:
                try:
                    from services.tournament_game import pvp_load_state
                    reconnect_state = pvp_load_state(reconnect_session) or {}
                except Exception:
                    reconnect_state = {}
            if (reconnect_session
                    and getattr(reconnect_session, "state", "") != "ended"
                    and str(getattr(reconnect_session, "session_name", "") or "").startswith("tourney-")
                    and reconnect_state.get("pvp")):
                session_uid = int(reconnect_session.session_id)
                try:
                    tournament_id = int(reconnect_session.session_name.split("-", 1)[1])
                except (IndexError, ValueError):
                    tournament_id = 0
                tournament_format = _db.execute(
                    "SELECT tt.format FROM tournaments t "
                    "JOIN tournament_types tt ON t.type_id=tt.id "
                    "WHERE t.id=? LIMIT 1", (tournament_id,)
                ).fetchone()
                reconnect_flags = _tournament_session_flags({
                    "format": tournament_format[0] if tournament_format else 0
                })
                deck_db_id = int(player_decks.get(reconnect_pid, 0) or 0)
                deck_uid = encoder.make_uid(244, deck_db_id) if deck_db_id else 0
                resp_inner = encode_objfmt_response(
                    ["Game.Client.Network.LoadBalancer.TryReconnectionToDisconnectedGameResponse",
                     "Game.Shared.SessionState", "Game.Shared.UID",
                     "Game.Shared.SessionStateEncounterData",
                     "Game.Shared.ResourceId", "System.Guid",
                     "Game.Shared.ESessionFlags", "System.UInt64",
                     "System.Int32", "System.String", "System.Boolean",
                     "Game.Shared.Network.LoadBalancer.ETryReconnectionToDisconnectedGameError",
                     "System.String"],
                    [("SessionState", "struct", ("Game.Shared.SessionState", [
                        ("SessionId", "uid", session_uid),
                        ("SessionName", "string", reconnect_session.session_name),
                        ("MinimumPlayerCount", "int", 2),
                        ("MaximumPlayerCount", "int", 2),
                        # The client dereferences EncounterData before it
                        # enters Battle and UIBattle later reads the flags.
                        # An empty `class` field decodes as null on the fixed
                        # client, so encode a real PvP encounter object even
                        # though the reconnect snapshot restores the board.
                        ("EncounterData", "struct", (
                            "Game.Shared.SessionStateEncounterData", [
                                ("SceneTemplateId", "struct", (
                                    "Game.Shared.ResourceId", [
                                        ("m_Guid", "guid",
                                         "00000000-0000-0000-0000-000000000000")])),
                                ("SessionFlags", "enum1", (
                                    "Game.Shared.ESessionFlags", reconnect_flags)),
                                ("TournamentID", "ulong", tournament_id),
                            ])),
                        ("JoinInsteadOfReconnect", "bool", False),
                    ])),
                     ("DeckID", "uid", deck_uid),
                     ("Error", "enum1", (
                         "Game.Shared.Network.LoadBalancer.ETryReconnectionToDisconnectedGameError", 0)),
                     ("ErrorMessage", "string", "")])
                log_req(f">>> Respond TryReconnectionToDisconnectedGame -> "
                        f"{reconnect_session.session_name}")
            else:
                log_req(">>> Respond TryReconnectionToDisconnectedGame -> no game")
                resp_inner = encode_objfmt_response(
                    ["Game.Client.Network.LoadBalancer.TryReconnectionToDisconnectedGameResponse",
                     "Game.Shared.UID",
                     "Game.Shared.Network.LoadBalancer.ETryReconnectionToDisconnectedGameError",
                     "System.String"],
                    [("DeckID", "uid", 0),
                     ("Error", "enum1", (
                         "Game.Shared.Network.LoadBalancer.ETryReconnectionToDisconnectedGameError", 0)),
                     ("ErrorMessage", "string", "")])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceLoadBalancer.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str,
                "target": target,
                "instance": instance,
                "reqid": resp_reqid,
                "c": comp,
                "conh": conh,
                "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent TryReconnection response ({len(dw_bytes)}b)")

        # ServiceLoadBalancer — FindSession (22019)
        elif data_type == 22019:
            log_req(">>> Respond FindSession -> no session")
            player_uid = make_uid(UID_TYPE["ServicePlayer"], int(self.client_reck_id))
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Network.LoadBalancer.FindSessionResponseArgs",
                 "Game.Shared.UID", "System.Boolean", "Game.Shared.SessionState"],
                [("RoutingPlayerId", "uid", player_uid),
                 ("Success", "bool", False),
                 ("SessionState", "class", "Game.Shared.SessionState")]
            )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceLoadBalancer.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str,
                "target": target,
                "instance": instance,
                "reqid": resp_reqid,
                "c": comp,
                "conh": conh,
                "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent FindSession response ({len(dw_bytes)}b)")

        # ServiceLoadBalancer — StartSession (22015)
        elif data_type == 22015:
            log_req(">>> StartSession (dt=22015) -> creating session")
            player_uid = make_uid(UID_TYPE["ServicePlayer"], int(self.client_reck_id))
            session_name = inner_obj.get("SessionName", "session")
            session = self._application.execute(StartSessionCommand(
                session_name=session_name,
                player_uid=player_uid,
            )).value
            sess_id = session.session_id

            resp_inner = encode_objfmt_response(
                ["Game.Shared.Network.LoadBalancer.StartSessionResponseArgs",
                 "Game.Shared.UID", "System.Boolean", "Game.Shared.SessionState",
                 "Game.Shared.UID", "System.String",
                 "System.Int32", "System.Int32",
                 "Game.Shared.SessionStateEncounterData", "System.Boolean"],
                [("RoutingPlayerId", "uid", player_uid),
                 ("Success", "bool", True),
                 ("SessionState", "struct", ("Game.Shared.SessionState", [
                     ("SessionId", "uid", sess_id),
                     ("SessionName", "string", session_name),
                     ("MinimumPlayerCount", "int", 2),
                     ("MaximumPlayerCount", "int", 2),
                     ("EncounterData", "class", "Game.Shared.SessionStateEncounterData"),
                     ("JoinInsteadOfReconnect", "bool", False),
                 ]))]
            )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceLoadBalancer.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent StartSession sid={sess_id} ({len(dw_bytes)}b)")

        # ServiceLoadBalancer — StartEncounter (22017)
        elif data_type == 22017:
            log_req(">>> Respond StartEncounter -> creating session")
            player_uid = make_uid(UID_TYPE["ServicePlayer"], int(self.client_reck_id))
            session_name = inner_obj.get("SessionName", "tutorial")
            encounter_data_raw = inner_obj.get("EncounterData", {})
            session = self._application.execute(StartEncounterCommand(
                session_name=session_name,
                encounter_data=encounter_data_raw,
                player_uid=player_uid,
            )).value
            sess_id = session.session_id
            server_id = session.server_id

            # Preserve the encounter scene in the SessionState returned by
            # StartEncounter.  The campaign notification already carries it,
            # but the fixed client replaces its battle context with this
            # response and otherwise receives a null EncounterData object;
            # that makes UIBattle fall back to an invalid (zero) EncounterDeck
            # and can leave the scene on the starfield.
            response_encounter_data = ("class", "Game.Shared.SessionStateEncounterData")
            if str(session_name).startswith("camp_"):
                scene_guid = campaign._last_encounter_scene.get(str(session_name))
                if scene_guid:
                    response_encounter_data = ("struct", (
                        "Game.Shared.SessionStateEncounterData", [
                            ("SceneTemplateId", "struct", (
                                "Game.Shared.ResourceId", [
                                    ("m_Guid", "guid", str(scene_guid))])),
                            ("SessionFlags", "enum1", (
                                "Game.Shared.ESessionFlags", 5)),
                        ]))

            resp_inner = encode_objfmt_response(
                ["Game.Shared.Network.LoadBalancer.StartEncounterResponseArgs",
                 "Game.Shared.UID", "System.Boolean", "Game.Shared.SessionState",
                 "Game.Shared.UID", "System.String",
                 "System.Int32", "System.Int32",
                     "Game.Shared.SessionStateEncounterData", "System.Boolean",
                     "Game.Shared.ResourceId", "System.Guid", "Game.Shared.ESessionFlags", "System.Int32",
                 "Game.Shared.UID"],
                [("RoutingPlayerId", "uid", player_uid),
                 ("Success", "bool", True),
                 ("SessionState", "struct", ("Game.Shared.SessionState", [
                     ("SessionId", "uid", sess_id),
                     ("SessionName", "string", session_name),
                     ("MinimumPlayerCount", "int", 2),
                     ("MaximumPlayerCount", "int", 2),
                     ("EncounterData", response_encounter_data[0], response_encounter_data[1]),
                     ("JoinInsteadOfReconnect", "bool", False),
                 ])),
                 ("SessionID", "uid", sess_id),
                 ("ServerID", "uid", server_id)]
            )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceLoadBalancer.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str,
                "target": target,
                "instance": instance,
                "reqid": resp_reqid,
                "c": comp,
                "conh": conh,
                "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent StartEncounter session={session_name} sid={session_id} ({len(dw_bytes)}b)")

        # ServiceLoadBalancer — JoinSession (22021)
        elif data_type == 22021:
            log_req(">>> JoinSession (dt=22021)")
            player_uid = make_uid(UID_TYPE["ServicePlayer"], int(self.client_reck_id))
            # Extract SessionId UID from raw inner bytes
            session_id_val = 0
            if isinstance(inner_bytes, bytes) and b"SessionId" in inner_bytes:
                pos = inner_bytes.find(b"SessionId")
                if pos >= 0:
                    rest = inner_bytes[pos:]
                    try:
                        # Skip to m_UID64, then 4 fields to hex value
                        idx = rest.find(b"m_UID64")
                        if idx >= 0:
                            rest2 = rest[idx + 7:]  # skip "m_UID64"
                            parts = rest2.split(b";", 5)
                            # parts: [sub_idx, sub_type, nprops, hex_value, ...]
                            if len(parts) >= 5:
                                hex_val = parts[4].decode("ascii")
                                session_id_val = struct.unpack("<Q", unhexlify(hex_val))[0]
                    except:
                        pass
            session = (self._application.execute(JoinSessionCommand(
                session_id=session_id_val,
                player_uid=player_uid,
            )).value if session_id_val else None)
            if session:
                resp_inner = encode_objfmt_response(
                    ["Game.Shared.Network.LoadBalancer.JoinSessionResponseArgs",
                     "Game.Shared.UID", "System.Boolean", "Game.Shared.SessionState",
                     "System.Collections.Generic.List`1#Game.Shared.PlayerState",
                     "Game.Shared.UID", "System.String",
                     "System.Int32", "System.Int32",
                     "Game.Shared.SessionStateEncounterData", "System.Boolean"],
                    [("RoutingPlayerId", "uid", player_uid),
                     ("Success", "bool", True),
                     ("SessionState", "struct", ("Game.Shared.SessionState", [
                         ("SessionId", "uid", session.session_id),
                         ("SessionName", "string", session.session_name),
                         ("MinimumPlayerCount", "int", 2),
                         ("MaximumPlayerCount", "int", 2),
                         ("EncounterData", "class", "Game.Shared.SessionStateEncounterData"),
                         ("JoinInsteadOfReconnect", "bool", False),
                     ])),
                     ("SessionPlayers", "coll", ("System.Collections.Generic.List`1#Game.Shared.PlayerState",
                         0, []))]
                )
                log_req(f"    JoinSession success, {len(session.players)} players")
            else:
                resp_inner = encode_objfmt_response(
                    ["Game.Shared.Network.LoadBalancer.JoinSessionResponseArgs",
                     "Game.Shared.UID", "System.Boolean"],
                    [("RoutingPlayerId", "uid", player_uid),
                     ("Success", "bool", False)]
                )
                log_req(f"    JoinSession failed: session not found")
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceLoadBalancer.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent JoinSession response ({len(dw_bytes)}b)")

        # ServiceLoadBalancer — ReadyForGameEvents (22029)
        elif data_type == 22029:
            log_req(">>> ReadyForGameEvents (dt=22029)")
            player_uid = make_uid(UID_TYPE["ServicePlayer"], int(self.client_reck_id))
            session = game_session.find_session_by_player(player_uid)
            # Tournament sessions
            if session and (session.session_name or "").startswith("tourney-"):
                from services.tournament_game import handle_ready_for_game_events
                handle_ready_for_game_events(self, session, _pvp_events_ready)

        # ServiceLoadBalancer — ReadyForGameSetup (22027)
        elif data_type == 22027:
            log_req(">>> ReadyForGameSetup (dt=22027)")
            player_uid = make_uid(UID_TYPE["ServicePlayer"], int(self.client_reck_id))
            ai_uid = make_uid(3, 1000)
            session = self._application.execute(SetSessionStateCommand(
                player_uid=player_uid,
                state="setup",
            )).value
            if session:
                resp_inner = encode_objfmt_response(
                    ["Game.Client.Network.LoadBalancer.ReadyForGameSetupResponse",
                     "Game.Shared.SessionState", "Game.Shared.UID",
                     "Game.Shared.ResourceId", "System.Guid",
                     "System.Collections.Generic.List`1#Game.Shared.PlayerState",
                     "System.Collections.Generic.List`1#Game.Shared.UID",
                     "System.UInt64", "System.Int32",
                     "Game.Shared.Network.LoadBalancer.EReadyForGameSetupError",
                     "System.String"],
                    [("SessionState", "struct", ("Game.Shared.SessionState", [
                         ("SessionId", "uid", session.session_id),
                         ("SessionName", "string", session.session_name),
                         ("MinimumPlayerCount", "int", 1),
                         ("MaximumPlayerCount", "int", 2),
                         ("EncounterData", "class", "Game.Shared.SessionStateEncounterData"),
                         ("JoinInsteadOfReconnect", "bool", False)])),
                     ("DeckId", "uid", player_uid),
                     ("DeckTemplateId", "struct", ("Game.Shared.ResourceId", [
                         ("guid", "guid", "00000000-0000-0000-0000-000000000000")])),
                     ("OpponentsInfo", "coll", ("System.Collections.Generic.List`1#Game.Shared.PlayerState", 0, [])),
                     ("TurnOrder", "coll", ("System.Collections.Generic.List`1#Game.Shared.UID", 0, [])),
                     ("seedZ", "ulong", 22222),
                     ("seedW", "ulong", 11111),
                     ("Error", "enum1", ("Game.Shared.Network.LoadBalancer.EReadyForGameSetupError", 0)),
                     ("ErrorMessage", "string", "")]
                )
            else:
                resp_inner = encode_objfmt_response(
                    ["Game.Client.Network.LoadBalancer.ReadyForGameSetupResponse",
                     "Game.Shared.Network.LoadBalancer.EReadyForGameSetupError",
                     "System.Int32", "System.String"],
                    [("Error", "enum1", ("Game.Shared.Network.LoadBalancer.EReadyForGameSetupError", 2)),
                     ("ErrorMessage", "string", "No session")]
                )
            # Tournament PvP: OpponentsInfo + PvP ready → delegated to reloadable module
            if session:
                from services.tournament_game import handle_ready_for_game_setup
                new_resp, game_started = handle_ready_for_game_setup(self, session, _pvp_ready, player_handlers)
                if new_resp is not None:
                    resp_inner = new_resp
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceLoadBalancer.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent ReadyForGameSetup response ({len(dw_bytes)}b)")

        # ServiceLoadBalancer — ReadyToStartGame (22031)
        elif data_type == 22031:
            player_uid = make_uid(UID_TYPE["ServicePlayer"], int(self.client_reck_id))
            log_req(f"    Player UID: reck={self.client_reck_id}, uid={player_uid:#x}")
            session = game_session.find_session_by_player(player_uid)
            if not session:
                log_req("    No session found for tutorial game!")
                return

            # A client reconnect can reach the game setup before re-sending
            # auth. Campaign ownership is persisted, so restore the profile
            # instead of allowing setup to crash on self.user_profile["id"].
            if not self.user_profile and (session.session_name or "").startswith("camp_"):
                try:
                    camp_id = int(session.session_name.split("_", 1)[1])
                except (TypeError, ValueError, IndexError):
                    camp_id = 0
                if camp_id:
                    profile_row = _db.execute(
                        "SELECT user_id FROM campaigns WHERE id=?",
                        (camp_id,)).fetchone()
                    if profile_row:
                        self.user_profile = db_get_user(profile_row[0])
                        if self.user_profile:
                            self._set_client_identity_from_profile()
                            log_req(
                                f"    Restored campaign profile: "
                                f"user={self.user_profile['id']}")

            # PvP/tournament sessions: skip PvE encounter setup
            if session.session_name and (session.session_name.startswith("pvp-") or session.session_name.startswith("tourney-")):
                log_req("    PvP/tourney session — ReadyToStartGame ack only")
                rts_resp = encode_objfmt_response(
                    ["Game.Client.Network.LoadBalancer.ReadyToStartGameResponse",
                     "System.Boolean"],
                    [("IsReady", "bool", True)]
                )
                rts_body = compress_gzip(rts_resp) if comp else rts_resp
                rts_dw = encode_datawrapper(reqid | 1, 22031, rts_body, comp and 1 or 0, "00000000-0000-0000-0000-000000000000")
                self.send({
                    "issuer": f"0.0.0.0.ServiceLoadBalancer.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid | 1}",
                    "target": target, "instance": instance,
                    "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid,
                }, rts_dw)
                return
            gs_instance = str(session.server_id)

            # Establish the coin-toss winner before any setup response exposes
            # the turn order. The tutorial keeps its historical deterministic
            # player-first behavior; FRA and completed tutorial battles toss
            # normally. If the AI wins, it makes the subsequent Play/Draw
            # choice automatically.
            is_campaign_session = (session.session_name or "").startswith("camp_")
            is_tutorial_session = False
            self._pve_player_cannot_choose_play_first = False
            if is_campaign_session:
                try:
                    camp_id = int((session.session_name or "camp_0").split("_")[-1] or 0)
                except (TypeError, ValueError):
                    camp_id = 0
                camp_row = _db.execute(
                    "SELECT state_json FROM campaigns WHERE id=?",
                    (camp_id,)).fetchone() if camp_id else None
                if camp_row:
                    try:
                        is_tutorial_session = not bool(
                            json.loads(camp_row[0] or "{}").get("TutorialDone", False))
                    except (TypeError, ValueError):
                        is_tutorial_session = True
                if camp_id:
                    self._pve_player_cannot_choose_play_first = \
                        campaign.player_cannot_choose_play_first(_db, camp_id)
                    if self._pve_player_cannot_choose_play_first:
                        log_req("    Campaign talent restriction: player cannot choose Play first")
            coin_winner_is_player = (
                True if is_tutorial_session else bool(random.getrandbits(1)))
            self._pve_coin_winner_is_player = coin_winner_is_player

            # Respond to 22031
            rts_resp = encode_objfmt_response(
                ["Game.Client.Network.LoadBalancer.ReadyToStartGameResponse",
                 "System.Boolean"],
                [("IsReady", "bool", True)]
            )
            rts_body = compress_gzip(rts_resp) if comp else rts_resp
            rts_dw = encode_datawrapper(reqid | 1, 22031, rts_body, comp and 1 or 0, "00000000-0000-0000-0000-000000000000")
            self.send({
                "issuer": f"0.0.0.0.ServiceLoadBalancer.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid | 1}",
                "target": target, "instance": instance,
                "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid,
            }, rts_dw)
            log_req(f"    Sent ReadyToStartGame response ({len(rts_dw)}b)")

            # Push ReadyForGameSetup (22027) with Opponent + TurnOrder - manual encode
            rfg_types = ["Game.Client.Network.LoadBalancer.ReadyForGameSetupResponse",
                         "Game.Shared.UID", "Game.Shared.ResourceId", "System.Guid",
                         "Game.Shared.SessionState", "System.Int32", "System.String",
                         "System.Collections.Generic.List`1#Game.Shared.PlayerState",
                         "Game.Shared.PlayerState", "System.UInt64",
                         "System.Collections.Generic.List`1#Game.Shared.UID",
                         "Game.Shared.Network.LoadBalancer.EReadyForGameSetupError"]
            def rfg_ft(t):
                if t not in rfg_types: rfg_types.append(t)
                return rfg_types.index(t)
            rfg_buf = io.BytesIO(); rfg_sz = []; rfg_w = lambda s: rfg_buf.write(s.encode("utf-8"))
            rfg_sp = lambda: rfg_buf.write(b";")
            rfg_sz.append(0)
            rfg_w(""); rfg_sp(); rfg_w("0"); rfg_sp(); rfg_w(str(rfg_ft(rfg_types[0]))); rfg_sp(); rfg_w("9"); rfg_sp()
            # Fields in alphabetical order: DeckId, DeckTemplateId, OpponentsInfo, SessionState, TurnOrder, seedW, seedZ, Error, ErrorMessage
            pl_uid_val = (int(self.client_reck_id) << 8) | 244  # ServicePlayer type
            ai_uid_val = (1000 << 8) | 3  # AIPlayer
            
            # DeckId (UID.Invalid — client keeps its selected deck)
            fd = rfg_buf.tell(); rfg_sz.append(0)
            rfg_w("DeckId"); rfg_sp(); rfg_w(str(len(rfg_sz)-1)); rfg_sp()
            rfg_w(str(rfg_ft("Game.Shared.UID"))); rfg_sp(); rfg_w("1"); rfg_sp()
            fd2 = rfg_buf.tell(); rfg_sz.append(0)
            rfg_w("m_UID64"); rfg_sp(); rfg_w(str(len(rfg_sz)-1)); rfg_sp(); rfg_w(str(rfg_ft("System.UInt64"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w(hexlify(struct.pack("<Q", pl_uid_val)).decode("ascii")); rfg_sp()
            rfg_sz[-1] = rfg_buf.tell()-fd2; rfg_sz[-2] = rfg_buf.tell()-fd
            
            # DeckTemplateId (ResourceId)
            fdt = rfg_buf.tell(); rfg_sz.append(0)
            rfg_w("DeckTemplateId"); rfg_sp(); rfg_w(str(len(rfg_sz)-1)); rfg_sp()
            rfg_w(str(rfg_ft("Game.Shared.ResourceId"))); rfg_sp(); rfg_w("1"); rfg_sp()
            fdt2 = rfg_buf.tell(); rfg_sz.append(0)
            rfg_w("m_Guid"); rfg_sp(); rfg_w(str(len(rfg_sz)-1)); rfg_sp(); rfg_w(str(rfg_ft("System.Guid"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w("36"); rfg_sp(); rfg_buf.write(b"00000000-0000-0000-0000-000000000000")
            rfg_sz[-1] = rfg_buf.tell()-fdt2; rfg_sz[-2] = rfg_buf.tell()-fdt
            
            # OpponentsInfo (List<PlayerState> with 1 AI opponent)
            foi = rfg_buf.tell(); rfg_sz.append(0); foi_idx = len(rfg_sz)-1
            rfg_w("OpponentsInfo"); rfg_sp(); rfg_w(str(foi_idx)); rfg_sp()
            rfg_w(str(rfg_ft("System.Collections.Generic.List`1#Game.Shared.PlayerState"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w("1"); rfg_sp()  # count=1
            # PlayerState element: PlayerId(UID), PlayerPosition(int), PlayerName(string)
            fps = rfg_buf.tell(); rfg_sz.append(0); fps_idx = len(rfg_sz)-1
            rfg_w("0"); rfg_sp(); rfg_w(str(fps_idx)); rfg_sp(); rfg_w(str(rfg_ft("Game.Shared.PlayerState"))); rfg_sp(); rfg_w("3"); rfg_sp()
            # PlayerId as UID
            fpi = rfg_buf.tell(); rfg_sz.append(0)
            rfg_w("PlayerId"); rfg_sp(); rfg_w(str(len(rfg_sz)-1)); rfg_sp()
            rfg_w(str(rfg_ft("Game.Shared.UID"))); rfg_sp(); rfg_w("1"); rfg_sp()
            fpi2 = rfg_buf.tell(); rfg_sz.append(0)
            rfg_w("m_UID64"); rfg_sp(); rfg_w(str(len(rfg_sz)-1)); rfg_sp(); rfg_w(str(rfg_ft("System.UInt64"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w(hexlify(struct.pack("<Q", ai_uid_val)).decode("ascii")); rfg_sp()
            rfg_sz[-1] = rfg_buf.tell()-fpi2; rfg_sz[-2] = rfg_buf.tell()-fpi
            # PlayerPosition (int)
            fpp = rfg_buf.tell(); rfg_sz.append(0)
            rfg_w("PlayerPosition"); rfg_sp(); rfg_w(str(len(rfg_sz)-1)); rfg_sp()
            rfg_w(str(rfg_ft("System.Int32"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w(hexlify(struct.pack("<i", 1)).decode("ascii")); rfg_sp()
            rfg_sz[-1] = rfg_buf.tell()-fpp
            # PlayerName (string)
            fpn = rfg_buf.tell(); rfg_sz.append(0)
            rfg_w("PlayerName"); rfg_sp(); rfg_w(str(len(rfg_sz)-1)); rfg_sp()
            rfg_w(str(rfg_ft("System.String"))); rfg_sp(); rfg_w("0"); rfg_sp()
            enc_name = b"AI Opponent"; rfg_w(str(len(enc_name))); rfg_sp(); rfg_buf.write(enc_name)
            rfg_sz[-1] = rfg_buf.tell()-fpn
            rfg_sz[fps_idx] = rfg_buf.tell()-fps
            rfg_sz[foi_idx] = rfg_buf.tell()-foi
            
            # SessionState
            fss = rfg_buf.tell(); rfg_sz.append(0); fss_idx = len(rfg_sz)-1
            rfg_w("SessionState"); rfg_sp(); rfg_w(str(fss_idx)); rfg_sp()
            rfg_w(str(rfg_ft("Game.Shared.SessionState"))); rfg_sp(); rfg_w("6"); rfg_sp()
            # EncounterData (class),
            rfg_w("EncounterData"); rfg_sp(); rfg_w(str(len(rfg_sz))); rfg_sp()
            rfg_w(str(rfg_ft("Game.Shared.SessionStateEncounterData"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_sz.append(0)
            # JoinInsteadOfReconnect
            rfg_w("JoinInsteadOfReconnect"); rfg_sp(); rfg_w(str(len(rfg_sz))); rfg_sp()
            rfg_w(str(rfg_ft("System.Boolean"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w("0"); rfg_sz.append(0)
            # MaxPlayerCount
            rfg_w("MaximumPlayerCount"); rfg_sp(); rfg_w(str(len(rfg_sz))); rfg_sp()
            rfg_w(str(rfg_ft("System.Int32"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w(hexlify(struct.pack("<i", 2)).decode("ascii")); rfg_sp()
            rfg_sz.append(0)
            # MinPlayerCount
            rfg_w("MinimumPlayerCount"); rfg_sp(); rfg_w(str(len(rfg_sz))); rfg_sp()
            rfg_w(str(rfg_ft("System.Int32"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w(hexlify(struct.pack("<i", 1)).decode("ascii")); rfg_sp()
            rfg_sz.append(0)
            # SessionId
            rfg_w("SessionId"); rfg_sp(); rfg_w(str(len(rfg_sz))); rfg_sp()
            rfg_w(str(rfg_ft("Game.Shared.UID"))); rfg_sp(); rfg_w("1"); rfg_sp()
            rfg_w("m_UID64"); rfg_sp(); rfg_w(str(len(rfg_sz))); rfg_sp()
            rfg_w(str(rfg_ft("System.UInt64"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w(hexlify(struct.pack("<Q", session.session_id)).decode("ascii")); rfg_sp()
            rfg_sz.append(0); rfg_sz.append(0)
            # SessionName
            rfg_w("SessionName"); rfg_sp(); rfg_w(str(len(rfg_sz))); rfg_sp()
            rfg_w(str(rfg_ft("System.String"))); rfg_sp(); rfg_w("0"); rfg_sp()
            enc_sname = session.session_name.encode("utf-8")
            rfg_w(str(len(enc_sname))); rfg_sp(); rfg_buf.write(enc_sname)
            rfg_sz.append(0)
            rfg_sz[fss_idx] = rfg_buf.tell()-fss
            
            # TurnOrder (List<UID> with 2 players)
            fto = rfg_buf.tell(); rfg_sz.append(0); fto_idx = len(rfg_sz)-1
            rfg_w("TurnOrder"); rfg_sp(); rfg_w(str(fto_idx)); rfg_sp()
            rfg_w(str(rfg_ft("System.Collections.Generic.List`1#Game.Shared.UID"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w("2"); rfg_sp()  # count=2
            turn_order_uids = ([pl_uid_val, ai_uid_val]
                               if coin_winner_is_player
                               else [ai_uid_val, pl_uid_val])
            for ti, t_uid_val in enumerate(turn_order_uids):
                ft = rfg_buf.tell(); rfg_sz.append(0); ft_idx = len(rfg_sz)-1
                rfg_w(str(ti)); rfg_sp(); rfg_w(str(ft_idx)); rfg_sp()
                rfg_w(str(rfg_ft("Game.Shared.UID"))); rfg_sp(); rfg_w("1"); rfg_sp()
                ft2 = rfg_buf.tell(); rfg_sz.append(0)
                rfg_w("m_UID64"); rfg_sp(); rfg_w(str(len(rfg_sz)-1)); rfg_sp()
                rfg_w(str(rfg_ft("System.UInt64"))); rfg_sp(); rfg_w("0"); rfg_sp()
                rfg_w(hexlify(struct.pack("<Q", t_uid_val)).decode("ascii")); rfg_sp()
                rfg_sz[-1] = rfg_buf.tell()-ft2; rfg_sz[ft_idx] = rfg_buf.tell()-ft
            rfg_sz[fto_idx] = rfg_buf.tell()-fto
            
            # seedW
            rfg_w("seedW"); rfg_sp(); rfg_w(str(len(rfg_sz))); rfg_sp()
            rfg_w(str(rfg_ft("System.UInt64"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w(hexlify(struct.pack("<Q", 0x12345678)).decode("ascii")); rfg_sp()
            rfg_sz.append(0)
            # seedZ
            rfg_w("seedZ"); rfg_sp(); rfg_w(str(len(rfg_sz))); rfg_sp()
            rfg_w(str(rfg_ft("System.UInt64"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w(hexlify(struct.pack("<Q", 0xABCDEF01)).decode("ascii")); rfg_sp()
            rfg_sz.append(0)
            # Error
            rfg_w("Error"); rfg_sp(); rfg_w(str(len(rfg_sz))); rfg_sp()
            rfg_w(str(rfg_ft("Game.Shared.Network.LoadBalancer.EReadyForGameSetupError"))); rfg_sp(); rfg_w("1"); rfg_sp()
            rfg_w("value__"); rfg_sp(); rfg_w(str(len(rfg_sz))); rfg_sp()
            rfg_w(str(rfg_ft("System.Int32"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w(hexlify(struct.pack("<i", 0)).decode("ascii")); rfg_sp()
            rfg_sz.append(0); rfg_sz.append(0)
            # ErrorMessage
            rfg_w("ErrorMessage"); rfg_sp(); rfg_w(str(len(rfg_sz))); rfg_sp()
            rfg_w(str(rfg_ft("System.String"))); rfg_sp(); rfg_w("0"); rfg_sp()
            rfg_w("0"); rfg_sp()
            rfg_sz.append(0)
            
            rfg_sz[0] = rfg_buf.tell()
            rfg_w(";".join(rfg_types)); rfg_buf.write(b"\n")
            for i, s in enumerate(rfg_sz):
                if i > 0: rfg_sp()
                rfg_w(str(s))
            rfg_resp = rfg_buf.getvalue()
            rfg_body = compress_gzip(rfg_resp) if comp else rfg_resp
            rfg_dw = encode_datawrapper(0, 22027, rfg_body, comp and 1 or 0, "00000000-0000-0000-0000-000000000000")
            self.scnt += 1
            self.send({
                "issuer": f"0.0.0.0.ServiceLoadBalancer.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{self.scnt}",
                "target": "ServiceLoadBalancer", "instance": "Shared",
                "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
            }, rfg_dw)
            log_req(f"    Pushed ReadyForGameSetup (22027, {len(rfg_dw)}b)")

            # GameStarted must follow ReadyForGameSetup. The client initializes
            # its session/player/card state from 22027 before accepting the
            # 3053 event; sending these in the opposite order causes the
            # battle scene to stall after joining the encounter.
            gs_inner = encode_objfmt_response(
                ["Game.Shared.Network.GameSession.GameStartedEventArgs",
                 "Game.Shared.UID", "System.UInt64", "System.Boolean",
                 "System.Collections.Generic.List`1#Game.Shared.UID"],
                [("RoutingPlayerId", "uid", player_uid),
                 ("SeedW", "ulong", 0xABCDEF0102030405),
                 ("SeedZ", "ulong", 0x1234567890ABCDEF),
                 ("Success", "bool", True),
                 ("TurnOrderPlayerIds", "uidlist", ("System.Collections.Generic.List`1#Game.Shared.UID", 2,
                                                    [int(player_uid), game_engine.UID.make(3, 1000).uid64]))]
            )
            gs_body = compress_gzip(gs_inner)
            gs_dw = encode_datawrapper(0, 3053, gs_body, 1, "00000000-0000-0000-0000-000000000000")
            self.scnt += 1
            self.send({
                "issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}",
                "target": "ServiceGameSession", "instance": gs_instance,
                "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
            }, gs_dw)
            log_req(f"    Pushed GameStarted (3053, {len(gs_dw)}b)")

            # Push 3055 game events AFTER 22027
            import struct as _struct
            pl_uid_t = game_engine.UID.make(244, int(self.client_reck_id))
            ai_uid_t = game_engine.UID.make(3, 1000)
            pl_uid_val = (int(self.client_reck_id) << 8) | 244
            ai_uid_val = (1000 << 8) | 3
            sess_tutorial = game_engine.UID(session.session_id)
            player_name = self.user_profile["name"] if self.user_profile else "TestPlayer"
            log_req(f"    SESS: raw={session.session_id:#x}, tut={sess_tutorial.uid64:#x}")

            # Game mode: campaign (training/dungeon), Practice mirror, or FRA.
            # Campaign sessions are named "camp_<CampID>" by the campaign
            # 'start' handler, so we can distinguish them thread-safely from
            # the DB-backed session row.
            game = game_engine.Game(sess_tutorial.uid64, pl_uid_t, ai_uid_t)
            self._scene_global_ability_guids = []
            self._scene_targeted_ability_guids = []
            self._scene_targeted_ability_owners = {}
            self._champion_granted_ability_guids = {}
            self._campaign_fortune_guid = None
            self._campaign_fortune_ability_guid = None
            self._campaign_fortune_starting_hand_bonus = 0
            player_champ_id = game._new_card_id()
            ai_champ_id = game._new_card_id()
            player_talents_json = "[]"

            is_campaign = (session.session_name or "").startswith("camp_")
            is_practice = (session.session_name or "").startswith("Session-")
            mode_name = ("CAMPAIGN" if is_campaign else
                         "PRACTICE" if is_practice else "FRA")
            log_req(f"    Game mode: {mode_name} (session={session.session_name})")

            if is_campaign:
                camp_id = int((session.session_name or "camp_0").split("_")[-1] or 0)
                cfg = campaign.resolve_battle_config(
                    self, _db, camp_id, session.session_name)
                scene_guid = cfg["scene_guid"]
                ai_deck_guid = cfg["ai_deck_guid"]
                ai_champ_guid = cfg["ai_champ_guid"]
                ai_name = cfg["ai_name"]
                ai_charge_power = cfg["ai_charge_power"]
                ai_personality = cfg["ai_personality"]
                ai_deck_personality = cfg.get("ai_deck_personality")
                deck_db_id = cfg["deck_db_id"]
                player_champ_name = cfg["player_champ_name"]
                race_num, cls_num, gnd_num = (cfg["race_num"], cfg["cls_num"],
                                               cfg["gnd_num"])
                race_name, cls_name, gnd_name = (cfg["race_name"], cfg["cls_name"],
                                                 cfg["gnd_name"])
                player_talents_json = cfg["player_talents_json"]
                player_starting_health = cfg["player_starting_health"]
                is_tutorial = cfg["is_tutorial"]
                player_champ_guid = cfg["player_champ_guid"]
                self._campaign_fortune_guid = cfg.get("fortune_guid")
                self._campaign_fortune_ability_guid = cfg.get(
                    "fortune_ability_guid")
                self._campaign_fortune_starting_hand_bonus = int(
                    cfg.get("fortune_starting_hand_bonus", 0) or 0)
                log_req(f"    Campaign battle: player_deck={deck_db_id} player={player_champ_name} ai={ai_name} ai_deck={ai_deck_guid} scene={scene_guid} player_champ={player_champ_guid}")
            else:
                # Get player champion from the selected deck. Practice uses
                # this same deck as the AI's mirror; FRA uses it only as the
                # player's deck and obtains an authored challenger below.
                arena = db_get_arena_state(self.user_profile["id"])
                deck_db_id = self._resolve_fra_deck_id(arena["deck_id"])
                player_champ_guid = db_get_player_champion_guid(deck_db_id) or "1d462ffb-0744-4996-804c-ba61b2c5c2f1"
                if _db.execute(
                        "SELECT 1 FROM champion_template_data WHERE guid=?",
                        (player_champ_guid,)).fetchone():
                    player_starting_health = self._champion_health_by_guid(player_champ_guid)
                else:
                    ext = _db.execute(
                        "SELECT starting_health FROM champion_templates_extended "
                        "WHERE guid=?", (player_champ_guid,)).fetchone()
                    player_starting_health = ext[0] if ext else 20
                # FRA uses the selected PvP deck rather than a campaign row,
                # but the champion record still owns the selected talent list.
                # Carry it into the same PreGame path used by campaign games.
                _talent_row = _db.execute(
                    "SELECT talents FROM champions WHERE user_id=? "
                    "AND last_deck_id=? AND is_deleted=0 "
                    "ORDER BY id LIMIT 1",
                    (self.user_profile["id"], deck_db_id)).fetchone()
                if _talent_row and _talent_row[0]:
                    player_talents_json = _talent_row[0]
                log_req(f"    {mode_name} battle: deck={deck_db_id} "
                        f"(arena={arena['deck_id']}) champ={player_champ_guid} "
                        f"health={player_starting_health}")

                if is_practice:
                    # Practice is a mirror match, not an FRA encounter. Keep
                    # the neutral AI champion presentation, but select its
                    # deck from the player's resolved deck below.
                    ai_champ_guid = "f8f86969-2e47-4901-8c9e-7fbf8d859e22"
                    ai_name = "AI Opponent"
                    ai_deck_guid = None
                else:
                    # Get AI champion from the current FRA challenger.
                    challengers = db_get_fra_challengers(self.user_profile["id"])
                    cidx = arena["challenger_index"]
                    if cidx < len(challengers):
                        ai_champ_guid = challengers[cidx]["champion_guid"]
                        ai_name = challengers[cidx]["name"]
                        ai_deck_guid = challengers[cidx]["deck"]
                    else:
                        ai_champ_guid = "f8f86969-2e47-4901-8c9e-7fbf8d859e22"
                        ai_name = "Angel of Dawn"
                        ai_deck_guid = None
                ai_charge_power = db_get_charge_power(ai_champ_guid)

                # FRA encounters currently have no authored campaign
                # attitude. An encounter scene may still provide a deck
                # strategy for the challenger's deck.
                ai_personality = None
                ai_deck_personality = None
                if ai_deck_guid:
                    _dp_row = _db.execute(
                        "SELECT ai_deck_personality FROM encounter_scenes "
                        "WHERE ai_deck_guid=? AND ai_deck_personality IS NOT NULL "
                        "LIMIT 1", (ai_deck_guid,)).fetchone()
                    if _dp_row:
                        ai_deck_personality = _dp_row[0]

            # Configure the client-style attitude and deck strategy once at
            # the battle boundary. Campaign attitude is the fallback when an
            # encounter deck has no explicit strategy.
            import ai as _ai
            _ai.configure_personality(
                self,
                deck_personality=ai_deck_personality,
                campaign_personality=ai_personality,
            )
            
            # Player champion abilities come from the champion's talents
            # (charge powers, spell powers, other passive abilities selected
            # during champion creation).  Each talent is a CardTemplate with
            # an m_CardAbilityId that points to the actual AbilityTemplate —
            # it's the ability GUID, not the talent GUID, that the client's
            # champion card expects in CardUpdated.abilities.
            pl_abilities = []
            try:
                import json as _tal_json
                talent_guids = _tal_json.loads(player_talents_json) if player_talents_json else []
                for tg in talent_guids:
                    # All abilities granted by this talent (one-to-many).
                    ab_rows = _db.execute(
                        "SELECT ability_guid FROM talent_abilities WHERE talent_guid=?",
                        (tg,)).fetchall()
                    if ab_rows:
                        pl_abilities.extend(game_engine.ResourceId.from_str(r[0]) for r in ab_rows)
                    else:
                        # Passive talent with no ability (e.g. Efficient) — skip.
                        pass
            except Exception:
                pass
            # The champion's signature charge power comes from gamedata
            # champion_abilities (e.g. Dimmid's "[DIAMOND][DIAMOND]: [BASIC] [2]
            # Target troop gets Lifedrain this turn") — include it even when it
            # isn't granted via a talent.
            from db import db_get_champion_ability_guids as _db_champ_ab
            for ag in _db_champ_ab(player_champ_guid):
                rid = game_engine.ResourceId.from_str(ag)
                if rid not in pl_abilities:
                    pl_abilities.append(rid)

            # Campaign class data supplies the base opening hand. Talent
            # modifiers are resolved from the extracted talent ability/BOM
            # metadata so dungeon-only effects do not leak into campaigns.
            self._campaign_starting_hand_size = 7
            self._campaign_max_hand_size = 7
            self._campaign_starting_hand_effects = []
            if is_campaign:
                hand_cfg = campaign.resolve_opening_hand_config(
                    _db, session, self.user_profile["id"], race_name, cls_name,
                    pl_abilities)
                self._campaign_starting_hand_size = max(
                    0, int(hand_cfg["starting_hand_size"]) +
                    self._campaign_fortune_starting_hand_bonus)
                self._campaign_max_hand_size = max(
                    0, int(hand_cfg["maximum_hand_size"]))
                self._campaign_starting_hand_effects = list(
                    hand_cfg["starting_hand_effects"])
                log_req(
                    f"    Campaign opening hand: {self._campaign_starting_hand_size}, "
                    f"maximum: {self._campaign_max_hand_size}, "
                    f"hand effects: {self._campaign_starting_hand_effects}")

            game.card_defs[player_champ_id] = game_engine.CardDef("Player",
                game_engine.ECardTypes.Champion, 0, player_starting_health, player_starting_health, [], pl_abilities)
            ai_starting_health = self._champion_health_by_guid(ai_champ_guid)
            from db import db_get_champion_ability_guids as _db_champ_ab_ai
            ai_abilities = [game_engine.ResourceId.from_str(g)
                            for g in _db_champ_ab_ai(ai_champ_guid)]
            game.card_defs[ai_champ_id] = game_engine.CardDef(ai_name,
                game_engine.ECardTypes.Champion, 0, ai_starting_health, ai_starting_health, [], ai_abilities)
            game.player_champion_card_id = player_champ_id
            self._player_champ_scid = player_champ_id
            self._ai_champ_scid = ai_champ_id
            self._player_champ_guid = player_champ_guid
            self._ai_champ_guid = ai_champ_guid
            self._player_champ_abilities = list(pl_abilities)  # ResourceId list
            self._ai_champ_ability_guids = [str(a.guid) for a in ai_abilities]
            # Persist starting health for the DB battle state (combat damage + reconnect).
            self._player_starting_health = player_starting_health
            self._ai_starting_health = ai_starting_health
            self._player_starting_charges = 0
            self._ai_starting_charges = 0
            game.ai_champion_card_id = ai_champ_id
            # Carry the real starting health into the Game so the initial
            # PlayerUpdated reports it (a fresh Game defaults to 20 — Iddi is 10).
            game.player_health = player_starting_health
            game.ai_health = ai_starting_health
            game.max_hand_size = self._max_hand_size(session)
            # GameStarted.ChampionNames drives the large pre-battle/coin-toss
            # banner in UIBattle.  Campaigns must identify the player's
            # champion there, not their account, just as ChampionCardPlayed
            # does for the in-battle HUD.
            if is_campaign:
                player_name = player_champ_name if player_champ_name else "Player"
            # Keep GameStarted's order identical to the setup response: the
            # first entry is the coin-toss winner, not necessarily the player
            # who eventually plays first.
            game.push_game_started(
                champion_names=[player_name, ai_name],
                champion_template_ids=[player_champ_guid, ai_champ_guid],
                player_first=coin_winner_is_player)
            # The AI does not receive a PickGoesFirst priority window. This
            # completes the client's coin-flip state before the automatic AI
            # Play/Draw result and Mulligan packet arrive.
            if not coin_winner_is_player:
                game.push_first_player_dictated(ai_uid_t)
            game.push_player_updated(pl_uid_t, champ_id=getattr(self, "_player_champ_scid", None))
            game.push_player_updated(ai_uid_t, champ_id=getattr(self, "_ai_champ_scid", None))
            game.push_card_updated(player_champ_id, pl_uid_t, game_engine.ECardCollections.None_,
                                   game_engine.ECardTypes.Champion, attack=0, defense=player_starting_health,
                                   template_id=player_champ_guid)
            game.push_card_updated(ai_champ_id, ai_uid_t, game_engine.ECardCollections.None_,
                                   game_engine.ECardTypes.Champion, attack=0, defense=ai_starting_health,
                                   template_id=ai_champ_guid)

            # HUD portrait name: use the champion's name (e.g. "Morgana") for
            # the player, and the trainer NPC name for the AI (e.g. "Iddi").
            # For campaign, player_champ_name was resolved via the campaign ->
            # champion -> deck chain; for FRA, look up via the deck's champion.
            if not is_campaign:
                player_name = self.user_profile["name"] if self.user_profile else "TestPlayer"
                if deck_db_id:
                    crow = _db.execute(
                        "SELECT ch.champion_name FROM decks d JOIN champions ch ON ch.id=d.pve_champion_id WHERE d.id=?",
                        (deck_db_id,)).fetchone()
                    if crow and crow[0]:
                        player_name = crow[0]
            log_req(f"    Battle HUD names: player={player_name} ai={ai_name}")
            game.push_champion_card_played(pl_uid_t, False, player_name, player_champ_id)
            game.push_champion_card_played(ai_uid_t, True, ai_name, ai_champ_id)
            
            # Populate game_cards from player's deck in DB
            import json as _json
            _db.execute("DELETE FROM game_cards WHERE session_id=?", (session.session_id,))
            deck_rows = None
            if deck_db_id and self.user_profile:
                deck_rows = _db.execute("SELECT cards FROM decks WHERE id=? AND user_id=?", (deck_db_id, self.user_profile["id"])).fetchone()
            player_deck_cards = []
            player_card_tpl_ids = []
            player_resolved_tpl_ids = []
            if deck_rows and deck_rows[0]:
                card_ids = _json.loads(deck_rows[0])
                random.shuffle(card_ids)
                for pos, card_ref in enumerate(card_ids):  # Push full deck
                    cid = game._new_card_id()
                    # card_ref is either a template GUID (campaign starter
                    # deck) or an instance id (FRA deck). Resolve to GUID.
                    if isinstance(card_ref, str) and "-" in card_ref:
                        card_tpl_id = card_ref
                    else:
                        card_tpl_id = card_ref
                    player_card_tpl_ids.append(card_tpl_id)
                    card_uid = cid.uid.to_uint64()
                    _tpl, ctype, _n, _c, _a, _d = self._resolve_card_ref(card_tpl_id)
                    player_resolved_tpl_ids.append(_tpl)
                    _db.execute("INSERT INTO game_cards (user_id, session_id, card_uid, card_template_id, card_type, template_guid, location, position, owner_user_id, original_template_guid) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (self.user_profile["id"], session.session_id, card_uid, card_tpl_id, ctype or "Unknown", _tpl, 'deck', pos, self.user_profile["id"], _tpl))
                    # Populate per-instance ability/attribute data from the template
                    # (the canonical source) so hand/warzone abilities + icons work.
                    self._sync_instance_card_data(
                        session, card_uid, _tpl, commit=False)
                    player_deck_cards.append(cid)
            _db.commit()
            # Derive socketed-gem abilities from the current ActiveGems map.
            # decks.gem_abilities is only a cache and may describe a previous
            # socket (for example Rage left behind after changing to Speed).
            import json as _gemj
            deck_gem_abilities = {}
            if deck_db_id:
                _ga_row = _db.execute(
                    "SELECT active_gems FROM decks WHERE id=?",
                    (deck_db_id,)).fetchone()
                if _ga_row and _ga_row[0]:
                    try:
                        deck_gem_abilities = self._resolve_gem_abilities(
                            _gemj.loads(_ga_row[0]) or {})
                    except Exception:
                        deck_gem_abilities = {}
            for i, cid in enumerate(player_deck_cards):
                # No opening hands yet — like PvP, both decks stay face-down
                # through PreGame/PickGoesFirst; each player draws 7 only after
                # the Play/Draw pick (see the 3029 ChoosePlay/ChooseDraw
                # handler).
                col = game_engine.ECardCollections.Deck
                instance_id = player_card_tpl_ids[i]
                # Look up card template GUID and type. The campaign starter
                # deck stores template GUIDs directly; FRA decks store
                # instance ids (resolve via card_instances).
                tpl_guid = "00000000-0000-0000-0000-000000000000"
                ct = game_engine.ECardTypes.Troop
                ctype_name = "Troop"
                cost, atk, def_ = 0, 0, 0
                shards = []
                row = None
                if isinstance(instance_id, str) and "-" in instance_id:
                    from db import db_get_card_type
                    r = _db.execute(
                        "SELECT ct.card_type, ct.name, ct.cost, ct.attack, ct.defense "
                        "FROM card_templates ct WHERE ct.guid=?",
                        (instance_id,)).fetchone()
                    if r:
                        # Normalize to the 6-col shape used below.
                        row = (instance_id, r[0], r[1], r[2], r[3], r[4])
                        tpl_guid = instance_id
                else:
                    row = _db.execute(
                        "SELECT ci.template_guid, ct.card_type, ct.name, ct.cost, ct.attack, ct.defense "
                        "FROM card_instances ci JOIN card_templates ct ON ci.template_guid=ct.guid WHERE ci.instance_id=?",
                         (instance_id,)).fetchone()
                if row:
                    tpl_guid = row[0]
                    ctype_name = row[1]
                    ct = game_engine.card_type_from_db(ctype_name)
                    cost, atk, def_ = row[3] or 0, row[4] or 0, row[5] or 0
                    # Fetch threshold data
                    import json as _json2
                    shards = []
                    from db import db_card_template_field
                    trow_val = db_card_template_field(tpl_guid, "threshold_json")
                    if trow_val:
                        try:
                            td = _json2.loads(trow_val)
                            shard_flags = {0:0, 1:4, 2:8, 3:16, 4:32, 5:64}
                            raw_list = td.get('list', [])
                            shards = [shard_flags.get(s, s) for s in raw_list]
                        except: pass
                    # Fetch abilities
                    abilities = []
                    ab_guids = []
                    ab_json_val = db_card_template_field(tpl_guid, "abilities_json")
                    if ab_json_val:
                        try:
                            ab_guids = _json2.loads(ab_json_val)
                            abilities = [game_engine.ResourceId.from_str(g) for g in ab_guids]
                        except: pass
                    # Gem-granted abilities (baked into the deck at save time)
                    # join the card's ability list from the very first push.
                    for _gem_ag in (deck_gem_abilities.get(str(instance_id)) or []):
                        if str(_gem_ag).lower() not in [str(g).lower() for g in ab_guids]:
                            ab_guids.append(str(_gem_ag).lower())
                    abilities = [game_engine.ResourceId.from_str(g) for g in ab_guids]
                    _db.execute(
                        "UPDATE game_cards SET card_abilities=? "
                        "WHERE session_id=? AND card_uid=?",
                        (_json2.dumps([str(g).lower() for g in ab_guids]),
                         session.session_id, cid.uid.to_uint64()))
                    # Fetch static attributes (e.g. Flight) so the initial-hand
                    # CardUpdated renders the attribute icons immediately.
                    attributes = game_engine.ECardAttributes.Unknown
                    attrs_val = db_card_template_field(tpl_guid, "attributes")
                    if attrs_val:
                        try:
                            attributes = int(attrs_val)
                        except: pass
                else:
                    # Missing instance — insert a placeholder instance
                    fallback = _db.execute("SELECT guid FROM card_templates LIMIT 1").fetchone()
                    if fallback:
                        tpl_guid = fallback[0]
                        _db.execute("INSERT OR IGNORE INTO card_instances (instance_id, template_guid) VALUES (?,?)",
                                   (instance_id, tpl_guid))
                        _db.commit()
                # Effective attributes = static template attributes + any
                # granted by the card's own abilities (passives), so attribute
                # icons show in hand from the first CardUpdated.
                cdef_attrs = game_engine.ECardAttributes.Unknown
                if row:
                    cdef_attrs = attributes | self._granted_attributes([a.lower() for a in ab_guids])
                game.card_defs[cid] = game_engine.CardDef(row[2] if row else "Card", ct, cost, atk, def_, shards, abilities, cdef_attrs)
                # Deck cards: minimal update so the client knows they exist.
                # Both players' decks stay face-down (nulling=True) — you
                # cannot inspect your own deck mid-game. The client shows the
                # deck sleeve and skips the examiner for Null cards.
                game.push_card_updated(cid, pl_uid_t, game_engine.ECardCollections.Deck, ct,
                                      nulling=True)
            if player_deck_cards:
                game.push_deck_created_with_cards(pl_uid_t, player_deck_cards)
                # Client-side workaround: a nulled deck card that carries
                # socketed-gem abilities (e.g. Shamed Gladiator's Rage gem) can
                # render as a ghost in the warzone at game start.  Re-assert
                # the gem-bearing deck cards into the deck during PreGame so
                # the client's zone placement matches the authoritative DB.
                if deck_gem_abilities:
                    for i, cid in enumerate(player_deck_cards):
                        if str(player_card_tpl_ids[i]) not in deck_gem_abilities:
                            continue
                        # cid is already a SessionCardId (game._new_card_id());
                        # wrapping it again made the CardMoved's session card
                        # id serialize a SessionCardId as its UID and crashed
                        # make_network_packet during PreGame.
                        c_scid = cid
                        game.push_card_moved(
                            c_scid, pl_uid_t, game_engine.ECardCollections.Deck,
                            game_engine.ECardLocations.Top, 0)
            
            # AI deck (shuffled). For campaign battles use the encounter's
            # real AI deck from encounter_deck_cards; otherwise a simple deck.
            ai_deck_cards = []
            import json as _aj
            ai_card_specs = []
            scene_ai_ability_guids = []
            scene_mod_card_guids = set()
            if is_practice:
                # Practice is explicitly a mirror match. Use the resolved
                # player deck templates, preserving one AI card per player
                # card and the current socketed-gem abilities.
                active_gems = {}
                if deck_db_id:
                    _ga_row = _db.execute(
                        "SELECT active_gems FROM decks WHERE id=?",
                        (deck_db_id,)).fetchone()
                    if _ga_row and _ga_row[0]:
                        try:
                            active_gems = _aj.loads(_ga_row[0]) or {}
                        except (TypeError, ValueError):
                            active_gems = {}
                for idx, cg in enumerate(player_resolved_tpl_ids):
                    if not cg:
                        continue
                    gem_key = str(player_card_tpl_ids[idx])
                    gem_type = int(active_gems.get(gem_key, 0) or 0)
                    gem_ability_guids = deck_gem_abilities.get(gem_key) or []
                    ai_card_specs.append((cg, gem_type, gem_ability_guids))
            elif not ai_deck_guid:
                # Practice sessions do not provide an encounter deck GUID.
                # Use a validated extracted starter deck so the opponent has
                # real card templates, resources, abilities, and a deck that
                # can also be inspected by !zones. The Human list is a
                # neutral fallback; encounter-specific decks still take
                # precedence below.
                fallback_deck = _STARTER_DECKS.get("Human") or {}
                for card_guid, quantity in fallback_deck.get("cards", []):
                    for _ in range(max(0, int(quantity or 0))):
                        if _db.execute(
                                "SELECT 1 FROM card_templates WHERE guid=?",
                                (card_guid,)).fetchone():
                            ai_card_specs.append((card_guid, 0, []))
            if ai_deck_guid or ai_card_specs:
                gem_types_by_name = {
                    str(name): (int(gem_type or 0),
                                _aj.loads(abilities_json or "[]")
                                if abilities_json else [])
                    for name, gem_type, abilities_json in _db.execute(
                        "SELECT gem_type_name, gem_type, abilities_json "
                        "FROM gem_templates").fetchall()
                }
                if ai_deck_guid:
                    rows = _db.execute(
                        "SELECT card_guid, quantity, gem_types_new_list_json "
                        "FROM encounter_deck_cards WHERE deck_guid=?",
                        (ai_deck_guid,)).fetchall()
                    for cg, q, gem_json in rows:
                        try:
                            gem_slots = _aj.loads(gem_json or "[]")
                        except (TypeError, ValueError):
                            gem_slots = []
                        for copy_index in range(int(q or 0)):
                            gem_type = 0
                            gem_ability_guids = []
                            socket_names = (gem_slots[copy_index]
                                            if copy_index < len(gem_slots)
                                            else [])
                            if not isinstance(socket_names, list):
                                socket_names = [socket_names]
                            for socket_name in socket_names:
                                gem_data = gem_types_by_name.get(str(socket_name))
                                if not gem_data:
                                    continue
                                if not gem_type:
                                    gem_type = gem_data[0]
                                for gem_guid in gem_data[1] or []:
                                    gem_guid = str(gem_guid).lower()
                                    if gem_guid not in gem_ability_guids:
                                        gem_ability_guids.append(gem_guid)
                            ai_card_specs.append(
                                (cg, gem_type, gem_ability_guids))
                # EncounterScene player modifications are authored metadata,
                # not card text.  Add cards targeted at the AI (for example
                # Taming Dire Toad's Taming Sphere) before the opening hand
                # is dealt so their GameStarted abilities can resolve.
                if is_campaign and scene_guid:
                    mrow = _db.execute(
                        "SELECT mods_json FROM encounter_scenes WHERE guid=?",
                        (scene_guid,)).fetchone()
                    try:
                        scene_mods = _aj.loads(mrow[0] or "[]") if mrow else []
                    except (TypeError, ValueError):
                        scene_mods = []
                    # A card attached to a concrete encounter player is a
                    # hidden battleboard controller, not a scene-wide grant.
                    # The scene metadata can also repeat that card in a
                    # descriptive modifier entry, so collect the targeted
                    # GUIDs before interpreting untargeted scene abilities.
                    targeted_scene_mod_guids = {
                        str(item.get("guid")).lower()
                        for mod in scene_mods if mod.get("target")
                        for item in mod.get("mods", [])
                        if item.get("guid")
                    }
                    scene_setup_card_guids = set()

                    def _has_game_started_grant(card_guid):
                        """Whether a scene card grants a GameStarted ability.

                        Untargeted encounter setup cards are authored as
                        scene modifiers, but their ``You`` and ``opposing
                        champion`` ownership is evaluated from the encounter
                        side that owns the hidden card.  Keep those cards in
                        the AI mod zone so the normal metadata/BOM resolver
                        applies the opposing-champion target correctly.
                        """
                        card_row = _db.execute(
                            "SELECT abilities_json FROM card_templates "
                            "WHERE guid=?", (card_guid,)).fetchone()
                        try:
                            ability_guids = _aj.loads(
                                card_row[0] or "[]") if card_row else []
                        except (TypeError, ValueError):
                            ability_guids = []
                        for ability_guid in ability_guids:
                            for _eg, effect_type, raw_param in _db.execute(
                                    "SELECT effect_guid,effect_type,param "
                                    "FROM ability_effects WHERE ability_guid=?",
                                    (ability_guid,)).fetchall():
                                if effect_type != "GrantAbilityEffectTemplate":
                                    continue
                                try:
                                    grant_data = _aj.loads(raw_param or "{}")
                                except (TypeError, ValueError):
                                    grant_data = None
                                granted_guid = (
                                    grant_data.get("ability_guid") or
                                    grant_data.get("abilityId")
                                    if isinstance(grant_data, dict)
                                    else str(raw_param or "").strip())
                                if not granted_guid:
                                    continue
                                trigger_row = _db.execute(
                                    "SELECT trigger_event_type "
                                    "FROM card_abilities_meta "
                                    "WHERE ability_guid=?", (str(
                                        granted_guid).lower(),)).fetchone()
                                if (trigger_row and
                                        "GameStartedEvent" in (trigger_row[0] or "")):
                                    return True
                        return False

                    for mod in scene_mods:
                        for item in mod.get("mods", []):
                            mg = item.get("guid")
                            mg_key = str(mg or "").lower()
                            # A scene-level card with a start-of-game grant is
                            # an encounter setup card.  Cockatwice's Taming
                            # modifier is the canonical example: the hidden AI
                            # source grants the sphere to its opposing player.
                            # If the authored data already contains an
                            # explicit AI copy (Wild Cub), use that copy once.
                            if (mg and not mod.get("target") and
                                    mg_key not in targeted_scene_mod_guids and
                                    _has_game_started_grant(mg)):
                                ai_card_specs.append((mg, 0, []))
                                scene_mod_card_guids.add(mg_key)
                                scene_setup_card_guids.add(mg_key)
                            if mg and mod.get("target") == "AIPlayer":
                                ai_card_specs.append((mg, 0, []))
                                scene_mod_card_guids.add(mg_key)
                                # Resolve controller-only grants (for example
                                # the Untamed Dire Toad passive) and apply them
                                # later to matching visible troops.
                                _mcard = _db.execute(
                                    "SELECT abilities_json FROM card_templates WHERE guid=?",
                                    (mg,)).fetchone()
                                try:
                                    _mags = _aj.loads(_mcard[0] or "[]") if _mcard else []
                                except (TypeError, ValueError):
                                    _mags = []
                                for _mag in _mags:
                                    for _meg, _met, _mep in _db.execute(
                                            "SELECT effect_guid,effect_type,param FROM ability_effects WHERE ability_guid=?",
                                            (_mag,)).fetchall():
                                        if _met != "GrantAbilityEffectTemplate":
                                            continue
                                        _granted = str(_mep or "").lower()
                                        if not _granted:
                                            continue
                                        _gtrow = _db.execute(
                                            "SELECT target_template_ids FROM card_abilities_meta WHERE ability_guid=?",
                                            (_granted,)).fetchone()
                                        try:
                                            _gtids = _aj.loads(_gtrow[0] or "[]") if _gtrow else []
                                        except (TypeError, ValueError):
                                            _gtids = []
                                        for _gtid in _gtids:
                                            _gfrow = _db.execute(
                                                "SELECT filter_json FROM target_templates WHERE template_id=?",
                                                (_gtid,)).fetchone()
                                            if _gfrow and (_gfrow[0] or "{}").strip() not in ("", "{}"):
                                                if _granted not in self._scene_targeted_ability_guids:
                                                    self._scene_targeted_ability_guids.append(_granted)
                                                # Preserve the controller of
                                                # the hidden setup card so a
                                                # typed IsControlledBy filter
                                                # continues to mean "your"
                                                # troops when the passive is
                                                # applied to cards materialized
                                                # later in the warzone.
                                                self._scene_targeted_ability_owners.setdefault(
                                                    _granted, 0 if mod.get("target") == "AIPlayer"
                                                    else int(self.user_profile["id"]))
                            # Scene ability cards (for example Big Game) grant
                            # a passive ability to the encounter's troops.
                            # Resolve the grant through the ability metadata;
                            # do not infer it from display text.
                            if (mg and mg_key not in targeted_scene_mod_guids
                                    and mg_key not in scene_setup_card_guids):
                                _arow = _db.execute(
                                    "SELECT abilities_json FROM card_templates WHERE guid=?",
                                    (mg,)).fetchone()
                                try:
                                    _ags = _aj.loads(_arow[0] or "[]") if _arow else []
                                except (TypeError, ValueError):
                                    _ags = []
                                for _ag in _ags:
                                    for _eg, _etype, _param in _db.execute(
                                            "SELECT effect_guid,effect_type,param FROM ability_effects WHERE ability_guid=?",
                                            (_ag,)).fetchall():
                                        if _etype != "GrantAbilityEffectTemplate":
                                            continue
                                        try:
                                            _gp = _aj.loads(_param or "{}")
                                            _grant = _gp.get("ability_guid") or _gp.get("abilityId")
                                        except (TypeError, ValueError):
                                            _grant = _param if _param else None
                                        if _grant and _grant not in scene_ai_ability_guids:
                                            scene_ai_ability_guids.append(_grant)
                    # Scene-wide statics are applied silently when a matching
                    # card is materialized by _card_full_data. This covers
                    # cards created later without pushing setup abilities onto
                    # the chain. The ability's own target filter determines
                    # which cards qualify.
                    self._scene_global_ability_guids = list(
                        scene_ai_ability_guids)
                # If we couldn't resolve an AI deck, fall back to FRA-style.
                if ai_card_specs:
                    random.shuffle(ai_card_specs)
                    for pos, (cg, gem_type, gem_ability_guids) in enumerate(
                            ai_card_specs):
                        cid = game._new_card_id()
                        _is_scene_mod = str(cg).lower() in scene_mod_card_guids
                        if not _is_scene_mod:
                            ai_deck_cards.append(cid)
                        r = _db.execute(
                            "SELECT card_type, name, cost, attack, defense FROM card_templates WHERE guid=?",
                            (cg,)).fetchone()
                        if r:
                            ct2 = game_engine.card_type_from_db(r[0])
                            game.card_defs[cid] = game_engine.CardDef(
                                r[1], ct2, r[2] or 0, r[3] or 0, r[4] or 0, [], [])
                        else:
                            game.card_defs[cid] = game_engine.CardDef(
                                "Card", game_engine.ECardTypes.Troop, 2, 2, 2, [], [])
                        # Encounter-mod cards are placed directly in the AI
                        # warzone so their GameStarted abilities resolve;
                        # normal encounter cards remain in the deck.
                        col = (game_engine.ECardCollections.Warzone
                               if _is_scene_mod else game_engine.ECardCollections.Deck)
                        if not _is_scene_mod:
                            game.push_card_updated(cid, ai_uid_t, col,
                                                   game.card_defs[cid].card_type,
                                                   nulling=True)
                        _ai_ct = r[0] if r else "Troop"
                        _db.execute("INSERT INTO game_cards (user_id, session_id, card_uid, card_template_id, card_type, template_guid, location, position, owner_user_id, original_template_guid) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (0, session.session_id, cid.uid.to_uint64(), cg, _ai_ct, cg,
                             'mod' if _is_scene_mod else 'deck', pos, 0, cg))
                        self._sync_instance_card_data(
                            session, cid.uid.to_uint64(), cg, commit=False)
                        if gem_ability_guids:
                            existing = _db.execute(
                                "SELECT card_abilities FROM game_cards "
                                "WHERE session_id=? AND card_uid=?",
                                (session.session_id, cid.uid.to_uint64())
                            ).fetchone()
                            try:
                                abilities = _aj.loads(existing[0] or "[]")
                            except (TypeError, ValueError):
                                abilities = []
                            for gem_guid in gem_ability_guids:
                                if gem_guid not in abilities:
                                    abilities.append(gem_guid)
                            _db.execute(
                                "UPDATE game_cards SET card_abilities=?, gems=? "
                                "WHERE session_id=? AND card_uid=?",
                                (_aj.dumps(abilities), gem_type,
                                 session.session_id, cid.uid.to_uint64()))
                        elif gem_type:
                            _db.execute(
                                "UPDATE game_cards SET gems=? "
                                "WHERE session_id=? AND card_uid=?",
                                (gem_type, session.session_id,
                                 cid.uid.to_uint64()))
                        # Build the authoritative CardDef after persisting the
                        # encounter gem. This makes the gem ability available
                        # to CardCreated/enter-play triggers before the card is
                        # drawn, and keeps later CardUpdated pushes consistent.
                        self._card_full_data(game, cid, cg)
                    _db.commit()
                    source = (f"encounter {ai_deck_guid}" if ai_deck_guid
                              else "player mirror" if is_practice
                              else "Human starter fallback")
                    log_req(f"    AI deck: {len(ai_deck_cards)} cards from "
                            f"{source} (hands dealt after PickGoesFirst)")
            if not ai_deck_cards:
                # If an encounter has no usable deck data, use a neutral
                # generated fallback.  Never copy the player's deck into a
                # campaign/FRA opponent: the opponent must have its own deck.
                # Ultimate fallback: generic 60-card deck when the encounter
                # itself has no usable card list.
                ai_card_ids = list(range(60))
                random.shuffle(ai_card_ids)
                for pos, i in enumerate(ai_card_ids):
                    cid = game._new_card_id()
                    ai_deck_cards.append(cid)
                    game.card_defs[cid] = game_engine.CardDef(f"Card_{i}", game_engine.ECardTypes.Troop, 2, 2, 2, [])
                    col = game_engine.ECardCollections.Deck
                    game.push_card_updated(cid, ai_uid_t, col,
                                           game_engine.ECardTypes.Troop,
                                           nulling=True)
                    _db.execute("INSERT INTO game_cards (user_id, session_id, card_uid, card_template_id, card_type, template_guid, location, position, owner_user_id, original_template_guid) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (0, session.session_id, cid.uid.to_uint64(), 8000+i, "Troop", None, 'deck', pos, 0, None))
                    self._sync_instance_card_data(session, cid.uid.to_uint64(), None)
            if ai_deck_cards:
                game.push_deck_created_with_cards(ai_uid_t, ai_deck_cards)
            _db.commit()
            # CardCreatedEvent triggers fire when the deck's cards are created
            # (the client's ActivateCardCreationAbilities during deck setup) —
            # e.g. Pterobot's permanent "cost -1 for each Dwarf and/or Robot
            # you control" modifier.  These resolve immediately (IgnoresChain)
            # into the DB; no priority window exists during setup.
            import ability as _abil_cc
            cc_bstate = {}
            for cid in player_deck_cards:
                _abil_cc.resolve_triggers(
                    _db, self, game, session, pl_uid_t, ai_uid_t, cc_bstate,
                    "CardCreatedEvent", cid.uid.to_uint64(),
                    self.user_profile["id"], zones=())
            for cid in ai_deck_cards:
                _abil_cc.resolve_triggers(
                    _db, self, game, session, pl_uid_t, ai_uid_t, cc_bstate,
                    "CardCreatedEvent", cid.uid.to_uint64(), 0, zones=())
            # NOTE: the AI's mulligan decision is NOT made here. It happens as
            # part of the Mulligan phase, resolved after the human acts (keep or
            # redraw) — see the 3029 handler's _resolve_ai_mulligan.
            
            # One phase at a time. PreGame first — no player input; the client
            # runs scenario setup synchronously and sends no transaction.
            # Apply PreGame-triggered champion abilities (Shard Attuned health)
            # for BOTH players before they pass priority in PreGame.
            import ability as _ability
            pl_guids = [str(a.guid) for a in getattr(self, "_player_champ_abilities", [])]
            if pl_guids:
                try:
                    _ability.apply_pregame_abilities(game, session, _db, self,
                        pl_uid_t, self.user_profile["id"], pl_guids, "player_health")
                except Exception as e:
                    log_req(f"    Player PreGame ability error: {e}")
            ai_guids = [ai_charge_power] if ai_charge_power else []
            if ai_guids:
                try:
                    _ability.apply_pregame_abilities(game, session, _db, self,
                        ai_uid_t, 0, ai_guids, "ai_health")
                except Exception as e:
                    log_req(f"    AI PreGame ability error: {e}")
            # Carry PreGame charge modifiers into the DB-backed state created
            # after Mulligan, just as we already carry starting health.
            self._player_starting_charges = getattr(game, "player_charges", 0)
            self._ai_starting_charges = getattr(game, "ai_charges", 0)
            # Persist the PreGame-boosted health so the DB battle state is
            # seeded from the ACTUAL value, not the un-boosted starting health.
            # Shard Attuned bumps game.player_health during PreGame; bstate is
            # seeded later from _player_starting_health, so without this every
            # phase push reports the un-boosted value (the 17->20 flicker at
            # turn start). game.player_health/ai_health are the boosted values.
            self._player_starting_health = game.player_health
            self._ai_starting_health = game.ai_health
            log_req(f"    PreGame: player health {game.player_health}, AI health {game.ai_health}")
            game.push_turn_phase(game_engine.ETurnPhases.PreGame)
            pkt = game.make_network_packet(pl_uid_t)
            evt_bytes = compress_gzip(encode_sync_event(pkt))
            evt_dw = encode_datawrapper(0, 3055, evt_bytes, 1, "00000000-0000-0000-0000-000000000000")
            self.scnt += 1
            self._game_scnt = self.scnt  # Save game scnt for subsequent events
            self.send({"issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}", "target": "ServiceGameSession", "instance": gs_instance, "reqid": 0, "c": 0, "conh": 0, "sid": self.sid}, evt_dw)
            log_req(f"    Pushed game init (decks, no hands yet) + PreGame ({len(evt_dw)}b)")

            if coin_winner_is_player:
                if getattr(self, "_pve_player_cannot_choose_play_first", False):
                    # The stock client cannot hide just the Play button.  Do
                    # not expose an invalid choice; resolve the toss winner's
                    # only legal result directly through the normal Draw path.
                    from types import SimpleNamespace
                    self._pve_forced_draw_first = True
                    forced_draw = SimpleNamespace(
                        inner_bytes=b"ChooseDrawTransaction",
                        is_set_ability_data=False,
                        is_ability_activate=False,
                        ai_choice=False)
                    log_req("    Weight: player won toss; forcing Draw first")
                    self._handle_choose_pick_transaction(session, forced_draw)
                else:
                    # The human won the toss: show the normal Play/Draw dialog
                    # and wait for its ChoosePlay/ChooseDraw transaction.
                    game2 = game_engine.Game(session.session_id, pl_uid_t, ai_uid_t)
                    game2.push_turn_phase(game_engine.ETurnPhases.PickGoesFirst, pl_uid_t, pl_uid_t)
                    game2.push_green_light(pl_uid_t, game_engine.EPriorityContext.Normal)
                    pkt2 = game2.make_network_packet(pl_uid_t)
                    evt_bytes2 = compress_gzip(encode_sync_event(pkt2))
                    evt_dw2 = encode_datawrapper(0, 3055, evt_bytes2, 1, "00000000-0000-0000-0000-000000000000")
                    self.scnt += 1
                    self._game_scnt = self.scnt  # Save game scnt for subsequent events
                    self.send({"issuer": f"0.0.0.0.ServiceGameSession.{SERVICE_GAME_SESSION_UID}.{session.session_id}.{self.scnt}", "target": "ServiceGameSession", "instance": gs_instance, "reqid": 0, "c": 0, "conh": 0, "sid": self.sid}, evt_dw2)
                    log_req(f"    Pushed PickGoesFirst phase ({len(evt_dw2)}b)")
            else:
                # The AI won the toss. It plays first 90% of the time and
                # draws first 10%; resolve the same server path as a player
                # choice so hands, client messaging, and pending turn state
                # stay identical.
                from types import SimpleNamespace
                ai_plays_first = random.random() < 0.90
                ai_pick = SimpleNamespace(
                    inner_bytes=(b"ChoosePlayTransaction"
                                 if ai_plays_first else b"ChooseDrawTransaction"),
                    is_set_ability_data=False,
                    is_ability_activate=False,
                    ai_choice=True)
                log_req("    FRA AI won coin toss: chose %s first (90/10)" %
                        ("Play" if ai_plays_first else "Draw"))
                self._handle_choose_pick_transaction(session, ai_pick)

        # ServiceCampaign Siege (150000)
        elif data_type == 150000:
            envelope = inner_obj.get("Envelope", b"{}")
            try:
                env_json = json.loads(envelope) if isinstance(envelope, (bytes, str)) else envelope
                log_req(f">>> Campaign Siege (dt=150000) action={env_json.get('RequestType', '?')}")
            except:
                log_req(f">>> Campaign Siege (dt=150000) raw={envelope[:80]}")
            resp_envelope = json.dumps({"SiegeStatus": 2, "Offset": 0, "Count": 0, "Results": []}).encode("utf-8")
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Campaign.Siege.Messaging+Response",
                 "System.Byte[]"],
                [("Envelope", "bytes", resp_envelope)]
            )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceCampaign.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str,
                "target": target,
                "instance": instance,
                "reqid": resp_reqid,
                "c": comp,
                "conh": conh,
                "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent Campaign Siege response ({len(dw_bytes)}b)")
    
        # ServiceCampaign Main (110000) — campaign/adventure system
        elif data_type == 110000:
            campaign.handle_campaign_request(self, _db, inner_obj, comp,
                                               session_id, reqid,
                                               target, instance, conh,
                                               SERVICE_MAIL_UID)

        # ServiceTournaments — TryReconnection (25021)
        elif data_type == 25021:
            log_req(">>> Respond TryReconnectionToDisconnectedTournament -> no tournament")
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Network.Tournaments.TryReconnectionToDisconnectedTournamentResponseArgs"],
                []
            )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str,
                "target": target,
                "instance": instance,
                "reqid": resp_reqid,
                "c": comp,
                "conh": conh,
                "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent Tournament reconnect response ({len(dw_bytes)}b)")
    
        # ServiceEscrow — GetStoreItems (6009)
        elif data_type == 6009:
            log_req(">>> Respond GetStoreItems -> 1 booster pack")
            resp_inner = encode_get_store_items_response()
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceEscrow.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str,
                "target": target,
                "instance": instance,
                "reqid": resp_reqid,
                "c": comp,
                "conh": conh,
                "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent GetStoreItems response ({len(dw_bytes)}b)")
    
        # ServiceEscrow — PurchaseItem (6011)
        elif data_type == 6011:
            quantity = inner_obj.get("Quanity", 1)
            item_id = inner_obj.get("Id", 1)
            log_req(f">>> PurchaseItem: qty={quantity} id={item_id}")
    
            # Look up item from DB
            row = _db.execute("SELECT name, price, currency, template_guid FROM store_items WHERE id=?", (int(item_id),)).fetchone()
            if not row:
                log_req(f"    Unknown item id={item_id}, defaulting to 100 Gold")
                item_name, cost, currency_type = "Unknown", 100, "Gold"
                template_guid = ""
            else:
                item_name, cost, currency_type, template_guid = row
            
            p = self.user_profile
            if currency_type == "Platinum":
                new_bal = p["platinum"] - (cost * quantity)
                db_update_resources(p["id"], platinum=new_bal)
                p["platinum"] = new_bal
                remaining = new_bal
            else:
                new_bal = p["gold"] - (cost * quantity)
                db_update_resources(p["id"], gold=new_bal)
                p["gold"] = new_bal
                remaining = new_bal
            
            # Build granted inventory item for the purchase.  Each booster has
            # a 2% chance to upgrade to its Primal pack (data-driven via
            # pack_set_map: only sets with a Primal version — core Sets 1-4 —
            # can upgrade; the granted GUID is simply swapped).
            granted = []  # [(template_guid, qty)]
            if template_guid:
                from db import db_primal_pack_for
                primal_guid = db_primal_pack_for(template_guid)
                if primal_guid:
                    normal_qty, primal_qty = _roll_primal_upgrade(quantity)
                    if primal_qty:
                        if normal_qty:
                            granted.append((template_guid, normal_qty))
                        granted.append((primal_guid, primal_qty))
                        log_req(f"    Primal upgrade: {primal_qty}/{quantity} "
                                f"{item_name} -> {primal_guid[:8]}")
                    else:
                        granted.append((template_guid, quantity))
                else:
                    granted.append((template_guid, quantity))
            granted_list = []
            for gi, (tg, qty) in enumerate(granted):
                uid = (1000 + item_id) if gi == 0 else (900000 + item_id + gi)
                granted_list.append((tg, uid, qty))
    
            resp_inner = encode_objfmt_response(
                ["Game.Client.Network.Escrow.PurchaseItemResponse",
                 "System.Int32", "System.String",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
                 "Game.Shared.Domain.inventory_bits",
                 "Game.Shared.ResourceId",
                 "System.Guid",
                 "System.DateTime",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits"],
                [("RemainingCurrency", "int", remaining),
                 ("TransactionCurrencyType", "string", currency_type),
                 ("PurchasedInventory", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits", 0)),
                 ("PurchasedDeckBits", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits", 0)),
                 ("PurchasedCards", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
                 ("GrantedInventory", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits", len(granted_list), granted_list)),
                 ("GrantedDeckBits", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits", 0)),
                 ("GrantedCards", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
                 ("ConsumedInventory", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits", 0)),
                 ("ConsumedCards", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
                 ("CurrencyInventory", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits", 0))]
            )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceEscrow.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send_and_cache({
                "issuer": issuer_str,
                "target": target,
                "instance": instance,
                "reqid": resp_reqid,
                "c": comp,
                "conh": conh,
                "sid": self.sid,
            }, dw_bytes, data_type, reqid, target, instance)
            for tg, qty in granted:
                gname = item_name if tg == template_guid else ""
                if not gname:
                    gname_row = _db.execute(
                        "SELECT name FROM store_items WHERE template_guid=?",
                        (tg,)).fetchone()
                    gname = gname_row[0] if gname_row else tg
                db_record_purchase(p["id"], gname, tg, cost * qty, currency_type)
            # Also add to player inventory for future pushes
            for gi, (tg, qty) in enumerate(granted):
                db_add_inventory(p["id"], tg, qty)
                # Store the client-side UID used in the GrantedInventory
                # response (1000 + store_item_id, or a distinct high UID for
                # any additional upgraded grant).
                uid = (1000 + item_id) if gi == 0 else (900000 + item_id + gi)
                _db.execute(
                    "UPDATE player_inventory SET client_item_uid=? "
                    "WHERE user_id=? AND template_guid=? AND client_item_uid=0",
                    (uid, p["id"], tg))
            _db.commit()
            log_req(f"    Sent PurchaseItem response: remaining={remaining} {currency_type}")
    
        # ServiceEscrow — RedeemCode (6013)
        elif data_type == 6013:
            redeem_code = inner_obj.get("RedeemCode", "")
            log_req(f">>> RedeemCode: code='{redeem_code}'")
    
            p = self.user_profile
            result = db_redeem_code(redeem_code)
            if result is None:
                log_req(f"    RedeemCode invalid: {redeem_code}")
                resp_inner = encode_objfmt_response(
                    ["Game.Client.Network.Escrow.RedeemCodeResponse",
                     "System.Collections.Generic.List`1#Game.Shared.ResourceId",
                     "Game.Shared.ResourceId",
                     "System.Guid",
                     "System.Int32",
                     "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                     "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits"],
                    [("ItemTemplateIds", "coll", ("System.Collections.Generic.List`1#Game.Shared.ResourceId", 0)),
                     ("GoldDelta", "int", 0),
                     ("PlatinumDelta", "int", 0),
                     ("CardBits", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
                     ("StarterDecksBits", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits", 0))]
                )
                resp_body = compress_gzip(resp_inner) if comp else resp_inner
                resp_reqid = reqid | 1
                dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
                issuer_str = f"0.0.0.0.ServiceEscrow.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
                self.scnt += 1
                self.send({
                    "issuer": issuer_str, "target": target, "instance": instance,
                    "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
                }, dw_bytes)
                log_req(f"    Sent RedeemCode (invalid) response ({len(dw_bytes)}b)")
            else:
                gold_delta = result["gold"]
                plat_delta = result["platinum"]
                new_gold = p["gold"] + gold_delta
                new_plat = p["platinum"] + plat_delta
                db_update_resources(p["id"], gold=new_gold, platinum=new_plat)
                p["gold"] = new_gold
                p["platinum"] = new_plat
    
                parts = []
                if gold_delta > 0:
                    parts.append(f"{gold_delta:,} Gold")
                if plat_delta > 0:
                    parts.append(f"{plat_delta:,} Platinum")
                reward_desc = ", ".join(parts)
    
                db_send_email(p["id"],
                    f"Code Redeemed: {redeem_code}",
                    f"You have successfully redeemed the code '{redeem_code}' and received {reward_desc}.\n\nThank you for playing Hex: Shards of Fate!",
                    "SYSTEM")
    
                log_req(f"    RedeemCode success: gold+{gold_delta} plat+{plat_delta}")
    
                resp_inner = encode_objfmt_response(
                    ["Game.Client.Network.Escrow.RedeemCodeResponse",
                     "System.Collections.Generic.List`1#Game.Shared.ResourceId",
                     "Game.Shared.ResourceId",
                     "System.Guid",
                     "System.Int32",
                     "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                     "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits"],
                    [("ItemTemplateIds", "coll", ("System.Collections.Generic.List`1#Game.Shared.ResourceId", 0)),
                     ("GoldDelta", "int", gold_delta),
                     ("PlatinumDelta", "int", plat_delta),
                     ("CardBits", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
                     ("StarterDecksBits", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits", 0))]
                )
                resp_body = compress_gzip(resp_inner) if comp else resp_inner
                resp_reqid = reqid | 1
                dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
                issuer_str = f"0.0.0.0.ServiceEscrow.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
                self.scnt += 1
                self.send_and_cache({
                    "issuer": issuer_str, "target": target, "instance": instance,
                    "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
                }, dw_bytes, data_type, reqid, target, instance)
                log_req(f"    Sent RedeemCode (ok) response ({len(dw_bytes)}b)")

        # === Tournaments — Battlegrounds Ladder / On-Demand / waiting rooms ======

        # ServiceTournaments — LadderRecord (70023)
        elif data_type == 70023:
            log_req(">>> Respond LadderRecord — empty ladder")
            resp_fields = [
                ("constructedIronStars", "int", 0), ("constructedStars", "int", 0),
                ("constructedMaxStars", "int", 0), ("constructedStreak", "int", 0),
                ("CosmicRank", "int", 0), ("limitedStars", "int", 0),
                ("limitedMaxStars", "int", 0), ("limitedStreak", "int", 0),
                ("limitedCosmicRank", "int", 0),
                ("limitedSleeves", "coll", ("System.Collections.Generic.List`1#Game.Shared.ResourceId", 0)),
                ("constructedSleeves", "coll", ("System.Collections.Generic.List`1#Game.Shared.ResourceId", 0)),
                ("includesDebugData", "bool", False), ("MMR", "int", 0), ("LimitedMMR", "int", 0)]
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Tournaments.Messages.Tournament+LadderRecord+Response"], resp_fields)
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
            self.scnt += 1
            self.send({"issuer": f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid|1}",
                       "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent LadderRecord response ({len(dw_bytes)}b)")

        elif data_type == 70027:
            log_req(">>> Respond LadderTierRewardsList — empty")
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Tournaments.Messages.Tournament+LadderTierRewardsList+Response"],
                [("tier_rewards", "coll", ("System.Collections.Generic.Dictionary`2#System.UInt32!Game.Shared.Tournaments.TournamentRewardInfo", 0))])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
            self.scnt += 1
            self.send({"issuer": f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid|1}",
                       "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent LadderTierRewardsList response ({len(dw_bytes)}b)")

        elif data_type == 70028:
            log_req(">>> Respond LadderRankingList — empty")
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Tournaments.Messages.Tournament+LadderRankingList+Response"],
                [("ConstructedLadderRanking", "coll", ("System.Collections.Generic.List`1#System.String", 0)),
                 ("LimitedLadderRanking", "coll", ("System.Collections.Generic.List`1#System.String", 0))])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
            self.scnt += 1
            self.send({"issuer": f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid|1}",
                       "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent LadderRankingList response ({len(dw_bytes)}b)")

        elif data_type == 70015:
            log_req(">>> PooledMsg reqflush — acking")
            resp_inner = encode_objfmt_response(
                ["Game.Shared.PooledMessaging.PooledMessagingRequestsInterface+Response"], [])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
            self.scnt += 1
            self.send({"issuer": f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid|1}",
                       "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)

        elif data_type == 25019:
            fmt = inner_obj.get("format", "constructed")
            room_id = _next_waiting_room_id()
            with _waiting_room_lock:
                _waiting_rooms[room_id] = {"id": room_id, "style": inner_obj.get("style", "se"),
                    "format": fmt, "playerCount": int(inner_obj.get("playerCount", 2)),
                    "players": {}, "status": "waiting"}
            log_req(f">>> CreateWaitingRoom: id={room_id}")
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Network.Tournaments.CreateWaitingRoomResponseArgs"], [])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
            self.scnt += 1
            self.send({"issuer": f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid|1}",
                       "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent CreateWaitingRoom response ({len(dw_bytes)}b)")

        # ServiceTournaments — TestTournamentEntry (25027)
        elif data_type == 25027:
            tournament_id = int(inner_obj.get("TournamentID", 0) or 0)
            is_wr = bool(inner_obj.get("IsWaitingRoom", False))
            log_req(f">>> TestTournamentEntry: tid={tournament_id} wr={is_wr}")
            room = db_tournament_by_id(tournament_id)
            success = bool(room and room["status"] == "waiting")
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Network.Tournaments.TestTournamentEntryResponseArgs"],
                [("success", "bool", success),
                 ("qualifyingEntryGroups", "intlist", ("System.Collections.Generic.List`1#System.Int32", 0, [])),
                 ("Error", "enum1", ("Game.Shared.Network.Tournaments.ETestTournamentEntryError", 0)),
                 ("ErrorMessage", "string", "")])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
            self.scnt += 1
            self.send({"issuer": f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid|1}",
                       "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent TestTournamentEntry ({len(dw_bytes)}b)")

        # ServiceTournaments — EnterTournament (25029)
        elif data_type == 25029:
            tournament_id = int(inner_obj.get("TournamentID", 0) or 0)
            log_req(f">>> EnterTournament: tid={tournament_id}")
            player_uid = int(self.client_reck_id) if self.client_reck_id else 0
            player_name = (self.user_profile.get("name", "Unknown") if self.user_profile else "Unknown")
            deck_id = uid_instance(inner_bytes, "DeckId")
            entry_group = int(inner_obj.get("EntryGroup", 0) or 0)
            tournament_server.leave_all(player_uid)
            ok, count, target_p, type_id = tournament_server.join_tournament(
                tournament_id, player_uid, player_name, deck_id=deck_id, entry_group=entry_group)
            if not ok:
                signup = db_tournament_signup_by_player(tournament_id, player_uid)
                if signup:
                    log_req(f"    Player already signed up")
                    is_waiting_room = False
                else:
                    dw_bytes = _encode_enter_tournament_error(comp, session_id, tournament_id, "InvalidTouranmentError")
                    self.scnt += 1
                    self.send({"issuer": f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid|1}",
                               "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
                    return
            else:
                is_waiting_room = target_p > 1
                log_req(f"    Player {player_name} joined room {tournament_id} ({count}/{target_p}) type={type_id}")
            with player_handler_lock:
                player_handlers[player_uid] = self
            if deck_id:
                player_decks[player_uid] = deck_id
                log_req(f"    Deck selected: {deck_id}")
            log_req(f"    EntryGroup: {entry_group}")
            for stype_id in (2, 3):
                if type_id == stype_id:
                    row = _db.execute("SELECT t.*, tt.set_id FROM tournaments t JOIN tournament_types tt ON t.type_id=tt.id WHERE t.id=? LIMIT 1", (tournament_id,)).fetchone()
                    if row:
                        set_id = row[-1] or "set01"
                        if stype_id == 2:
                            tournament_server.generate_sealed_deck(tournament_id, player_uid, set_id)
                        else:
                            tournament_server.generate_draft_deck(tournament_id, player_uid, set_id)
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Network.Tournaments.EnterTournamentResponseArgs",
                 "Game.Shared.Domain.deck_bits", "System.UInt64", "System.String",
                 "Game.Shared.ResourceId", "System.Guid",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                 "Game.Shared.Mechanics.EDeckLock", "Game.Shared.Mechanics.EDeckPersonality", "System.Int32"],
                [("isWaitingRoom", "bool", is_waiting_room),
                 ("TournamentID", "ulong", tournament_id),
                 ("TournamentDeckInfo", "deckbits", _make_deck_data(deck_id if deck_id else 0)),
                 ("Error", "enum1", ("Game.Shared.Network.Tournaments.EEnterTournamentError", 0))])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
            self.scnt += 1
            self.send({"issuer": f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid|1}",
                       "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent EnterTournament response ({len(dw_bytes)}b)")
            for s in db_tournament_signups_by_tournament(tournament_id):
                handler = player_handlers.get(s["player_uid"])
                if handler and handler is not self and tournament_id:
                    try:
                        push_tournament_room_data(handler, f"tourn:waitingroom-{tournament_id}_full", "")
                        push_tournament_room_data(handler, f"tourn:tournament-{tournament_id}_full", "")
                        push_tournament_room_data(handler, "tourn:lobby_full", "")
                        # Also push TournamentInfo (25058) to populate m_TournamentInfos
                        ti_inner = encode_objfmt_response(
                            ["Game.Shared.Network.Tournaments.TournamentInfoEventArgs",
                             "Game.Shared.Tournaments.TournamentInfo",
                             "Game.Shared.Tournaments.ETournamentStatus",
                             "Game.Shared.Tournaments.ETournamentCompletionType",
                             "System.UInt64", "System.Int32", "System.Int64", "System.Boolean"],
                            [("Info", "struct", ("Game.Shared.Tournaments.TournamentInfo", [
                                ("TournamentID", "ulong", tournament_id),
                                ("TournamentStatus", "enum1", (
                                    "Game.Shared.Tournaments.ETournamentStatus", 2)),
                                ("CompletionType", "enum1", (
                                    "Game.Shared.Tournaments.ETournamentCompletionType", 0)),
                                ("ResgistrationOpenTime", "long", 0),
                                ("Public", "bool", False)]))])
                        ti_body = compress_gzip(ti_inner)
                        ti_dw = encode_datawrapper(0, 25058, ti_body, 1, "00000000-0000-0000-0000-000000000000")
                        handler.scnt += 1
                        handler.send({
                            "issuer": f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{handler.scnt}",
                            "target": target, "instance": instance,
                            "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
                        }, ti_dw)
                        log_req(f"    Pushed TournamentInfo (25058) for tid={tournament_id} from EnterTournament")
                    except Exception: pass
            if target_p > 0 and count >= target_p:
                log_req(f"    >>> Room {tournament_id} full — starting game")
                # Preserve the handlers found during this join.  The start
                # path sends several unsolicited events after the Enter
                # request; a shared-registry lookup can otherwise lose one
                # of the two recipients during a reconnect/concurrent join.
                room_handlers = {player_uid: self}
                with player_handler_lock:
                    for signup in db_tournament_signups_by_tournament(tournament_id):
                        signup_pid = int(signup["player_uid"])
                        room_handlers.setdefault(
                            signup_pid, player_handlers.get(signup_pid))
                start_waiting_room_game(
                    tournament_id, handler_overrides=room_handlers)

        elif data_type == 25005:
            log_req(">>> UnsubscribeDescriptionListener — ack")
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Network.Tournaments.UnsubscribeDescriptionListenerResponseArgs"], [])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
            self.scnt += 1
            self.send({"issuer": f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid|1}",
                       "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent UnsubscribeDescriptionListener response ({len(dw_bytes)}b)")

        elif data_type == 25007:
            tournament_id = int(inner_obj.get("TournamentID", 0))
            log_req(f">>> GetTournamentInfo: tid={tournament_id}")
            resp_inner = encode_objfmt_response(
                ["Game.Client.Network.Tournaments.GetTournamentInfoResponse",
                 "Game.Shared.Tournaments.TournamentInfo",
                 "System.UInt64",
                 "Game.Shared.Network.Tournaments.EGetTournamentInfoError",
                 "System.String"],
                [("Results", "struct", ("Game.Shared.Tournaments.TournamentInfo", [
                    ("TournamentID", "ulong", tournament_id)])),
                 ("Error", "enum1", ("Game.Shared.Network.Tournaments.EGetTournamentInfoError", 0)),
                 ("ErrorMessage", "string", "")])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
            self.scnt += 1
            self.send({"issuer": f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid|1}",
                       "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent GetTournamentInfo response ({len(dw_bytes)}b)")

        elif data_type == 25035:
            log_req(">>> LeaveWaitingRoom")
            player_uid = int(self.client_reck_id) if self.client_reck_id else 0
            tournament_server.leave_all(player_uid)
            resp_inner = encode_objfmt_response(["Game.Shared.Network.Tournaments.LeaveWaitingRoomResponseArgs"], [])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
            self.scnt += 1
            self.send({"issuer": f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid|1}",
                       "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent LeaveWaitingRoom response ({len(dw_bytes)}b)")

        elif data_type == 25031:
            log_req(">>> LeaveTournament")
            player_uid = int(self.client_reck_id) if self.client_reck_id else 0
            tournament_id = int(inner_obj.get("TournamentID", 0) or 0)
            active_tournament_session = _db.execute(
                "SELECT session_id FROM tournament_matches "
                "WHERE tournament_id=? AND state!='Complete' "
                "AND (player1_uid=? OR player2_uid=?) LIMIT 1",
                (tournament_id, player_uid, player_uid)).fetchone() if tournament_id else None
            if tournament_id and record_tournament_forfeit(
                    tournament_id, player_uid, self):
                log_req(f"    Recorded tournament forfeit: tid={tournament_id} "
                        f"loser={player_uid}")
                status = _db.execute(
                    "SELECT status FROM tournaments WHERE id=?",
                    (tournament_id,)).fetchone()
                if (status and str(status[0]).lower() == "complete"
                        and active_tournament_session):
                    db_delete_game_session(active_tournament_session[0])
                    log_req(f"    Tournament complete: cleaned PvP session "
                            f"{active_tournament_session[0]}")
            tournament_server.leave_all(player_uid)
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Network.Tournaments.LeaveTournamentResponseArgs",
                 "System.UInt64"],
                [("success", "bool", True),
                 ("TournamentID", "ulong", tournament_id)])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            dw_bytes = encode_datawrapper(reqid | 1, data_type, resp_body, comp, session_id)
            self.scnt += 1
            self.send({"issuer": f"0.0.0.0.ServiceTournaments.{SERVICE_MAIL_UID}.ServicePlayer.{self.client_uid}.{reqid|1}",
                       "target": target, "instance": instance, "reqid": reqid | 1, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent LeaveTournament response ({len(dw_bytes)}b)")    
        # CreateNewDeckFromTemplate (2091)
        elif data_type == 2091:
            log_req(f">>> CreateNewDeckFromTemplate (dt=2091)")
            resp_inner = encode_objfmt_response(
                ["Game.Client.Network.Profile.CreateNewDeckFromTemplateResponse",
                 "System.Boolean",
                 "Game.Shared.Domain.deck_bits",
                 "Game.Shared.UID",
                 "System.String",
                 "System.UInt64",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                 "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                 "System.Collections.Generic.List`1#Game.Shared.ResourceId",
                 "Game.Shared.ResourceId",
                 "System.Guid"],
                [("succeded", "bool", True),
                 ("Deckbits", "class", "Game.Shared.Domain.deck_bits"),
                 ("failingCardTemplaes", "coll", ("System.Collections.Generic.List`1#Game.Shared.ResourceId", 0))]
            )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent CreateNewDeckFromTemplate response ({len(dw_bytes)}b)")

        # GetPlayerDecks (2081) — return deck IDs and names
        elif data_type == 2081:
            log_req(">>> GetPlayerDecks (dt=2081)")
            decks = []
            if self.user_profile:
                decks = db_get_decks(self.user_profile["id"])
            log_req(f"    Returning {len(decks)} decks")
            
            # Manual encoding: GetPlayerDecksResponse { PlayerDeckIDs, PlayerDeckNames, Error, ErrorMessage }
            tn = ["Game.Client.Network.Profile.GetPlayerDecksResponse",
                  "System.Collections.Generic.List`1#Game.Shared.UID",
                  "Game.Shared.UID", "System.UInt64",
                  "System.Collections.Generic.List`1#System.String",
                  "System.String",
                  "Game.Shared.Network.Profile.EGetPlayerDecksError",
                  "System.Int32"]
            def ft(x):
                if x not in tn: tn.append(x)
                return tn.index(x)
            sz = []; buf = io.BytesIO()
            W = lambda s: buf.write(s.encode("utf-8"))
            S = lambda: buf.write(b";")

            sz.append(0)
            W(""); S(); W("0"); S(); W(str(ft(tn[0]))); S(); W("4"); S()

            # PlayerDeckIDs: List<UID>
            fc1 = buf.tell(); sz.append(0); list1_idx = len(sz)-1
            W("PlayerDeckIDs"); S(); W(str(list1_idx)); S(); W(str(ft(tn[1]))); S(); W("0"); S()
            W(str(len(decks))); S()
            for i, dk in enumerate(decks):
                fe = buf.tell(); sz.append(0); eidx = len(sz)-1
                deck_uid64 = (dk["id"] << 8) | 17
                W(str(i)); S(); W(str(eidx)); S(); W(str(ft(tn[2]))); S(); W("1"); S()
                f1 = buf.tell(); sz.append(0)
                W("m_UID64"); S(); W(str(len(sz)-1)); S(); W(str(ft("System.UInt64"))); S(); W("0"); S()
                W(hexlify(struct.pack("<Q", deck_uid64)).decode("ascii")); S()
                sz[-1] = buf.tell() - f1
                sz[eidx] = buf.tell() - fe
            sz[list1_idx] = buf.tell() - fc1

            # PlayerDeckNames: List<string>
            fc2 = buf.tell(); sz.append(0); list2_idx = len(sz)-1
            W("PlayerDeckNames"); S(); W(str(list2_idx)); S(); W(str(ft(tn[4]))); S(); W("0"); S()
            W(str(len(decks))); S()
            for i, dk in enumerate(decks):
                fe = buf.tell(); sz.append(0); eidx = len(sz)-1
                W(str(i)); S(); W(str(eidx)); S(); W(str(ft(tn[5]))); S(); W("0"); S()
                enc = dk["name"].encode("utf-8")
                W(str(len(enc))); S(); buf.write(enc)
                sz[eidx] = buf.tell() - fe
            sz[list2_idx] = buf.tell() - fc2

            # Error (enum)
            f3 = buf.tell(); sz.append(0)
            W("Error"); S(); W(str(len(sz)-1)); S(); W(str(ft(tn[6]))); S(); W("1"); S()
            f3v = buf.tell(); sz.append(0)
            W("value__"); S(); W(str(len(sz)-1)); S(); W(str(ft("System.Int32"))); S(); W("0"); S()
            W(hexlify(struct.pack("<i", 0)).decode("ascii")); S()
            sz[-1] = buf.tell() - f3v; sz[-2] = buf.tell() - f3

            # ErrorMessage
            f4 = buf.tell(); sz.append(0)
            W("ErrorMessage"); S(); W(str(len(sz)-1)); S(); W(str(ft("System.String"))); S(); W("0"); S()
            W("0"); S()
            sz[-1] = buf.tell() - f4

            sz[0] = buf.tell()
            W(";".join(tn)); buf.write(b"\n")
            for i, s in enumerate(sz):
                if i > 0: S()
                W(str(s))
            resp_inner = buf.getvalue()
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({"issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent GetPlayerDecks ({len(decks)} decks, {len(dw_bytes)}b)")

        # AddNewDeck (2089)
        elif data_type == 2089:
            log_req(f">>> AddNewDeck (dt=2089)")
            # Extract deck name from raw bytes (parser fails on ResourceId)
            deck_name = "FirstDeck"
            if isinstance(inner_obj, dict) and inner_obj.get("DeckName"):
                deck_name = inner_obj["DeckName"]
            elif isinstance(inner_bytes, bytes) and b"DeckName" in inner_bytes:
                pos = inner_bytes.find(b"DeckName")
                if pos >= 0:
                    rest = inner_bytes[pos+8:]
                    parts = rest.split(b";", 5)
                    if len(parts) >= 5:
                        try:
                            namelen = int(parts[4])
                            namebytes = parts[5][:namelen] if len(parts) > 5 else b""
                            deck_name = namebytes.decode("utf-8", errors="replace")
                        except:
                            pass
            log_req(f"    Deck name: {deck_name}")

            # Extract card IDs from raw bytes and save to DB first
            card_ids = []
            if isinstance(inner_obj, dict) and inner_obj.get("DeckCardIDs"):
                card_ids = inner_obj["DeckCardIDs"]
            elif isinstance(inner_bytes, bytes) and b"DeckCardIDs" in inner_bytes:
                pos = inner_bytes.find(b"DeckCardIDs")
                if pos >= 0:
                    rest = inner_bytes[pos+12:]
                    parts = rest.split(b";", 5)
                    if len(parts) >= 4:
                        try:
                            count = int(parts[3])
                            tail = b";".join(parts[4:])
                            for _ in range(count):
                                # Card ID: idx;size;type;0;value;
                                seg = tail.split(b";", 5)
                                if len(seg) >= 5:
                                    card_ids.append(struct.unpack("<Q", unhexlify(seg[4]))[0])
                                    tail = b";".join(seg[5:])
                        except:
                            pass

            # The real deck ID comes from db_save_deck()'s last_insert_rowid().
            # Parse champion/gems/sleeve from raw bytes
            pve_champion_uid = None
            pvp_champ_guid = None
            active_gems = {}
            deck_sleeve_guid = None
            gameboard_guid = None
            coin_guid = None
            if isinstance(inner_bytes, bytes):
                # PvEChampionId (UID) — the champion this deck belongs to.
                pos = inner_bytes.find(b'PvEChampionId')
                if pos >= 0:
                    uid_pos = inner_bytes.find(b'm_UID64', pos)
                    if uid_pos >= 0:
                        rest = inner_bytes[uid_pos:]; parts = rest.split(b';', 5)
                        if len(parts) >= 5:
                            try: pve_champion_uid = struct.unpack("<Q", unhexlify(parts[4]))[0]
                            except: pve_champion_uid = None
                pos = inner_bytes.find(b'PvPChampionId')
                if pos >= 0:
                    gpos = inner_bytes.find(b'm_Guid', pos)
                    if gpos >= 0:
                        rest = inner_bytes[gpos:]; parts = rest.split(b';', 5)
                        if len(parts) >= 5:
                            try: pvp_champ_guid = parts[5][:int(parts[4])].decode('utf-8', errors='replace')
                            except: pass
                pos = inner_bytes.find(b'ActiveGems')
                if pos >= 0:
                    rest = inner_bytes[pos:]; parts = rest.split(b';', 5)
                    if len(parts) >= 5:
                        try:
                            count = int(parts[4]); tail = b';'.join(parts[5:])
                            for _ in range(count):
                                seg = tail.split(b';', 18)
                                if len(seg) >= 18:
                                    key_val = struct.unpack("<Q", unhexlify(seg[8]))[0]
                                    v_val = struct.unpack("<Q", unhexlify(seg[17]))[0] & 0xFFFFFFFF
                                    active_gems[str(key_val)] = v_val
                                    tail = b';'.join(seg[18:])
                        except: pass
                for fname in [b'DeckSleeveId', b'GameboardId', b'CoinId']:
                    pos2 = inner_bytes.find(fname)
                    if pos2 >= 0:
                        gpos2 = inner_bytes.find(b'm_Guid', pos2)
                        if gpos2 >= 0:
                            rest2 = inner_bytes[gpos2:]; parts2 = rest2.split(b';', 5)
                            if len(parts2) >= 5:
                                try: val = parts2[5][:int(parts2[4])].decode('utf-8', errors='replace')
                                except: continue
                                if fname == b'DeckSleeveId': deck_sleeve_guid = val
                                elif fname == b'GameboardId': gameboard_guid = val
                                elif fname == b'CoinId': coin_guid = val
            if not self.user_profile:
                # No authenticated user — error out rather than fabricate a
                # bogus deck ID that the client can't look up again.
                log_req("    ERROR: No user_profile for AddNewDeck — rejecting")
                err_inner = encode_objfmt_response(
                    ["Game.Client.Network.Profile.AddNewDeckResponse",
                     "Game.Shared.Network.Profile.EAddNewDeckError", "System.String"],
                    [("Error", "enum", "Game.Shared.Network.Profile.EAddNewDeckError", 1),
                     ("ErrorMessage", "string", "Not authenticated")]
                )
                err_body = compress_gzip(err_inner) if comp else err_inner
                err_reqid = reqid | 1
                dw_err = encode_datawrapper(err_reqid, data_type, err_body, comp, session_id)
                issuer_err = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{err_reqid}"
                self.scnt += 1
                self.send({
                    "issuer": issuer_err, "target": target, "instance": instance,
                    "reqid": err_reqid, "c": comp, "conh": conh, "sid": self.sid,
                }, dw_err)
                log_req(f"    Sent AddNewDeck error ({len(dw_err)}b)")
                return
            cards_json = json.dumps(card_ids)
            # Persist an explicit empty map too; otherwise removing a gem can
            # leave the previous ActiveGems/gem_abilities pair on the deck.
            ag_json = json.dumps(active_gems)
            gem_abilities = self._resolve_gem_abilities(active_gems)
            ga_json = json.dumps(gem_abilities)
            deck_db_id = db_save_deck(self.user_profile["id"], deck_name, cards_json,
                pve_champion_id=pve_champion_uid, pvp_champion_guid=pvp_champ_guid,
                active_gems_json=ag_json, gem_abilities_json=ga_json,
                deck_sleeve_guid=deck_sleeve_guid, gameboard_guid=gameboard_guid, coin_guid=coin_guid)
            deck_uid = deck_db_id
            log_req(f"    Saved deck '{deck_name}' id={deck_db_id} with {len(card_ids)} cards")
            # Link this deck as the champion's last used deck (pve_champion_id is
            # the champion UID = (db_id << 8) | 12).
            if pve_champion_uid:
                champ_db_id = pve_champion_uid >> 8
                _db.execute("UPDATE champions SET last_deck_id=? WHERE id=?",
                            (deck_db_id, champ_db_id))
                _db.commit()
                log_req(f"    Set champion {champ_db_id} last_deck_id={deck_db_id}")
            deck_uid64 = (deck_uid << 8) | 17  # UID.Type.Deck=17
            # Build deck_bits manually with all 23 fields (skip ActiveGems)
            tn = ["Game.Client.Network.Profile.AddNewDeckResponse",
                  "System.String", "Game.Shared.UID", "System.UInt64",
                  "Game.Shared.Domain.deck_bits"]
            def ft2(x):
                if x not in tn: tn.append(x)
                return tn.index(x)
            sz = [] ; b = io.BytesIO()
            W = lambda s2: b.write(s2.encode("utf-8"))
            S = lambda: b.write(b";") ; L = lambda: b.write(b"\n")
            sz.append(0)
            W(""); S(); W("0"); S(); W(str(ft2(tn[0]))); S(); W("3"); S()
            # DeckName
            f = b.tell(); sz.append(0)
            W("DeckName"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.String"))); S(); W("0"); S()
            e = deck_name.encode("utf-8"); W(str(len(e))); S(); b.write(e)
            sz[-1] = b.tell() - f
            # DeckID (UID)
            f = b.tell(); sz.append(0) ; uidx = len(sz)-1
            W("DeckID"); S(); W(str(uidx)); S(); W(str(ft2("Game.Shared.UID"))); S(); W("1"); S()
            fs = b.tell(); sz.append(0) ; midx = len(sz)-1
            W("m_UID64"); S(); W(str(midx)); S(); W(str(ft2("System.UInt64"))); S(); W("0"); S()
            W(hexlify(struct.pack("<Q", deck_uid64)).decode("ascii")); S()
            sz[midx] = b.tell() - fs ; sz[uidx] = b.tell() - f
            # Deckbits (manual 22 fields — skip ActiveGems, Lock, Personality)
            dbidx = len(sz)-1
            f = b.tell(); sz.append(0) ; dbx = len(sz)-1
            W("Deckbits"); S(); W(str(dbx)); S(); W(str(ft2("Game.Shared.Domain.deck_bits"))); S(); W("22"); S()
            def wfld(name, typ, val):
                f2 = b.tell(); sz.append(0)
                W(name); S(); W(str(len(sz)-1)); S(); W(str(ft2(typ))); S()
                if typ == "System.String":
                    W("0"); S()
                    enc = val.encode("utf-8") if isinstance(val, str) else val
                    W(str(len(enc))); S(); b.write(enc)
                elif typ == "System.Boolean":
                    W("0"); S()
                    W("1" if val else "0")
                else:
                    W("0"); S()
                    W(val); S()
                sz[-1] = b.tell() - f2
            wfld("Id", "System.UInt64", hexlify(struct.pack("<Q", deck_uid)).decode("ascii"))
            # DeckName (string — manual for proper encoding)
            f2 = b.tell(); sz.append(0)
            W("DeckName"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.String"))); S(); W("0"); S()
            e2 = deck_name.encode("utf-8"); W(str(len(e2))); S(); b.write(e2)
            sz[-1] = b.tell() - f2
            wfld("PVEChampionId", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
            # ResourceId fields — use empty guid
            def wrid2(name):
                f2 = b.tell(); sz.append(0)
                W(name); S(); W(str(len(sz)-1)); S(); W(str(ft2("Game.Shared.ResourceId"))); S(); W("1"); S()
                fs2 = b.tell(); sz.append(0)
                W("guid"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.Guid"))); S(); W("0"); S()
                W("36"); S(); b.write(b"00000000-0000-0000-0000-000000000000")
                sz[-1] = b.tell() - fs2 ; sz[-2] = b.tell() - f2
            wrid2("PVPChampionId")
            for t in range(1,6): wrid2(f"talent_{t}")
            for eq in range(1,7): wrid2(f"equipment_{eq}")
            # CardsInDeck with actual card data from card_instances
            f2 = b.tell(); sz.append(0)
            W("CardsInDeck"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits"))); S(); W("0"); S()
            W(str(len(card_ids))); S()
            for ci, cid in enumerate(card_ids):
                # Look up template GUID from card_instances
                tguid = "00000000-0000-0000-0000-000000000000"
                if self.user_profile:
                    row = _db.execute("SELECT template_guid FROM card_instances WHERE user_id=? AND instance_id=?",
                                      (self.user_profile["id"], cid)).fetchone()
                    if row:
                        tguid = row[0]
                fe = b.tell(); sz.append(0); eidx = len(sz)-1
                W(str(ci)); S(); W(str(eidx)); S(); W(str(ft2("Game.Shared.Domain.card_instance_bits"))); S(); W("6"); S()
                # Id
                f1 = b.tell(); sz.append(0)
                W("Id"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.UInt64"))); S(); W("0"); S()
                W(hexlify(struct.pack("<Q", cid)).decode("ascii")); S()
                sz[-1] = b.tell() - f1
                # TemplateID
                f2r = b.tell(); sz.append(0); tidx = len(sz)-1
                W("TemplateID"); S(); W(str(tidx)); S(); W(str(ft2("Game.Shared.ResourceId"))); S(); W("1"); S()
                gs = b.tell(); sz.append(0); gidx = len(sz)-1
                W("guid"); S(); W(str(gidx)); S(); W(str(ft2("System.Guid"))); S(); W("0"); S()
                W("36"); S(); b.write(tguid.encode())
                sz[gidx] = b.tell() - gs; sz[tidx] = b.tell() - f2r
                for bname in ("IsFoil", "IsExtended", "IsNotTradeable"):
                    fb = b.tell(); sz.append(0)
                    W(bname); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.Boolean"))); S(); W("0"); S()
                    W("0"); sz[-1] = b.tell() - fb
                # EscrowStatus
                fb = b.tell(); sz.append(0)
                W("EscrowStatus"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.String"))); S(); W("0"); S()
                enc = b"Clean"; W(str(len(enc))); S(); b.write(enc)
                sz[-1] = b.tell() - fb; sz[eidx] = b.tell() - fe
            sz[-1] = b.tell() - f2
            # CardsInSideboard (empty)
            f2 = b.tell(); sz.append(0)
            W("CardsInSideboard"); S(); W(str(len(sz)-1)); S(); W(str(ft2("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits"))); S(); W("0"); S()
            W("0"); S()
            sz[-1] = b.tell() - f2
            wfld("LockHolder", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
            wrid2("deck_sleeve")
            wrid2("gameboard")
            wrid2("Coin")
            wfld("player_id", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
            sz[dbx] = b.tell() - f
            # Size table
            sz[0] = b.tell()
            W(";".join(tn)); L()
            for i, v in enumerate(sz):
                if i > 0: S()
                W(str(v))
            resp_inner = b.getvalue()
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent AddNewDeck response (deck={deck_uid}, {len(dw_bytes)}b)")

        # UpdateDeck (2095)
        elif data_type == 2095:
            log_req(">>> UpdateDeck (dt=2095)")
            deck_id = 0
            deck_name = None
            pve_champion_id = None
            pvp_champion_guid = None
            card_ids = None
            active_gems = {}
            deck_sleeve_guid = None
            gameboard_guid = None
            coin_guid = None
            if isinstance(inner_bytes, bytes):
                # DeckID
                pos = inner_bytes.find(b'DeckID')
                if pos >= 0:
                    uid_pos = inner_bytes.find(b'm_UID64', pos)
                    if uid_pos >= 0:
                        rest = inner_bytes[uid_pos:]; parts = rest.split(b';', 5)
                        if len(parts) >= 5:
                            try: deck_id = struct.unpack("<Q", unhexlify(parts[4]))[0] >> 8
                            except: pass
                # DeckName
                pos = inner_bytes.find(b'DeckName')
                if pos >= 0:
                    rest = inner_bytes[pos:]; parts = rest.split(b';', 5)
                    if len(parts) >= 5:
                        try: deck_name = parts[5][:int(parts[4])].decode('utf-8', errors='replace')
                        except: pass
                # PvEChampionId
                pos = inner_bytes.find(b'PvEChampionId')
                if pos >= 0:
                    uid_pos = inner_bytes.find(b'm_UID64', pos)
                    if uid_pos >= 0:
                        rest = inner_bytes[uid_pos:]; parts = rest.split(b';', 5)
                        if len(parts) >= 5:
                            try: pve_champion_id = struct.unpack("<Q", unhexlify(parts[4]))[0]
                            except: pass
                # PvPChampionId
                pos = inner_bytes.find(b'PvPChampionId')
                if pos >= 0:
                    guid_pos = inner_bytes.find(b'm_Guid', pos)
                    if guid_pos >= 0:
                        rest = inner_bytes[guid_pos:]; parts = rest.split(b';', 5)
                        if len(parts) >= 5:
                            try: pvp_champion_guid = parts[5][:int(parts[4])].decode('utf-8', errors='replace')
                            except: pass
                # DeckCardIDs
                pos = inner_bytes.find(b'DeckCardIDs')
                if pos >= 0:
                    rest = inner_bytes[pos:]; parts = rest.split(b';', 5)
                    if len(parts) >= 5:
                        try:
                            count = int(parts[4]); card_ids = []
                            tail = b';'.join(parts[5:])
                            for _ in range(count):
                                seg = tail.split(b';', 5)
                                if len(seg) >= 5:
                                    card_ids.append(struct.unpack("<Q", unhexlify(seg[4]))[0])
                                    tail = b';'.join(seg[5:])
                        except: pass
                # ActiveGems
                pos = inner_bytes.find(b'ActiveGems')
                if pos >= 0:
                    rest = inner_bytes[pos:]; parts = rest.split(b';', 5)
                    if len(parts) >= 5:
                        try:
                            count = int(parts[4]); tail = b';'.join(parts[5:])
                            for _ in range(count):
                                seg = tail.split(b';', 18)
                                if len(seg) >= 18:
                                    key_val = struct.unpack("<Q", unhexlify(seg[8]))[0]
                                    v_val = struct.unpack("<Q", unhexlify(seg[17]))[0] & 0xFFFFFFFF
                                    active_gems[str(key_val)] = v_val
                                    tail = b';'.join(seg[18:])
                        except: pass
                # DeckSleeveId, GameboardId, CoinId
                for fname, vname in [(b'DeckSleeveId', 'deck_sleeve'), (b'GameboardId', 'gameboard'), (b'CoinId', 'coin')]:
                    pos2 = inner_bytes.find(fname)
                    if pos2 >= 0:
                        gpos = inner_bytes.find(b'm_Guid', pos2)
                        if gpos >= 0:
                            rest = inner_bytes[gpos:]; parts = rest.split(b';', 5)
                            if len(parts) >= 5:
                                try:
                                    val = parts[5][:int(parts[4])].decode('utf-8', errors='replace')
                                    if fname == b'DeckSleeveId': deck_sleeve_guid = val
                                    elif fname == b'GameboardId': gameboard_guid = val
                                    elif fname == b'CoinId': coin_guid = val
                                except: pass
            log_req(f"    Parsed: id={deck_id}, name={deck_name}, cards={len(card_ids) if card_ids else 0}, gems={len(active_gems)}, sleeve={deck_sleeve_guid}")

            success = False
            owner_id = None
            if deck_id > 0:
                if self.user_profile:
                    owner_id = self.user_profile["id"]
                else:
                    # Reconnect path: the client can send a pending deck edit on
                    # a fresh connection before the auth handshake completes
                    # (e.g. right after a server restart — the 16:42 log shows
                    # UpdateDeck with no preceding auth:req, which used to drop
                    # the save silently).  Attribute the deck by its DB owner:
                    # the client only ever edits its own decks, and the deck id
                    # is the handle the client must already hold.
                    _orow = _db.execute(
                        "SELECT user_id FROM decks WHERE id=?",
                        (deck_id,)).fetchone()
                    owner_id = _orow[0] if _orow else None
            if owner_id:
                cards_json = json.dumps(card_ids) if card_ids is not None else None
                # Keep the denormalized cache in lockstep with ActiveGems,
                # including the empty-socket case.
                ag_json = json.dumps(active_gems)
                gem_abilities = self._resolve_gem_abilities(active_gems)
                ga_json = json.dumps(gem_abilities)
                success = db_update_deck(deck_id, owner_id,
                    deck_name=deck_name, cards_json=cards_json,
                    pve_champion_id=pve_champion_id, pvp_champion_guid=pvp_champion_guid,
                    active_gems_json=ag_json, gem_abilities_json=ga_json,
                    deck_sleeve_guid=deck_sleeve_guid, gameboard_guid=gameboard_guid, coin_guid=coin_guid)
                log_req(f"    DB update: {'OK' if success else 'FAILED'} "
                        f"(owner={owner_id})")
                # Link this deck as the champion's last used deck (pve_champion_id
                # is the champion UID = (db_id << 8) | 12).
                if pve_champion_id:
                    champ_db_id = pve_champion_id >> 8
                    _db.execute("UPDATE champions SET last_deck_id=? WHERE id=?",
                                (deck_id, champ_db_id))
                    _db.commit()
                    log_req(f"    Set champion {champ_db_id} last_deck_id={deck_id}")

            # Only report a real deck UID; on failure return invalid (updated=False
            # tells the client it didn't save).
            deck_uid64 = (deck_id << 8) | 17 if deck_id > 0 else 0
            resp_inner = encode_objfmt_response(
                ["Game.Client.Network.Profile.UpdateDeckResponse",
                 "Game.Shared.UID", "System.UInt64", "System.Boolean"],
                [("DeckID", "uid", deck_uid64),
                 ("updated", "bool", success)]
            )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({"issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent UpdateDeck response (deck={deck_id}, {len(dw_bytes)}b)")

        # Deck stubs
        elif data_type in (22013, 25021, 150000):
            log_req(f">>> Stub dt={data_type}")
            resp_inner = encode_objfmt_response([], [])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({"issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)

        # Cluster pick (140000) — resolve service UIDs
        elif data_type == 140000:
            log_req(f">>> Cluster pick (dt=140000) target={target}")
            # UID format in C#: [FieldOffset(0)] public ulong m_UID64 = type | (instance << 8)
            if target == "ServiceCampaign":
                service_uid = UID_TYPE["ServiceCampaign"]  # 253
                log_req(f"    Returning ServiceCampaign UID: {service_uid}")
            else:
                service_uid = UID_TYPE["ServiceGameSession"]  # 246
                log_req(f"    Returning ServiceGameSession UID: {service_uid}")
            env_json = json.dumps({"Kind": "pickres", "ServiceID": {"m_UID64": service_uid}})
            resp_inner = encode_objfmt_response(
                ["Game.Shared.Cluster.ClusterComms+EnvelopeR", "System.Byte[]"],
                [("Data", "bytes", env_json.encode("utf-8"))])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({"issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent Cluster pick response ({len(dw_bytes)}b)")

        # GetDeckInfo (2083)
        elif data_type == 2083:
            log_req(">>> GetDeckInfo (dt=2083)")
            # Extract deck ID from request raw bytes (UID encoded with m_UID64)
            deck_id_val = 0
            deck_id = 0
            if isinstance(inner_bytes, bytes) and b"DeckID" in inner_bytes:
                pos = inner_bytes.find(b"DeckID")
                if pos >= 0:
                    rest = inner_bytes[pos:]
                    # Find m_UID64 sub-field within the DeckID UID
                    uid_pos = rest.find(b"m_UID64")
                    if uid_pos >= 0:
                        rest2 = rest[uid_pos:]
                        parts = rest2.split(b";", 5)
                        if len(parts) >= 5:
                            try:
                                deck_id_val = struct.unpack("<Q", unhexlify(parts[4]))[0]
                                # deck_id_val is the combined UID: (deck_db_id << 8) | 17
                                deck_id = deck_id_val >> 8
                            except:
                                pass
            log_req(f"    deck_id={deck_id}")
            # Look up deck from DB
            deck_name = ""
            db_deck = None
            if self.user_profile and deck_id > 0:
                db_decks = db_get_decks(self.user_profile["id"])
                for dk in db_decks:
                    if dk["id"] == deck_id:
                        db_deck = dk
                        deck_name = dk.get("name", "")
                        break
            if not db_deck:
                # Return error response
                from objfmt_builder import ObjFmtBuilder
                b = ObjFmtBuilder("Game.Client.Network.Profile.GetDeckInfoResponse")
                b.field_enum("Error", "Game.Shared.Network.Profile.EGetDeckInfoError", 2)  # no_such_deck
                b.field_str("ErrorMessage", "Deck not found")
                resp_inner = b.finish(2)
            else:
                from objfmt_builder import ObjFmtBuilder
                b = ObjFmtBuilder("Game.Client.Network.Profile.GetDeckInfoResponse")
                b.field_str("DeckName", deck_name)
                b.field_uid("DeckID", deck_id_val)
                b.field_uid("PvEChampionId", int(db_deck.get("pve_champion_id") or 0))
                b.field_resource_id("PvPChampionId", db_deck.get("pvp_champion_guid") or "00000000-0000-0000-0000-000000000000")
                # Parse card IDs from DB
                card_ids = []
                try:
                    card_ids = json.loads(db_deck.get("cards", "[]")) if db_deck.get("cards") else []
                except: pass
                b.begin_list("DeckCardIDs", "System.Collections.Generic.List`1#System.UInt64", len(card_ids))
                for ci, cid in enumerate(card_ids):
                    b.add_list_item_uint64(ci, cid)
                b.begin_list("SideboardCardIDs", "System.Collections.Generic.List`1#System.UInt64", 0)
                b.begin_list("EquipmentIDs", "System.Collections.Generic.List`1#Game.Shared.ResourceId", 0)
                b.begin_list("TalentIDs", "System.Collections.Generic.List`1#Game.Shared.ResourceId", 0)
                b.field_resource_id("DeckSleeveId", db_deck.get("deck_sleeve_guid") or "00000000-0000-0000-0000-000000000000")
                b.field_resource_id("GameboardId", db_deck.get("gameboard_guid") or "00000000-0000-0000-0000-000000000000")
                b.field_resource_id("CoinId", db_deck.get("coin_guid") or "00000000-0000-0000-0000-000000000000")
                # ActiveGems
                active_gems = {}
                try:
                    ag_raw = db_deck.get("active_gems", "{}")
                    active_gems = json.loads(ag_raw) if ag_raw else {}
                except: pass
                b.begin_list("ActiveGems", "System.Collections.Generic.Dictionary`2#System.UInt64!Game.Shared.Mechanics.EGemTypesNew", len(active_gems))
                for gi, (k_str, v_val) in enumerate(active_gems.items()):
                    b.add_dict_entry_gem(gi, int(k_str), int(v_val))
                b.field_enum("Persona", "Game.Shared.Mechanics.EDeckPersonality", 0)
                b.field_enum("Error", "Game.Shared.Network.Profile.EGetDeckInfoError", 0)
                b.field_str("ErrorMessage", "")
                resp_inner = b.finish(15)
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({"issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent GetDeckInfo response ({len(dw_bytes)}b)")

        # GetPlayerCardIDList (2043)
        elif data_type == 2043:
            log_req(f">>> GetPlayerCardIDList (dt=2043)")
            p = self.user_profile
            db_cards = []
            if p:
                rows = _db.execute("SELECT ct.guid, ct.name, ct.cost, ct.attack, ct.defense, col.quantity "
                                   "FROM collections col JOIN card_templates ct ON ct.guid = col.card_template_id "
                                   "WHERE col.user_id=? ORDER BY ct.name", (p["id"],)).fetchall()
                db_cards = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows]
            ctype_names = [
                "Game.Client.Network.Profile.GetPlayerCardIDListResponse",
                "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                "Game.Shared.Domain.card_instance_bits",
                "Game.Shared.ResourceId", "System.Guid", "System.UInt64",
            ]
            def ft(tn):
                if tn not in ctype_names: ctype_names.append(tn)
                return ctype_names.index(tn)
            csizes = []; cbuf = io.BytesIO(); w = lambda s: cbuf.write(s.encode("utf-8")); sep = lambda: cbuf.write(b";"); lf = lambda: cbuf.write(b"\n")
            csizes.append(0)
            total_cards = sum(q for _, _, _, _, _, _, q in db_cards)
            w(""); sep(); w("0"); sep(); w(str(ft(ctype_names[0]))); sep(); w("1"); sep()
            fc = cbuf.tell(); csizes.append(0)
            w("CardDetails"); sep(); w("1"); sep(); w(str(ft(ctype_names[1]))); sep(); w("0"); sep()
            w(str(total_cards)); sep()
            card_idx = 0
            for guid, name, cost, atk, def_, qty in db_cards:
                for _ in range(qty):
                    fe = cbuf.tell(); csizes.append(0); eidx = len(csizes)-1
                    w(str(card_idx)); sep(); w(str(eidx)); sep(); w(str(ft(ctype_names[2]))); sep(); w("6"); sep()
                    f1 = cbuf.tell(); csizes.append(0)
                    w("Id"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
                    w(hexlify(struct.pack("<Q", 6000 + card_idx)).decode("ascii")); sep()
                    csizes[-1] = cbuf.tell() - f1
                    f2 = cbuf.tell(); csizes.append(0); tidx = len(csizes)-1
                    w("TemplateID"); sep(); w(str(tidx)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
                    gs = cbuf.tell(); csizes.append(0); gidx = len(csizes)-1
                    w("guid"); sep(); w(str(gidx)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
                    w("36"); sep(); cbuf.write(guid.encode())
                    csizes[gidx] = cbuf.tell() - gs; csizes[tidx] = cbuf.tell() - f2
                    f4 = cbuf.tell(); csizes.append(0)
                    w("IsFoil"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
                    w("0"); csizes[-1] = cbuf.tell() - f4
                    f5 = cbuf.tell(); csizes.append(0)
                    w("IsExtended"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
                    w("0"); csizes[-1] = cbuf.tell() - f5
                    f7 = cbuf.tell(); csizes.append(0)
                    w("IsNotTradeable"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
                    w("0"); csizes[-1] = cbuf.tell() - f7
                    f8 = cbuf.tell(); csizes.append(0)
                    w("EscrowStatus"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
                    enc = b"Clean"; w(str(len(enc))); sep(); cbuf.write(enc)
                    csizes[-1] = cbuf.tell() - f8; csizes[eidx] = cbuf.tell() - fe
                    card_idx += 1
            csizes[1] = cbuf.tell() - fc; csizes[0] = cbuf.tell()
            w(";".join(ctype_names)); lf()
            for i, s in enumerate(csizes):
                if i > 0: w(";")
                w(str(s))
            resp_inner = cbuf.getvalue()
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({"issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent GetPlayerCardIDList response ({total_cards} cards, {len(dw_bytes)}b)")
    
        # OpenCardPack (2127)
        elif data_type == 2127:
            item_id = inner_obj.get("ItemId", "{}")
            open_amount = inner_obj.get("OpenAmount", 1)
            log_req(f">>> OpenCardPack: id={item_id} amount={open_amount}")
    
            # Extract template guid from client raw bytes
            # ResourceId uses DataMember field "m_Guid" (not "guid")
            pack_guid = "0382f729-7710-432b-b761-13677982dcd2"
            if isinstance(inner_bytes, bytes) and b"ItemId" in inner_bytes:
                pos = inner_bytes.find(b"ItemId")
                if pos >= 0:
                    rest = inner_bytes[pos:]
                    for fname in (b"m_Guid", b"guid"):
                        gpos = rest.find(fname)
                        if gpos >= 0:
                            rest2 = rest[gpos:]
                            parts = rest2.split(b";", 5)
                            if len(parts) >= 5 and parts[4].isdigit():
                                try:
                                    guid_len = int(parts[4])
                                    tail = b";".join(rest2.split(b";", 5)[:5])
                                    remain = rest2[len(tail)+1:]
                                    if len(remain) >= guid_len:
                                        pack_guid = remain[:guid_len].decode("ascii")
                                        break
                                except:
                                    pass
            log_req(f">>> OpenCardPack: pack_guid={pack_guid} amount={open_amount}")

            # Map store item GUID to card set GUID — query pack_set_map table
            row = _db.execute(
                "SELECT set_guid, is_full_set, is_primal FROM pack_set_map "
                "WHERE pack_guid=?", (pack_guid,)).fetchone()
            if row:
                set_guid, is_full_set, is_primal = row
            else:
                set_guid = pack_guid  # fallback: treat pack GUID as set GUID
                is_full_set = 0
                is_primal = 0
            log_req(f"    pack_guid={pack_guid} → set_guid={set_guid} full={is_full_set} primal={is_primal}")

            # Validate player owns this pack
            pack_error = None
            if self.user_profile:
                existing = _db.execute("SELECT id, quantity, client_item_uid FROM player_inventory WHERE user_id=? AND template_guid=?",
                                       (self.user_profile["id"], pack_guid)).fetchone()
                if not existing or existing[1] < open_amount:
                    pack_error = "NotEnoughInventory"
                    log_req(f"    Pack not in inventory! (have={existing[1] if existing else 0}, want={open_amount})")
            
            # Generate card instances for the pack
            card_templates = _load_card_templates()
            all_cards = []
            if not pack_error:
                if is_full_set:
                    # Grant every PVP card from the set (4x each)
                    pool = card_templates.get(set_guid, [])
                    pool = _full_set_pool(pool)
                    for _ in range(open_amount):
                        for c in pool:
                            for _ in range(4):  # 4x each card
                                all_cards.append((c[0], c[1], c[3], c[4], c[5]))
                    log_req(f"    Full set: {len(pool)} unique cards x4 = {len(all_cards)} total")
                else:
                    for _ in range(open_amount):
                        all_cards.extend(_generate_booster(card_templates, set_guid))
    
            # Track instance IDs for response encoding (fallback: 5000+)
            card_instance_ids = [5000 + i for i in range(len(all_cards))]
            chest_rarity = None
            chest_db_id = None
    
            # Persist generated cards to user's collection
            if self.user_profile and not pack_error:
                for guid, _, _, _, _ in all_cards:
                    db_add_card(self.user_profile["id"], guid)
    
                # Remove the booster pack from inventory
                pack_client_uid = existing[2] if existing else 0
                if existing and existing[1] > 0:
                    new_qty = existing[1] - open_amount
                    if new_qty <= 0:
                        _db.execute("DELETE FROM player_inventory WHERE id=?", (existing[0],))
                    else:
                        _db.execute("UPDATE player_inventory SET quantity=? WHERE id=?", (new_qty, existing[0]))
                    _db.commit()
                    log_req(f"    Consumed {open_amount}x pack {pack_guid} from inventory (remaining={max(0, new_qty)})")
                    # Push inventory update so client pack count decrements
                    if pack_client_uid:
                        self.push_inventory_to_client(qty=max(0, new_qty), template_guid=pack_guid, item_id=pack_client_uid)
    
                # Persist new cards to card_instances and get their instance IDs
                max_id = 5000
                exist_row = _db.execute("SELECT MAX(instance_id) FROM card_instances WHERE user_id=?",
                                        (self.user_profile["id"],)).fetchone()
                if exist_row and exist_row[0]:
                    max_id = max(max_id, exist_row[0] + 1)
                new_card_data = []
                for i, (guid, name, cost, atk, def_) in enumerate(all_cards):
                    cid = max_id + i
                    _db.execute("INSERT OR IGNORE INTO card_instances (user_id, instance_id, template_guid) VALUES (?,?,?)",
                                (self.user_profile["id"], cid, guid))
                    new_card_data.append((guid, name, cost, atk, def_, cid, 0))
                    card_instance_ids[i] = cid  # Update for response encoding
                _db.commit()
                log_req(f"    Created {len(new_card_data)} card_instances IDs {new_card_data[0][5]}-{new_card_data[-1][5]}")

                # Push these specific new cards to client via ProfileGenericUpdate (adds to CardList)
                self.push_opened_cards_via_generic(new_card_data)

                # Generate treasure chest for this pack opening
                import random as _rand
                probs = _db.execute("SELECT rarity, weight FROM chest_probabilities").fetchall()
                if probs:
                    total_weight = sum(p[1] for p in probs)
                    roll = _rand.randint(1, total_weight)
                    cumulative = 0
                    for rarity, weight in probs:
                        cumulative += weight
                        if roll <= cumulative:
                            chest_rarity = rarity
                            break
                    if chest_rarity:
                        cid = _db.execute("INSERT INTO treasure_chests (user_id, set_guid, chest_rarity) VALUES (?,?,?)",
                                          (self.user_profile["id"], set_guid, chest_rarity))
                        _db.commit()
                        chest_db_id = cid.lastrowid
                        log_req(f"    Generated {chest_rarity} chest id={chest_db_id}")
    
            # Encode OpenCardPackResponse using builder
            from objfmt_builder import ObjFmtBuilder
            b = ObjFmtBuilder("Game.Client.Network.Profile.OpenCardPackResponse")

            # NewCardInstances
            card_count = len(all_cards)
            b.begin_list("NewCardInstances",
                "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", card_count)
            for i, (tguid, tname, cost, atk, def_) in enumerate(all_cards):
                b.begin_element(i, "Game.Shared.Domain.card_instance_bits", 6)
                b.card_fields(tguid, card_instance_ids[i])

            # NewGemInstances (empty)
            b.begin_list("NewGemInstances",
                "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits", 0)

            # NewChestInstances
            if chest_rarity and chest_db_id:
                b.begin_list("NewChestInstances",
                    "System.Collections.Generic.List`1#Game.Shared.Domain.chest_bits", 1)
                b.begin_element(0, "Game.Shared.Domain.chest_bits", 8)
                chest_map = {"Common":0, "Uncommon":1, "Rare":2, "Legendary":3, "Primal":4}
                b.chest_fields(chest_map.get(chest_rarity, 0), 2, set_guid, 9000 + chest_db_id)
                # Override BoosterPackType with set_guid
            else:
                b.begin_list("NewChestInstances",
                    "System.Collections.Generic.List`1#Game.Shared.Domain.chest_bits", 0)

            # Error + ErrorMessage
            error_val = 5 if pack_error else 0
            error_msg = "Not enough inventory" if pack_error else ""
            b.field_enum("Error", "Game.Shared.Network.Profile.EOpenCardPackError", error_val)
            b.field_str("ErrorMessage", error_msg)

            resp_inner = b.finish(5)
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent OpenCardPack response ({len(all_cards)} cards, {len(dw_bytes)}b)")
    
        # DeleteChampion (2035)
        elif data_type == 2035:
            champ_id = int(inner_obj.get("ChampionId", 0))
            log_req(f">>> DeleteChampion: id={champ_id}")
            if self.user_profile and champ_id > 0:
                _db.execute("UPDATE champions SET is_deleted=1 WHERE id=? AND user_id=?", (champ_id, self.user_profile["id"]))
                _db.commit()
                log_req(f"    Marked champion {champ_id} as deleted")
            
            # Encode DeleteChampionResponse (ChampionBits + Error + ErrorMessage)
            tnames = ["Game.Client.Network.Profile.DeleteChampionResponse",
                       "Game.Shared.Domain.champion_bits",
                       "Game.Shared.Network.Profile.EDeleteChampionError",
                       "System.Int32", "System.String", "System.UInt64"]
            def dft(tn):
                if tn not in tnames: tnames.append(tn)
                return tnames.index(tn)
            dsizes = []; dbuf = io.BytesIO()
            dw = lambda s: dbuf.write(s.encode("utf-8"))
            dsep = lambda: dbuf.write(b";"); dlf = lambda: dbuf.write(b"\n")
            dsizes.append(0)
            dw(""); dsep(); dw("0"); dsep(); dw(str(dft(tnames[0]))); dsep(); dw("3"); dsep()
            # ChampionBits
            fc = dbuf.tell(); dsizes.append(0)
            dw("ChampionBits"); dsep(); dw(str(len(dsizes)-1)); dsep(); dw(str(dft(tnames[1]))); dsep(); dw("1"); dsep()
            fc_id = dbuf.tell(); dsizes.append(0)
            dw("Id"); dsep(); dw(str(len(dsizes)-1)); dsep(); dw(str(dft("System.UInt64"))); dsep(); dw("0"); dsep()
            dw(hexlify(struct.pack("<Q", champ_id)).decode("ascii")); dsep()
            dsizes[-1] = dbuf.tell() - fc_id
            dsizes[-2] = dbuf.tell() - fc
            # Error enum
            fe = dbuf.tell(); dsizes.append(0)
            dw("Error"); dsep(); dw(str(len(dsizes)-1)); dsep(); dw(str(dft(tnames[2]))); dsep(); dw("1"); dsep()
            fes = dbuf.tell(); dsizes.append(0)
            dw("value__"); dsep(); dw(str(len(dsizes)-1)); dsep(); dw(str(dft(tnames[3]))); dsep(); dw("0"); dsep()
            dw(hexlify(struct.pack("<i", 0)).decode("ascii")); dsep()
            dsizes[-1] = dbuf.tell() - fes
            dsizes[-2] = dbuf.tell() - fe
            # ErrorMessage
            fe2 = dbuf.tell(); dsizes.append(0)
            dw("ErrorMessage"); dsep(); dw(str(len(dsizes)-1)); dsep(); dw(str(dft(tnames[4]))); dsep(); dw("0"); dsep()
            dw("0"); dsep()
            dsizes[-1] = dbuf.tell() - fe2
            dsizes[0] = dbuf.tell()
            dw(";".join(tnames)); dlf()
            for i, s in enumerate(dsizes):
                if i > 0: dw(";")
                dw(str(s))
            
            resp_inner = dbuf.getvalue()
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent DeleteChampionResponse ({len(dw_bytes)}b)")

        # AddChampion (2033)
        elif data_type == 2033:
            champ_name = inner_obj.get("ChampionName", "Unknown")
            pet_name = str(inner_obj.get("PetName", "") or "")
            owner_champ_id = 0
            try:
                if "OwnerChampionId" in inner_obj:
                    o = inner_obj["OwnerChampionId"]
                    owner_champ_id = int(o) if isinstance(o, str) and o.isdigit() else int(o)
            except: pass
            race_val = 1
            cls_val = 3
            gender_val = 1
            try:
                if "Race" in inner_obj:
                    r = inner_obj["Race"]
                    race_val = int(r[1]) if isinstance(r, list) else int(r.split(":'")[1].split("'")[0]) if isinstance(r, str) else int(r.get("value__", 1))
            except: pass
            try:
                if "ChampionClass" in inner_obj:
                    c = inner_obj["ChampionClass"]
                    cls_val = int(c[1]) if isinstance(c, list) else int(c.split(":'")[1].split("'")[0]) if isinstance(c, str) else int(c.get("value__", 3))
            except: pass
            try:
                if "Gender" in inner_obj:
                    g = inner_obj["Gender"]
                    gender_val = int(g[1]) if isinstance(g, list) else int(g.split(":'")[1].split("'")[0]) if isinstance(g, str) else int(g.get("value__", 1))
            except: pass
            log_req(f">>> AddChampion: name={champ_name} pet={pet_name!r} race={race_val} class={cls_val} gender={gender_val}")

            starting_talents = _default_talents_for_champion(
                race_val, cls_val, gender_val)

            if self.user_profile:
                cur = _db.execute(
                    "INSERT INTO champions "
                    "(user_id, champion_name, race, champion_class, gender, pet_name, talents) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (self.user_profile["id"], champ_name, race_val, cls_val,
                     gender_val, pet_name, json.dumps(starting_talents)))
                _db.commit()
                champ_db_id = cur.lastrowid

                # Grant starter deck cards for this race (once only per race)
                race_name = _RACE_DECK_MAP.get(race_val)
                if race_name and race_name in _STARTER_DECKS:
                    deck = _STARTER_DECKS[race_name]
                    # Check if this PvE starter deck was already granted
                    pve_template_guid = _PVE_CHAMPION_GUIDS.get(race_name, "")
                    already_granted = False
                    if pve_template_guid:
                        existing_purchase = _db.execute(
                            "SELECT id FROM store_purchases WHERE user_id=? AND item_template_id=?",
                            (self.user_profile["id"], pve_template_guid)).fetchone()
                        if existing_purchase:
                            already_granted = True
                    if not already_granted:
                        cards_granted = 0
                        for card_guid, count in deck["cards"]:
                            existing = _db.execute("SELECT quantity FROM collections WHERE user_id=? AND card_template_id=?",
                                                   (self.user_profile["id"], card_guid)).fetchone()
                            if existing:
                                _db.execute("UPDATE collections SET quantity=quantity+? WHERE user_id=? AND card_template_id=?",
                                            (count, self.user_profile["id"], card_guid))
                            else:
                                _db.execute("INSERT INTO collections (user_id, card_template_id, quantity) VALUES (?,?,?)",
                                            (self.user_profile["id"], card_guid, count))
                            cards_granted += count
                        # Create card_instances for each individual card
                        max_id = 5000
                        exist_row = _db.execute("SELECT MAX(instance_id) FROM card_instances WHERE user_id=?",
                                                (self.user_profile["id"],)).fetchone()
                        if exist_row and exist_row[0]:
                            max_id = max(max_id, exist_row[0] + 1)
                        cid = max_id
                        for card_guid, count in deck["cards"]:
                            for _ in range(count):
                                _db.execute("INSERT OR IGNORE INTO card_instances (user_id, instance_id, template_guid) VALUES (?,?,?)",
                                            (self.user_profile["id"], cid, card_guid))
                                cid += 1
                        # Record as a free grant in store_purchases
                        if pve_template_guid:
                            _db.execute("INSERT INTO store_purchases (user_id, item_name, item_template_id, price, currency) VALUES (?,?,?,?,?)",
                                        (self.user_profile["id"], f"PvE {race_name} Starter", pve_template_guid, 0, "Free"))
                        _db.commit()
                        log_req(f"    Granted {cards_granted} starter deck cards for {race_name} (first grant)")
                        self.push_cards_to_client()
                    else:
                        log_req(f"    Starter deck for {race_name} already granted — skipping")

                    # Every newly created champion needs its own starter deck,
                    # even when this user already received the race's cards.
                    cards_list = []
                    for card_guid, count in deck["cards"]:
                        cards_list.extend([card_guid] * count)
                    cards_json = json.dumps(cards_list)
                    deck_db_id = db_save_deck(
                        self.user_profile["id"],
                        f"{champ_name}'s {race_name} Deck",
                        cards_json,
                        pve_champion_id=champ_db_id)
                    _db.execute("UPDATE champions SET last_deck_id=? WHERE id=?", (deck_db_id, champ_db_id))
                    _db.commit()
                    log_req(f"    Auto-created deck id={deck_db_id} for champion {champ_db_id}")
            else:
                champ_db_id = 1

            champ_uid64 = (champ_db_id << 8) | 12  # 12 = UID.Type.Champion

            # Read back the champion's actual last_deck_id (set by the auto-created
            # starter deck above, or 0 if the deck was already granted previously).
            ldid_row = _db.execute(
                "SELECT last_deck_id, pet_name FROM champions WHERE id=?", (champ_db_id,)).fetchone()
            resp_last_deck_id = ldid_row[0] if ldid_row else 0
            resp_pet_name = ldid_row[1] if ldid_row else pet_name

            # Manual ObjFmt encode: AddChampionResponse
            type_names = [
                "Game.Client.Network.Profile.AddChampionResponse",
                "Game.Shared.Domain.champion_bits",
                "Game.Shared.Domain.deck_bits",
                "Game.Shared.Network.Profile.EAddChampionError",
                "System.UInt64", "System.String", "System.Int32",
                "Game.Shared.Mechanics.EChampionClass",
                "Game.Shared.Mechanics.ERace",
                "Game.Shared.Mechanics.EGender",
                "System.Boolean",
                "Game.Shared.ResourceId", "System.Guid",
                "System.Collections.Generic.List`1#Game.Shared.ResourceId",
            ]
            def ft(tn):
                if tn not in type_names: type_names.append(tn)
                return type_names.index(tn)

            sizes = []; buf = io.BytesIO()
            w = lambda s: buf.write(s.encode("utf-8"))
            sep = lambda: buf.write(b";"); lf = lambda: buf.write(b"\n")

            def wf_bool(name, val):
                """Write a bool field. Client reads exactly 1 byte, no trailing ;."""
                sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
                w(name); sep(); w(str(fi)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
                w("1" if val else "0")
                sizes[fi] = buf.tell() - f_start

            def wf(name, type_name, val_hex, num_props=0):
                """Write a simple field header + value."""
                sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
                w(name); sep(); w(str(fi)); sep(); w(str(ft(type_name))); sep(); w(str(num_props)); sep()
                w(val_hex); sep()
                sizes[fi] = buf.tell() - f_start

            def wf_enum(name, enum_type, val):
                """Write an enum field with value__ sub-field."""
                sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
                w(name); sep(); w(str(fi)); sep(); w(str(ft(enum_type))); sep(); w("1"); sep()
                sizes.append(0); si = len(sizes)-1; fsub_start = buf.tell()
                w("value__"); sep(); w(str(si)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
                w(hexlify(struct.pack("<i", val)).decode("ascii")); sep()
                sizes[si] = buf.tell() - fsub_start
                sizes[fi] = buf.tell() - f_start

            def wf_str(name, val):
                enc = val.encode("utf-8")
                sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
                w(name); sep(); w(str(fi)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
                w(str(len(enc))); sep(); buf.write(enc)
                sizes[fi] = buf.tell() - f_start

            # --- Root: AddChampionResponse, 4 sub-fields ---
            sizes.append(0)
            w(""); sep(); w("0"); sep(); w(str(ft(type_names[0]))); sep(); w("4"); sep()

            # ChampionBits - all 17 supported fields, including the authored
            # mandatory talents needed for the initial talent-point display.
            sizes.append(0); fi = len(sizes)-1; fc_start = buf.tell()
            w("ChampionBits"); sep(); w(str(fi)); sep(); w(str(ft(type_names[1]))); sep(); w("17"); sep()
            wf_str("Name", champ_name)
            wf("Id", "System.UInt64", hexlify(struct.pack("<Q", champ_db_id)).decode("ascii"))
            wf("Level", "System.Int32", hexlify(struct.pack("<i", 1)).decode("ascii"))
            wf("CurrentXP", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
            wf_enum("ChampionClass", "Game.Shared.Mechanics.EChampionClass", cls_val)
            wf_enum("Race", "Game.Shared.Mechanics.ERace", race_val)
            wf_enum("Gender", "Game.Shared.Mechanics.EGender", gender_val)
            wf("OwnerChampionId", "System.UInt64", hexlify(struct.pack("<Q", owner_champ_id)).decode("ascii"))
            wf("LastCampaignID", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
            wf("LastDeckID", "System.UInt64", hexlify(struct.pack("<Q", resp_last_deck_id)).decode("ascii"))
            wf_bool("IsDeleted", False)
            wf("LastRespec", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
            wf("FreeRespec", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
            wf("RespecGoldCost", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
            wf_str("PetName", resp_pet_name or "")
            wf("DeckTemplateId", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))

            def wf_rid_list(name, guids):
                sizes.append(0); list_idx = len(sizes)-1; list_start = buf.tell()
                w(name); sep(); w(str(list_idx)); sep()
                w(str(ft("System.Collections.Generic.List`1#Game.Shared.ResourceId")))
                sep(); w("0"); sep(); w(str(len(guids))); sep()
                for i, guid_value in enumerate(guids):
                    element_start = buf.tell(); sizes.append(0); element_idx = len(sizes)-1
                    w(str(i)); sep(); w(str(element_idx)); sep()
                    w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
                    field_start = buf.tell(); sizes.append(0); field_idx = len(sizes)-1
                    w("m_Guid"); sep(); w(str(field_idx)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
                    encoded_guid = str(guid_value).encode("utf-8")
                    w(str(len(encoded_guid))); sep(); buf.write(encoded_guid)
                    sizes[field_idx] = buf.tell() - field_start
                    sizes[element_idx] = buf.tell() - element_start
                sizes[list_idx] = buf.tell() - list_start

            wf_rid_list("ChampionTalents", starting_talents)
            sizes[fi] = buf.tell() - fc_start

            # DeckBits
            sizes.append(0); fi = len(sizes)-1; fd_start = buf.tell()
            w("DeckBits"); sep(); w(str(fi)); sep(); w(str(ft(type_names[2]))); sep(); w("3"); sep()
            # The client caches this deck using the ID and later looks it up via
            # ChampionBits.LastDeckID, so this must be the persisted deck ID.
            wf("Id", "System.UInt64", hexlify(struct.pack("<Q", resp_last_deck_id)).decode("ascii"))
            wf_str("DeckName", f"{champ_name}'s Starter")
            wf("PVEChampionId", "System.UInt64", hexlify(struct.pack("<Q", champ_db_id)).decode("ascii"))
            sizes[fi] = buf.tell() - fd_start

            # Error (enum)
            wf_enum("Error", "Game.Shared.Network.Profile.EAddChampionError", 0)

            # ErrorMessage
            wf_str("ErrorMessage", "")

            sizes[0] = buf.tell()
            w(";".join(type_names)); lf()
            for i, s in enumerate(sizes):
                if i > 0: w(";")
                w(str(s))

            resp_inner = buf.getvalue()
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent AddChampionResponse (id={champ_db_id}, name={champ_name}, {len(dw_bytes)}b)")

        # UpdateChampionDeckID (2187)
        elif data_type == 2187:
            log_req(">>> UpdateChampionDeckID (dt=2187)")
            champ_uid = 0; deck_id = 0; auth_id = 0
            if isinstance(inner_obj, dict):
                champ_uid = int(inner_obj.get("ChampionID", 0))
                deck_id = int(inner_obj.get("LastDeckID", 0))
                auth_id = int(inner_obj.get("AuthID", 0))
            elif isinstance(inner_bytes, bytes):
                for fname, key in [(b"AuthID", "auth"),
                                   (b"ChampionID", "champ"),
                                   (b"LastDeckID", "deck")]:
                    pos = inner_bytes.find(fname)
                    if pos >= 0:
                        rest = inner_bytes[pos:]; parts = rest.split(b";", 5)
                        if len(parts) >= 5:
                            try:
                                val = struct.unpack("<Q", unhexlify(parts[4]))[0]
                                if key == "auth": auth_id = val
                                elif key == "champ": champ_uid = val
                                else: deck_id = val
                            except: pass
            # On reconnect the client may send this profile update before the
            # auth request. Restore the profile from its stable SAuthID so a
            # following campaign setup has the correct database owner.
            if not self.user_profile and auth_id:
                self.user_profile = db_get_user_by_client_auth_id(auth_id)
                if self.user_profile:
                    self._set_client_identity_from_profile()
                    log_req(f"    Restored profile for auth id {auth_id}: "
                            f"user={self.user_profile['id']}")
            champ_db_id = champ_uid >> 8 if champ_uid > 0xff else champ_uid
            log_req(f"    ChampionID={champ_uid} (db={champ_db_id}) LastDeckID={deck_id}")
            if self.user_profile and champ_db_id > 0:
                _db.execute("UPDATE champions SET last_deck_id=? WHERE id=? AND user_id=?",
                            (deck_id, champ_db_id, self.user_profile["id"]))
                _db.commit()
                log_req(f"    Updated champion {champ_db_id} last_deck_id={deck_id}")
            # Return success response with Error=Ok
            resp_inner = encode_objfmt_response(
                ["Game.Client.Network.Profile.UpdateChampionDeckIDResponse",
                 "Game.Shared.Network.Profile.EUpdateChampionDeckIDError"],
                [("Error", "enum1", ("Game.Shared.Network.Profile.EUpdateChampionDeckIDError", 0))])
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({"issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent UpdateChampionDeckID response ({len(dw_bytes)}b)")

        # UpdateChampionTalents (2037)
        elif data_type == 2037:
            log_req(">>> UpdateChampionTalents (dt=2037)")
            # Parse ChampionId (ulong) + Talents (List<ResourceId> GUIDs) from the
            # request. The client always sends the champion's full talent list, so
            # we just persist it — no server-side defaults needed.
            champ_id = None
            talents = []
            if isinstance(inner_bytes, bytes):
                import re as _r
                m = _r.search(rb"ChampionId;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});", inner_bytes)
                if m:
                    try:
                        champ_id = struct.unpack('<Q', bytes.fromhex(m.group(1).decode()))[0]
                    except Exception:
                        champ_id = None
                tpos = inner_bytes.find(b"Talents")
                if tpos >= 0:
                    for gm in _r.finditer(
                            rb"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
                            inner_bytes[tpos:]):
                        talents.append(gm.group(1).decode())
            champ_name = ""
            race_val = 1; cls_val = 3; gender_val = 1; level_val = 1
            last_deck = 0
            if champ_id is not None:
                _db.execute("UPDATE champions SET talents=? WHERE id=? AND user_id=?",
                            (json.dumps(talents), champ_id, self.user_profile["id"] if self.user_profile else 0))
                _db.commit()
                log_req(f"    Saved talents for champion {champ_id}: {talents}")
                crow = _db.execute(
                    "SELECT champion_name, race, champion_class, gender, level, last_deck_id, pet_name FROM champions WHERE id=?",
                    (champ_id,)).fetchone()
                if crow:
                    champ_name = crow[0] or ""
                    race_val = crow[1] or 1
                    cls_val = crow[2] or 3
                    gender_val = crow[3] or 1
                    last_deck = crow[5] or 0
                    level_val = crow[4] or 1
                    pet_name = crow[6] or ""
            else:
                log_req(f"    Could not parse champion id / talents ({len(talents)} guids found)")

            # Build the response with a full ChampionBits (incl. ChampionTalents)
            # so the client's HandleTalentsUpdate can update the champion.
            type_names = [
                "Game.Client.Network.Profile.UpdateChampionTalentsResponse",
                "Game.Shared.Domain.champion_bits",
                "Game.Shared.Network.Profile.EUpdateChampionTalentsError",
                "System.UInt64", "System.String", "System.Int32",
                "Game.Shared.Mechanics.EChampionClass",
                "Game.Shared.Mechanics.ERace",
                "Game.Shared.Mechanics.EGender",
                "System.Boolean",
                "Game.Shared.ResourceId", "System.Guid",
                "System.Collections.Generic.List`1#Game.Shared.ResourceId",
            ]
            def ft(tn):
                if tn not in type_names: type_names.append(tn)
                return type_names.index(tn)

            sizes = []; buf = io.BytesIO()
            w = lambda s: buf.write(s.encode("utf-8"))
            sep = lambda: buf.write(b";"); lf = lambda: buf.write(b"\n")

            def wf_bool(name, val):
                sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
                w(name); sep(); w(str(fi)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
                w("1" if val else "0")
                sizes[fi] = buf.tell() - f_start

            def wf(name, type_name, val_hex, num_props=0):
                sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
                w(name); sep(); w(str(fi)); sep(); w(str(ft(type_name))); sep(); w(str(num_props)); sep()
                w(val_hex); sep()
                sizes[fi] = buf.tell() - f_start

            def wf_enum(name, enum_type, val):
                sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
                w(name); sep(); w(str(fi)); sep(); w(str(ft(enum_type))); sep(); w("1"); sep()
                sizes.append(0); si = len(sizes)-1; fsub_start = buf.tell()
                w("value__"); sep(); w(str(si)); sep(); w(str(ft("System.Int32"))); sep(); w("0"); sep()
                w(hexlify(struct.pack("<i", val)).decode("ascii")); sep()
                sizes[si] = buf.tell() - fsub_start
                sizes[fi] = buf.tell() - f_start

            def wf_str(name, val):
                enc = val.encode("utf-8")
                sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
                w(name); sep(); w(str(fi)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
                w(str(len(enc))); sep(); buf.write(enc)
                sizes[fi] = buf.tell() - f_start

            def wf_rid_list(name, guids):
                """List<ResourceId> — each element uses ResourceId.m_Guid."""
                sizes.append(0); fi = len(sizes)-1; f_start = buf.tell()
                w(name); sep(); w(str(fi)); sep(); w(str(ft("System.Collections.Generic.List`1#Game.Shared.ResourceId"))); sep(); w("0"); sep()
                w(str(len(guids))); sep()
                for i, g in enumerate(guids):
                    felem = buf.tell(); slot = len(sizes); sizes.append(0)
                    w(str(i)); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
                    fsub = buf.tell(); sizes.append(0)
                    w("m_Guid"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
                    gb = g.encode("utf-8")
                    w(str(len(gb))); sep(); buf.write(gb)
                    sizes[-1] = buf.tell() - fsub
                    sizes[slot] = buf.tell() - felem
                sizes[fi] = buf.tell() - f_start

            # --- Root: UpdateChampionTalentsResponse, 3 sub-fields ---
            sizes.append(0)
            w(""); sep(); w("0"); sep(); w(str(ft(type_names[0]))); sep(); w("3"); sep()

            # ChampionBits (17 fields incl. ChampionTalents)
            sizes.append(0); fi = len(sizes)-1; fc = buf.tell()
            w("ChampionBits"); sep(); w(str(fi)); sep(); w(str(ft(type_names[1]))); sep(); w("17"); sep()
            wf_str("Name", champ_name)
            wf("Id", "System.UInt64", hexlify(struct.pack("<Q", champ_id or 0)).decode("ascii"))
            wf("Level", "System.Int32", hexlify(struct.pack("<i", level_val)).decode("ascii"))
            wf("CurrentXP", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
            wf_enum("ChampionClass", "Game.Shared.Mechanics.EChampionClass", cls_val)
            wf_enum("Race", "Game.Shared.Mechanics.ERace", race_val)
            wf_enum("Gender", "Game.Shared.Mechanics.EGender", gender_val)
            wf("OwnerChampionId", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
            wf("LastCampaignID", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
            wf("LastDeckID", "System.UInt64", hexlify(struct.pack("<Q", last_deck)).decode("ascii"))
            wf_bool("IsDeleted", False)
            wf("LastRespec", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
            wf("FreeRespec", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
            wf("RespecGoldCost", "System.Int32", hexlify(struct.pack("<i", 0)).decode("ascii"))
            wf_str("PetName", pet_name)
            wf("DeckTemplateId", "System.UInt64", hexlify(struct.pack("<Q", 0)).decode("ascii"))
            wf_rid_list("ChampionTalents", talents)
            sizes[fi] = buf.tell() - fc

            # Error (enum) + ErrorMessage (string)
            wf_enum("Error", "Game.Shared.Network.Profile.EUpdateChampionTalentsError", 0)
            wf_str("ErrorMessage", "")

            sizes[0] = buf.tell()
            w(";".join(type_names)); lf()
            for i, s in enumerate(sizes):
                if i > 0: w(";")
                w(str(s))

            resp_inner = buf.getvalue()
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({"issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid}, dw_bytes)
            log_req(f"    Sent UpdateChampionTalents response ({len(dw_bytes)}b)")

        # ServiceProfile — UpgradeExtendedArtOnCard (2185)
        elif data_type == 2185:
            log_req(">>> UpgradeExtendedArtOnCard (dt=2185)")
            card_instance_id = int(inner_obj.get("CardInstanceId", 0))
            user_id = self.user_profile["id"] if self.user_profile else 0
            row = _db.execute("SELECT id, template_guid, is_extended_art FROM card_instances WHERE user_id=? AND instance_id=?",
                              (user_id, card_instance_id)).fetchone()
            if not row:
                log_req(f"    Card instance {card_instance_id} not found for user {user_id}")
                resp_inner = encode_objfmt_response(
                    ["Game.Client.Network.Profile.UpgradeExtendedArtOnCardResponse",
                     "System.UInt64", "System.UInt64"],
                    [("StardustInventoryId", "ulong", 0),
                     ("CardInstanceId", "ulong", card_instance_id)]
                )
            elif row[2]:
                log_req(f"    Card instance {card_instance_id} already extended art")
                resp_inner = encode_objfmt_response(
                    ["Game.Client.Network.Profile.UpgradeExtendedArtOnCardResponse",
                     "System.UInt64", "System.UInt64"],
                    [("StardustInventoryId", "ulong", 0),
                     ("CardInstanceId", "ulong", card_instance_id)]
                )
            else:
                _db.execute("UPDATE card_instances SET is_extended_art=1 WHERE user_id=? AND instance_id=?",
                            (user_id, card_instance_id))
                _db.commit()
                log_req(f"    Upgraded card instance {card_instance_id} to extended art")
                resp_inner = encode_objfmt_response(
                    ["Game.Client.Network.Profile.UpgradeExtendedArtOnCardResponse",
                     "System.UInt64", "System.UInt64"],
                    [("StardustInventoryId", "ulong", card_instance_id),
                     ("CardInstanceId", "ulong", card_instance_id)]
                )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent UpgradeExtendedArtOnCard ({len(dw_bytes)}b)")

         # GameSession Service — PlayerTransaction (3029)
        elif data_type == 3029:
            player_uid = make_uid(UID_TYPE["ServicePlayer"], int(self.client_reck_id))
            session = game_session.find_session_by_player(player_uid)
            self._ai_turn_depth = 0  # reset turn-advance recursion guard
            transaction_capture_id = None

            # Decode the raw transaction once at the application boundary;
            # execution below consumes typed flags without reparsing bytes.
            transaction = classify_player_transaction(inner_bytes)
            txn_info = transaction.fields
            txn_id_str = txn_info.get("m_TransactionId", "?")
            txn_id_int = transaction.transaction_id
            quit_series = txn_info.get("m_QuitEntireSeries", "?")

            # PassPriorityTransaction.m_TurnPhase — the phase the client thinks it
            # is in. Used to ignore stale auto-passes from phases the server has
            # already auto-advanced past.
            pass_turn_phase = transaction.pass_turn_phase

            # Detect the client's phase-stop preference (SetTurnPhasesTransaction).
            is_set_stops = transaction.is_set_stops
            
            # Detect mulligan keep/redraw by transaction type name
            is_mulligan_keep = transaction.is_mulligan_keep
            is_mulligan_redraw = transaction.is_mulligan_redraw
            
            # Detect DebugCheatTransaction
            is_cheat = transaction.is_cheat

            # Detect priority pass (player clicked Pass Priority).
            is_pass_priority = transaction.is_pass_priority

            # Detect the Play/Draw pick (ChoosePlayTransaction / ChooseDrawTransaction).
            is_choose_pick = transaction.is_choose_pick

            is_discard = transaction.is_discard

            is_ability_activate = transaction.is_ability_activate

            # Follow-up to a class-23 AbilityActivationDataRequired prompt: the
            # player supplied targets (e.g. chose a card to discard) for an
            # ability already resolving.
            is_set_ability_data = transaction.is_set_ability_data

            # Combat: player declared attackers (CommitTroopsToAttackTransaction).
            is_commit_attack = transaction.is_commit_attack

            # Combat: player declared blockers during the AI's turn
            # (CommitTroopsToDefenseTransaction).
            is_commit_defense = transaction.is_commit_defense

            # F10 / "Skip": the client asks to auto-pass priority to the end of
            # its own turn (SetAutoPassTransaction, m_PassingState EndOfTurn=2 /
            # EndPhase=3 / Attack=1) or cancels it (CancelAutoPassTransaction).
            is_set_auto_pass = transaction.is_set_auto_pass
            is_cancel_auto_pass = transaction.is_cancel_auto_pass

            # Combat: player assigned damage order (AssignDamageOrderTransaction) —
            # the client auto-sends this at the AssignDamage step when no combat
            # has blockers (BattleStateAssignDamage auto-commits).
            is_assign_damage = transaction.is_assign_damage

            # Priority resync: the client lost priority tracking (e.g. a
            # TurnPhaseUpdated arrived without a preceding GreenLight) and asks the
            # server to re-send the current greenlight. Must be handled explicitly —
            # falling through to the card-play handler re-pushes MAIN-phase options
            # (making hand cards playable mid-combat) and clobbers attack options.
            is_priority_sync = transaction.is_priority_sync

            # Preserve the inbound action before any resolver branch mutates
            # the authoritative session. This is intentionally separate from
            # session_events, which is the server-to-client replay stream.
            if (session and (session.session_name or "").startswith(
                    ("tourney-", "pvp-", "Challenge_"))):
                classification = {
                    "fields": txn_info,
                    "pass_turn_phase": pass_turn_phase,
                    "is_set_stops": is_set_stops,
                    "is_mulligan_keep": is_mulligan_keep,
                    "is_mulligan_redraw": is_mulligan_redraw,
                    "is_cheat": is_cheat,
                    "is_pass_priority": is_pass_priority,
                    "is_choose_pick": is_choose_pick,
                    "is_discard": is_discard,
                    "is_ability_activate": is_ability_activate,
                    "is_set_ability_data": is_set_ability_data,
                    "is_commit_attack": is_commit_attack,
                    "is_commit_defense": is_commit_defense,
                    "is_set_auto_pass": is_set_auto_pass,
                    "is_cancel_auto_pass": is_cancel_auto_pass,
                    "is_assign_damage": is_assign_damage,
                    "is_priority_sync": is_priority_sync,
                }
                try:
                    transaction_capture_id = db_record_session_transaction(
                        session.session_id, player_uid, reqid, data_type, comp,
                        txn_id_int,
                        inner_obj.get("__type__", "")
                        if isinstance(inner_obj, dict) else "",
                        classification, inner_bytes,
                        db_session_state_hash(session.session_id))
                except Exception as capture_error:
                    # Diagnostics must never make a live transaction fail.
                    log_req(f"    Transaction capture failed: {capture_error}")

            log_req(f">>> PlayerTransaction (dt=3029) txId={txn_id_str} quit={quit_series} cheat={is_cheat} pass={is_pass_priority}")
            
            # Handle mulligan keep — push AcceptedStartingHand + phase sequence
            handled = False
            if is_priority_sync and session:
                self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_priority_sync_transaction(
                        session, command),
                )
                handled = True
            if is_choose_pick and session and not handled:
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_choose_pick_transaction(
                        session, command),
                )
            if is_mulligan_keep and session and not handled:
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_mulligan_keep_transaction(
                        session, command),
                )
            if is_pass_priority and session and not handled:
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_pass_priority_transaction(
                        session, command),
                )
            if is_set_stops and session and not handled:
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_set_turn_phases_transaction(
                        session, command),
                )
            # The Mono client can serialize the class-23 discard choice as a
            # generic ability transaction (without the SetAbility marker).
            # While that picker is pending, consume the selected hand-card UID
            # through the discard continuation before normal card-play or
            # ability classification can see it.
            if session and not handled and isinstance(transaction.inner_bytes, bytes):
                import battle_engine as _be_dispatch
                pending_state = _be_dispatch.load_state(session)
                if (pending_state.get("pending_discard_ability") and
                        b"m_UID64" in transaction.inner_bytes):
                    handled = self._application.dispatch_player_transaction(
                        transaction,
                        lambda command: self._handle_set_ability_data_transaction(
                            session, command),
                    )
            if is_discard and session and not handled:
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_discard_transaction(
                        session, command),
                )
            if is_set_auto_pass and session and not handled:
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_set_auto_pass_transaction(
                        session, command),
                )
            if is_cancel_auto_pass and session and not handled:
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_cancel_auto_pass_transaction(
                        session, command),
                )
            if (is_assign_damage and session and not handled
                    and not (session.session_name or "").startswith("tourney-")):
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_assign_damage_transaction(
                        session, command),
                )
            if (is_commit_attack and session and not handled
                    and not (session.session_name or "").startswith("tourney-")):
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_commit_attack_transaction(
                        session, command),
                )
            if (is_commit_defense and session and not handled
                    and not (session.session_name or "").startswith("tourney-")):
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_commit_defense_transaction(
                        session, command),
                )
            if (is_ability_activate and session and not handled
                    and not (session.session_name or "").startswith("tourney-")):
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_ability_activate_transaction(
                        session, command),
                )
            if (is_set_ability_data and session and not handled
                    and not (session.session_name or "").startswith("tourney-")):
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_set_ability_data_transaction(
                        session, command),
                )
            if is_mulligan_redraw and session and not handled:
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_mulligan_redraw_transaction(
                        session, command),
                )
            if is_cheat and session and not handled:
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_debug_cheat_transaction(
                        session, command),
                )
            # Handle discard — player selected cards to discard during Discard phase
            # (hand over the max-hand-size limit).
            # Handle combat: player declared attackers (CommitTroopsToAttackTransaction).
            # Combat: player declared blockers (CommitTroopsToDefenseTransaction)
            # during the AI's turn at DeclareDefense. The client sends one
            # DefenseDeclaration per attacker (AttackerId, then DefendingCardIds);
            # the serialized order is attacker, its blockers, next attacker, ...
            # Store the blockers, mark the blocking troops, push BlockersAssigned +
            # an updated CombatListing so the client re-draws the block lines, then
            # resume the AI turn (the commit is the player's DeclareDefense action).
            # F10 / "Skip" auto-pass: the player asked to pass all priority to the
            # end of their own turn. Record the auto-pass in battle state and
            # auto-advance (the server drives the advance; the client also
            # auto-passes at any window it still has priority). Must send a 3055
            # ack so the client's next transaction isn't dropped.
            # Cancel auto-pass (the player took an action / re-pressed skip).
            # Handle combat: AssignDamageOrderTransaction — the client auto-sends
            # this at BOTH the AssignFirstStrikeDamage and AssignDamage steps
            # (BattleStateAssignDamage is pushed for either phase and auto-commits
            # when no combat has blockers). Damage only resolves at the AssignDamage
            # step; at AssignFirstStrikeDamage we simply advance (no swiftstrike
            # troops are implemented yet, and the AI never blocks).
            # PvP tournaments resolve combat through tournament_game
            # (_pvp_resolve_combat) — the human-vs-AI state machine below would
            # load a battle_engine state and CLOBBER the PvP turn_order, dropping
            # the game into the AI path (victory screen, no warzone troops).
            # RequestPrioritySyncTransaction is handled by the application command above.
            # Handle champion ability activation (charge/spell powers).
            # PvP tournaments activate champion abilities through
            # tournament_game (_pvp_activate_champion_ability) — the
            # human-vs-AI state machine below loads a battle_engine bstate and
            # would CLOBBER the PvP turn_order (game drops into the AI path).
            # Handle the follow-up to a class-23 discard prompt: the player chose a
            # hand card; discard it and clear the pending flag.
            # Handle mulligan redraw — reshuffle hand, draw 7 new cards
            if not handled and session and session.state != "ended":
                handled = self._application.dispatch_player_transaction(
                    transaction,
                    lambda command: self._handle_generic_player_transaction(
                        session, command, session_id, comp, conh),
                )

            # PlayerTransaction is fire-and-forget in the battle client. Its
            # SessionClient advances only after a 3055 sync packet, and the
            # client has no usable 3029 response handler for this server-side
            # transaction path. Sending the legacy 3029 response therefore
            # produces "Command handler not found for data wrapper type 3029"
            # and can strand the next transaction. Every handled path above
            # sends either its event packet or the empty 3055 acknowledgement.

            if transaction_capture_id is not None:
                try:
                    db_complete_session_transaction(
                        transaction_capture_id,
                        db_session_state_hash(session.session_id),
                        handled)
                except Exception as capture_error:
                    log_req(f"    Transaction completion capture failed: {capture_error}")

        # SpinWheelOfFate (2049) — chest spinning
        elif data_type == 2049:
            chest_id_raw = inner_obj.get("ChestID", "0")
            chest_uid = int(chest_id_raw) if chest_id_raw.isdigit() else 0
            chest_db_id = chest_uid - 9000 if chest_uid >= 9000 else 0
            log_req(f">>> SpinWheelOfFate: ChestID={chest_uid} db_id={chest_db_id}")

            if not self.user_profile or chest_db_id <= 0:
                log_req(f"    Invalid chest or no profile")
                resp_inner = b""
            else:
                from db import db_get_chest_by_id
                chest = db_get_chest_by_id(chest_db_id, self.user_profile["id"])
                if not chest:
                    log_req(f"    Chest {chest_db_id} not found or already opened")
                    resp_inner = b""
                else:
                    import random as _rand2
                    # Crayburn Promo chests have no set GUID; use their
                    # authored template-keyed pool. Other chests retain the
                    # normal set/rarity-based booster behavior.
                    card_templates = _load_card_templates()
                    chest_cards = _generate_crayburn_chest(card_templates, chest[4])
                    if chest_cards is None:
                        chest_cards = _generate_booster(card_templates, chest[1])
                        # Reduce to a smaller set based on chest rarity
                        rarity_counts = {"Common": 3, "Uncommon": 2, "Rare": 1, "Legendary": 0, "Primal": 0}
                        keep_count = rarity_counts.get(chest[2], 3)
                        if len(chest_cards) > keep_count:
                            chest_cards = _rand2.sample(chest_cards, keep_count)
                    is_crayburn = chest[4] in CRAYBURN_PACK_CARD_SEEDS
                    
                    # Persist cards and create instances
                    from db import db_next_card_instance_id, db_create_card_instance, db_open_chest
                    max_cid = db_next_card_instance_id()
                    reward_card_bits = []
                    for i, (guid, name, cost, atk, def_) in enumerate(chest_cards):
                        cid = max_cid + i
                        db_add_card(self.user_profile["id"], guid)
                        db_create_card_instance(self.user_profile["id"], cid, guid)
                        reward_card_bits.append((guid, name, cost, atk, def_, cid, 0))
                    db_open_chest(chest_db_id)
                    log_req(f"    Spun {'Crayburn ' if is_crayburn else ''}chest {chest[2]} id={chest_db_id}, awarded {len(reward_card_bits)} cards")

                    # Push reward cards to client
                    self._send_cards_chunk(reward_card_bits)

                    # Encode SpinWheelOfFateResponse
                    # Chest (chest_bits, marked opened), RewardCards (List<card_instance_bits>), RewardItems (empty)
                    rtn = ["Game.Client.Network.Profile.SpinWheelOfFateResponse",
                           "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                           "Game.Shared.Domain.card_instance_bits",
                           "Game.Shared.ResourceId", "System.Guid", "System.UInt64",
                           "System.Boolean", "System.String", "System.Int32",
                           "System.UInt32",
                           "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
                           "Game.Shared.Domain.chest_bits"]
                    def rft(tn):
                        if tn not in rtn: rtn.append(tn)
                        return rtn.index(tn)
                    rsizes = []; rbuf = io.BytesIO()
                    rw = lambda s: rbuf.write(s.encode("utf-8"))
                    rsep = lambda: rbuf.write(b";")
                    rsizes.append(0)
                    rw(""); rsep(); rw("0"); rsep(); rw(str(rft(rtn[0]))); rsep(); rw("6"); rsep()

                    # Chest field (chest_bits, 8 props, WasOpened=true)
                    rc = rbuf.tell(); rsizes.append(0)
                    rw("Chest"); rsep(); rw("1"); rsep(); rw(str(rft("Game.Shared.Domain.chest_bits"))); rsep(); rw("0"); rsep()
                    rw("1"); rsep()
                    rfe = rbuf.tell(); rsizes.append(0); reidx = len(rsizes)-1
                    rw("0"); rsep(); rw(str(reidx)); rsep(); rw(str(rft("Game.Shared.Domain.chest_bits"))); rsep(); rw("8"); rsep()
                    # ChestRarity
                    rf1 = rbuf.tell(); rsizes.append(0)
                    rw("ChestRarity"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
                    cmap = {"Common":0, "Uncommon":1, "Rare":2, "Legendary":3, "Primal":4, "Promo":5}
                    rw(hexlify(struct.pack("<i", cmap.get(chest[2], 0))).decode("ascii")); rsep()
                    rsizes[-1] = rbuf.tell() - rf1
                    # WOFSpinStatus = 0
                    rf2 = rbuf.tell(); rsizes.append(0)
                    rw("WOFSpinStatus"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
                    rw(hexlify(struct.pack("<i", 0)).decode("ascii")); rsep()
                    rsizes[-1] = rbuf.tell() - rf2
                    # BoosterPackType
                    rf3 = rbuf.tell(); rsizes.append(0); rti = len(rsizes)-1
                    rw("BoosterPackType"); rsep(); rw(str(rti)); rsep(); rw(str(rft("Game.Shared.ResourceId"))); rsep(); rw("1"); rsep()
                    rgs = rbuf.tell(); rsizes.append(0); rgi = len(rsizes)-1
                    rw("guid"); rsep(); rw(str(rgi)); rsep(); rw(str(rft("System.Guid"))); rsep(); rw("0"); rsep()
                    booster_type_guid = chest[4] or chest[1]
                    rw("36"); rsep(); rbuf.write(booster_type_guid.encode())
                    rsizes[rgi] = rbuf.tell() - rgs; rsizes[rti] = rbuf.tell() - rf3
                    # WasOpened = true
                    rf4 = rbuf.tell(); rsizes.append(0)
                    rw("WasOpened"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Boolean"))); rsep(); rw("0"); rsep()
                    rw("1"); rsizes[-1] = rbuf.tell() - rf4
                    # InventoryId
                    rf5 = rbuf.tell(); rsizes.append(0)
                    rw("InventoryId"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.UInt64"))); rsep(); rw("0"); rsep()
                    rw(hexlify(struct.pack("<Q", chest_uid)).decode("ascii")); rsep()
                    rsizes[-1] = rbuf.tell() - rf5
                    # PromoID
                    rf6 = rbuf.tell(); rsizes.append(0)
                    rw("PromoID"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.UInt32"))); rsep(); rw("0"); rsep()
                    rw(hexlify(struct.pack("<I", 0)).decode("ascii")); rsep()
                    rsizes[-1] = rbuf.tell() - rf6
                    # TempateID
                    rf7 = rbuf.tell(); rsizes.append(0); rti2 = len(rsizes)-1
                    rw("TempateID"); rsep(); rw(str(rti2)); rsep(); rw(str(rft("Game.Shared.ResourceId"))); rsep(); rw("1"); rsep()
                    rgs2 = rbuf.tell(); rsizes.append(0); rgi2 = len(rsizes)-1
                    rw("guid"); rsep(); rw(str(rgi2)); rsep(); rw(str(rft("System.Guid"))); rsep(); rw("0"); rsep()
                    rw("36"); rsep(); rbuf.write(booster_type_guid.encode())
                    rsizes[rgi2] = rbuf.tell() - rgs2; rsizes[rti2] = rbuf.tell() - rf7
                    # Vendor
                    rf8 = rbuf.tell(); rsizes.append(0)
                    rw("Vendor"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
                    rw(hexlify(struct.pack("<i", 0)).decode("ascii")); rsep()
                    rsizes[-1] = rbuf.tell() - rf8
                    rsizes[reidx] = rbuf.tell() - rfe
                    rsizes[1] = rbuf.tell() - rc

                    # RewardCards (List<card_instance_bits>, encoded with reward cards)
                    rfc = rbuf.tell(); rsizes.append(0)
                    rw("RewardCards"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft(rtn[1]))); rsep(); rw("0"); rsep()
                    rw(str(len(reward_card_bits))); rsep()
                    for ri, (guid, name, cost, atk, def_, cid, iext) in enumerate(reward_card_bits):
                        rfe2 = rbuf.tell(); rsizes.append(0); rei = len(rsizes)-1
                        rw(str(ri)); rsep(); rw(str(rei)); rsep(); rw(str(rft(rtn[2]))); rsep(); rw("6"); rsep()
                        # Id
                        f1c = rbuf.tell(); rsizes.append(0)
                        rw("Id"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.UInt64"))); rsep(); rw("0"); rsep()
                        rw(hexlify(struct.pack("<Q", cid)).decode("ascii")); rsep()
                        rsizes[-1] = rbuf.tell() - f1c
                        # TemplateID
                        f2c = rbuf.tell(); rsizes.append(0); rti3 = len(rsizes)-1
                        rw("TemplateID"); rsep(); rw(str(rti3)); rsep(); rw(str(rft("Game.Shared.ResourceId"))); rsep(); rw("1"); rsep()
                        rgs3 = rbuf.tell(); rsizes.append(0); rgi3 = len(rsizes)-1
                        rw("guid"); rsep(); rw(str(rgi3)); rsep(); rw(str(rft("System.Guid"))); rsep(); rw("0"); rsep()
                        rw("36"); rsep(); rbuf.write(guid.encode())
                        rsizes[rgi3] = rbuf.tell() - rgs3; rsizes[rti3] = rbuf.tell() - f2c
                        for bname in ("IsFoil", "IsExtended", "IsNotTradeable"):
                            tb = rbuf.tell(); rsizes.append(0)
                            rw(bname); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Boolean"))); rsep(); rw("0"); rsep()
                            rw("0"); rsizes[-1] = rbuf.tell() - tb
                        # EscrowStatus
                        te = rbuf.tell(); rsizes.append(0)
                        rw("EscrowStatus"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.String"))); rsep(); rw("0"); rsep()
                        enc = b"Clean"; rw(str(len(enc))); rsep(); rbuf.write(enc)
                        rsizes[-1] = rbuf.tell() - te
                        rsizes[rei] = rbuf.tell() - rfe2
                    rsizes[-1] = rbuf.tell() - rfc

                    # RewardItems (empty list)
                    fri = rbuf.tell(); rsizes.append(0)
                    rw("RewardItems"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft(rtn[10]))); rsep(); rw("0"); rsep()
                    rw("0"); rsep()
                    rsizes[-1] = rbuf.tell() - fri

                    # GoldAward (int 0)
                    fga = rbuf.tell(); rsizes.append(0)
                    rw("GoldAward"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
                    rw(hexlify(struct.pack("<i", 0)).decode("ascii")); rsep()
                    rsizes[-1] = rbuf.tell() - fga

                    # SpinEntryColors (List<int>, 3 entries — random symbols for slot reels)
                    import random as _rand3
                    fsc = rbuf.tell(); rsizes.append(0)
                    rw("SpinEntryColors"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Collections.Generic.List`1#System.Int32"))); rsep(); rw("0"); rsep()
                    colors = [_rand3.randint(0, 2) for _ in range(3)]
                    rw(str(len(colors))); rsep()
                    for ci, cv in enumerate(colors):
                        fec = rbuf.tell(); rsizes.append(0); eci = len(rsizes)-1
                        rw(str(ci)); rsep(); rw(str(eci)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
                        rw(hexlify(struct.pack("<i", cv)).decode("ascii")); rsep()
                        rsizes[eci] = rbuf.tell() - fec
                    rsizes[-1] = rbuf.tell() - fsc

                    # SpinEntrySymbols (List<int>, 3 entries)
                    fss = rbuf.tell(); rsizes.append(0)
                    rw("SpinEntrySymbols"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Collections.Generic.List`1#System.Int32"))); rsep(); rw("0"); rsep()
                    symbols = [_rand3.randint(0, 7) for _ in range(3)]
                    rw(str(len(symbols))); rsep()
                    for si, sv in enumerate(symbols):
                        fes = rbuf.tell(); rsizes.append(0); esi = len(rsizes)-1
                        rw(str(si)); rsep(); rw(str(esi)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
                        rw(hexlify(struct.pack("<i", sv)).decode("ascii")); rsep()
                        rsizes[esi] = rbuf.tell() - fes
                    rsizes[-1] = rbuf.tell() - fss

                    # Error (Ok=0) — enum struct format
                    ferr = rbuf.tell(); rsizes.append(0)
                    rw("Error"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("Game.Shared.Network.Profile.ESpinWheelOfFateError"))); rsep(); rw("1"); rsep()
                    ferrv = rbuf.tell(); rsizes.append(0)
                    rw("value__"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.Int32"))); rsep(); rw("0"); rsep()
                    rw(hexlify(struct.pack("<i", 0)).decode("ascii")); rsep()
                    rsizes[-1] = rbuf.tell() - ferrv; rsizes[-2] = rbuf.tell() - ferr

                    # ErrorMessage (empty string)
                    fem = rbuf.tell(); rsizes.append(0)
                    rw("ErrorMessage"); rsep(); rw(str(len(rsizes)-1)); rsep(); rw(str(rft("System.String"))); rsep(); rw("0"); rsep()
                    rw("0"); rsep()
                    rsizes[-1] = rbuf.tell() - fem

                    rsizes[0] = rbuf.tell()
                    rw(";".join(rtn))
                    for i, s in enumerate(rsizes):
                        if i > 0: rw(";")
                        rw(str(s))
                    resp_inner = rbuf.getvalue()

            if not resp_inner:
                resp_inner = encode_objfmt_response(
                    ["Game.Client.Network.Profile.SpinWheelOfFateResponse"],
                    []
                )
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{resp_reqid}"
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(f"    Sent SpinWheelOfFate response ({len(dw_bytes)}b)")

        # OpenChest (2129) — direct opening for NoSpin Promo chests.
        # The pack-opening UI uses this transaction for Crayburn Castle packs;
        # SpinWheelOfFate (2049) is reserved for chests with a spin sequence.
        elif data_type == 2129:
            import random as _rand2
            raw_chest_ids = inner_obj.get("ChestIds", [])
            if isinstance(raw_chest_ids, str):
                try:
                    raw_chest_ids = json.loads(raw_chest_ids)
                except (TypeError, ValueError, json.JSONDecodeError):
                    raw_chest_ids = re.findall(r"\d+", raw_chest_ids)
            if not isinstance(raw_chest_ids, (list, tuple)):
                raw_chest_ids = [raw_chest_ids]
            requested_ids = []
            for raw_id in raw_chest_ids:
                try:
                    chest_uid = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if chest_uid > 0:
                    requested_ids.append(chest_uid)
            log_req(f">>> OpenChest: ids={requested_ids}")

            valid_chest_ids = []
            card_template_ids = []
            reward_card_bits = []
            opened_inventory_items = []
            error_val = 0
            error_message = ""
            if not self.user_profile:
                error_val = 1  # InvalidChestID
                error_message = "Invalid chest ID"
            else:
                from db import (db_get_chest_by_id, db_next_card_instance_id,
                                db_create_card_instance, db_open_chest)
                card_templates = _load_card_templates()
                for chest_uid in requested_ids:
                    chest_db_id = chest_uid - 9000 if chest_uid >= 9000 else 0
                    chest = db_get_chest_by_id(
                        chest_db_id, self.user_profile["id"])
                    if not chest:
                        error_val = 1  # InvalidChestID
                        error_message = "Invalid chest ID"
                        continue

                    chest_cards = _generate_crayburn_chest(
                        card_templates, chest[4])
                    if chest_cards is None:
                        chest_cards = _generate_booster(
                            card_templates, chest[1])
                        rarity_counts = {
                            "Common": 3, "Uncommon": 2, "Rare": 1,
                            "Legendary": 0, "Primal": 0,
                        }
                        keep_count = rarity_counts.get(chest[2], 3)
                        if len(chest_cards) > keep_count:
                            chest_cards = _rand2.sample(chest_cards, keep_count)

                    max_cid = db_next_card_instance_id()
                    for offset, (guid, name, cost, atk, def_) in enumerate(
                            chest_cards):
                        cid = max_cid + offset
                        db_add_card(self.user_profile["id"], guid)
                        db_create_card_instance(
                            self.user_profile["id"], cid, guid)
                        card_template_ids.append(guid)
                        reward_card_bits.append(
                            (guid, name, cost, atk, def_, cid, 0))
                    db_open_chest(chest_db_id)
                    valid_chest_ids.append(chest_uid)
                    opened_inventory_items.append((chest_uid, chest[4]))

                if requested_ids and not valid_chest_ids and not error_val:
                    error_val = 1
                    error_message = "Invalid chest ID"

            from objfmt_builder import ObjFmtBuilder
            b = ObjFmtBuilder("Game.Client.Network.Profile.OpenChestResponse")
            b.field_ulong_list("validChestIds", valid_chest_ids)
            b.field_resource_id_list("inventoryTemplateIDs", [])
            b.field_resource_id_list("cardTemplateIDs", card_template_ids)
            b.field_int("goldAcquired", 0)
            b.field_int("platinumAcquired", 0)
            b.field_enum(
                "Error", "Game.Shared.Network.Profile.EOpenChestError",
                error_val)
            b.field_str("ErrorMessage", error_message)
            resp_inner = b.finish(7)
            resp_body = compress_gzip(resp_inner) if comp else resp_inner
            resp_reqid = reqid | 1
            dw_bytes = encode_datawrapper(
                resp_reqid, data_type, resp_body, comp, session_id)
            issuer_str = (
                f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}."
                f"ServicePlayer.{self.client_uid}.{resp_reqid}")
            self.scnt += 1
            self.send({
                "issuer": issuer_str, "target": target, "instance": instance,
                "reqid": resp_reqid, "c": comp, "conh": conh, "sid": self.sid,
            }, dw_bytes)
            log_req(
                f"    Sent OpenChest response ({len(card_template_ids)} cards, "
                f"{len(dw_bytes)}b)")
            # OpenChestResponse only drives the reward display.  Collection
            # cards and the consumed inventory item arrive through the normal
            # profile events, just as they do for the other pack-opening
            # paths.
            if reward_card_bits:
                self._send_cards_chunk(reward_card_bits)
            for inventory_id, template_guid in opened_inventory_items:
                self._send_inventory_updated(
                    template_guid, inventory_id, quantity=0)

        # --- Friend / Social ---
        elif data_type == 2149:
            from services.social import handle_add_friend
            handle_add_friend(self, target, instance, reqid, comp, session_id,
                              conh, inner_obj, inner_bytes,
                              SERVICE_PROFILE_UID=SERVICE_PROFILE_UID)

        elif data_type == 2157:
            from services.social import handle_accept_friend_request
            handle_accept_friend_request(self, target, instance, reqid, comp,
                                         session_id, conh, inner_obj, inner_bytes,
                                         SERVICE_PROFILE_UID=SERVICE_PROFILE_UID)

        elif data_type == 2159:
            from services.social import handle_ignore_friend_request
            handle_ignore_friend_request(self, target, instance, reqid, comp,
                                         session_id, conh, inner_obj, inner_bytes,
                                         SERVICE_PROFILE_UID=SERVICE_PROFILE_UID)

        elif data_type == 2161:
            from services.social import handle_remove_friend
            handle_remove_friend(self, target, instance, reqid, comp, session_id,
                                 conh, inner_obj, inner_bytes,
                                 SERVICE_PROFILE_UID=SERVICE_PROFILE_UID)

        elif data_type == 2163:
            from services.social import handle_ignore_player
            handle_ignore_player(self, target, instance, reqid, comp, session_id,
                                 conh, inner_obj, inner_bytes,
                                 SERVICE_PROFILE_UID=SERVICE_PROFILE_UID)

        elif data_type == 2165:
            from services.social import handle_unignore_player
            handle_unignore_player(self, target, instance, reqid, comp, session_id,
                                   conh, inner_obj, inner_bytes,
                                   SERVICE_PROFILE_UID=SERVICE_PROFILE_UID)

        # Matchmaking — Ping (4001)
        elif data_type == 4001:
            from services.matchmaking import handle_ping_matchmaking
            handle_ping_matchmaking(self, target, instance, reqid, comp,
                                    session_id, conh, inner_obj, inner_bytes,
                                    log_req=log_req)

        # Matchmaking — SendQuickMatchChallenge (4013)
        elif data_type == 4013:
            from services.matchmaking import handle_send_quick_match_challenge
            handle_send_quick_match_challenge(self, target, instance, reqid, comp,
                                              session_id, conh, inner_obj, inner_bytes,
                                              log_req=log_req)

        # Matchmaking — SendChallengeResponse (4017)
        elif data_type == 4017:
            from services.matchmaking import handle_send_challenge_response
            handle_send_challenge_response(self, target, instance, reqid, comp,
                                           session_id, conh, inner_obj, inner_bytes,
                                           log_req=log_req)

        # Tournament PvP — JoinDisconnectedGame (22023)
        elif data_type == 22023:
            from services.tournament_game import handle_join_disconnected_game
            handle_join_disconnected_game(self, target, instance, reqid, comp,
                                          session_id, conh, inner_obj, inner_bytes,
                                          log_req=log_req)

        # Tournament PvP — ReadyToContinueGame (22025)
        elif data_type == 22025:
            from services.tournament_game import handle_ready_to_continue_game
            handle_ready_to_continue_game(self, target, instance, reqid, comp,
                                          session_id, conh, inner_obj, inner_bytes,
                                          log_req=log_req)

        else:
            log_req(f">>> Unhandled DataType={data_type}")
            log_req(f"    target={target} instance={instance} inner_keys={list(inner_obj.keys()) if isinstance(inner_obj, dict) else '?'}")
            if inner_obj:
                log_req(f"    inner_obj={ {k: str(v)[:100] for k, v in inner_obj.items()} }")
    
    

    def _handle_chat_command(self, cmd: str, room: str, username: str) -> str:
        import commands
        return commands.handle_command(self, cmd, room, username)

    def push_profile_stream(self):
        p = self.user_profile
        username = display_name_from_identity(p["name"])
        gold = p["gold"]
        platinum = p["platinum"]
        
        ident = encode_objfmt_response(
            ["Game.Shared.Profile.Network+Ident",
             "System.UInt64", "System.UInt64"],
             [("AuthId", "ulong", int(self.client_auth_id)),
              ("ReckId", "ulong", int(self.client_reck_id))]
        )
        args = encode_objfmt_response(
            ["Game.Shared.Network.Profile.ProfileStreamEventArgs",
             "System.Byte[]", "System.Boolean"],
            [("Data", "bytes", ident),
             ("done", "bool", False)]
        )
        compressed = compress_gzip(args)
        dw = encode_datawrapper(0, 2210, compressed, 1, "00000000-0000-0000-0000-000000000000")
        issuer = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.0"
        self.scnt += 1
        self.send({
            "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        log_req(f">>> PUSH Ident (dt=2210, auth={self.client_auth_id}, reck={self.client_reck_id}) dw_sz={len(dw)}")

        # Push server-configured feature strings. PlayerProfile handles these
        # as individual strings in the ProfileStream (dt=2210), e.g.
        # ``allowcon`` enables the developer console and ``allowreplay``
        # enables the replay UI hook.
        for feature_flag in PROFILE_FEATURE_FLAGS:
            flag_inner = encode_objfmt_string(feature_flag)
            flag_profile = encode_objfmt_response(
                ["Game.Shared.Network.Profile.ProfileStreamEventArgs",
                 "System.Byte[]", "System.Boolean"],
                [("Data", "bytes", flag_inner),
                 ("done", "bool", False)]
            )
            flag_compressed = compress_gzip(flag_profile)
            flag_dw = encode_datawrapper(
                0, 2210, flag_compressed, 1,
                "00000000-0000-0000-0000-000000000000")
            issuer_flag = (
                f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}."
                f"ServicePlayer.{self.client_uid}.{self.scnt}"
            )
            self.scnt += 1
            self.send({
                "issuer": issuer_flag, "target": "ServiceProfile", "instance": "Shared",
                "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
            }, flag_dw)
            log_req(f">>> PUSH {feature_flag} (dt=2210) dw_sz={len(flag_dw)}")

        # Push any unopened treasure chests so they survive a re-login.
        # The client collects List<chest_bits> from the profile stream and
        # feeds them to CreateLocalTreasureCache (PlayerProfile.cs).
        self._push_chests_stream(p)

        now_str = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())
        
        # Build inventory items from DB (only purchased items, no stardust/chests)
        inv_items = []
        item_id = 1
        
        # Add purchased items from DB
        purchased = db_get_inventory(p["id"])
        for tguid, qty in purchased:
            inv_items.append((tguid, item_id, qty))
            # Store client item UID in the DB so we can reference it later
            from db import db_set_inventory_client_uid
            db_set_inventory_client_uid(p["id"], tguid, item_id)
            item_id += 1

        # Add unopened chests as inventory items. The client expects the chest
        # to be BOTH a chest_bits entry (m_InventoryChests, via the chest
        # stream push above) AND an inventory_bits entry with the
        # CommonTreasureChest template, keyed by the same InventoryId so the
        # pack list can match them up (see UIPackListViewModel.DoUpdateCardPackList
        # and UIPackContentViewModel.openPackResponseHandler).
        from db import db_get_unopened_chests
        chest_rows = db_get_unopened_chests(p["id"])
        for crow in chest_rows:
            # Named promotional chests retain their inventory template; old
            # standard rows have no template and continue using the generic
            # CommonTreasureChest item.
            chest_template = crow[1] if len(crow) > 1 and crow[1] else \
                "a9ae9af2-e27a-48e0-9cd2-490d252fffe4"
            inv_items.append((chest_template, 9000 + crow[0], 1))
        
        inv_count = len(inv_items)
        
        # Load decks from DB
        deck_data = []
        if self.user_profile:
            db_decks = db_get_decks(self.user_profile["id"])
            for dk in db_decks:
                deck_uid = dk["id"]
                deck_uid64 = (deck_uid << 8) | 17
                # Match deck to champion by name
                champ_id = 0
                dname = dk.get("name", "")
                from db import db_get_champion_deck_match
                for c_row in db_get_champion_deck_match(self.user_profile["id"]):
                    if dname.startswith(c_row[1]):
                        champ_id = c_row[0]
                        break
                # Pre-resolve card IDs to template GUIDs
                import json as _json
                try:
                    card_ids = _json.loads(dk.get("cards", "[]"))
                except:
                    card_ids = []
                card_guids = []  # CardsInDeck kept empty in profile push
                deck_data.append((deck_uid64, dname, deck_uid, champ_id, dk.get("cards", "[]"), card_guids))
        deck_count = len(deck_data)
        log(f">>> Profile push: {deck_count} decks from DB")
        
        # Load champions from DB
        champ_data = []
        if self.user_profile:
            from db import db_get_user_champions
            import json as _json
            db_champs = db_get_user_champions(self.user_profile["id"])
            for c in db_champs:
                champ_id = c[0]
                champ_uid64 = (champ_id << 8) | 12  # UID.Type.Champion=12
                # LastDeckID must be the RAW DB deck id, NOT a pre-encoded UID:
                # the client's GetDeck(ulong) wraps it in new UID(Deck, id)
                # (=(id<<8)|17) before looking up its DeckList, which is keyed
                # by (db_id<<8)|17. Sending the encoded UID shifts it twice.
                # We deliberately push LastDeckID=0: the Globe champion select
                # (UIGlobeArenaPanelViewModel.SelectDeck) LAUNCHES the campaign
                # when LastDeckID is set+valid, else it opens the deck editor —
                # pushing 0 lets the player edit their champion deck from the
                # Champion Select / Globe screen. The DB value is kept for
                # battle deck selection (updated when they pick a deck).
                try:
                    champion_talents = _json.loads(c[9] or "[]")
                    if not isinstance(champion_talents, list):
                        champion_talents = []
                except (TypeError, ValueError):
                    champion_talents = []
                champ_data.append((champ_uid64, c[1], champ_id, c[5], c[6], c[3], c[2], c[4],
                                   c[8] or 0, 0, champion_talents, c[10] or ""))
        champ_count = len(champ_data)
        log(f">>> Profile push: {champ_count} champions from DB")
        reck = encode_objfmt_response(
             ["Game.Shared.Domain.reckoning_bits",
              "System.UInt64", "System.String", "System.Int32", "System.Int32",
              "System.Int32",
              "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
              "Game.Shared.Domain.inventory_bits",
              "Game.Shared.ResourceId",
              "System.Guid",
              "System.DateTime",
              "System.Collections.Generic.List`1#Game.Shared.Domain.champion_bits",
              "Game.Shared.Domain.champion_bits",
              "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
              "Game.Shared.Domain.card_instance_bits",
              "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits",
              "Game.Shared.Domain.deck_bits",
              "Game.Shared.Domain.authentication_bits",
              "System.Int32",
              "System.Collections.Generic.List`1#Game.Shared.Domain.buyback_inventory_bits",
              "System.DateTime", "System.DateTime",
              "System.UInt64", "System.Boolean",
              "System.Int32", "System.DateTime", "System.Int32",
              "System.UInt64",
              # Pre-register types added dynamically by champlist/decklist
              "Game.Shared.Mechanics.EChampionClass",
              "Game.Shared.Mechanics.ERace",
              "Game.Shared.Mechanics.EGender",
              "Game.Shared.Mechanics.EDeckLock",
              "Game.Shared.Mechanics.EDeckPersonality",
              "System.Collections.Generic.Dictionary`2#System.UInt64!Game.Shared.Mechanics.EGemTypesNew",
              "Game.Shared.ResourceId", "System.Guid",
              "System.Collections.Generic.List`1#Game.Shared.ResourceId"],
             [("ReckID",     "ulong",   int(self.client_reck_id)),
              ("Name",       "string",  username),
              ("ExperiencePoints", "int", p.get("experience", 0)),
              ("Gold",       "int",     gold),
              ("Platinum",   "int",     platinum),
              ("InventoryIds", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits", inv_count, inv_items)),
               ("Champions", "champlist", ("System.Collections.Generic.List`1#Game.Shared.Domain.champion_bits", champ_count, champ_data)),
               ("Cards",     "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
               ("Decks",     "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits", 0)),
              ("Profile",    "class", "Game.Shared.Domain.authentication_bits"),
              ("EloRank",    "int",     1500),
              ("BuybackInventoryIds", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.buyback_inventory_bits", 0)),
              ("LastLogin",  "datetime", now_str),
              ("LastDisconnect", "datetime", now_str),
              ("AITournamentFlags", "ulong", 0),
              ("CanDisableProfanityFilter", "bool", True),
              ("Level",      "int",     1),
              ("XpGainTimer","datetime", now_str),
              ("XpGain",     "int",     p.get("daily_bonus_xp", 0)),
              ("ProfileId",  "ulong",   int(self.client_auth_id))]
        )
        log(f">>> reck raw ({len(reck)}b) hex={hexlify(reck[:200]).decode()}...")
        # Push EncodedDecks BEFORE reckoning_bits done=true
        if deck_count > 0:
                from encoded_decks import encode_encoded_decks
                db_decks = db_get_decks(self.user_profile["id"])
                ed_bytes = encode_encoded_decks(db_decks, self.user_profile["id"])
                with open("/tmp/encoded_decks.bin", "wb") as f:
                    f.write(ed_bytes)
                ed_inner = encode_objfmt_response(
                    ["Game.Shared.Profile.Network+EncodedDecks", "System.Byte[]"],
                    [("Data", "bytes", ed_bytes)])
                ed_profile = encode_objfmt_response(
                    ["Game.Shared.Network.Profile.ProfileStreamEventArgs",
                     "System.Byte[]", "System.Boolean"],
                    [("Data", "bytes", ed_inner),
                     ("done", "bool", False)])
                ed_compressed = compress_gzip(ed_profile)
                ed_dw = encode_datawrapper(0, 2210, ed_compressed, 1, "00000000-0000-0000-0000-000000000000")
                issuer_ed = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{self.scnt}"
                self.scnt += 1
                self.send({
                    "issuer": issuer_ed, "target": "ServiceProfile", "instance": "Shared",
                    "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
                }, ed_dw)
                log_req(f">>> PUSH EncodedDecks (dt=2210) {deck_count} decks, dw_sz={len(ed_dw)}")

        args2 = encode_objfmt_response(
            ["Game.Shared.Network.Profile.ProfileStreamEventArgs",
             "System.Byte[]", "System.Boolean"],
            [("Data", "bytes", reck),
             ("done", "bool", True)]
        )
        compressed2 = compress_gzip(args2)
        dw2 = encode_datawrapper(0, 2210, compressed2, 1, "00000000-0000-0000-0000-000000000000")
        issuer2 = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.1"
        self.scnt += 1
        self.send({
            "issuer": issuer2, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw2)
        log_req(f">>> PUSH reckoning_bits + done (dt=2210, reck={self.client_reck_id}) dw_sz={len(dw2)}")

        args3 = encode_login_stream_done()
        compressed3 = compress_gzip(args3)
        dw3 = encode_datawrapper(0, 2211, compressed3, 1, "00000000-0000-0000-0000-000000000000")
        issuer3 = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.2"
        self.scnt += 1
        self.send({
            "issuer": issuer3, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw3)
        log_req(f">>> PUSH LoginStreamDone (dt=2211) dw_sz={len(dw3)}")

        # The client normally requests 60007 during login, but older service
        # initialization can drop that request.  Push the event as well so the
        # mail counter is initialized from the authoritative unread rows.
        from services.mail import push_unread_notification
        push_unread_notification(self)

        # Flag inventory + social push for after client is ready
        self._inventory_pending = True
        self._social_pending = True

    def _push_chests_stream(self, profile):
        """Push unopened treasure chests in the login profile stream.

        Sends a standalone List<chest_bits> wrapped in ProfileStreamEventArgs
        (dt=2210) so the client's HandleProfileStream buffers it and calls
        CreateLocalTreasureCache once the stream is done.
        """
        from encoder import encode_chest_list
        if not profile:
            return
        rows = _db.execute(
            "SELECT id, set_guid, chest_rarity, template_guid FROM treasure_chests "
            "WHERE user_id=? AND opened=0", (profile["id"],)).fetchall()
        # Promo/named chests are reconstructed from their inventory_bits
        # template during the reckoning profile push.  Sending them through
        # the generic chest stream first would create a duplicate key when
        # ProcessNonStandardChests handles that same inventory item.
        rows = [r for r in rows if not r[3]]
        if not rows:
            return
        chest_map = {"Common": 0, "Uncommon": 1, "Rare": 2,
                     "Legendary": 3, "Primal": 4, "Promo": 5}
        chests = [(chest_map.get(r[2], 0), 0, r[1], 9000 + r[0]) for r in rows]
        inner = encode_chest_list(chests)
        profile_args = encode_objfmt_response(
            ["Game.Shared.Network.Profile.ProfileStreamEventArgs",
             "System.Byte[]", "System.Boolean"],
            [("Data", "bytes", inner),
             ("done", "bool", False)]
        )
        compressed = compress_gzip(profile_args)
        dw = encode_datawrapper(0, 2210, compressed, 1, "00000000-0000-0000-0000-000000000000")
        issuer = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{self.scnt}"
        self.scnt += 1
        self.send({
            "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        log_req(f">>> PUSH Chests stream (dt=2210) {len(chests)} chests, dw_sz={len(dw)}")

    def push_cards_to_client(self):
        """Push card instances from DB to the client via CardsAdded event (2205), chunked."""
        if not self.user_profile:
            return
        p = self.user_profile
        rows = _db.execute("SELECT ci.template_guid, ct.name, ct.cost, ct.attack, ct.defense, ci.instance_id, ci.is_extended_art "
                           "FROM card_instances ci JOIN card_templates ct ON ct.guid = ci.template_guid "
                           "WHERE ci.user_id=?", (p["id"],)).fetchall()
        if not rows:
            return
        all_cards = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows]

        CHUNK = 500
        for start in range(0, len(all_cards), CHUNK):
            chunk = all_cards[start:start + CHUNK]
            self._send_cards_chunk(chunk)

    def push_opened_cards_via_generic(self, cards):
        """Push newly opened cards via ProfileGenericUpdate (2211)."""
        if not self.user_profile or not cards:
            return
        from objfmt_builder import ObjFmtBuilder

        # Inner: ProfileGenericBatchUpdate with Cards list
        b = ObjFmtBuilder("Game.Shared.ProfileGenericBatchUpdate")
        list_idx, _ = b.begin_list("Cards",
            "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", len(cards))
        for i, (guid, name, cost, atk, def_, cid, is_ext) in enumerate(cards):
            b.begin_element(i, "Game.Shared.Domain.card_instance_bits", 6)
            b.card_fields(guid, cid, is_ext)
        batch_bytes = b.finish(1)

        # Wrap in ProfileGenericUpdateEventArgs → Message → Data
        b2 = ObjFmtBuilder("Game.Shared.Network.Profile.ProfileGenericUpdateEventArgs")
        msg_idx, _ = b2.begin_list("Message", "Game.Shared.ProfileGenericMessage", 1)
        b2.begin_element(0, "Game.Shared.ProfileGenericMessage", 1)
        b2.field_bytes("Data", batch_bytes)
        args = b2.finish(1)

        compressed = compress_gzip(args)
        dw = encode_datawrapper(0, 2211, compressed, 1)
        issuer = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{self.scnt}"
        self.scnt += 1
        self.send({
            "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        log_req(f">>> PUSH Cards via GenericUpdate (dt=2211) {len(cards)} cards, dw_sz={len(dw)}")

    def push_display_rewards(self, rewards):
        """Push ProfileGenericDisplayRewards so the client shows a reward popup.

        This is the same profile-generic channel used by the live client for
        CARD/GOLD/PLAT rewards.  Collection/card-instance persistence is done
        by the campaign service before this event is emitted.
        """
        if not self.user_profile or not rewards:
            return
        from objfmt_builder import ObjFmtBuilder

        b = ObjFmtBuilder("Game.Shared.ProfileGenericDisplayRewards")
        b.begin_list(
            "Rewards",
            "System.Collections.Generic.List`1#Game.Shared.Profile.Network+RewardResult",
            len(rewards),
        )
        for i, reward in enumerate(rewards):
            b.begin_element(i, "Game.Shared.Profile.Network+RewardResult", 6)
            b.field_str("Id", str(reward.get("id", "")))
            b.field_str("Template", str(reward.get("template", "")))
            b.field_int("Quantity", int(reward.get("quantity", 1) or 1))
            b.field_str("Type", str(reward.get("type", "CARD")))
            b.field_ulong("LedgerID", int(reward.get("ledger_id", 0) or 0))
            b.field_bool("Boa", bool(reward.get("boa", False)))
        reward_bytes = b.finish(1)

        wrapper = ObjFmtBuilder(
            "Game.Shared.Network.Profile.ProfileGenericUpdateEventArgs")
        wrapper.begin_list("Message", "Game.Shared.ProfileGenericMessage", 1)
        wrapper.begin_element(0, "Game.Shared.ProfileGenericMessage", 1)
        wrapper.field_bytes("Data", reward_bytes)
        args = wrapper.finish(1)

        compressed = compress_gzip(args)
        dw = encode_datawrapper(0, 2211, compressed, 1)
        issuer = (
            f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}."
            f"ServicePlayer.{self.client_uid}.{self.scnt}"
        )
        self.scnt += 1
        self.send({
            "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        log_req(f">>> PUSH DisplayRewards via GenericUpdate (dt=2211) "
                 f"{len(rewards)} reward(s), dw_sz={len(dw)}")

    def _send_cards_chunk(self, cards):
        ctype_names = [
            "Game.Shared.Network.Profile.CardsAddedEventArgs",
            "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
            "Game.Shared.Domain.card_instance_bits",
            "Game.Shared.ResourceId", "System.Guid", "System.UInt64",
            "System.Boolean", "System.String",
        ]
        def ft(tn):
            if tn not in ctype_names: ctype_names.append(tn)
            return ctype_names.index(tn)

        csizes = [] ; cbuf = io.BytesIO() ; w = lambda s: cbuf.write(s.encode("utf-8"))
        sep = lambda: cbuf.write(b";") ; lf = lambda: cbuf.write(b"\n")

        csizes.append(0)
        w(""); sep(); w("0"); sep(); w(str(ft(ctype_names[0]))); sep(); w("1"); sep()
        fc = cbuf.tell(); csizes.append(0)
        w("CardBits"); sep(); w("1"); sep(); w(str(ft(ctype_names[1]))); sep(); w("0"); sep()
        w(str(len(cards))); sep()

        for i, (guid, name, cost, atk, def_, cid, is_ext) in enumerate(cards):
            fe = cbuf.tell(); csizes.append(0) ; eidx = len(csizes)-1
            w(str(i)); sep(); w(str(eidx)); sep(); w(str(ft(ctype_names[2]))); sep(); w("6"); sep()
            f1 = cbuf.tell(); csizes.append(0)
            w("Id"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
            w(hexlify(struct.pack("<Q", cid)).decode("ascii")); sep()
            csizes[-1] = cbuf.tell() - f1
            f2 = cbuf.tell(); csizes.append(0) ; tidx = len(csizes)-1
            w("TemplateID"); sep(); w(str(tidx)); sep(); w(str(ft("Game.Shared.ResourceId"))); sep(); w("1"); sep()
            gs = cbuf.tell(); csizes.append(0) ; gidx = len(csizes)-1
            w("guid"); sep(); w(str(gidx)); sep(); w(str(ft("System.Guid"))); sep(); w("0"); sep()
            w("36"); sep(); cbuf.write(guid.encode())
            csizes[gidx] = cbuf.tell() - gs ; csizes[tidx] = cbuf.tell() - f2
            f4 = cbuf.tell(); csizes.append(0)
            w("IsFoil"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
            w("0") ; csizes[-1] = cbuf.tell() - f4
            f5 = cbuf.tell(); csizes.append(0)
            w("IsExtended"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
            w("1" if is_ext else "0") ; csizes[-1] = cbuf.tell() - f5
            f7 = cbuf.tell(); csizes.append(0)
            w("IsNotTradeable"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.Boolean"))); sep(); w("0"); sep()
            w("0") ; csizes[-1] = cbuf.tell() - f7
            f8 = cbuf.tell(); csizes.append(0)
            w("EscrowStatus"); sep(); w(str(len(csizes)-1)); sep(); w(str(ft("System.String"))); sep(); w("0"); sep()
            enc = b"Clean"; w(str(len(enc))); sep(); cbuf.write(enc)
            csizes[-1] = cbuf.tell() - f8 ; csizes[eidx] = cbuf.tell() - fe

        csizes[1] = cbuf.tell() - fc ; csizes[0] = cbuf.tell()
        w(";".join(ctype_names)); lf()
        for i, s in enumerate(csizes):
            if i > 0: w(";")
            w(str(s))
        resp_inner = cbuf.getvalue()
        compressed = compress_gzip(resp_inner)
        dw = encode_datawrapper(0, 2205, compressed, 1, "00000000-0000-0000-0000-000000000000")
        issuer = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.{self.scnt}"
        self.scnt += 1
        self.send({"issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid}, dw)
        log_req(f">>> PUSH CardsAdded (dt=2205) {len(cards)} cards, dw_sz={len(dw)}")

    def _send_inventory_updated(self, template_guid, inventory_id, quantity=0):
        """Push the authoritative quantity for one inventory item.

        PlayerProfile removes an item when InventoryUpdated carries quantity
        zero (or a non-minimum claim date).  The fixed client checks the
        latter on this event rather than checking ``ev.Quantity`` directly,
        so consumed items must carry a non-minimum ClaimDate.  This is
        required for direct-opening chests because OpenChestResponse itself
        only contains reward IDs.
        """
        from objfmt_builder import ObjFmtBuilder

        b = ObjFmtBuilder(
            "Game.Shared.Network.Profile.InventoryUpdatedEventArgs")
        b.field_resource_id("ItemId", template_guid or
                            "00000000-0000-0000-0000-000000000000")
        b.field_int("Quantity", int(quantity))
        # UID.Type.InventoryItem is 11 in the client UID enum.
        b.field_uid("ItemInstanceUid", make_uid(11, int(inventory_id)))
        # PlayerProfile.HandleInventoryUpdate removes a cached item when the
        # event's ClaimDate is greater than DateTime.MinValue.  A zero
        # quantity alone is not sufficient in the client implementation.
        claim_date = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())
        b.field_datetime("ClaimDate", claim_date)
        body = b.finish(4)
        compressed = compress_gzip(body)
        dw = encode_datawrapper(
            0, 2207, compressed, 1,
            "00000000-0000-0000-0000-000000000000")
        issuer = (
            f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}."
            f"ServicePlayer.{self.client_uid}.{self.scnt}")
        self.scnt += 1
        self.send({
            "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        log_req(
            f">>> PUSH InventoryUpdated (dt=2207) item={inventory_id} "
            f"quantity={quantity}, dw_sz={len(dw)}")

    def push_inventory_to_client(self, qty=1, template_guid="", item_id=1001):
        """Push an inventory item to the client via ProfileGenericUpdate (dt=2211).

        Structure the client expects (PlayerProfile.HandleProfileGenericUpdate):
            ProfileGenericUpdateEventArgs.Message  (single ProfileGenericMessage)
                .Data = ObjFmt bytes of ProfileGenericBatchUpdate
                          .Items = List<inventory_bits>
                          .GoldDelta = int
        """
        # Inner: ProfileGenericBatchUpdate with Items list + GoldDelta
        batch_bytes = encode_objfmt_response(
            ["Game.Shared.ProfileGenericBatchUpdate",
             "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
             "Game.Shared.Domain.inventory_bits", "System.UInt64",
             "Game.Shared.ResourceId", "System.Guid", "System.Boolean",
             "System.Int32", "System.DateTime", "System.String"],
            [("Items", "coll", ("System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits", 1,
                                [(template_guid, item_id, qty)])),
             ("GoldDelta", "int", 0)]
        )

        # Wrap in ProfileGenericUpdateEventArgs → Message (single) → Data
        args = encode_objfmt_response(
            ["Game.Shared.Network.Profile.ProfileGenericUpdateEventArgs",
             "Game.Shared.ProfileGenericMessage", "System.Byte[]"],
            [("Message", "struct", ("Game.Shared.ProfileGenericMessage", [("Data", "bytes", batch_bytes)]))]
        )

        compressed = compress_gzip(args)
        dw = encode_datawrapper(0, 2211, compressed, 1, "00000000-0000-0000-0000-000000000000")
        issuer = f"0.0.0.0.ServiceProfile.{SERVICE_PROFILE_UID}.ServicePlayer.{self.client_uid}.99"
        self.scnt += 1
        self.send({
            "issuer": issuer, "target": "ServiceProfile", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": self.sid,
        }, dw)
        log_req(f">>> PUSH Inventory item (dt=2211) template={template_guid}")
        # Store client item UID so we can push quantity updates later
        if self.user_profile and template_guid:
            _db.execute("UPDATE player_inventory SET client_item_uid=? WHERE user_id=? AND template_guid=? AND client_item_uid=0",
                        (item_id, self.user_profile["id"], template_guid))


def main():
    global _reload_requested
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

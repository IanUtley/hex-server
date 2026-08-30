"""
Generate golden-file snapshots for every encode_* function in hconnect_server.py.

Usage:  python3 generate_goldens.py

Outputs one .golden file per function under tests/goldens/.
Re-run whenever an encode function intentionally changes.
"""
import io, os, sys, struct, json
from binascii import hexlify

sys.path.insert(0, os.path.dirname(__file__))

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "tests", "goldens")
os.makedirs(GOLDEN_DIR, exist_ok=True)

# ── test data constants ──────────────────────────────────────────────
TEST_GUID      = "abc12345-1234-5678-9012-abcdef000001"
TEST_GUID2     = "def67890-1234-5678-9012-abcdef000002"
TEST_NAME      = "TestName"
TEST_DECK_NAME = "TestDeck"
TEST_ITEM_ID   = 1001
TEST_CARD_ID   = 5000

# ── helpers ──────────────────────────────────────────────────────────
def ft_builder(type_names):
    def ft(tn):
        if tn not in type_names:
            type_names.append(tn)
        return type_names.index(tn)
    return ft


def save_golden(name, data):
    path = os.path.join(GOLDEN_DIR, f"{name}.golden")
    with open(path, "wb") as f:
        f.write(data)
    print(f"  wrote {len(data):5d} bytes → tests/goldens/{name}.golden")


def make_type_names_fresh():
    """Return an empty type-names list for building fresh outputs."""
    return []


# ── actual golden generation ─────────────────────────────────────────

def gen_encode_objfmt_response():
    """Test basic types, enum1, uid, coll, struct."""
    # -- basic types --
    type_names = ["Test.Basic"]
    fields = [
        ("MyInt",    "int",    42),
        ("MyUlong",  "ulong",  1234),
        ("MyBool",   "bool",   True),
        ("MyBoolF",  "bool",   False),
        ("MyGuid",   "guid",   "12345678-1234-5678-9012-123456789012"),
        ("MyStr",    "string", "hello"),
    ]
    from hconnect_server import encode_objfmt_response
    save_golden("encode_objfmt_response_basic",
                encode_objfmt_response(type_names, fields))

    # -- enum1 --
    type_names2 = ["Test.WithEnum", "Test.MyEnum", "System.Int32"]
    fields2 = [("Error", "enum1", ("Test.MyEnum", 0))]
    save_golden("encode_objfmt_response_enum1",
                encode_objfmt_response(type_names2, fields2))

    # -- uid --
    type_names3 = ["Test.WithUid", "Game.Shared.UID", "System.UInt64"]
    fields3 = [("PlayerId", "uid", 67890)]
    save_golden("encode_objfmt_response_uid",
                encode_objfmt_response(type_names3, fields3))

    # -- struct --
    type_names4 = ["Test.WithStruct", "Test.SubType", "System.Int32", "System.String"]
    fields4 = [("Inner", "struct", ("Test.SubType", [("X", "int", 1), ("Y", "str" if False else "string", "hi")]))]
    save_golden("encode_objfmt_response_struct",
                encode_objfmt_response(type_names4, fields4))

    # -- raw --
    type_names5 = ["Test.WithRaw", "System.Byte[]"]
    fields5 = [("Data", "raw", ("System.Byte[]", b"\x01\x02\x03"))]
    save_golden("encode_objfmt_response_raw",
                encode_objfmt_response(type_names5, fields5))

    # -- coll (inventory items) --
    type_names6 = ["Test.WithColl",
                   "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
                   "Game.Shared.Domain.inventory_bits",
                   "System.UInt64", "Game.Shared.ResourceId", "System.Guid",
                   "System.Boolean", "System.Int32", "System.DateTime", "System.String"]
    fields6 = [("Items", "coll", (type_names6[1], 2,
        [
            (TEST_GUID,  1001, 3),
            (TEST_GUID2, 1002, 1),
        ]
    ))]
    save_golden("encode_objfmt_response_coll",
                encode_objfmt_response(type_names6, fields6))

    # -- cardlist --
    type_names7 = ["Test.WithCards",
                   "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                   "Game.Shared.Domain.card_instance_bits",
                   "System.UInt64", "Game.Shared.ResourceId", "System.Guid",
                   "System.Boolean", "System.String"]
    fields7 = [("Cards", "cardlist", (type_names7[1], 1,
        [(TEST_GUID, "Bunoshi", TEST_CARD_ID, 3, 2, 2)]
    ))]
    save_golden("encode_objfmt_response_cardlist",
                encode_objfmt_response(type_names7, fields7))

    # -- class (null) --
    type_names8 = ["Test.WithClass"]
    fields8 = [("NullField", "class", "Test.SomeClass")]
    save_golden("encode_objfmt_response_class",
                encode_objfmt_response(type_names8, fields8))

    # -- 00 numProps corner case --
    type_names9 = ["Test.ZeroPropsHeader", "System.Int32", "System.String"]
    # Use 00 for the root field index to test the 2-digit numProps issue
    buf9 = io.BytesIO()
    sizes9 = [0]
    def w9(s): buf9.write(s.encode("utf-8"))
    def s9():  buf9.write(b";")
    w9(""); s9(); w9("00"); s9(); w9("0"); s9(); w9("2"); s9()
    f1 = buf9.tell(); sizes9.append(0)
    w9("A"); s9(); w9("1"); s9(); w9(str(type_names9.index("System.Int32"))); s9(); w9("0"); s9()
    w9(hexlify(struct.pack("<i", 99)).decode("ascii")); s9()
    sizes9[-1] = buf9.tell() - f1
    f2 = buf9.tell(); sizes9.append(0)
    w9("B"); s9(); w9("2"); s9(); w9(str(type_names9.index("System.String"))); s9(); w9("0"); s9()
    enc = b"test"; w9(str(len(enc))); s9(); buf9.write(enc)
    sizes9[-1] = buf9.tell() - f2
    sizes9[0] = buf9.tell()
    w9(";".join(type_names9)); buf9.write(b"\n")
    for i, s in enumerate(sizes9):
        if i > 0: w9(";")
        w9(str(s))
    save_golden("encode_objfmt_response_zeroprop", buf9.getvalue())


def gen_encode_inventory_item():
    from hconnect_server import encode_inventory_item
    sizes = []
    buf = io.BytesIO()
    w = lambda s: buf.write(s.encode("utf-8"))
    s = lambda: buf.write(b";")
    sizes.append(0)
    w(""); s(); w("0"); s(); w("0"); s(); w("1"); s()

    f = buf.tell(); sizes.append(0)
    w("Items"); s(); w("1"); s()
    type_names = ["Test.Items",
                  "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits",
                  "Game.Shared.Domain.inventory_bits",
                  "System.UInt64", "Game.Shared.ResourceId", "System.Guid",
                  "System.Boolean", "System.Int32", "System.DateTime", "System.String"]
    ft = ft_builder(type_names)
    w(str(ft(type_names[1]))); s(); w("0"); s(); w("1"); s()
    encode_inventory_item(buf, sizes, ft, TEST_GUID, TEST_ITEM_ID, 0, 3, bound=True)
    sizes[1] = buf.tell() - f; sizes[0] = buf.tell()
    w(";".join(type_names)); buf.write(b"\n")
    for i, sz in enumerate(sizes):
        if i > 0: w(";")
        w(str(sz))
    save_golden("encode_inventory_item", buf.getvalue())


def gen_encode_card_instance():
    from hconnect_server import encode_card_instance
    sizes = []
    buf = io.BytesIO()
    w = lambda s: buf.write(s.encode("utf-8"))
    s = lambda: buf.write(b";")
    sizes.append(0)
    w(""); s(); w("0"); s(); w("0"); s(); w("1"); s()

    f = buf.tell(); sizes.append(0)
    w("Cards"); s(); w("1"); s()
    type_names = ["Test.Cards",
                  "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                  "Game.Shared.Domain.card_instance_bits",
                  "System.UInt64", "Game.Shared.ResourceId", "System.Guid",
                  "System.Boolean", "System.String"]
    ft = ft_builder(type_names)
    w(str(ft(type_names[1]))); s(); w("0"); s(); w("1"); s()
    encode_card_instance(buf, sizes, ft, TEST_GUID, TEST_NAME, TEST_CARD_ID, 3, 2, 2, 0)
    sizes[1] = buf.tell() - f; sizes[0] = buf.tell()
    w(";".join(type_names)); buf.write(b"\n")
    for i, sz in enumerate(sizes):
        if i > 0: w(";")
        w(str(sz))
    save_golden("encode_card_instance", buf.getvalue())


def gen_encode_deck_bits():
    from hconnect_server import encode_deck_bits
    sizes = []
    buf = io.BytesIO()
    w = lambda s: buf.write(s.encode("utf-8"))
    s = lambda: buf.write(b";")
    sizes.append(0)
    w(""); s(); w("0"); s(); w("0"); s(); w("1"); s()

    f = buf.tell(); sizes.append(0)
    w("Decks"); s(); w("1"); s()
    type_names = ["Test.Decks",
                  "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits",
                  "Game.Shared.Domain.deck_bits",
                  "System.UInt64", "System.String",
                  "Game.Shared.ResourceId", "System.Guid",
                  "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
                  "System.Collections.Generic.Dictionary`2#System.UInt64!Game.Shared.Mechanics.EGemTypesNew",
                  "Game.Shared.Mechanics.EDeckLock", "System.Int32",
                  "Game.Shared.Mechanics.EDeckPersonality",
                  "System.Boolean"]
    ft = ft_builder(type_names)
    w(str(ft(type_names[1]))); s(); w("0"); s(); w("1"); s()
    encode_deck_bits(buf, sizes, ft, TEST_DECK_NAME, TEST_DECK_NAME, 1, 0, [], 0)
    sizes[1] = buf.tell() - f; sizes[0] = buf.tell()
    w(";".join(type_names)); buf.write(b"\n")
    for i, sz in enumerate(sizes):
        if i > 0: w(";")
        w(str(sz))
    save_golden("encode_deck_bits", buf.getvalue())


def gen_encode_champion_bits_minimal():
    from hconnect_server import encode_champion_bits_minimal
    sizes = []
    buf = io.BytesIO()
    w = lambda s: buf.write(s.encode("utf-8"))
    s = lambda: buf.write(b";")
    sizes.append(0)
    w(""); s(); w("0"); s(); w("0"); s(); w("1"); s()

    f = buf.tell(); sizes.append(0)
    w("Champions"); s(); w("1"); s()
    type_names = ["Test.Champs",
                  "System.Collections.Generic.List`1#Game.Shared.Domain.champion_bits",
                  "Game.Shared.Domain.champion_bits",
                  "System.String", "System.UInt64", "System.Int32",
                  "Game.Shared.Mechanics.EChampionClass",
                  "Game.Shared.Mechanics.ERace",
                  "Game.Shared.Mechanics.EGender"]
    ft = ft_builder(type_names)
    w(str(ft(type_names[1]))); s(); w("0"); s(); w("1"); s()
    encode_champion_bits_minimal(buf, sizes, ft,
                                 12345, "TestChamp", 5000,  # cu64, cname, cid
                                 1, 0, 3, 1, 1,             # lvl, xp, cc, rc, gd
                                 0)                          # idx
    sizes[1] = buf.tell() - f; sizes[0] = buf.tell()
    w(";".join(type_names)); buf.write(b"\n")
    for i, sz in enumerate(sizes):
        if i > 0: w(";")
        w(str(sz))
    save_golden("encode_champion_bits_minimal", buf.getvalue())


def gen_encode_objfmt_string():
    from hconnect_server import encode_objfmt_string
    save_golden("encode_objfmt_string_hello", encode_objfmt_string("hello"))
    save_golden("encode_objfmt_string_empty", encode_objfmt_string(""))


def gen_encode_session_state():
    from hconnect_server import encode_session_state
    save_golden("encode_session_state",
                encode_session_state(42, "TestSession"))


def gen_encode_campaign_session_state():
    from encoder import encode_campaign_session_state
    save_golden("encode_campaign_session_state",
                encode_campaign_session_state(
                    42, "camp_1",
                    "abc12345-1234-5678-9012-abcdef000001",
                    session_flags=1 | 4))


def gen_encode_sync_event():
    from hconnect_server import encode_sync_event
    import game_engine

    pkt = game_engine.NetworkPacketSessionEventArgs()
    pkt.player_id   = game_engine.UID(67890)
    pkt.session_id  = game_engine.UID(12345)
    pkt.event_ids   = [1, 2, 3]
    pkt.event_data  = [b"test_event_data_12345678"]

    save_golden("encode_sync_event", encode_sync_event(pkt))


def gen_encode_challenger_list():
    from hconnect_server import encode_challenger_list
    challengers = [
        {"id": 1, "name": "BossOne", "deck": TEST_GUID, "boss": "True"},
        {"id": 2, "name": "MinionTwo", "deck": TEST_GUID2, "boss": "False"},
    ]
    save_golden("encode_challenger_list",
                encode_challenger_list(challengers))


def gen_encode_get_challengers_response():
    from hconnect_server import encode_get_challengers_response
    challengers = [
        {"id": 1, "name": "BossOne", "deck": TEST_GUID, "boss": "True"},
    ]
    save_golden("encode_get_challengers_response",
                encode_get_challengers_response(True, challengers))


def gen_encode_login_stream_done():
    from hconnect_server import encode_login_stream_done
    save_golden("encode_login_stream_done", encode_login_stream_done())


def gen_encode_datawrapper():
    from hconnect_server import encode_datawrapper
    body = b"test body payload 42"
    save_golden("encode_datawrapper", encode_datawrapper(1, 2127, body, 0))
    save_golden("encode_datawrapper_compressed",
                encode_datawrapper(1, 2127, body, 1))


def gen_encode_get_unread_mail_count_response():
    from hconnect_server import encode_get_unread_mail_count_response
    save_golden("encode_get_unread_mail_count_response",
                encode_get_unread_mail_count_response(0))
    save_golden("encode_get_unread_mail_count_response_5",
                encode_get_unread_mail_count_response(5))


def gen_encode_ping_mail_server_response():
    from hconnect_server import encode_ping_mail_server_response
    save_golden("encode_ping_mail_server_response",
                encode_ping_mail_server_response("01/15/2025 12:00:00"))


def gen_encode_profile_response():
    from hconnect_server import encode_profile_response
    env = json.dumps({"test": True}).encode("utf-8")
    save_golden("encode_profile_response",
                encode_profile_response(env))


def gen_encode_get_store_items_response():
    # Requires DB — use the real one if available, otherwise skip
    from hconnect_server import encode_get_store_items_response
    try:
        result = encode_get_store_items_response()
        save_golden("encode_get_store_items_response", result)
    except Exception as e:
        print(f"  SKIP encode_get_store_items_response: {e}")


def gen_encode_store_response():
    from hconnect_server import encode_store_response
    items = [
        {"n": "Test Booster", "s": "A test booster pack", "price": 200,
         "currency": "Gold", "template_guid": TEST_GUID, "t": "ShopBoosterTab"},
        {"n": "Starter Deck", "s": "Beginner deck", "price": 1000,
         "currency": "Platinum", "template_guid": TEST_GUID2, "t": "collectordeck"},
    ]
    save_golden("encode_store_response", encode_store_response(items))

    # Empty store
    save_golden("encode_store_response_empty", encode_store_response([]))


def gen_encode_store_item_set1_booster():
    from hconnect_server import encode_store_item_set1_booster
    try:
        result = encode_store_item_set1_booster()
        save_golden("encode_store_item_set1_booster", result)
    except Exception as e:
        print(f"  SKIP encode_store_item_set1_booster: {e}")


def gen_session_event_streams():
    """Golden snapshots of the SessionEventArgs binary streams the server
    pushes to the client during battle phases (class-255 NetworkPacket bytes).
    Deterministic inputs pin the exact wire format for combat + ability events.
    """
    import game_engine as ge
    from hconnect_server import encode_sync_event

    pl = ge.UID.make(244, 1001)     # player
    ai = ge.UID.make(3, 1000)       # AI champion
    troop = ge.SessionCardId(ge.UID.make(1, 5001))
    ai_champ = ge.SessionCardId(ge.UID.make(1, 5000))

    def snapshot(name, game):
        pkt = game.make_network_packet(pl)
        save_golden(name, encode_sync_event(pkt))

    # 1. DeclareCombatPriorityWindow stop: phase + greenlight + both players.
    g = ge.Game(42, pl, ai)
    g.player_resources = 3
    g.player_total_resources = 4
    g.player_charges = 2
    g.player_spell_points = 1
    g.ai_health = 10
    g.push_turn_phase(ge.ETurnPhases.DeclareCombatPriorityWindow, pl, pl)
    g.push_green_light(pl, ge.EPriorityContext.ProcedeToCombat)
    g.push_player_updated(pl, champ_id=ge.SessionCardId(ge.UID.make(1, 6000)))
    g.push_player_updated(ai, champ_id=ai_champ)
    snapshot("sync_combat_window", g)

    # 2. DeclareAttack: attacker committed (AttackDeclared 27 + CombatListing 62)
    g = ge.Game(42, pl, ai)
    g.player_resources = 3
    g.player_total_resources = 4
    g.push_attack_declared(ge.CombatId(pl, 1), pl, ai_champ, troop)
    cs = ge.CombatSessionEventArgs()
    cs.player_id = pl
    cs.id = ge.CombatId(pl, 1)
    cs.attacker = troop
    cs.blockers = []
    g.push_combat_listing(pl, [cs])
    snapshot("sync_declare_attack", g)

    # 3. AssignDamage: combat damage to AI champion (CombatPhaseResolved 29 +
    #    ChampionHealthChanged 38 + PlayerUpdated 65)
    g = ge.Game(42, pl, ai)
    g.ai_health = 8
    g.push_combat_phase_resolved(ge.CombatId(pl, 1), troop, ai_champ, [])
    ev = ge.ChampionHealthChangedSessionEventArgs()
    ev.player_id = ai
    ev.old_damage_value = 10
    ev.new_damage_value = 8
    g._push(ev)
    g.push_player_updated(ai, champ_id=ai_champ)
    snapshot("sync_combat_damage", g)

    # 4. CardUpdated with a manual troop ability (Shift) + attribute
    g = ge.Game(42, pl, ai)
    cdef = ge.CardDef("Gemsoul Feeder", ge.ECardTypes.Troop, 2, 1, 2, [],
                     [ge.ResourceId.from_str("44605164-fbfc-d2f7-433a-ebf79d35adff"),
                      ge.ResourceId.from_str("2af60616-96ee-743d-33b8-cdb4c3d9f0a5")],
                     ge.ECardAttributes.SpiritDrain)
    g.card_defs[troop] = cdef
    g.push_card_updated(troop, pl, ge.ECardCollections.Warzone, ge.ECardTypes.Troop,
                        template_id="7071ddd7-b81b-46b2-8682-9f391ab3f12e",
                        attributes=ge.ECardAttributes.SpiritDrain)
    snapshot("sync_troop_ability_card", g)

    # 5. ShiftPower: the TACAbilityEffectTemplate leaf + ability transfer
    g = ge.Game(42, pl, ai)
    src = ge.SessionCardId(ge.UID.make(1, 7001))
    tgt = ge.SessionCardId(ge.UID.make(1, 7002))
    for scid, nm, ab, attrs in (
        (src, "Gemsoul Feeder", ["44605164-fbfc-d2f7-433a-ebf79d35adff", "2af60616-96ee-743d-33b8-cdb4c3d9f0a5"], ge.ECardAttributes.SpiritDrain),
        (tgt, "Shin'hare Militia", [], 0),
    ):
        cdef = ge.CardDef(nm, ge.ECardTypes.Troop, 2, 1, 2, [],
                          [ge.ResourceId.from_str(a) for a in ab], attrs)
        g.card_defs[scid] = cdef
        g.push_card_updated(scid, pl, ge.ECardCollections.Warzone, ge.ECardTypes.Troop,
                            template_id="7071ddd7-b81b-46b2-8682-9f391ab3f12e",
                            attributes=attrs)
    snapshot("sync_shift_transfer", g)


GENERATORS = [
    ("encode_objfmt_response",       gen_encode_objfmt_response),
    ("encode_inventory_item",        gen_encode_inventory_item),
    ("encode_card_instance",         gen_encode_card_instance),
    ("encode_deck_bits",             gen_encode_deck_bits),
    ("encode_champion_bits_minimal", gen_encode_champion_bits_minimal),
    ("encode_objfmt_string",         gen_encode_objfmt_string),
    ("encode_session_state",         gen_encode_session_state),
    ("encode_campaign_session_state", gen_encode_campaign_session_state),
    ("encode_sync_event",            gen_encode_sync_event),
    ("encode_challenger_list",       gen_encode_challenger_list),
    ("encode_get_challengers_response", gen_encode_get_challengers_response),
    ("encode_login_stream_done",     gen_encode_login_stream_done),
    ("encode_datawrapper",           gen_encode_datawrapper),
    ("encode_get_unread_mail_count_response", gen_encode_get_unread_mail_count_response),
    ("encode_ping_mail_server_response",      gen_encode_ping_mail_server_response),
    ("encode_profile_response",      gen_encode_profile_response),
    ("encode_get_store_items_response", gen_encode_get_store_items_response),
    ("encode_store_response",        gen_encode_store_response),
    ("encode_store_item_set1_booster", gen_encode_store_item_set1_booster),
    ("session_event_streams",        gen_session_event_streams),
]

def main():
    failed = 0
    for name, fn in GENERATORS:
        try:
            print(f"[{name}]")
            fn()
        except Exception as e:
            print(f"  FAIL: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{failed}/{len(GENERATORS)} generators failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

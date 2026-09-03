"""Hex private server schema and server-owned seed configuration.

Client-derived rows are loaded into fresh databases by
``AssetExtraction.gamedata_seed`` from ``HEX_GAMEDATA`` or the checked-in
``Records/`` snapshot.  They deliberately do not live in this module.
"""

import json

# ---------------------------------------------------------------------------
# DDL — all tables the server expects to exist.
# ---------------------------------------------------------------------------

DDL = [
    # --- players / accounts -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        auth_id TEXT,
        reck_id TEXT,
        gold INTEGER DEFAULT 10000,
        platinum INTEGER DEFAULT 10000,
        experience INTEGER DEFAULT 0,
        level INTEGER DEFAULT 1,
        last_login TEXT,
        last_ip TEXT,
        flags TEXT DEFAULT "{}",
        password_hash TEXT DEFAULT NULL,
        email TEXT DEFAULT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        sid TEXT PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        username TEXT,
        client_auth_id TEXT,
        client_reck_id TEXT,
        client_uid TEXT,
        addr TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,

    # --- collection / cards -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS collections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        card_template_id TEXT,
        quantity INTEGER DEFAULT 1,
        added_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_collections_user_template
        ON collections(user_id, card_template_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS card_instances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        instance_id INTEGER NOT NULL,
        template_guid TEXT NOT NULL,
        is_extended_art INTEGER DEFAULT 0,
        UNIQUE(user_id, instance_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_card_instances_instance
        ON card_instances(instance_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS gem_templates (
        gem_type INTEGER PRIMARY KEY,
        gem_type_name TEXT DEFAULT '',
        name TEXT DEFAULT '',
        abilities_json TEXT DEFAULT '[]'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS card_templates (
        guid TEXT PRIMARY KEY,
        set_guid TEXT,
        name TEXT,
        rarity TEXT,
        cost INTEGER DEFAULT 0,
        attack INTEGER DEFAULT 0,
        defense INTEGER DEFAULT 0,
        card_type TEXT DEFAULT '',
        socket_count INTEGER DEFAULT 0,
        no_pvp INTEGER DEFAULT 0,
        is_pve INTEGER DEFAULT 0,
        threshold_json TEXT DEFAULT '[]',
        abilities_json TEXT DEFAULT '[]',
        attributes INTEGER DEFAULT 0,
        sacrifice_target TEXT DEFAULT '',
        variable_cost INTEGER DEFAULT 0,
        variable_cost_minimum INTEGER DEFAULT 0,
        rage_value INTEGER DEFAULT 0,
        subtype TEXT DEFAULT '',
        current_resources_granted INTEGER DEFAULT 0,
        max_resources_granted INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deck_template_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deck_template_guid TEXT NOT NULL,
        deck_name TEXT NOT NULL,
        card_template_guid TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 1,
        UNIQUE(deck_template_guid, card_template_guid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS starter_decks (
        guid TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        price INTEGER DEFAULT 1000,
        card_pack_type TEXT DEFAULT 'StarterDeck',
        deck_template_guid TEXT,
        race TEXT DEFAULT '',
        icon TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stardust (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        rarity TEXT NOT NULL,
        quantity INTEGER DEFAULT 0,
        UNIQUE(user_id, rarity)
    )
    """,

    # --- inventory / store --------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS player_inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        template_guid TEXT NOT NULL,
        quantity INTEGER DEFAULT 1,
        acquired_at TEXT DEFAULT (datetime('now')),
        client_item_uid INTEGER DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_player_inventory_user_template
        ON player_inventory(user_id, template_guid)
    """,
    """
    CREATE TABLE IF NOT EXISTS store_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        template_guid TEXT NOT NULL,
        name TEXT NOT NULL,
        short_desc TEXT,
        price INTEGER DEFAULT 100,
        currency TEXT DEFAULT 'Gold',
        store_tab TEXT DEFAULT 'ShopBoosterTab'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS store_purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        item_name TEXT,
        item_template_id TEXT,
        price INTEGER,
        currency TEXT,
        purchased_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS treasure_chests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        set_guid TEXT NOT NULL,
        chest_rarity TEXT NOT NULL,
        opened INTEGER DEFAULT 0,
        template_guid TEXT DEFAULT '',
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chest_probabilities (
        rarity TEXT PRIMARY KEY,
        probability REAL NOT NULL,
        weight INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chest_templates (
        guid TEXT PRIMARY KEY,
        name TEXT,
        set_guid TEXT,
        chest_type TEXT NOT NULL DEFAULT 'Common',
        spin_type TEXT NOT NULL DEFAULT 'NoSpin',
        promotional_id INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS redeem_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        gold_delta INTEGER DEFAULT 0,
        platinum_delta INTEGER DEFAULT 0,
        uses INTEGER DEFAULT 0,
        max_uses INTEGER DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS encounter_scenes (
        guid TEXT PRIMARY KEY,
        name TEXT,
        title TEXT,
        gameboard TEXT,
        ai_deck_guid TEXT,
        ai_champion_guid TEXT,
        ai_deck_personality TEXT DEFAULT NULL,
        mods_json TEXT DEFAULT '[]',
        rewards_json TEXT DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS encounter_deck_cards (
        deck_guid TEXT NOT NULL,
        card_guid TEXT NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 1,
        gem_types_new_list_json TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (deck_guid, card_guid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS champion_class_data (
        race TEXT NOT NULL, champion_class TEXT NOT NULL,
        starting_health INTEGER, starting_hand_size INTEGER,
        PRIMARY KEY (race, champion_class)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS champion_template_data (
        guid TEXT PRIMARY KEY, name TEXT, champion_class TEXT, race TEXT,
        starting_health INTEGER, starting_hand_size INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS champion_templates (
        guid TEXT PRIMARY KEY,
        race TEXT NOT NULL,
        champion_class TEXT NOT NULL,
        gender TEXT,
        is_player INTEGER DEFAULT 0,
        charge_power TEXT DEFAULT NULL,
        default_talents TEXT DEFAULT '[]'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS talent_data (
        talent_guid TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        ability_guid TEXT,
        has_ability INTEGER NOT NULL DEFAULT 0,
        description TEXT NOT NULL DEFAULT '',
        charge_cost INTEGER DEFAULT 0,
        spell_cost INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS talent_abilities (
        talent_guid TEXT NOT NULL,
        ability_guid TEXT NOT NULL,
        charge_cost INTEGER DEFAULT 0,
        spell_cost INTEGER DEFAULT 0,
        activatable_phases INTEGER DEFAULT 0,
        casting_behavior INTEGER DEFAULT 0,
        condition TEXT DEFAULT '',
        target_template_ids TEXT DEFAULT '[]',
        PRIMARY KEY (talent_guid, ability_guid)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_talent_abilities_ability ON talent_abilities(ability_guid)
    """,
    # Bill-of-materials: top-level ability template -> ordered leaf effect
    # templates (from AbilityTemplate.m_AbilityEffectList). Cost + phase
    # requirements live on the top-level ability in talent_abilities; this
    # table expands it into the effect chain to execute on activation.
    # effect_type = AbilityEffectTemplate class name; param = m_AbilityToInvoke
    # (for ActivateAbilityEffectTemplate) so the resolver can recurse.
    """
    CREATE TABLE IF NOT EXISTS ability_effects (
        ability_guid TEXT NOT NULL,
        effect_guid TEXT NOT NULL,
        effect_order INTEGER NOT NULL DEFAULT 0,
        effect_type TEXT DEFAULT '',
        param TEXT DEFAULT '',
        effect_group_id INTEGER DEFAULT 0,
        condition_id TEXT DEFAULT '',
        target_index INTEGER DEFAULT -1,
        effect_instance_id INTEGER DEFAULT -1,
        contingent_effect_instance_id INTEGER DEFAULT -1,
        secondary_target_index INTEGER DEFAULT -1,
        recalculate_targets INTEGER DEFAULT -1,
        is_optional INTEGER DEFAULT 0,
        effect_duration TEXT DEFAULT 'Instant',
        output_variables TEXT DEFAULT '{}',
        PRIMARY KEY (ability_guid, effect_order)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS card_counter_templates (
        template_id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ability_effect_conditions (
        condition_id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        condition_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ability_effects_ability ON ability_effects(ability_guid)
    """,
    # Per-ability activation metadata for CARD abilities (troop/artifact/etc.).
    # Mirrors the champion talent_abilities cost/phase columns, plus the
    # fields that gate whether a manual ability can be activated on a card
    # instance (casting_behavior, activation_cost, uses limits, exhaust).
    # Populated from client gamedata or Records by gamedata_seed.py.
    """
    CREATE TABLE IF NOT EXISTS card_abilities_meta (
        ability_guid TEXT PRIMARY KEY,
        casting_behavior INTEGER DEFAULT 0,
        is_manual INTEGER DEFAULT 0,
        activation_cost INTEGER DEFAULT 0,
        uses_per_game INTEGER DEFAULT 0,
        uses_per_turn INTEGER DEFAULT 0,
        cooldown INTEGER DEFAULT 0,
        exhausts_on_use INTEGER DEFAULT 0,
        is_triggered INTEGER DEFAULT 0,
        target_template_ids TEXT DEFAULT '[]',
        trigger_event_type TEXT DEFAULT '',
        game_text TEXT DEFAULT '',
        raw_json TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS target_templates (
        template_id TEXT PRIMARY KEY,
        game_text TEXT DEFAULT '',
        is_auto_target INTEGER DEFAULT 0,
        is_random_target INTEGER DEFAULT 0,
        optional INTEGER DEFAULT 0,
        explicit INTEGER DEFAULT 0,
        player_filter TEXT DEFAULT '',
        collection_flags TEXT DEFAULT '',
        min_target_count INTEGER DEFAULT 1,
        max_target_count INTEGER DEFAULT 1,
        filter_json TEXT DEFAULT '{}',
        target_kind TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pack_set_map (
        pack_guid TEXT PRIMARY KEY,
        set_guid TEXT NOT NULL,
        is_full_set INTEGER DEFAULT 0,
        is_primal INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS game_sessions (
        session_id TEXT PRIMARY KEY,
        server_id TEXT NOT NULL,
        session_name TEXT NOT NULL UNIQUE,
        owner_uid TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'created',
        encounter_data TEXT DEFAULT '{}',
        players_json TEXT DEFAULT '[]',
        turn_order_json TEXT DEFAULT '[]',
        seed_z INTEGER DEFAULT 12345,
        seed_w INTEGER DEFAULT 67890,
        deck_template_id TEXT DEFAULT '00000000-0000-0000-0000-000000000000',
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS champion_abilities (
        champion_guid TEXT NOT NULL,
        champion_name TEXT NOT NULL,
        ability_guid TEXT NOT NULL,
        ability_name TEXT NOT NULL DEFAULT '',
        charge_cost INTEGER NOT NULL DEFAULT 0,
        spell_cost INTEGER NOT NULL DEFAULT 0,
        threshold_colors TEXT NOT NULL DEFAULT '',
        game_text TEXT NOT NULL DEFAULT '',
        casting_behavior INTEGER NOT NULL DEFAULT 0,
        thresholds_json TEXT NOT NULL DEFAULT '[]',
        target_template_ids TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (champion_guid, ability_guid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS champion_templates_extended (
        guid TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        race TEXT NOT NULL DEFAULT '',
        champion_class TEXT NOT NULL DEFAULT '',
        gender TEXT NOT NULL DEFAULT '',
        is_selectable INTEGER NOT NULL DEFAULT 0,
        starting_health INTEGER NOT NULL DEFAULT 20,
        faction TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tournament_types (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        style TEXT NOT NULL DEFAULT 'sw',
        format INTEGER NOT NULL DEFAULT 0,
        min_players INTEGER NOT NULL DEFAULT 2,
        max_players INTEGER NOT NULL DEFAULT 2,
        games_count INTEGER NOT NULL DEFAULT 1,
        set_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tournaments (
        id INTEGER PRIMARY KEY,
        type_id INTEGER NOT NULL REFERENCES tournament_types(id),
        status TEXT NOT NULL DEFAULT 'waiting',
        players_json TEXT DEFAULT '{}',
        session_id TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tournaments_status_type
        ON tournaments(status, type_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS tournament_decks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
        player_uid INTEGER NOT NULL,
        cards_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tournament_decks_tournament_player
        ON tournament_decks(tournament_id, player_uid)
    """,
    """
    CREATE TABLE IF NOT EXISTS tournament_signups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
        player_uid INTEGER NOT NULL,
        player_name TEXT NOT NULL,
        deck_id INTEGER NOT NULL DEFAULT 0,
        entry_group INTEGER NOT NULL DEFAULT 0,
        fee_paid INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(tournament_id, player_uid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tournament_matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
        round_id INTEGER NOT NULL DEFAULT 1,
        match_id INTEGER NOT NULL DEFAULT 1,
        player1_uid INTEGER NOT NULL,
        player2_uid INTEGER NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'PlayGame',
        status TEXT NOT NULL DEFAULT 'InProgress',
        start_time INTEGER NOT NULL DEFAULT 0,
        end_time INTEGER NOT NULL DEFAULT 0,
        game1_winner INTEGER NOT NULL DEFAULT 0,
        game2_winner INTEGER NOT NULL DEFAULT 0,
        game3_winner INTEGER NOT NULL DEFAULT 0,
        UNIQUE(tournament_id, session_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tournament_matches_tournament_round
        ON tournament_matches(tournament_id, round_id DESC, id DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value INTEGER NOT NULL
    )
    """,
    # Event log for replay: every SessionEventArgs batch pushed to a player is
    # recorded here (player actions AND AI actions), in send order. Stored as raw
    # event bytes + class id so the client's GameEventLog replay format can be
    # reconstructed offline.
    """
    CREATE TABLE IF NOT EXISTS session_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        target_player_uid TEXT NOT NULL,
        seq INTEGER NOT NULL,
        event_class INTEGER NOT NULL,
        event_bytes BLOB NOT NULL,
        sent_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_session_events ON session_events (session_id, seq)
    """,
    # Raw inbound PvP PlayerTransaction payloads. This is deliberately
    # separate from session_events: session_events contains server-to-client
    # packets used to build .replay files, while this table preserves the
    # client actions needed to replay rules resolution.
    """
    CREATE TABLE IF NOT EXISTS session_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        player_uid TEXT NOT NULL,
        received_seq INTEGER NOT NULL,
        received_at TEXT DEFAULT (datetime('now')),
        data_type INTEGER NOT NULL DEFAULT 3029,
        request_id INTEGER NOT NULL DEFAULT 0,
        compressed INTEGER NOT NULL DEFAULT 0,
        transaction_id INTEGER NOT NULL DEFAULT -1,
        transaction_type TEXT NOT NULL DEFAULT '',
        classification_json TEXT NOT NULL DEFAULT '{}',
        inner_bytes BLOB NOT NULL,
        pre_state_hash TEXT NOT NULL DEFAULT '',
        post_state_hash TEXT,
        status TEXT NOT NULL DEFAULT 'received',
        handled INTEGER,
        completed_at TEXT,
        error TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_session_transactions
        ON session_transactions (session_id, id)
    """,

    # Public replay index and generated replay artifact metadata.  The replay
    # worker starts alongside the server and queries this table immediately,
    # so it must be part of the canonical schema for fresh databases.
    """
    CREATE TABLE IF NOT EXISTS game_replays (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL UNIQUE,
        session_name TEXT NOT NULL UNIQUE,
        server_id TEXT NOT NULL DEFAULT '',
        session_flags INTEGER NOT NULL DEFAULT 0,
        start_time TEXT NOT NULL DEFAULT '',
        end_time TEXT NOT NULL DEFAULT '',
        tournament_round INTEGER NOT NULL DEFAULT -1,
        is_public INTEGER NOT NULL DEFAULT 1,
        series_format TEXT NOT NULL DEFAULT 'UNKNOWN',
        series_points INTEGER NOT NULL DEFAULT 0,
        series_template TEXT NOT NULL DEFAULT '',
        players_json TEXT NOT NULL DEFAULT '[]',
        winners_json TEXT NOT NULL DEFAULT '[]',
        replay_path TEXT NOT NULL DEFAULT '',
        generation_count INTEGER NOT NULL DEFAULT 0,
        event_count INTEGER NOT NULL DEFAULT 0,
        source_event_max_id INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'building',
        error TEXT NOT NULL DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_game_replays_end_time
        ON game_replays (end_time DESC)
    """,

    # --- champions / decks / campaign ---------------------------------------
    """
    CREATE TABLE IF NOT EXISTS champions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        champion_name TEXT NOT NULL,
        race INTEGER DEFAULT 1,
        champion_class INTEGER DEFAULT 3,
        gender INTEGER DEFAULT 1,
        champion_uid INTEGER DEFAULT 0,
        level INTEGER DEFAULT 1,
        xp INTEGER DEFAULT 0,
        last_campaign_id INTEGER DEFAULT 0,
        last_deck_id INTEGER DEFAULT 0,
        pet_name TEXT DEFAULT '',
        is_deleted INTEGER DEFAULT 0,
        talents TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        deck_name TEXT NOT NULL,
        cards TEXT DEFAULT '[]',
        pve_champion_id INTEGER DEFAULT NULL,
        pvp_champion_guid TEXT DEFAULT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        active_gems TEXT DEFAULT '{}',
        gem_abilities TEXT DEFAULT '{}',
        deck_sleeve_guid TEXT DEFAULT NULL,
        gameboard_guid TEXT DEFAULT NULL,
        coin_guid TEXT DEFAULT NULL,
        last_saved TEXT DEFAULT ''
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_decks_user_last_saved
        ON decks(user_id, last_saved DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS campaigns (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        camp_uid_lo INTEGER DEFAULT 0,
        camp_uid_hi INTEGER DEFAULT 0,
        champion_id INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        champion_name TEXT DEFAULT '',
        template_name TEXT DEFAULT 'AZ1',
        campaign_type TEXT DEFAULT 'AREA',
        is_started  INTEGER DEFAULT 0,
        state_json  TEXT,
        created_at  TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS campaign_node_edges (
        campaign_template TEXT NOT NULL,
        from_node TEXT NOT NULL,
        to_node TEXT NOT NULL,
        path_name TEXT DEFAULT '',
        PRIMARY KEY (campaign_template, from_node, to_node)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_campaign_node_edges_to
        ON campaign_node_edges(campaign_template, to_node)
    """,
    """
    CREATE TABLE IF NOT EXISTS campaign_node_conversations (
        campaign_template TEXT NOT NULL,
        node_id TEXT NOT NULL,
        conversation_guid TEXT NOT NULL,
        conversation_name TEXT NOT NULL DEFAULT '',
        trigger_json TEXT NOT NULL DEFAULT '{}',
        priority INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (campaign_template, node_id, conversation_guid)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_campaign_node_conversations_node
        ON campaign_node_conversations(campaign_template, node_id, priority)
    """,
    """
    CREATE TABLE IF NOT EXISTS quest_templates (
        script_name TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        objectives_json TEXT NOT NULL DEFAULT '[]',
        campaign_group TEXT NOT NULL DEFAULT 'AREA',
        start_hook TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quest_conversations (
        quest_script TEXT NOT NULL,
        conversation_guid TEXT NOT NULL,
        campaign_template TEXT NOT NULL,
        node_id TEXT NOT NULL,
        npc TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'start',
        faction TEXT NOT NULL DEFAULT '',
        conversation_name TEXT NOT NULL DEFAULT '',
        start_hook TEXT NOT NULL DEFAULT '',
        conditions_json TEXT NOT NULL DEFAULT '{}',
        priority INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (quest_script, conversation_guid)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_quest_conversations_lookup
        ON quest_conversations(conversation_guid, campaign_template, node_id, role)
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_rewards (
        conversation_guid TEXT PRIMARY KEY,
        reward_json TEXT NOT NULL DEFAULT '{}',
        one_time INTEGER NOT NULL DEFAULT 1,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,

    # --- arena (Frost Ring Arena) -------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS arena_state (
        user_id INTEGER PRIMARY KEY,
        deck_id INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0,
        challenger_index INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now')),
        fight_history TEXT DEFAULT '[]',
        gold_earned INTEGER DEFAULT 0,
        chests_earned INTEGER DEFAULT 0,
        sacks_earned INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fra_challengers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        challenger_index INTEGER NOT NULL,
        name TEXT NOT NULL,
        champion_guid TEXT NOT NULL,
        encounter_deck_guid TEXT NOT NULL,
        is_boss INTEGER NOT NULL DEFAULT 0,
        UNIQUE(user_id, challenger_index)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fra_challengers_user_rank
        ON fra_challengers(user_id, challenger_index)
    """,
    """
    CREATE TABLE IF NOT EXISTS fra_encounters (
        deck_guid TEXT PRIMARY KEY,
        deck_name TEXT NOT NULL,
        name TEXT NOT NULL,
        champion_guid TEXT NOT NULL,
        is_boss INTEGER DEFAULT NULL,
        tier INTEGER DEFAULT NULL,
        min_rank INTEGER DEFAULT NULL,
        max_rank INTEGER DEFAULT NULL,
        is_elite INTEGER NOT NULL DEFAULT 0,
        base_deck_name TEXT NOT NULL,
        set_guid TEXT NOT NULL DEFAULT '',
        deck_flavor TEXT NOT NULL DEFAULT '',
        deck_sleeve_guid TEXT NOT NULL DEFAULT '',
        equipment_ids_json TEXT NOT NULL DEFAULT '[]',
        dont_shuffle_first_n_cards INTEGER NOT NULL DEFAULT 0,
        maximum_duplicates INTEGER NOT NULL DEFAULT 0,
        maximum_total_cards INTEGER NOT NULL DEFAULT 0,
        opening_hand_size INTEGER DEFAULT NULL,
        encounter_deck_guid TEXT DEFAULT NULL,
        gameboard TEXT DEFAULT NULL,
        deck_texture TEXT DEFAULT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fra_encounters_tier_boss
        ON fra_encounters(tier, is_boss, is_elite)
    """,
    """
    CREATE TABLE IF NOT EXISTS fra_challenges (
        conversation_guid TEXT PRIMARY KEY,
        challenge_key TEXT NOT NULL UNIQUE,
        challenge_name TEXT NOT NULL,
        challenge_order INTEGER NOT NULL DEFAULT 0,
        probability_percent INTEGER NOT NULL DEFAULT 5,
        owner_name TEXT NOT NULL DEFAULT '',
        champion_guid TEXT NOT NULL DEFAULT '',
        build_tag TEXT NOT NULL DEFAULT '',
        dialogue_text TEXT NOT NULL DEFAULT '',
        answer_text TEXT NOT NULL DEFAULT '',
        objective_heading TEXT NOT NULL DEFAULT '',
        objective_text TEXT NOT NULL DEFAULT '',
        modifications_json TEXT NOT NULL DEFAULT '[]',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        enabled INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fra_challenges_order
        ON fra_challenges(challenge_order, enabled)
    """,

    # --- mail / chat --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS emails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        sender TEXT NOT NULL DEFAULT 'SYSTEM',
        subject TEXT NOT NULL,
        body TEXT,
        sent_at TEXT DEFAULT (datetime('now')),
        read_at TEXT DEFAULT NULL,
        gold_delivered INTEGER DEFAULT 0,
        platinum_delivered INTEGER DEFAULT 0,
        claimed_at TEXT DEFAULT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        sender TEXT NOT NULL,
        room TEXT NOT NULL,
        message TEXT NOT NULL,
        icon TEXT DEFAULT '',
        flags TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'utc'))
    )
    """,

    # --- friends / social ---------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS friends (
        user_id INTEGER NOT NULL REFERENCES users(id),
        friend_user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, friend_user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS friend_requests (
        from_user_id INTEGER NOT NULL REFERENCES users(id),
        to_user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (from_user_id, to_user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ignored_players (
        user_id INTEGER NOT NULL REFERENCES users(id),
        ignored_user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, ignored_user_id)
    )
    """,

    # --- battle sessions ----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS game_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        session_id INTEGER NOT NULL,
        card_uid INTEGER NOT NULL,
        card_template_id INTEGER NOT NULL,
        location TEXT DEFAULT 'deck',
        position INTEGER DEFAULT 0,
        is_champion BOOLEAN DEFAULT 0,
        card_type TEXT DEFAULT 'Unknown',
        template_guid TEXT,
        card_state INTEGER DEFAULT 0,
        card_attributes INTEGER DEFAULT 0,
        temporary_attributes INTEGER DEFAULT 0,
        card_abilities TEXT DEFAULT '[]',
        owner_user_id INTEGER DEFAULT 0,
        card_uses TEXT DEFAULT '{}',
        card_attack_mod INTEGER DEFAULT 0,
        card_defense_mod INTEGER DEFAULT 0,
        card_cost_mod INTEGER DEFAULT 0,
        cost_mod_json TEXT DEFAULT '[]',
        card_damage INTEGER DEFAULT 0,
        permanent_buffs TEXT DEFAULT '{}',
        temporary_buffs TEXT DEFAULT '{}',
        original_template_guid TEXT DEFAULT '',
        resolved_at INTEGER DEFAULT 0,
        gems INTEGER DEFAULT 0
    )
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_game_cards_session ON game_cards(session_id)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_game_cards_session_card
        ON game_cards(session_id, card_uid)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_game_cards_session_owner_zone_position
        ON game_cards(session_id, user_id, location, position)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_game_cards_card ON game_cards(card_uid)
    """,

    # Per-user battle preferences (persist across sessions).
    """
    CREATE TABLE IF NOT EXISTS user_prefs (
        user_id INTEGER PRIMARY KEY,
        self_stops TEXT,
        opp_stops TEXT
    )
    """,
]

# ---------------------------------------------------------------------------
# Static seed data.
# ---------------------------------------------------------------------------

# Booster / deck store catalogue.
STORE_ITEMS = [
    ("a8b78207-686a-4994-b6cd-4548d1349841", "Set 1: Shards of Fate", "17 cards from Shards of Fate", 100, "Gold", "ShopBoosterTab"),
    ("f37fc1bc-4d7b-4bab-a2a2-770957d9a7b1", "Set 2: Shattered Destiny", "17 cards from Shattered Destiny", 200, "Gold", "ShopBoosterTab"),
    ("237866c1-aea2-4cb4-89ca-418babda3595", "Set 3: Armies of Myth", "17 cards from Armies of Myth", 300, "Gold", "ShopBoosterTab"),
    ("a8e324e3-b9fb-4bb6-b659-f2773982aed2", "Set 4: Primal Dawn", "17 cards from Primal Dawn", 400, "Gold", "ShopBoosterTab"),
    ("84c65d9b-779b-4128-879d-b0779e6e6edc", "Set 5: Herofall", "17 cards from Herofall", 500, "Gold", "ShopBoosterTab"),
    ("63273f9b-5f4d-4db7-a418-fc6e2c4c9900", "Set 6: Scars of War", "17 cards from Scars of War", 600, "Platinum", "ShopBoosterTab"),
    ("df144885-0fb5-4238-942c-79b35870dabc", "Set 7: Frostheart", "17 cards from Frostheart", 700, "Platinum", "ShopBoosterTab"),
    ("902193e6-645b-41be-ac51-23196335b788", "Set 8: Dead of Winter", "17 cards from Dead of Winter", 800, "Platinum", "ShopBoosterTab"),
    ("3dacd91b-84f1-4ff9-99df-dae3f7740702", "Set 9", "17 cards from Set 9", 900, "Platinum", "ShopBoosterTab"),
    ("3346fb56-d7e0-424d-906b-7c19352b64a2", "Common Only Booster", "Common cards only", 50, "Gold", "ShopBoosterTab"),
    ("0b762d7f-04cd-4d43-9f78-c62af5e737d0", "Starter Pack", "Starter pack", 500, "Gold", "collectordeck"),
    ("2822821c-fb21-4ac1-8bfc-5aabd4b362fa", "Dwarf Starter Deck", "Dwarf starter deck", 750, "Gold", "collectordeck"),
    ("32409ae4-f154-405a-8295-3511561c7790", "Human Starter Deck", "Human starter deck", 750, "Gold", "collectordeck"),
    ("decc9d0a-c30c-4c9b-a92d-228210950d52", "Orc Starter Deck", "Orc starter deck", 750, "Gold", "collectordeck"),
    ("e7926be0-1c97-466c-9ccd-d8309f9e8703", "Shin'hare Starter Deck", "Shin'hare starter deck", 750, "Gold", "collectordeck"),
    ("28f593e9-0d65-49f4-9d0d-4fe8bf5b06bd", "Coyotle Starter Deck", "Coyotle starter deck", 750, "Gold", "collectordeck"),
    ("ef60488d-b7bd-4869-b98f-3adc153037ba", "Elf Starter Deck", "Elf starter deck", 750, "Gold", "collectordeck"),
    ("a2138381-c7a8-4a91-ad75-084c803fdcc5", "Necrotic Starter Deck", "Necrotic starter deck", 750, "Gold", "collectordeck"),
    ("c34c11b9-26f7-4c04-a334-3778b3fcfd59", "Vennen Starter Deck", "Vennen starter deck", 750, "Gold", "collectordeck"),
    ("8d20082a-4163-4f42-8fce-d4c056f9da04", "Primal Pack: Set 1", "Primal pack - Shards of Fate", 500, "Platinum", "special"),
    ("653f153b-8288-4ece-a304-2804c1e2ffb9", "Primal Pack: Set 2", "Primal pack - Shattered Destiny", 500, "Platinum", "special"),
    ("7a6424ba-8b53-4050-82df-ed756406d87c", "Primal Pack: Set 3", "Primal pack - Armies of Myth", 500, "Platinum", "special"),
    ("1db5d6c5-47eb-45e8-b98c-8441ed09e590", "Primal Pack: Set 4", "Primal pack - Primal Dawn", 500, "Platinum", "special"),
    ("e296f8e0-680f-41e3-a764-7e9953b10be5", "Dwarf Tribe Booster", "Dwarf-themed cards", 150, "Gold", "special"),
    ("4625f688-2987-4c57-8a8c-c1ab5b5ea744", "Human Tribe Booster", "Human-themed cards", 150, "Gold", "special"),
    ("6307c815-0473-4f86-8967-2e3401730f74", "Orc Tribe Booster", "Orc-themed cards", 150, "Gold", "special"),
    ("91c91bad-30a9-4c0c-9951-7c9432345da7", "Shin'hare Tribe Booster", "Shin'hare-themed cards", 150, "Gold", "special"),
    ("1e94b92a-a7a9-4843-88a9-6c51a6699b79", "Coyotle Tribe Booster", "Coyotle-themed cards", 150, "Gold", "special"),
    ("dde85dd8-9b2d-4b66-a2d6-11d4df385e7f", "Elf Tribe Booster", "Elf-themed cards", 150, "Gold", "special"),
    ("aa12781d-effa-47fc-8306-b1a4c491551f", "Necrotic Tribe Booster", "Necrotic-themed cards", 150, "Gold", "special"),
    ("d4de5a3a-6eba-4ebc-856d-1f161c675c83", "Vennen Tribe Booster", "Vennen-themed cards", 150, "Gold", "special"),
    ("949a018e-8360-4623-b3da-dc915dcd1b38", "Kismet's Reverie I", "Kismet pack", 1000, "Platinum", "special"),
    ("8d0947d9-91f1-4258-a965-d08dc97a8966", "Kismet's Reverie II", "Kismet pack", 1000, "Platinum", "special"),
    ("c37d2d3b-a6ec-42bd-b75c-5faf27441dca", "Kismet's Reverie III", "Kismet pack", 1000, "Platinum", "special"),
    ("d49e1966-be68-4f17-a6eb-6cd11d43ada0", "Kismet's Reverie IV", "Kismet pack", 1000, "Platinum", "special"),
    ("7b8390fd-7d3d-44d4-b285-1aeae3aef98b", "AZ1 Campaign Booster", "PvE campaign cards", 300, "Gold", "special"),
    ("d77471f5-7b32-4464-9552-c992a38fb4be", "AZ2 Campaign Booster", "PvE campaign cards", 300, "Gold", "special"),
    ("cbf0f839-6827-4d94-b9c5-2a38389cf6a7", "Set 9 Draft Pack", "Draft pack", 500, "Platinum", "special"),
    ("b3684e64-7634-4055-960c-620569160d2d", "Ardent Alliance Starter", "Dueling Pit starter", 1000, "Gold", "collectordeck"),
    ("49c835d9-a0e1-4560-b5b8-e53f7193624c", "Ardent Fervor Starter", "Dueling Pit starter", 1000, "Gold", "collectordeck"),
    ("f3551d71-8e68-466d-9012-f89efa8b3bb5", "Deadling Swarm Starter", "Dueling Pit starter", 1000, "Gold", "collectordeck"),
    ("f3ecfc7f-8eef-4dbd-a950-c1a8c82f0eea", "Reckless Assault Starter", "Dueling Pit starter", 1000, "Gold", "collectordeck"),
    ("09ed8e8a-cee9-4eee-9ef6-3a8a32948d8a", "Underworld Schemes Starter", "Dueling Pit starter", 1000, "Gold", "collectordeck"),
    ("ed4e3d9a-afd0-471e-8912-386cc22ff0c8", "Valorous Victory Starter", "Dueling Pit starter", 1000, "Gold", "collectordeck"),
    ("3c1a8175-d137-40f4-86ee-3d39e0b1158d", "PvE Champion: Coyotle", "PvE champion starter", 800, "Gold", "collectordeck"),
    ("a0484173-ad18-42d2-a4b8-08cc21088c69", "PvE Champion: Dwarf", "PvE champion starter", 800, "Gold", "collectordeck"),
    ("15eb25d9-359e-4dc2-ba54-a76462303723", "PvE Champion: Elf", "PvE champion starter", 800, "Gold", "collectordeck"),
    ("752ef2e4-8eb3-4e24-93f2-6bf707c0aaff", "PvE Champion: Human", "PvE champion starter", 800, "Gold", "collectordeck"),
    ("1351fed5-a0d5-44be-892c-78f4b40f7eb1", "PvE Champion: Necrotic", "PvE champion starter", 800, "Gold", "collectordeck"),
    ("04a4dd5b-453f-468f-84ff-c9207c637525", "PvE Champion: Orc", "PvE champion starter", 800, "Gold", "collectordeck"),
    ("cdfaf80e-4564-4690-9bfe-9486e0a9dbc1", "PvE Champion: Shin'hare", "PvE champion starter", 800, "Gold", "collectordeck"),
    ("9672371b-00aa-49b0-b16a-aaa8aab45c73", "PvE Champion: Vennen", "PvE champion starter", 800, "Gold", "collectordeck"),
    ("1fc81645-2f4c-42d7-818a-9b1fba9c49eb", "PvE Champion Starter 1", "PvE champion starter", 800, "Gold", "collectordeck"),
    ("adb5ec90-88aa-4237-ba5a-cc58f620dcfc", "PvE Champion Starter 2", "PvE champion starter", 800, "Gold", "collectordeck"),
    ("5e338ab1-da47-41ce-b980-4020f1b5b4fc", "Full Set: Shards of Fate", "Every card from Set 1", 5000, "Platinum", "special"),
    ("17e0a0ff-7ec3-4ed7-a261-adb4dcfd6625", "Full Set: Shattered Destiny", "Every card from Set 2", 5000, "Platinum", "special"),
    ("3bdc41b5-c265-4616-812e-9f1965787c33", "Full Set: Armies of Myth", "Every card from Set 3", 5000, "Platinum", "special"),
    ("5bdb9ea1-9e03-42af-a23c-6968fff55ce5", "Full Set: Primal Dawn", "Every card from Set 4", 5000, "Platinum", "special"),
    ("37fb3559-6f2e-4f53-b6d7-05c28c6c075d", "Full Set: Herofall", "Every card from Set 5", 5000, "Platinum", "special"),
    ("a9ae9af2-e27a-48e0-9cd2-490d252fffe4", "Common Chest (Test)", "Test chest", 10, "Gold", "special"),
    ("fc805076-1e91-4846-8b84-987c7779f7b9", "Ardent Assault", "Dueling Pit starter", 1000, "Gold", "collectordeck"),
    ("45094301-308e-4b90-989f-62cfc4be4f51", "Collector's Deck - Shamrock, The Goldfather", "Collector deck", 1000, "Platinum", "collectordeck"),
    ("f122e840-9e13-4c6d-9cd8-c94eff0e663c", "Collector's Deck - Eyes of the Heart", "Collector deck", 1000, "Platinum", "collectordeck"),
    ("c547ecaf-240b-459d-bf3e-3ee5ab69b3df", "Collector's Deck - Hero of Legend", "Collector deck", 1000, "Platinum", "collectordeck"),
    ("9bcd6ba9-b49d-48a3-9929-2f26f603dea2", "Collector's Deck - Locke of the Pack", "Collector deck", 1000, "Platinum", "collectordeck"),
    ("030b84da-6adb-4177-bd1d-8602dd3be2fe", "Collector's Deck - Lord Blightbark", "Collector deck", 1000, "Platinum", "collectordeck"),
    ("f5b4a7b1-8b5e-476d-800c-d37090c51c0d", "Collector's Deck - Lyvaanth", "Collector deck", 1000, "Platinum", "collectordeck"),
    ("d9d666b9-8f78-42b7-9f56-9ff811d8aede", "Collector's Deck - Sunsoul Phoenix", "Collector deck", 1000, "Platinum", "collectordeck"),
    ("62abd6a9-f58c-4a27-9d93-98cde9d093fb", "Collector's Deck - The Librarian", "Collector deck", 1000, "Platinum", "collectordeck"),
    ("e7b9790f-7360-4480-8832-3286fbf87889", "Starter Pack - Coyotle", "Starter pack", 1000, "Gold", "collectordeck"),
    ("fc9d814d-61da-4280-af93-503b957803f4", "Starter Deck - Dwarves", "Starter deck", 1000, "Gold", "collectordeck"),
    ("08c5bcd9-bcb9-48b4-a540-6ab3e0b28609", "Starter Deck - Elf", "Starter deck", 1000, "Gold", "collectordeck"),
    ("0ab2642f-0e03-49bb-97b8-db0a543eca2f", "Starter Deck - Humans", "Starter deck", 1000, "Gold", "collectordeck"),
    ("f2a4d7a5-0595-4757-9014-26bbccccdebb", "Starter Deck - Necrotic", "Starter deck", 1000, "Gold", "collectordeck"),
    ("393a1905-b31e-4435-85cc-530c7c8dd733", "Starter Deck - Orcs", "Starter deck", 1000, "Gold", "collectordeck"),
    ("fffd4e8b-f22e-403c-a684-69c06b11a452", "Pathfinder Control", "Prebuilt deck", 1000, "Gold", "collectordeck"),
    ("966cff30-4c16-4379-9361-1ecefba2d6a7", "Starter Deck - Shin'hare", "Starter deck", 1000, "Gold", "collectordeck"),
    ("13ce6004-3d86-40ce-8886-e9e8f0e563ac", "Havoc's Rowdy Sugar Rush", "Prebuilt deck", 2500, "Gold", "collectordeck"),
    ("b593d10b-d5c6-43bb-b001-50784b53512b", "NeroJinous' Momentum Mastery", "Prebuilt deck", 2500, "Gold", "collectordeck"),
    ("c1e7d15e-90b9-4c5e-b20b-97cd16bd244a", "Snake's Refuel Rampage", "Prebuilt deck", 2500, "Gold", "collectordeck"),
    ("0ce09934-9846-41de-a02c-7920d240441b", "Starter Deck - Vennen", "Starter deck", 1000, "Gold", "collectordeck"),
    ("0951326e-2535-4a65-b410-f2490e54b3f5", "Yotul Burn", "Prebuilt deck", 1000, "Gold", "collectordeck"),
]

# Chest drop probabilities.
CHEST_PROBABILITIES = [
    ("Common", 0.800, 800),
    ("Uncommon", 0.150, 150),
    ("Rare", 0.045, 45),
    ("Legendary", 0.004, 4),
    ("Primal", 0.001, 1),
]

# Redeem codes (gold_delta, plat_delta, max_uses).
REDEEM_CODES = [
    ("5000plat", 0, 5000, 10),
    ("10000gold", 10000, 0, 10),
    ("5000gold", 5000, 0, 10),
    ("5000all", 5000, 5000, 10),
    ("expiredcode", 0, 0, 0),
]

# Crayburn Castle's race-specific report-success conversations.  The reward
# payload is deliberately JSON so later campaign conversations can add cards,
# platinum, or other reward types without another schema migration.  These
# entries are one-time rewards; INSERT OR IGNORE preserves local adjustments.
CONVERSATION_REWARD_SEEDS = [
    ("9c139a1c-40a4-4ed7-b6ff-378c6f6bc1ea", '{"gold": 150, "xp": 500, "chest_guid": "c96a6213-69ac-44d2-8b35-32ad6d55b981"}', 1),  # Coyotle
    ("21c62741-49c8-4da5-8b8d-440247911027", '{"gold": 150, "xp": 500, "chest_guid": "8b145038-961b-4f1c-93f7-3d81dcb3d39b"}', 1),  # Dwarf
    ("bbc4460d-8452-4d49-a6e9-c64216f483b3", '{"gold": 150, "xp": 500, "chest_guid": "2780fa12-7cf5-41bb-a19c-7a8496a33fed"}', 1),  # Elf
    ("0427b61f-251c-47b5-b89d-a0f2e2f42b1a", '{"gold": 150, "xp": 500, "chest_guid": "5d2fb7e1-702e-424f-afe0-0b1bbb914e7f"}', 1),  # Human
    ("922aa66e-e41c-41ad-94a2-adb84f356430", '{"gold": 150, "xp": 500, "chest_guid": "4f006210-7c29-438c-ad6e-89d43831fa25"}', 1),  # Necrotic
    ("463a5ea3-847f-4102-83e4-512eb0ca97ab", '{"gold": 150, "xp": 500, "chest_guid": "e748185d-e993-44a5-bbe3-bb48acb3c96e"}', 1),  # Orc
    ("5f119ac9-1018-4161-9b87-2cc23dba9c71", '{"gold": 150, "xp": 500, "chest_guid": "26f41860-aa46-418f-bf94-46fdb484160b"}', 1),  # Shin'hare
    ("49eaafb9-6645-4c04-9966-5190c4e8ca3d", '{"gold": 150, "xp": 500, "chest_guid": "a7f16a72-2975-464d-8d3a-ff6f5bfd7c3b"}', 1),  # Vennen
]

# Fixed contents of the race-specific Crayburn Castle reward chests.  These
# are keyed by the chest template GUID because these Promo chests do not have
# a card-set GUID.  The lists contain standard card printings plus the
# authored PvE champion card for each race; pack opening grants one copy of
# every entry.
CRAYBURN_PACK_CARD_SEEDS = {
    # Crayburn Castle (Human) Pack
    "5d2fb7e1-702e-424f-afe0-0b1bbb914e7f": [
        "605e3c79-21db-4281-927c-7685f39144e8",  # Noble Citizenry
        "2f07d3ba-1eb4-40b2-b114-dd2f36f6211b",  # Phoenix Guard Trainer
        "548aadae-036a-4c19-8e06-10dd8a834ea0",  # Captain of the Dragon Guard
        "00e13fdf-b2c3-4fe7-a064-ce4481b24e8d",  # Shards of Fate
        "c5b4b6a4-00cd-4488-8750-60c1a3eccce5",  # Gareth Kay
    ],
    # Crayburn Castle (Elf) Pack
    "2780fa12-7cf5-41bb-a19c-7a8496a33fed": [
        "a5b56a7b-4f1a-499b-b7af-b67c1dcecddd",  # Rotroot Enchanter
        "ab5b62ce-68b0-4f98-ab5c-7141b72f8f57",  # Emberleaf Wardancer
        "730ea063-830a-4433-a3e9-e62972dda465",  # Merry Minstrels
        "999085c0-401f-47d0-94aa-27d261b18815",  # Rootforged Regalia
        "642b5c7a-4591-47ce-a41b-b665ec25c6bc",  # Nerissa
    ],
    # Crayburn Castle (Coyotle) Pack
    "c96a6213-69ac-44d2-8b35-32ad6d55b981": [
        "10604fdb-ccb7-47a6-afcd-3b706a129eb5",  # Nightsky Stargazer
        "74454a5c-1fd5-4d89-a775-72b96830f247",  # Brightmoon Brave
        "306051ab-e7df-48a4-ad59-015c38551f03",  # Tyrannosaurus Hex
        "00e13fdf-b2c3-4fe7-a064-ce4481b24e8d",  # Shards of Fate
        "83f19fe5-4922-4a4e-9dfc-e5adb68da241",  # Whispering Breeze
    ],
    # Crayburn Castle (Orc) Pack
    "e748185d-e993-44a5-bbe3-bb48acb3c96e": [
        "14909185-1070-48df-9508-61d5a9650bd2",  # Darkspire Priestess
        "8a78b93c-a5af-4f8f-b7b0-82785cc09be1",  # Mazat Spearman
        "e072ecea-83b1-4730-9e24-f45974bf2c0a",  # Wrathseeker
        "4af069b0-ba22-45cc-bdeb-18175abc2b4e",  # Assault Bot
        "0db7cf77-7e41-4f08-801b-0aa3d5bf7b41",  # Moqui
    ],
    # Crayburn Castle (Dwarf) Pack
    "8b145038-961b-4f1c-93f7-3d81dcb3d39b": [
        "9c0ad719-2ca8-4e85-b428-6d4d487db971",  # Axe Bot
        "98dc762b-6444-46cf-8993-e706c0fc01c0",  # Researcher Adept
        "e1ff963c-6959-41e6-84a1-683ad3dfd530",  # War Bot Bunker
        "4335c1aa-5c0d-4a97-b226-3a427ef2fa13",  # Construction Plans: War Hulk
        "e0365f0b-0251-48c2-a16c-5fe4df7e214c",  # Glendower
    ],
    # Crayburn Castle (Necrotic) Pack
    "4f006210-7c29-438c-ad6e-89d43831fa25": [
        "1b5eb250-112d-4cb8-9e96-4e3fae7b9dde",  # Call the Grave
        "3e59b02f-a9e5-4eb5-b03e-c1fe295906fb",  # Spiritbound Spy
        "669eb23c-868f-4df7-b6b3-328f6dc3eb32",  # Deepgaze Acolyte
        "00e13fdf-b2c3-4fe7-a064-ce4481b24e8d",  # Shards of Fate
        "8fe56a07-b697-4bb6-86f2-60f0d02db52c",  # Iddi
    ],
    # Crayburn Castle (Shinhare) Pack
    "26f41860-aa46-418f-bf94-46fdb484160b": [
        "bfce3e26-1d85-4d9f-a72a-f4cead4c93c1",  # Concubunny
        "306051ab-e7df-48a4-ad59-015c38551f03",  # Tyrannosaurus Hex
        "d08b01e8-2cb1-4e9a-b829-097b3030239b",  # Shroomtank
        "a916c540-9f4e-4af4-8237-78243e437e38",  # Ebony Pawn
        "81cc3482-c9ed-4f2f-b85e-11b37911c99e",  # Sora
    ],
    # Crayburn Castle (Vennen) Pack
    "a7f16a72-2975-464d-8d3a-ff6f5bfd7c3b": [
        "428b4342-937d-4241-83d1-2c54e8975fb5",  # Hatchery Priest
        "98ea62ca-fdb4-4684-8896-b4e6819bf4de",  # Incubation Webs
        "c339dd6b-f4a4-4d86-a50f-09800d569dd4",  # Vicious Vivisector
        "148e0956-246a-4224-bf96-0958aa9c3ef6",  # Shield Bot
        "a92836fd-cd8f-4741-86b6-8132e9939358",  # Zilth
    ],
}

# AZ1 map topology.  Node positions and visuals remain client-owned by the
# NodesPrefab; the server only needs the graph to validate movement and reveal
# fog-of-war neighbours.  This seed contains the authored opening route and
# the confirmed direct Dunnwood -> Road of Oaks branch.  Additional extracted
# edges can be inserted without changing campaign logic.
_AZ1_PATH_NAMES = """
Path_Node001_Node002 Path_Node002_Node003 Path_Node003_Node004
Path_Node003_Node005 Path_Node003_Node007 Path_Node005_Node006
Path_Node007_Node009 Path_Node007_Node00R Path_Node008_Node025
Path_Node009_Node010 Path_Node009_Node012 Path_Node00R_Node012
Path_Node00R_Node025 Path_Node00X_Node021 Path_Node00Y_Node00Z
Path_Node00Y_Node048 Path_Node011_Node025 Path_Node012_Node015
Path_Node013_Node016 Path_Node015_Node016 Path_Node016_Node017
Path_Node016_Node018 Path_Node016_Node019 Path_Node016_Node030
Path_Node019_Node021 Path_Node019_Node023 Path_Node019_Node077
Path_Node020_Node040 Path_Node020_Node042 Path_Node021_Node022
Path_Node023_Node026 Path_Node024_Node029 Path_Node026_Node027
Path_Node027_Node028 Path_Node027_Node029 Path_Node030_Node031
Path_Node030_Node038 Path_Node031_Node032 Path_Node032_Node033
Path_Node033_Node038 Path_Node034_Node038 Path_Node035_Node057
Path_Node037_Node064 Path_Node037_Node070 Path_Node038_Node039
Path_Node038_Node044 Path_Node039_Node040 Path_Node041_Node052
Path_Node042_Node065 Path_Node044_Node045 Path_Node044_Node046
Path_Node046_Node047 Path_Node046_Node048 Path_Node048_Node049
Path_Node048_Node050 Path_Node048_Node064 Path_Node050_Node051
Path_Node051_Node053 Path_Node051_Node066 Path_Node052_Node064
Path_Node053_Node054 Path_Node053_Node060 Path_Node053_Node063
Path_Node054_Node055 Path_Node055_Node056 Path_Node056_Node057
Path_Node057_Node058 Path_Node057_Node061 Path_Node058_Node059
Path_Node059_Node060 Path_Node061_Node062 Path_Node062_Node063
Path_Node064_Node073 Path_Node067_Node068 Path_Node067_Node070
Path_Node068_Node069 Path_Node071_Node073 Path_Node071_Node074
Path_Node074_Node075 Path_Node075_Node078
""".split()

_AZ2_PATH_NAMES = """
Path_Node001_Node003 Path_Node001_Node005 Path_Node001_Node057
Path_Node001_Node099 Path_Node002_Node057 Path_Node003_Node057
Path_Node004_Node008 Path_Node004_Node009 Path_Node005_Node006
Path_Node005_Node008 Path_Node006_Node007 Path_Node009_Node010
Path_Node00B_NodeB01 Path_Node00B_NodeB02 Path_Node00B_NodeB03
Path_Node00Z_Node099 Path_Node010_Node011 Path_Node011_Node012
Path_Node012_Node013 Path_Node012_Node014 Path_Node013_Node017
Path_Node014_Node015 Path_Node015_Node016 Path_Node016_Node017
Path_Node016_Node023 Path_Node017_Node023 Path_Node018_Node022
Path_Node018_Node024 Path_Node018_Node027 Path_Node018_Node028
Path_Node019_Node019B Path_Node019_Node023 Path_Node020_Node021
Path_Node020_Node023 Path_Node021_Node022 Path_Node023_Node025
Path_Node024_Node025 Path_Node024_Node026 Path_Node028_Node029
Path_Node029_Node030 Path_Node030_Node031 Path_Node031_Node032
Path_Node031_Node033 Path_Node032_Node057 Path_Node033_Node034
Path_Node033_Node042 Path_Node034_Node035 Path_Node035_Node036
Path_Node035_Node056 Path_Node036_Node037 Path_Node037_Node038
Path_Node038_Node039 Path_Node039_Node040 Path_Node040_Node041
Path_Node042_Node043 Path_Node042_Node049 Path_Node043_Node044
Path_Node044_Node045 Path_Node045_Node046 Path_Node046_Node047
Path_Node047_Node048 Path_Node049_Node050 Path_Node050_Node051
Path_Node050_Node052 Path_Node050_Node055 Path_Node051_Node054
Path_Node052_Node053 Path_NodeB01_NodeB02 Path_NodeB01_NodeB04
Path_NodeB02_NodeB03 Path_NodeB02_NodeB05 Path_NodeB03_NodeB06
Path_NodeB03_NodeB07 Path_NodeB04_NodeB08 Path_NodeB04_NodeB09
Path_NodeB04_NodeB18 Path_NodeB05_NodeB06 Path_NodeB05_NodeB10
Path_NodeB05_NodeB13 Path_NodeB06_NodeB07 Path_NodeB06_NodeB14
Path_NodeB08_NodeB11 Path_NodeB09_NodeB10 Path_NodeB09_NodeB11
Path_NodeB09_NodeB12 Path_NodeB0X_NodeB17 Path_NodeB10_NodeB18
Path_NodeB11_NodeB15 Path_NodeB12_NodeB15 Path_NodeB13_NodeB14
Path_NodeB13_NodeB15 Path_NodeB13_NodeB16 Path_NodeB14_NodeB17
Path_NodeB16_NodeB17
""".split()


def _path_seed_rows(template, path_names):
    rows = []
    for path in path_names:
        parts = path.removeprefix("Path_").split("_")
        if len(parts) == 2:
            rows.append((template, parts[0], parts[1], path))
    return rows


AZ1_NODE_EDGE_SEEDS = (_path_seed_rows("AZ1", _AZ1_PATH_NAMES) +
                       _path_seed_rows("AZ2", _AZ2_PATH_NAMES))


# ---------------------------------------------------------------------------
# ensure_schema(db) — create tables and re-seed static data if missing.
# ---------------------------------------------------------------------------


def _migrate_short_tournament_ids(db):
    """Move tournament IDs into the range the legacy client can format."""
    rows = db.execute(
        "SELECT id FROM tournaments WHERE id < 10000 ORDER BY id"
    ).fetchall()
    if not rows:
        return

    used = {int(row[0]) for row in db.execute(
        "SELECT id FROM tournaments WHERE id >= 10000").fetchall()}
    next_id = max(10000, max(used, default=9999) + 1)
    migrations = []
    for (old_id,) in rows:
        while next_id in used:
            next_id += 1
        migrations.append((int(old_id), next_id))
        used.add(next_id)
        next_id += 1

    for old_id, new_id in migrations:
        db.execute(
            "INSERT INTO tournaments "
            "(id, type_id, status, players_json, session_id, created_at) "
            "SELECT ?, type_id, status, players_json, session_id, created_at "
            "FROM tournaments WHERE id=?",
            (new_id, old_id),
        )
        for table in ("tournament_decks", "tournament_signups",
                      "tournament_matches"):
            db.execute(
                f"UPDATE {table} SET tournament_id=? WHERE tournament_id=?",
                (new_id, old_id),
            )
        db.execute(
            "UPDATE game_sessions SET session_name=? "
            "WHERE session_name=?",
            (f"tourney-{new_id}", f"tourney-{old_id}"),
        )
        db.execute("DELETE FROM tournaments WHERE id=?", (old_id,))

    db.commit()


def ensure_schema(db):
    """Create all tables and (re)seed static reference data.

    Safe to call on every startup: CREATE IF NOT EXISTS is a no-op for
    existing tables, and each seed check only inserts when empty.
    """
    for stmt in DDL:
        db.execute(stmt)
    db.commit()

    # Seed map topology independently of SceneData.  Store both directions so
    # the campaign handler can perform a simple adjacency lookup regardless of
    # which endpoint the client reports as the current node.
    try:
        edge_rows = []
        for template, start, end, path_name in AZ1_NODE_EDGE_SEEDS:
            edge_rows.append((template, start, end, path_name))
            edge_rows.append((template, end, start, path_name))
        db.executemany(
            "INSERT OR IGNORE INTO campaign_node_edges "
            "(campaign_template,from_node,to_node,path_name) VALUES (?,?,?,?)",
            edge_rows,
        )
        db.commit()
    except Exception as exc:
        print(f"Campaign node edge seed skipped: {exc}")

    # ConversationTemplate names retain the AZ1/AZ2 node relationship even
    # though the extracted SceneData does not retain Unity map path data.
    # Populate this catalog incrementally so campaign.py can resolve authored
    # conversations without hard-coding hundreds of GUIDs.
    try:
        templates = {
            row[0] for row in db.execute(
                "SELECT DISTINCT campaign_template "
                "FROM campaign_node_conversations"
            ).fetchall()
        }
        if not {"AZ1", "AZ2"}.issubset(templates):
            from AssetExtraction.gamedata_seed import (
                extract_campaign_node_conversations,
            )
            conversation_rows = extract_campaign_node_conversations()
            db.executemany(
                "INSERT OR IGNORE INTO campaign_node_conversations "
                "(campaign_template,node_id,conversation_guid,"
                "conversation_name,trigger_json,priority,enabled) "
                "VALUES (?,?,?,?,?,?,?)",
                conversation_rows,
            )
            db.commit()
            print(
                "Seeded AZ1/AZ2 campaign node conversations: "
                f"{len(conversation_rows)} authored rows"
            )
    except Exception as exc:
        print(f"Campaign node conversation seed skipped: {exc}")

    # QuestTemplate records provide the objective definitions while authored
    # conversation names identify the NPC/node and quest stage.  Keep both in
    # server-owned tables so campaign.py can grant quests and show !/? markers
    # without hard-coded quest-specific branches.
    try:
        from AssetExtraction.gamedata_seed import (
            extract_quest_conversations, extract_quest_templates,
        )
        qcols = {row[1] for row in db.execute("PRAGMA table_info(quest_templates)")}
        if "start_hook" not in qcols:
            db.execute("ALTER TABLE quest_templates ADD COLUMN start_hook TEXT DEFAULT ''")
        if not db.execute("SELECT 1 FROM quest_templates LIMIT 1").fetchone():
            quest_rows = extract_quest_templates()
            db.executemany(
                "INSERT OR IGNORE INTO quest_templates "
                "(script_name,name,title,objectives_json,campaign_group,start_hook,enabled) "
                "VALUES (?,?,?,?,?,?,?)", quest_rows)
        if not db.execute("SELECT 1 FROM quest_conversations LIMIT 1").fetchone():
            quest_conversation_rows = extract_quest_conversations()
            db.executemany(
                "INSERT OR IGNORE INTO quest_conversations "
                "(quest_script,conversation_guid,campaign_template,node_id,npc,"
                "role,faction,conversation_name,start_hook,conditions_json,"
                "priority,enabled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                quest_conversation_rows,
            )
        # Backfill faction metadata for generic Tamed conversations whose
        # authored names identify the NPC but omit the faction suffix.
        db.execute(
            "UPDATE quest_conversations SET faction='Ardent' "
            "WHERE faction='' AND lower(npc)='belarius'"
        )
        db.execute(
            "UPDATE quest_conversations SET faction='Underworld' "
            "WHERE faction='' AND lower(npc)='takumi'"
        )
        # Existing databases predate the quest-template hook column.  Keep
        # the migration idempotent and backfill the two current map hooks.
        db.execute("UPDATE quest_templates SET start_hook='az1_tamed_start' "
                   "WHERE script_name='az01_tamed' AND (start_hook IS NULL OR start_hook='')")
        db.execute("UPDATE quest_templates SET start_hook='az1_find_horwich_sea_start' "
                   "WHERE script_name='q_seawitch' AND (start_hook IS NULL OR start_hook='')")
        db.execute("UPDATE quest_templates SET start_hook='az1_find_cave_in_start' "
                   "WHERE script_name='az01_uw_find_cave_in' AND (start_hook IS NULL OR start_hook='')")
        db.execute("UPDATE quest_templates SET start_hook='az1_find_ambling_mesa_start' "
                   "WHERE script_name='az01_ar_find_ambling_mesa' AND (start_hook IS NULL OR start_hook='')")
        # Add state qualifiers to existing node-conversation rows when a
        # database was seeded before the extractor recorded them.  The
        # conversation name is authored metadata; this is only an idempotent
        # schema/data migration, not runtime conversation selection logic.
        for row in db.execute(
                "SELECT campaign_template, node_id, conversation_guid, "
                "conversation_name, trigger_json FROM campaign_node_conversations").fetchall():
            campaign_template, node_id, conversation_guid, name, raw = row
            try:
                trigger = json.loads(raw or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                trigger = {}
            if not isinstance(trigger, dict):
                trigger = {}
            lower_name = str(name or "").lower()
            changed = False
            if "player already has fortune" in lower_name and trigger.get("state") != "fortune":
                trigger["state"] = "fortune"
                changed = True
            elif "first encounter" in lower_name and trigger.get("visit") != "first":
                trigger["visit"] = "first"
                changed = True
            elif "repeat" in lower_name and trigger.get("visit") != "repeat":
                trigger["visit"] = "repeat"
                changed = True
            if changed:
                db.execute(
                    "UPDATE campaign_node_conversations SET trigger_json=? "
                    "WHERE campaign_template=? AND node_id=? AND conversation_guid=?",
                    (json.dumps(trigger), campaign_template, node_id,
                     conversation_guid))
        db.commit()
        print(
            "Seeded quest metadata: templates={} conversation_links={}".format(
                db.execute("SELECT COUNT(*) FROM quest_templates").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM quest_conversations").fetchone()[0],
            )
        )
    except Exception as exc:
        print(f"Quest metadata seed skipped: {exc}")

    # Named promotional chests were added after the original chest table.
    # Keep existing standard chests valid while retaining their generic
    # template fallback.
    try:
        chest_cols = {r[1] for r in db.execute("PRAGMA table_info(treasure_chests)")}
        if "template_guid" not in chest_cols:
            db.execute("ALTER TABLE treasure_chests ADD COLUMN template_guid TEXT DEFAULT ''")
            db.commit()
    except Exception:
        pass

    # The client crashes while formatting tournament IDs shorter than five
    # digits, so migrate the early tournament rows before the server exposes
    # them through the battlegrounds lobby.
    _migrate_short_tournament_ids(db)

    # Migration: add buff JSON columns to game_cards if missing
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(game_cards)")}
        dcols = {r[1] for r in db.execute("PRAGMA table_info(decks)")}
        ecols = {r[1] for r in db.execute("PRAGMA table_info(ability_effects)")}
        if "gem_abilities" not in dcols:
            db.execute("ALTER TABLE decks ADD COLUMN gem_abilities TEXT DEFAULT '{}'")
        if "effect_group_id" not in ecols:
            db.execute("ALTER TABLE ability_effects ADD COLUMN effect_group_id INTEGER DEFAULT 0")
        if "condition_id" not in ecols:
            db.execute("ALTER TABLE ability_effects ADD COLUMN condition_id TEXT DEFAULT ''")
        if "target_index" not in ecols:
            db.execute("ALTER TABLE ability_effects ADD COLUMN target_index INTEGER DEFAULT -1")
        if "effect_instance_id" not in ecols:
            db.execute("ALTER TABLE ability_effects ADD COLUMN effect_instance_id INTEGER DEFAULT -1")
        if "contingent_effect_instance_id" not in ecols:
            db.execute("ALTER TABLE ability_effects ADD COLUMN contingent_effect_instance_id INTEGER DEFAULT -1")
        if "secondary_target_index" not in ecols:
            db.execute("ALTER TABLE ability_effects ADD COLUMN secondary_target_index INTEGER DEFAULT -1")
        if "recalculate_targets" not in ecols:
            db.execute("ALTER TABLE ability_effects ADD COLUMN recalculate_targets INTEGER DEFAULT -1")
        if "is_optional" not in ecols:
            db.execute("ALTER TABLE ability_effects ADD COLUMN is_optional INTEGER DEFAULT 0")
        if "effect_duration" not in ecols:
            db.execute("ALTER TABLE ability_effects ADD COLUMN effect_duration TEXT DEFAULT 'Instant'")
        if "output_variables" not in ecols:
            db.execute("ALTER TABLE ability_effects ADD COLUMN output_variables TEXT DEFAULT '{}'")
        if "permanent_buffs" not in cols:
            db.execute("ALTER TABLE game_cards ADD COLUMN permanent_buffs TEXT DEFAULT '{}'")
        if "temporary_buffs" not in cols:
            db.execute("ALTER TABLE game_cards ADD COLUMN temporary_buffs TEXT DEFAULT '{}'")
        if "card_cost_mod" not in cols:
            db.execute("ALTER TABLE game_cards ADD COLUMN card_cost_mod INTEGER DEFAULT 0")
        if "temporary_attributes" not in cols:
            db.execute("ALTER TABLE game_cards ADD COLUMN temporary_attributes INTEGER DEFAULT 0")
        if "cost_mod_json" not in cols:
            db.execute("ALTER TABLE game_cards ADD COLUMN cost_mod_json TEXT DEFAULT '[]'")
        if "resolved_at" not in cols:
            db.execute("ALTER TABLE game_cards ADD COLUMN resolved_at INTEGER DEFAULT 0")
        if "gems" not in cols:
            db.execute("ALTER TABLE game_cards ADD COLUMN gems INTEGER DEFAULT 0")
        db.commit()
    except Exception:
        pass

    # Fresh databases get all client-derived rows from the authoritative
    # gamedata blob or the checked-in Records snapshot. Existing databases
    # are left intact so runtime state and local fixes are not replaced.
    if db.execute("SELECT COUNT(*) FROM card_templates").fetchone()[0] == 0:
        from AssetExtraction.gamedata_seed import extract, seed_database

        client_seed = extract()
        inserted = seed_database(db, client_seed)
        print(
            "Seeded client data from {}: {}".format(
                client_seed["source"],
                ", ".join(f"{table}={count}" for table, count in sorted(inserted.items())),
            )
        )

    # Existing databases predate the race-specific Crayburn encounter seed.
    # Add only the missing client-derived encounter rows; do not reseed or
    # replace runtime card/profile data.  Without this incremental repair the
    # campaign scene resolves to an encounter deck whose card list is absent,
    # and setup falls back to template-less placeholder cards.
    try:
        ecols = {r[1] for r in db.execute("PRAGMA table_info(encounter_scenes)")}
        if "ai_champion_guid" not in ecols:
            db.execute("ALTER TABLE encounter_scenes ADD COLUMN ai_champion_guid TEXT")
        if "ai_deck_personality" not in ecols:
            db.execute("ALTER TABLE encounter_scenes ADD COLUMN ai_deck_personality TEXT DEFAULT NULL")
        if "mods_json" not in ecols:
            db.execute("ALTER TABLE encounter_scenes ADD COLUMN mods_json TEXT DEFAULT '[]'")
        if "rewards_json" not in ecols:
            db.execute("ALTER TABLE encounter_scenes ADD COLUMN rewards_json TEXT DEFAULT '{}'")
            db.commit()
        needs_encounter_seed = not db.execute(
            "SELECT 1 FROM encounter_scenes "
            "WHERE name LIKE '% Tutorial Castle Gatehouse' LIMIT 1"
        ).fetchone()
        if not needs_encounter_seed:
            needs_encounter_seed = not db.execute(
                "SELECT 1 FROM encounter_deck_cards edc "
                "JOIN encounter_scenes es ON es.ai_deck_guid=edc.deck_guid "
                "WHERE es.name LIKE '% Tutorial Castle Gatehouse' LIMIT 1"
            ).fetchone()
        if not needs_encounter_seed:
            needs_encounter_seed = not db.execute(
                "SELECT 1 FROM encounter_scenes "
                "WHERE name LIKE 'AZ 1 - NODE%' LIMIT 1"
            ).fetchone()
        if needs_encounter_seed:
            from AssetExtraction.gamedata_seed import extract
            client_seed = extract()
            encounter_tables = client_seed["tables"]
            db.executemany(
                "INSERT OR IGNORE INTO encounter_scenes "
                "(guid,name,title,gameboard,ai_deck_guid,ai_champion_guid,mods_json,rewards_json) VALUES (?,?,?,?,?,?,?,?)",
                encounter_tables.get("encounter_scenes", []),
            )
            db.executemany(
                "INSERT OR IGNORE INTO encounter_deck_cards "
                "(deck_guid,card_guid,quantity,gem_types_new_list_json) "
                "VALUES (?,?,?,?)",
                encounter_tables.get("encounter_deck_cards", []),
            )
            db.commit()
            print("Seeded missing campaign encounter scenes and deck cards")
        # Keep the authored AZ1 reward metadata up to date on existing
        # databases as well as freshly extracted seeds.  Preserve any
        # encounter-specific entries (card choices/conditional captures).
        for eguid, ename, eraw in db.execute(
                "SELECT guid, name, rewards_json FROM encounter_scenes "
                "WHERE name LIKE 'AZ 1%' OR name LIKE 'AZ0_%' "
                "OR name LIKE '% Tutorial %'").fetchall():
            try:
                rewards = json.loads(eraw or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                rewards = {}
            if not isinstance(rewards, dict):
                rewards = {}
            if (ename or "").upper().startswith("AZ0_"):
                amount = 100
            elif " TUTORIAL " in f" {(ename or '').upper()} ":
                amount = 200
            else:
                amount = 100
            # Currency is repeatable for repeatable encounters.  Wild Cub's
            # conditional captured-card reward is a separate one-time record;
            # keeping it separate prevents an old claim from suppressing the
            # next run's normal gold/XP.
            if (ename or "").upper() == "AZ 1 - NODE 03 - WILD CUB":
                rewards.pop("gold", None)
                rewards.pop("xp", None)
                rewards.pop("one_time", None)
                rewards.pop("end_of_game_condition", None)
                rewards.pop("card_guid", None)
                rewards.pop("quantity", None)
                rewards["end_of_game_rewards"] = [
                    {"gold": amount, "xp": amount, "one_time": False},
                    {"end_of_game_condition": {
                        "type": "void_tamed_troop", "owner": "opponent"},
                     "card_guid": "$condition.template_guid",
                     "quantity": 1, "one_time": True},
                ]
            elif (ename or "").upper() == "AZ 1 - NODE 09 - COCKATWICE CHICK":
                # Cockatwice awards Effigy of Nulzann once on a successful
                # completion.  Keep the normal currency reward repeatable,
                # while making the card its own one-time claim so retries do
                # not duplicate it.
                rewards.pop("gold", None)
                rewards.pop("xp", None)
                rewards.pop("one_time", None)
                rewards.pop("end_of_game_condition", None)
                rewards.pop("card_guid", None)
                rewards.pop("quantity", None)
                rewards["end_of_game_rewards"] = [
                    {"gold": amount, "xp": amount, "one_time": False},
                    {"card_guid": "3b18b39a-ff4f-4ecf-b7a8-c446f9d89bb0",
                     "quantity": 1, "one_time": True},
                ]
            else:
                rewards["gold"] = amount
                rewards["xp"] = amount
                rewards["one_time"] = False
            db.execute("UPDATE encounter_scenes SET rewards_json=? WHERE guid=?",
                       (json.dumps(rewards), eguid))
        db.commit()
    except Exception as exc:
        print(f"Encounter seed repair skipped: {exc}")

    # Migration: raw ability record JSON on card_abilities_meta if missing
    try:
        acols = {r[1] for r in db.execute("PRAGMA table_info(card_abilities_meta)")}
        if "raw_json" not in acols:
            db.execute("ALTER TABLE card_abilities_meta ADD COLUMN raw_json TEXT DEFAULT ''")
        db.commit()
    except Exception:
        pass

    # Migration: target template kind (PlayerTargetTemplate / AbilityTargetTemplate)
    # on target_templates if missing.
    try:
        tcols = {r[1] for r in db.execute("PRAGMA table_info(target_templates)")}
        if "target_kind" not in tcols:
            db.execute("ALTER TABLE target_templates ADD COLUMN target_kind TEXT DEFAULT ''")
        db.commit()
    except Exception:
        pass

    # Migration: champion ability thresholds/targets if missing.
    try:
        chcols = {r[1] for r in db.execute("PRAGMA table_info(champion_abilities)")}
        if "thresholds_json" not in chcols:
            db.execute("ALTER TABLE champion_abilities ADD COLUMN thresholds_json TEXT DEFAULT '[]'")
        if "target_template_ids" not in chcols:
            db.execute("ALTER TABLE champion_abilities ADD COLUMN target_template_ids TEXT DEFAULT '[]'")
        db.commit()
    except Exception:
        pass

    # Migration: player champion templates need their authored level-1 default
    # talents so new champions can receive the same list in their first
    # response. The corresponding abilities remain resolved through
    # talent_abilities, avoiding a second copy of that relationship.
    try:
        tcols = {r[1] for r in db.execute("PRAGMA table_info(champion_templates)")}
        if "default_talents" not in tcols:
            db.execute("ALTER TABLE champion_templates ADD COLUMN default_talents TEXT DEFAULT '[]'")
            db.commit()
        if db.execute(
                "SELECT 1 FROM champion_templates "
                "WHERE is_player=1 AND (default_talents IS NULL OR "
                "default_talents IN ('', '[]')) LIMIT 1"
        ).fetchone():
            from AssetExtraction.gamedata_seed import extract
            template_rows = extract()["tables"].get("champion_templates", [])
            db.executemany(
                "UPDATE champion_templates SET default_talents=? WHERE guid=?",
                [(row[5], row[0]) for row in template_rows if len(row) >= 6],
            )
            db.commit()
            print("Seeded default champion talents: "
                  f"{sum(1 for row in template_rows if len(row) >= 6)} player templates")
    except Exception as exc:
        print(f"Default champion talent seed skipped: {exc}")

    # Migration: talent ability target templates.  Older databases contain
    # the talent cost/phase rows but not the target contract from the source
    # AbilityTemplate records; targeted talent powers then cannot open the
    # client's picker (e.g. Warrior's Battle power).
    try:
        tacols = {r[1] for r in db.execute("PRAGMA table_info(talent_abilities)")}
        added_talent_targets = False
        if "target_template_ids" not in tacols:
            db.execute("ALTER TABLE talent_abilities ADD COLUMN target_template_ids TEXT DEFAULT '[]'")
            added_talent_targets = True
        db.commit()
        if added_talent_targets:
            # Populate the new column from the same authoritative gamedata
            # source used for fresh databases.  This is a one-time migration;
            # subsequent startups leave the runtime database untouched.
            from AssetExtraction.gamedata_seed import extract
            talent_rows = extract()["tables"].get("talent_abilities", [])
            for talent_row in talent_rows:
                if len(talent_row) >= 8:
                    db.execute(
                        "UPDATE talent_abilities SET target_template_ids=? "
                        "WHERE talent_guid=? AND ability_guid=?",
                        (talent_row[7], talent_row[0], talent_row[1]))
            db.commit()
    except Exception:
        pass

    if db.execute("SELECT COUNT(*) FROM store_items").fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO store_items (template_guid, name, short_desc, price, currency, store_tab) "
            "VALUES (?,?,?,?,?,?)",
            STORE_ITEMS)

    # Pack-to-set GUID mapping (store pack GUID → card_templates.set_guid).
    PACK_SET_MAP = [
        # Booster packs
        ("a8b78207-686a-4994-b6cd-4548d1349841", "0382f729-7710-432b-b761-13677982dcd2", 0, 0),  # Set 1: Shards of Fate
        ("f37fc1bc-4d7b-4bab-a2a2-770957d9a7b1", "b05e69d2-299a-4eed-ac31-3f1b4fa36470", 0, 0),  # Set 2: Shattered Destiny
        ("237866c1-aea2-4cb4-89ca-418babda3595", "fce480eb-15f9-4096-8d12-6beee9118652", 0, 0),  # Set 3: Armies of Myth
        ("a8e324e3-b9fb-4bb6-b659-f2773982aed2", "2d05262c-d7a0-408f-a280-36d206a29344", 0, 0),  # Set 4: Primal Dawn
        ("84c65d9b-779b-4128-879d-b0779e6e6edc", "ecdbc188-5750-48ef-acac-05e2bcbcc46f", 0, 0),  # Set 5: Herofall
        ("63273f9b-5f4d-4db7-a418-fc6e2c4c9900", "fbbac856-2264-4d31-97b0-0d8a646b9597", 0, 0),  # Set 6: Scars of War
        ("df144885-0fb5-4238-942c-79b35870dabc", "326602fa-e183-4dfe-8300-55cc0c7c4ce8", 0, 0),  # Set 7: Frostheart
        ("902193e6-645b-41be-ac51-23196335b788", "9a824393-cd11-4273-a05e-41e35eb50dbe", 0, 0),  # Set 8: Dead of Winter
        ("3dacd91b-84f1-4ff9-99df-dae3f7740702", "54f14f51-2afe-4a26-be28-d251b06a9cc4", 0, 0),  # Set 9: Doombringer
        # Full Set packs (4x every PVP card)
        ("5e338ab1-da47-41ce-b980-4020f1b5b4fc", "0382f729-7710-432b-b761-13677982dcd2", 1, 0),  # Full Set 1
        ("17e0a0ff-7ec3-4ed7-a261-adb4dcfd6625", "b05e69d2-299a-4eed-ac31-3f1b4fa36470", 1, 0),  # Full Set 2
        ("3bdc41b5-c265-4616-812e-9f1965787c33", "fce480eb-15f9-4096-8d12-6beee9118652", 1, 0),  # Full Set 3
        ("5bdb9ea1-9e03-42af-a23c-6968fff55ce5", "2d05262c-d7a0-408f-a280-36d206a29344", 1, 0),  # Full Set 4
        ("37fb3559-6f2e-4f53-b6d7-05c28c6c075d", "ecdbc188-5750-48ef-acac-05e2bcbcc46f", 1, 0),  # Full Set 5
        # Primal packs
        ("8d20082a-4163-4f42-8fce-d4c056f9da04", "0382f729-7710-432b-b761-13677982dcd2", 0, 1),  # Primal Set 1
        ("653f153b-8288-4ece-a304-2804c1e2ffb9", "b05e69d2-299a-4eed-ac31-3f1b4fa36470", 0, 1),  # Primal Set 2
        ("7a6424ba-8b53-4050-82df-ed756406d87c", "fce480eb-15f9-4096-8d12-6beee9118652", 0, 1),  # Primal Set 3
        ("1db5d6c5-47eb-45e8-b98c-8441ed09e590", "2d05262c-d7a0-408f-a280-36d206a29344", 0, 1),  # Primal Set 4
    ]
    if PACK_SET_MAP:
        db.executemany(
            "INSERT INTO pack_set_map (pack_guid, set_guid, is_full_set, is_primal) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(pack_guid) DO UPDATE SET "
            "set_guid=excluded.set_guid, is_full_set=excluded.is_full_set, "
            "is_primal=excluded.is_primal",
            PACK_SET_MAP)

    # Tournament types — seeded from code so the Battlegrounds always has rooms.
    # format is the ETournamentFormats bitmask (matches the client enum):
    # Constructed=0, Sealed_Deck=1, Booster_Draft=2, Chapter1=4, Chapter2=8,
    # Immortal=16, Chapter3=32, Chapter4=64, Chapter5=128, ...
    TOURNAMENT_TYPES = [
        (1, "1v1 Immortal - Best of 1", "se", 16,           2, 2, 1, None),
        (2, "Limited Sealed - 5 Games","sw", 1,            1, 1, 5, "set01"),
        (3, "Set 1 Draft (AI)",        "sw", 2,            1, 1, 3, "set01"),
    ]
    if db.execute("SELECT COUNT(*) FROM tournament_types").fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO tournament_types (id, name, style, format, "
            "min_players, max_players, games_count, set_id) VALUES (?,?,?,?,?,?,?,?)",
            TOURNAMENT_TYPES)
    # Keep the seeded 1v1 event aligned with the actual server behavior: one
    # game, two players, and single-elimination completion semantics.
    db.execute(
        "UPDATE tournament_types SET name=?, style='se', format=16, "
        "min_players=2, max_players=2, games_count=1 WHERE id=1",
        ("1v1 Immortal - Best of 1",),
    )

    if db.execute("SELECT COUNT(*) FROM chest_probabilities").fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO chest_probabilities (rarity, probability, weight) VALUES (?,?,?)",
            CHEST_PROBABILITIES)

    for code, gold, plat, maxu in REDEEM_CODES:
        existing = db.execute("SELECT id FROM redeem_codes WHERE code=?", (code,)).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO redeem_codes (code, gold_delta, platinum_delta, max_uses) VALUES (?,?,?,?)",
                (code, gold, plat, maxu))

    db.executemany(
        "INSERT OR IGNORE INTO conversation_rewards "
        "(conversation_guid, reward_json, one_time) VALUES (?,?,?)",
        CONVERSATION_REWARD_SEEDS)

    # Upgrade the initial placeholder values seeded by the first version of
    # conversation rewards.  Only the known 100 gold/100 XP placeholders are
    # changed; custom reward JSON remains untouched.
    for conversation_guid, reward_json, _one_time in CONVERSATION_REWARD_SEEDS:
        existing = db.execute(
            "SELECT reward_json FROM conversation_rewards WHERE conversation_guid=?",
            (conversation_guid,)).fetchone()
        if not existing:
            continue
        try:
            old_reward = json.loads(existing[0] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            old_reward = {}
        if (isinstance(old_reward, dict) and old_reward.get("gold") == 100
                and old_reward.get("xp") == 100
                and "chest_guid" not in old_reward):
            db.execute(
                "UPDATE conversation_rewards SET reward_json=? WHERE conversation_guid=?",
                (reward_json, conversation_guid))
    db.commit()

    # Seed stardust for existing users who don't have any (idempotent backfill).
    for (uid,) in db.execute("SELECT id FROM users").fetchall():
        if db.execute("SELECT COUNT(*) FROM stardust WHERE user_id=?", (uid,)).fetchone()[0] == 0:
            for rarity in ("common", "uncommon", "rare", "legendary", "promo"):
                db.execute(
                    "INSERT INTO stardust (user_id, rarity, quantity) VALUES (?, ?, 100)",
                    (uid, rarity))

    # Resource cards grant current/max resources from the gamedata template
    # fields (basic shards grant 1/1; Shards of Fate grants 0/1 — it increases
    # MAX mana only, matching CardTemplate.m_CurrentResourcesGranted /
    # m_MaxResourcesGranted in the client).
    try:
        from db import db_ensure_resource_grants
        db_ensure_resource_grants(db)
    except Exception:
        pass

    # Restore the gamedata effect structure (groups/conditions/targets) onto
    # ability_effects from each ability's raw_json.
    try:
        from db import db_backfill_ability_effect_meta
        db_backfill_ability_effect_meta(db)
    except Exception:
        pass

    db.commit()

    # Post-extraction data fixes (card_fix.py) — adjust gamedata values that
    # are technically correct but produce wrong/confusing in-game behaviour.
    import card_fix
    card_fix.apply_fixes(db)

"""Domain constants shared across the Hex TCG server."""

# Optional replay-recording hook, installed by hconnect_server at startup.
# Signature: event_logger(session_id, target_player_uid, list_of_event_bytes).
# Called from Game.make_network_packet for every event batch pushed to
# any player so both player and AI actions are persisted for replay.
event_logger = None

# Gamedata AbilityTargetTemplate: "a card from your hand"
DISCARD_TARGET_TEMPLATE = "84e4acf1-1f2e-abac-069d-8c6eb18b2b12"

# Client BuiltInResources.PlayCardAbilityTemplateId
PLAY_CARD_ABILITY_TEMPLATE_ID = "5a8783b0-e420-4f41-b2a1-96f70b0cd851"

# Gamedata AbilityTargetTemplate: "You" — self-target template
SELF_TARGET_TEMPLATE = "eb7e48cd-1c85-813f-6635-d43f50cf7809"

# UID type bytes used by the original Game.Shared wire contracts.
CARD_UID_TYPE = 1
AI_UID_TYPE = 3
PLAYER_UID_TYPE = 244

# The ObjFmt UID64 field carries the SessionCardId UID kind in this slot.
SESSION_CARD_UID_FIELD_TYPE = 7

# Default health used by the standard two-player session setup.
DEFAULT_STARTING_HEALTH = 20
DEFAULT_MAX_HAND_SIZE = 7
PLAYED_CARD_POSITION = 9999

# Shared wire/service identifiers from the client protocol.
SERVICE_PLAYER_UID_TYPE = PLAYER_UID_TYPE
SERVICE_GAME_SESSION_UID_TYPE = 246
AUTHORITATIVE_SESSION_UID_TYPE = 13
PLAYER_TRANSACTION_DATA_TYPE = 3029
TOURNAMENT_GAME_DATA_TYPE = 25029
TOURNAMENT_INFO_DATA_TYPE = 25058
TOURNAMENT_DECK_CONSTRUCTION_DATA_TYPE = 25072
TOURNAMENT_SESSION_START_DATA_TYPE = 25060

# ProfileService.BannedCardList event and the client format values used by
# Game.Shared.Mechanics.Format.SetFormatBan.
PROFILE_BANNED_CARD_LIST_EVENT = 2214
ICONOCLAST_SET_FORMAT = 16779262
ICONOCLAST_PLAY_FORMAT = 512

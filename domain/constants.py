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

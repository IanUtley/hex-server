"""Dispatch table for the request families moved out of hconnect_server."""

from services.legacy_mail import LEGACY_HANDLERS as _legacy_mail
from services.legacy_store import LEGACY_HANDLERS as _legacy_store
from services.legacy_loadbalancer import LEGACY_HANDLERS as _legacy_loadbalancer
from services.legacy_tournaments import LEGACY_HANDLERS as _legacy_tournaments
from services.legacy_decks import LEGACY_HANDLERS as _legacy_decks
from services.legacy_profile import LEGACY_HANDLERS as _legacy_profile
from services.legacy_campaign import LEGACY_HANDLERS as _legacy_campaign
from services.legacy_social import LEGACY_HANDLERS as _legacy_social
from services.legacy_matchmaking import LEGACY_HANDLERS as _legacy_matchmaking
from services.legacy_session import LEGACY_HANDLERS as _legacy_session

LEGACY_HANDLERS = {}
LEGACY_HANDLERS.update(_legacy_mail)
LEGACY_HANDLERS.update(_legacy_store)
LEGACY_HANDLERS.update(_legacy_loadbalancer)
LEGACY_HANDLERS.update(_legacy_tournaments)
LEGACY_HANDLERS.update(_legacy_decks)
LEGACY_HANDLERS.update(_legacy_profile)
LEGACY_HANDLERS.update(_legacy_campaign)
LEGACY_HANDLERS.update(_legacy_social)
LEGACY_HANDLERS.update(_legacy_matchmaking)
LEGACY_HANDLERS.update(_legacy_session)

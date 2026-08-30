"""
Domain types for the Hex TCG private server.

- binary_io:  .NET BinaryWriter/BinaryReader emulation
- types:      UID, ResourceId, SessionCardId, CombatId
- enums:      ETurnPhases, ECardTypes, ECardStates, ECardAttributes, etc.
- serializer: SessionEventArgs binary serializer
- events:     SessionEventArgs + 60+ subclass event types
- game:       CardDef, Game (event queue, card database, tutorial engine)
- constants:  module-level constants and hooks
"""

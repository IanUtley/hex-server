# domain/

Shared domain types used across the entire server. Nothing in this package
depends on network, database, or game-logic modules — only standard library.

## Submodules

| File | Contents |
|------|----------|
| `binary_io.py` | `.NET BinaryWriter` / `BinaryReader` emulation |
| `enums.py` | All game enums: `ETurnPhases`, `ECardTypes`, `ECardStates`, `ECardAttributes`, etc. |
| `types.py` | `UID`, `ResourceId`, `SessionCardId`, `CombatId` |
| `serializer.py` | `SessionEventArgs` binary wire-format serializer |
| `events.py` | `SessionEventArgs` base class and the session-event subclasses |
| `game.py` | `CardDef`, `Game` (event queue, tutorial engine), `parse_tutorial_script` |
| `constants.py` | `event_logger` hook, ability-target-template GUIDs |

## How to add things

### New enum value

Edit `enums.py`.  Values must match the client's `Game.Shared.Mechanics` constants
exactly or the client will misbehave.

### New event type

Edit `events.py`.  Subclass `SessionEventArgs`, assign a unique `CLASS_ID`, and
implement `to_byte_array()` using the serializer.  The `CLASS_ID` must match the
client's expected integer for that event class.

### New domain type

Add a new `.py` file.  If other submodules need it, import it in `__init__.py`
so that `from domain import Foo` works.

## Re-export chain

The top-level `game_engine.py` re-exports everything from this package so
existing callers (`import game_engine`) continue to work unchanged.

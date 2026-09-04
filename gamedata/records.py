"""Deserializer and lazy store for the extracted client Records files.

Each JSONL line is an outer JSON string containing the actual near-JSON
serialized object.  The inner object uses ``_t`` for its concrete C# type and
``_v`` for the serializer's version map.  The loader deliberately retains
both values and every unrecognised field: losing fields here would make the
server guess at mechanics that the client already describes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, ClassVar


_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_TYPE_REGISTRY: dict[str, type["RecordObject"]] = {}
_MODELS_LOADED = False


class RecordDecodeError(ValueError):
    """Raised when one extracted record cannot be decoded."""


@dataclass(frozen=True)
class RecordIssue:
    section: str
    line_number: int
    error: str


def register_type(*type_names: str):
    """Register a RecordObject subclass by full or short C# type name."""

    def decorator(cls: type[RecordObject]):
        for type_name in type_names:
            _TYPE_REGISTRY[type_name] = cls
            _TYPE_REGISTRY[type_name.rsplit(".", 1)[-1]] = cls
        return cls

    return decorator


class RecordObject(Mapping[str, Any]):
    """A deserialized polymorphic client object.

    Known mechanics types subclass this class and add semantic properties,
    while the base object remains usable for types we have not modelled yet.
    Unknown fields are intentionally available through the mapping interface.
    """

    _field_aliases: ClassVar[dict[str, str]] = {}

    def __init__(self, type_name: str, fields: Mapping[str, Any],
                 raw: Mapping[str, Any] | None = None):
        self.type_name = str(type_name or "")
        self.fields = dict(fields)
        self.raw = dict(raw or self.to_dict(include_metadata=True))
        self.versions = self._read_versions(self.raw.get("_v"))

    @staticmethod
    def _read_versions(value: Any) -> dict[str, int]:
        versions: dict[str, int] = {}
        if not isinstance(value, list):
            return versions
        for entry in value:
            if not isinstance(entry, dict):
                continue
            for name, version in entry.items():
                try:
                    versions[str(name)] = int(version)
                except (TypeError, ValueError):
                    continue
        return versions

    @property
    def short_type(self) -> str:
        return self.type_name.rsplit(".", 1)[-1]

    @property
    def guid(self) -> str:
        for name in ("m_TemplateId", "m_AbilityTemplateId",
                     "m_CardTemplateId", "m_CardCounterId", "m_Id",
                     "m_Guid"):
            value = self.fields.get(name)
            guid = reference_guid(value)
            if guid:
                return guid
        return ""

    def is_a(self, type_name: str) -> bool:
        """Match the concrete full type or its short name.

        Serializer version entries are retained as provenance, but they are
        not type aliases. This server supports one extracted Records
        snapshot, so a version number must never select a different rules
        interpretation.
        """
        wanted = str(type_name or "")
        return (self.type_name == wanted
                or self.short_type == wanted.rsplit(".", 1)[-1])

    def field(self, name: str, default: Any = None) -> Any:
        actual = self._field_aliases.get(name, name)
        return self.fields.get(actual, default)

    def __getitem__(self, key: str) -> Any:
        return self.fields[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.fields)

    def __len__(self) -> int:
        return len(self.fields)

    def __getattr__(self, name: str) -> Any:
        # This is intentionally limited to serialized field names.  It makes
        # m_Name/m_AbilityEffectList pleasant to inspect without hiding typos
        # in semantic properties defined by subclasses.
        fields = self.__dict__.get("fields", {})
        if name in fields:
            return fields[name]
        raise AttributeError(name)

    def to_dict(self, *, include_metadata: bool = True) -> dict[str, Any]:
        value = {
            str(key): _plain_value(item) for key, item in self.fields.items()
        }
        if include_metadata:
            value = {"_t": self.type_name, **value}
            if self.versions:
                value["_v"] = [dict(entry) for entry in (
                    self.raw.get("_v") or [])]
        return value

    def __repr__(self) -> str:
        label = self.guid or self.short_type or "RecordObject"
        return f"<{self.__class__.__name__} {label}>"


def _ensure_models_loaded() -> None:
    global _MODELS_LOADED
    if not _MODELS_LOADED:
        # Importing models registers the concrete classes.  This lazy import
        # avoids a records/models circular import during package initialisation.
        from . import models  # noqa: F401
        _MODELS_LOADED = True


def _class_for(type_name: str) -> type[RecordObject]:
    _ensure_models_loaded()
    if type_name in _TYPE_REGISTRY:
        return _TYPE_REGISTRY[type_name]
    short = type_name.rsplit(".", 1)[-1]
    if short in _TYPE_REGISTRY:
        return _TYPE_REGISTRY[short]
    # Concrete subclasses in Records have names such as
    # ``BuiltInPlayCardAbilityEffectTemplate`` and
    # ``AbilitySourceCardTargetTemplate`` while the Python model covers the
    # shared contracts.  Longest matching name wins so
    # ``AbilityEffectConditionTemplate`` is not mistaken for the shorter
    # effect-template base.
    candidates = [
        (key, cls) for key, cls in _TYPE_REGISTRY.items()
        if key in type_name
    ]
    return max(candidates, key=lambda item: len(item[0]))[1] \
        if candidates else RecordObject


def _plain_value(value: Any) -> Any:
    if isinstance(value, RecordObject):
        return value.to_dict()
    if isinstance(value, list):
        return [_plain_value(item) for item in value]
    if isinstance(value, tuple):
        return [_plain_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain_value(item) for key, item in value.items()}
    return value


def reference_guid(value: Any) -> str:
    """Extract a GUID from a serialized ResourceId-like value."""
    if isinstance(value, RecordObject):
        value = value.fields
    if isinstance(value, Mapping):
        guid = value.get("m_Guid")
        return str(guid or "")
    return str(value or "") if isinstance(value, str) else ""


def deserialize(value: Mapping[str, Any] | str) -> Any:
    """Recursively deserialize a decoded record or serialized inner JSON."""
    return _deserialize(value, parse_string=True)


def _deserialize(value: Any, *, parse_string: bool = False) -> Any:
    if isinstance(value, str):
        # Strings are ordinary scalar fields during recursive descent.  Only
        # attempt a second JSON decode for the outer serialized object;
        # otherwise a card name such as ``Play Card Ability`` or card text
        # beginning with ``[BASIC]`` would be treated as broken JSON.
        if not parse_string or not value.lstrip().startswith(("{", "[")):
            return value
        try:
            value = json.loads(_TRAILING_COMMA.sub(r"\1", value))
        except json.JSONDecodeError as exc:
            raise RecordDecodeError(str(exc)) from exc
    if isinstance(value, list):
        return [_deserialize(item) if isinstance(item, (dict, list, str))
                else item for item in value]
    if not isinstance(value, dict):
        return value
    type_name = value.get("_t")
    fields = {
        key: _deserialize(item) if isinstance(item, (dict, list, str))
        else item
        for key, item in value.items()
        if key not in ("_t", "_v")
    }
    if not type_name:
        return fields
    cls = _class_for(str(type_name))
    return cls(str(type_name), fields, value)


def deserialize_line(line: str) -> RecordObject:
    """Decode one Records JSONL line, including its outer JSON string."""
    try:
        outer = json.loads(line)
        inner = outer if isinstance(outer, str) else outer
        value = deserialize(inner)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecordDecodeError(str(exc)) from exc
    if not isinstance(value, RecordObject):
        raise RecordDecodeError("record did not contain a typed object")
    return value


class RecordStore:
    """Lazy, thread-safe access to Records sections indexed by GUID."""

    def __init__(self, records_dir: str | Path | None = None,
                 *, strict: bool = False):
        self.records_dir = Path(records_dir) if records_dir else (
            Path(__file__).resolve().parents[1] / "Records")
        self.strict = strict
        self._cache: dict[str, list[RecordObject]] = {}
        self._index: dict[str, dict[str, RecordObject]] = {}
        self._issues: list[RecordIssue] = []
        self._lock = RLock()

    @staticmethod
    def _section_name(section: str) -> str:
        return str(section).removesuffix(".jsonl")

    def load(self, section: str) -> list[RecordObject]:
        name = self._section_name(section)
        with self._lock:
            if name in self._cache:
                return self._cache[name]
            path = self.records_dir / (name + ".jsonl")
            records: list[RecordObject] = []
            if not path.exists():
                self._cache[name] = records
                self._index[name] = {}
                return records
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    # extract_records.py retains the section delimiter as the
                    # first JSONL entry.  It is useful provenance, not a
                    # mechanics object, so do not report it as a bad record.
                    try:
                        header = json.loads(line)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        header = None
                    if (isinstance(header, str)
                            and header.lstrip().startswith("$$$---$$$")):
                        continue
                    try:
                        records.append(deserialize_line(line))
                    except RecordDecodeError as exc:
                        issue = RecordIssue(name, line_number, str(exc))
                        self._issues.append(issue)
                        if self.strict:
                            raise
            self._cache[name] = records
            self._index[name] = {
                record.guid.lower(): record for record in records
                if record.guid
            }
            return records

    def get(self, section: str, guid: str) -> RecordObject | None:
        if not guid:
            return None
        name = self._section_name(section)
        self.load(name)
        return self._index[name].get(str(guid).lower())

    def find(self, section: str, predicate) -> list[RecordObject]:
        return [record for record in self.load(section) if predicate(record)]

    @property
    def issues(self) -> tuple[RecordIssue, ...]:
        return tuple(self._issues)

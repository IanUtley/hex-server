"""Pure Frost Ring Arena roster rules.

This module deliberately has no database or protocol dependencies.  The
service and persistence layers provide the encounter rows and store the
selected roster; this module only applies the run's rank and boss rules.
"""

import random


# One-based positions in the four five-fight Arena tiers.  The client marks
# every fifth position as a boss fight; the extracted roster currently has
# explicit boss classifications for the later three positions.
FIXED_BOSS_RANKS = frozenset((10, 15, 20))
FIXED_ELITE_RANKS = frozenset((9, 12, 14, 17, 19))
# DeckTemplate has no generic boss field.  These are the boss families
# recovered from the Arena data; their elite variants are the boss versions.
KNOWN_BOSS_BASES = frozenset((
    "Arena_Phenteo",
    "Arena_Eurig",
    "Arena_Princess_Cory",
    "Arena_Hogarth",
))
RUN_LENGTH = 20


def is_boss_encounter(encounter):
    """Return whether an encounter is a boss, not merely an elite upgrade."""
    return bool(encounter.get("is_boss")) or (
        bool(encounter.get("is_elite"))
        and encounter.get("base") in KNOWN_BOSS_BASES
    )


def encounter_family(encounter):
    """Return the identity used to prevent a deck family repeating.

    Normal and elite rows for the same Arena deck share ``base``.  They must
    therefore count as the same opponent even though their deck and champion
    GUIDs differ.  Small test/custom encounter rows may omit ``base``; use a
    stable deck/name fallback for those rows.
    """
    return str(encounter.get("base") or encounter.get("deck")
               or encounter.get("name") or "")


def select_fra_roster(encounters, rng=None, run_length=RUN_LENGTH):
    """Select one encounter for each one-based position in an FRA run.

    ``encounters`` is an iterable of dictionaries containing ``min_rank``,
    ``max_rank``, ``is_boss``, ``is_elite`` and ``base``.  The returned list
    contains ``(rank, encounter)`` pairs.  Ranks 10, 15 and 20 use the known
    boss encounters.  Ranks 9, 12, 14, 17 and 19 always use an eligible elite
    version of a normal deck family; all other positions use normal decks.
    A family may only be selected once, and boss families are reserved for
    the fixed boss positions.
    """
    rng = rng or random.SystemRandom()
    encounters = list(encounters)
    selected = []
    used_families = set()
    boss_families = {
        encounter_family(encounter) for encounter in encounters
        if is_boss_encounter(encounter)
    }

    for rank in range(1, int(run_length) + 1):
        eligible = [
            encounter for encounter in encounters
            if int(encounter.get("min_rank", 6)) <= rank <=
            int(encounter.get("max_rank", 19))
            and encounter_family(encounter) not in used_families
        ]
        if rank in FIXED_BOSS_RANKS:
            candidates = [
                encounter for encounter in eligible
                if is_boss_encounter(encounter)
            ]
        elif rank in FIXED_ELITE_RANKS:
            elite_by_base = {}
            for encounter in eligible:
                if (encounter.get("is_elite")
                        and not is_boss_encounter(encounter)):
                    elite_by_base.setdefault(encounter_family(encounter), []).append(
                        encounter
                    )
            normal_candidates = [
                encounter for encounter in eligible
                if not encounter.get("is_elite")
                and encounter_family(encounter) in elite_by_base
            ]
            if normal_candidates:
                chosen_base = rng.choice(normal_candidates)
                chosen = rng.choice(
                    elite_by_base[encounter_family(chosen_base)])
                selected.append((rank, chosen))
                used_families.add(encounter_family(chosen))
                continue
            candidates = [
                encounter for encounter in eligible
                if encounter.get("is_elite") and not is_boss_encounter(encounter)
            ]
        else:
            candidates = [
                encounter for encounter in eligible
                if not encounter.get("is_elite")
                and encounter_family(encounter) not in boss_families
            ]

        if not candidates:
            raise RuntimeError(f"No FRA encounter is eligible for rank {rank}")

        chosen = rng.choice(candidates)
        selected.append((rank, chosen))
        used_families.add(encounter_family(chosen))

    return selected

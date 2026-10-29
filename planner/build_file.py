import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from planner.tree import load_tree


@dataclass
class LevelRange:
    low: int = 0
    high: int = 100

    @classmethod
    def parse(cls, value) -> "LevelRange":
        if value is None:
            return cls()
        if isinstance(value, int):
            return cls(value, 100)
        return cls(value[0], value[1] if len(value) > 1 else 100)

    def contains(self, level: int) -> bool:
        return self.low <= level <= self.high


@dataclass
class PlannedPassive:
    string_id: str
    node_id: int | None
    levels: LevelRange
    weapon_set: int | None
    note: str | None


@dataclass
class PlannedGem:
    gem_id: str
    levels: LevelRange
    note: str | None
    supports: list["PlannedGem"] = field(default_factory=list)

    @property
    def short_name(self) -> str:
        return self.gem_id.rsplit("/", 1)[-1].removeprefix("SkillGem").removeprefix("SupportGem")


@dataclass
class SlotHint:
    inventory_id: str
    levels: LevelRange
    unique_name: str | None
    note: str | None


@dataclass
class BuildPlan:
    name: str
    author: str | None
    description: str | None
    ascendancy: str | None
    passives: list[PlannedPassive]
    skills: list[PlannedGem]
    slots: list[SlotHint]

    def passives_at(self, level: int) -> set[int]:
        return {p.node_id for p in self.passives if p.node_id is not None and p.levels.contains(level)}

    def gear_level(self) -> int:
        return max((s.levels.low for s in self.slots if s.levels.low > 0), default=0)

    def slot_hints_at(self, level: int) -> dict[str, SlotHint]:
        out: dict[str, SlotHint] = {}
        for hint in self.slots:
            slot = pob_slot(hint.inventory_id)
            if slot and hint.levels.contains(level):
                if slot not in out or hint.levels.low >= out[slot].levels.low:
                    out[slot] = hint
        return out


# ids de inventario del juego -> slots de PoB
_SLOT_NAMES = {"Weapon1": "Weapon 1", "Weapon2": "Weapon 1 Swap", "Offhand1": "Weapon 2", "Offhand2": "Weapon 2 Swap",
               "Helm1": "Helmet", "BodyArmour1": "Body Armour", "Gloves1": "Gloves", "Boots1": "Boots",
               "Amulet1": "Amulet", "Ring1": "Ring 1", "Ring2": "Ring 2", "Belt1": "Belt"}


def pob_slot(inventory_id: str) -> str | None:
    return _SLOT_NAMES.get(inventory_id)


def hint_item_text(hint: SlotHint) -> str | None:
    if hint.unique_name or not hint.note:
        return None
    lines = [ln.strip() for ln in hint.note.splitlines() if ln.strip()]
    mods = [re.sub(r"^\d+\.\s*", "", ln) for ln in lines[1:] if re.match(r"^\d+\.", ln)]
    if not mods:
        return None
    return "\n".join(["Rarity: RARE", "Guide Target", lines[0], "Implicits: 0", *mods])


def _entry(value) -> dict:
    return {"id": value} if isinstance(value, str) else value


def _gem(raw) -> PlannedGem:
    raw = _entry(raw)
    return PlannedGem(
        gem_id=raw["id"],
        levels=LevelRange.parse(raw.get("level_interval")),
        note=raw.get("additional_text"),
        supports=[_gem(s) for s in raw.get("support_skills", [])],
    )


def load_build_file(path: str | Path, tree_version: str = "0_5") -> BuildPlan:
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    tree = load_tree(tree_version)
    passives = []
    for p in map(_entry, raw.get("passives", [])):
        passives.append(PlannedPassive(
            string_id=p["id"],
            node_id=tree.by_string_id.get(p["id"]),
            levels=LevelRange.parse(p.get("level_interval")),
            weapon_set=p.get("weapon_set"),
            note=p.get("additional_text"),
        ))
    return BuildPlan(
        name=raw["name"],
        author=raw.get("author"),
        description=raw.get("description"),
        ascendancy=raw.get("ascendancy"),
        passives=passives,
        skills=[_gem(s) for s in raw.get("skills", [])],
        slots=[SlotHint(
            inventory_id=s["inventory_id"],
            levels=LevelRange.parse(s.get("level_interval")),
            unique_name=s.get("unique_name"),
            note=s.get("additional_text"),
        ) for s in raw.get("inventory_slots", [])],
    )

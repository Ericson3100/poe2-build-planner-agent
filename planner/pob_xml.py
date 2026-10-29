import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

_PROPERTY_KEYS = {
    "Unique ID", "Item Level", "LevelReq", "Quality", "Sockets", "Rune",
    "Armour", "Evasion", "Energy Shield", "Ward", "Charm Slots", "Radius",
}
_TAG_RE = re.compile(r"\{([^}]*)\}")


@dataclass
class Mod:
    text: str
    tags: list[str] = field(default_factory=list)


@dataclass
class Item:
    id: int
    rarity: str
    name: str
    base: str
    properties: dict[str, str] = field(default_factory=dict)
    runes: list[str] = field(default_factory=list)
    implicits: list[Mod] = field(default_factory=list)
    explicits: list[Mod] = field(default_factory=list)
    corrupted: bool = False

    @property
    def item_level(self) -> int | None:
        value = self.properties.get("Item Level")
        return int(value) if value else None


@dataclass
class Gem:
    name: str
    gem_id: str
    level: int
    quality: int
    enabled: bool
    is_support: bool


@dataclass
class SkillGroup:
    gems: list[Gem]
    slot: str | None
    granted_by: str | None
    enabled: bool
    is_main: bool


@dataclass
class Build:
    class_name: str
    ascendancy: str
    level: int
    tree_version: str
    league: str | None
    nodes: set[int]
    weapon_set_nodes: dict[int, set[int]]
    jewel_sockets: dict[int, int]
    skills: list[SkillGroup]
    items: dict[int, Item]
    slots: dict[str, int]
    stats: dict[str, float]
    config: dict[str, str | float]

    def equipped(self) -> dict[str, Item]:
        return {slot: self.items[item_id] for slot, item_id in self.slots.items() if item_id in self.items}


def _split_tags(line: str) -> Mod:
    tags = _TAG_RE.findall(line)
    return Mod(text=_TAG_RE.sub("", line).strip(), tags=tags)


def parse_item_text(item_id: int, raw: str) -> Item:
    lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
    rarity = lines[0].removeprefix("Rarity: ").strip()
    # raros y únicos tienen línea de nombre y de base
    if rarity in ("RARE", "UNIQUE"):
        name, base, rest = lines[1], lines[2], lines[3:]
    else:
        name, base, rest = lines[1], lines[1], lines[2:]

    item = Item(id=item_id, rarity=rarity, name=name, base=base)
    i = 0
    while i < len(rest):
        line = rest[i]
        key, sep, value = line.partition(": ")
        if line.startswith("Implicits: "):
            count = int(value)
            item.implicits = [_split_tags(ln) for ln in rest[i + 1 : i + 1 + count]]
            for ln in rest[i + 1 + count :]:
                if ln == "Corrupted":
                    item.corrupted = True
                else:
                    item.explicits.append(_split_tags(ln))
            break
        if sep and key == "Rune":
            if value != "None":
                item.runes.append(value)
        elif sep and key in _PROPERTY_KEYS:
            item.properties[key] = value
        i += 1
    return item


def _attr(elem: ET.Element, name: str, default: str = "") -> str:
    value = elem.get(name, default)
    return default if value == "nil" else value


def _node_list(value: str) -> set[int]:
    return {int(n) for n in value.split(",") if n}


BUILDS_DIR = Path(__file__).resolve().parent.parent / "builds"


def resolve_build(name: str | Path | None = None, folder: str | Path | None = None) -> Path:
    folder = Path(folder) if folder else BUILDS_DIR
    files = sorted(folder.glob("*.xml"), key=lambda p: p.stat().st_mtime, reverse=True)
    if name:
        path = Path(name)
        if path.is_file():
            return path
        path = folder / (path.name if path.suffix.lower() == ".xml" else f"{path.name}.xml")
        if path.is_file():
            return path
        raise FileNotFoundError(f"No existe la build '{name}' en {folder}. Disponibles: {[p.name for p in files]}")
    if not files:
        raise FileNotFoundError(f"No hay builds en {folder}: guarda tu personaje desde PoB2 en esa carpeta.")
    if len(files) > 1:
        print(f"{len(files)} builds en {folder.name}/; se usa la más reciente: {files[0].name}")
    return files[0]


def load_build(path: str | Path) -> Build:
    root = ET.parse(path).getroot()
    build_el = root.find("Build")
    spec = root.find("Tree/Spec")

    stats = {ps.get("stat"): float(ps.get("value")) for ps in build_el.findall("PlayerStat")}

    weapon_sets = {}
    for ws in spec:
        if ws.tag.startswith("WeaponSet"):
            weapon_sets[int(ws.tag.removeprefix("WeaponSet"))] = _node_list(ws.get("nodes", ""))
    jewel_sockets = {int(s.get("nodeId")): int(s.get("itemId")) for s in spec.findall("Sockets/Socket")}

    skills_el = root.find("Skills")
    active_set = skills_el.get("activeSkillSet", "1")
    main_group = int(_attr(build_el, "mainSocketGroup", "0") or 0)
    skills = []
    for index, skill_el in enumerate(skills_el.findall(f"SkillSet[@id='{active_set}']/Skill"), 1):
        gems = [
            Gem(
                name=g.get("nameSpec"),
                gem_id=g.get("gemId"),
                level=int(_attr(g, "level", "0")),
                quality=int(_attr(g, "quality", "0")),
                enabled=_attr(g, "enabled") == "true",
                is_support="SupportGem" in g.get("gemId", ""),
            )
            for g in skill_el.findall("Gem")
        ]
        skills.append(SkillGroup(
            gems=gems,
            slot=_attr(skill_el, "slot") or None,
            granted_by=_attr(skill_el, "source") or None,
            enabled=_attr(skill_el, "enabled") == "true",
            is_main=index == main_group,
        ))

    items_el = root.find("Items")
    items = {}
    for item_el in items_el.findall("Item"):
        item_id = int(item_el.get("id"))
        items[item_id] = parse_item_text(item_id, item_el.text or "")
    active_item_set = items_el.get("activeItemSet", "1")
    slots = {
        s.get("name"): int(s.get("itemId"))
        for s in items_el.findall(f"ItemSet[@id='{active_item_set}']/Slot")
        if s.get("itemId", "0") != "0"
    }

    config_el = root.find(f"Config/ConfigSet[@id='{root.find('Config').get('activeConfigSet', '1')}']")
    config = {}
    for inp in list(config_el) if config_el is not None else []:
        name = inp.get("name")
        if inp.get("number") is not None:
            config[name] = float(inp.get("number"))
        else:
            config[name] = inp.get("string") or inp.get("boolean", "")

    import_el = root.find("Import")
    return Build(
        class_name=build_el.get("className"),
        ascendancy=build_el.get("ascendClassName"),
        level=int(build_el.get("level")),
        tree_version=spec.get("treeVersion"),
        league=import_el.get("lastLeague") if import_el is not None else None,
        nodes=_node_list(spec.get("nodes", "")),
        weapon_set_nodes=weapon_sets,
        jewel_sockets=jewel_sockets,
        skills=skills,
        items=items,
        slots=slots,
        stats=stats,
        config=config,
    )

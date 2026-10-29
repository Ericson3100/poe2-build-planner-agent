import re
from functools import lru_cache
from pathlib import Path

RUNES_FILE = Path(__file__).resolve().parent.parent / "vendor" / "PathOfBuilding-PoE2" / "src" / "Data" / "ModRunes.lua"
_ENTRY_RE = re.compile(r'^\t\["(?P<name>[^"]+)"\] = \{\n(?P<body>.*?)^\t\},?$', re.M | re.S)
_SLOT_RE = re.compile(r'^\t\t\["(?P<slot>[^"]+)"\] = \{\n(?P<body>.*?)^\t\t\},?$', re.M | re.S)
_MOD_RE = re.compile(r'^\t\t\t\t"(?P<mod>.+)",$', re.M)

@lru_cache(maxsize=1)
def rune_table() -> dict[str, dict[str, list[str]]]:
    text = RUNES_FILE.read_text(encoding="utf-8")
    table: dict[str, dict[str, list[str]]] = {}
    for entry in _ENTRY_RE.finditer(text):
        slots = {m["slot"]: _MOD_RE.findall(m["body"]) for m in _SLOT_RE.finditer(entry["body"])}
        if slots:
            table[entry["name"]] = slots
    return table


def runes_for(kind: str) -> dict[str, list[str]]:
    return {name: slots[kind] for name, slots in rune_table().items() if kind in slots}

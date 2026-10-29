import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "vendor" / "PathOfBuilding-PoE2" / "src" / "Data"
_BASE_RE = re.compile(r'^itemBases\["(?P<name>[^"]+)"\] = \{\n(?P<body>.*?)^\}', re.M | re.S)
_TYPE_RE = re.compile(r'^\ttype = "(?P<value>[^"]*)"', re.M)
_REQ_LEVEL_RE = re.compile(r"req = \{[^}]*\blevel = (\d+)")
_UNIQUE_RE = re.compile(r"\[\[\n(?P<text>.*?)\]\]", re.S)
_UNIQUE_LEVEL_RE = re.compile(r"^Requires Level (\d+)", re.M)


@dataclass
class Base:
    name: str
    type: str
    level: int


@dataclass
class Unique:
    name: str
    base: str
    kind: str
    text: str
    level: int

    def item_text(self) -> str:
        return "Rarity: UNIQUE\n" + self.text


@lru_cache(maxsize=1)
def bases() -> dict[str, Base]:
    out: dict[str, Base] = {}
    for f in sorted((DATA_DIR / "Bases").glob("*.lua")):
        for m in _BASE_RE.finditer(f.read_text(encoding="utf-8")):
            kind = _TYPE_RE.search(m["body"])
            level = _REQ_LEVEL_RE.search(m["body"])
            out[m["name"]] = Base(m["name"], kind["value"] if kind else "", int(level.group(1)) if level else 0)
    return out


@lru_cache(maxsize=1)
def uniques() -> dict[str, Unique]:
    out: dict[str, Unique] = {}
    for f in sorted((DATA_DIR / "Uniques").glob("*.lua")):
        for m in _UNIQUE_RE.finditer(f.read_text(encoding="utf-8")):
            text = m["text"].strip("\n")
            lines = text.splitlines()
            if len(lines) < 2:
                continue
            name, base = lines[0].strip(), lines[1].strip()
            level = _UNIQUE_LEVEL_RE.search(text)
            base_info = bases().get(base)
            out.setdefault(name, Unique(
                name=name, base=base, kind=f.stem, text=text,
                level=int(level.group(1)) if level else (base_info.level if base_info else 0),
            ))
    return out

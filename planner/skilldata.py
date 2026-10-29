import re
from functools import lru_cache
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "vendor" / "PathOfBuilding-PoE2" / "src" / "Data" / "Skills"
_ENTRY_RE = re.compile(r'^\tname = "(?P<name>[^"]+)",\n(?P<body>.*?)^\tdescription = "(?P<desc>(?:[^"\\]|\\.)*)",', re.M | re.S)


@lru_cache(maxsize=1)
def descriptions() -> dict[str, str]:
    out: dict[str, str] = {}
    for f in sorted(SKILLS_DIR.glob("*.lua")):
        for m in _ENTRY_RE.finditer(f.read_text(encoding="utf-8")):
            if "\n\tname = " in m["body"]:  # se juntaron dos entradas
                continue
            out.setdefault(m["name"], m["desc"].replace('\\"', '"'))
    return out


def describe(name: str) -> str | None:
    return descriptions().get(name)

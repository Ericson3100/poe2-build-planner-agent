import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Wallet:
    league: str
    gold: int
    currency: dict[str, int] = field(default_factory=dict)
    costs: dict[str, float] = field(default_factory=dict)


def load_wallet(path: str | Path) -> Wallet:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return Wallet(
        league=data["league"],
        gold=int(data.get("gold", 0)),
        currency={name: int(count) for name, count in data.get("currency", {}).items() if int(count) > 0},
        costs={k: float(v) for k, v in data.get("costs", {}).items()},
    )

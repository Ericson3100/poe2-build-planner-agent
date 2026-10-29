from dataclasses import dataclass
from urllib.parse import urlencode

from planner.net import request_json

NINJA = "https://poe.ninja/poe2/api/economy"
TTL = 3600

EXCHANGE_TYPES = [
    "Currency", "Fragments", "Abyss", "UncutGems", "LineageSupportGems", "Essences",
    "SoulCores", "Idols", "Runes", "Ritual", "Expedition", "Delirium", "Breach", "Verisium",
]
UNIQUE_TYPES = [
    "UniqueWeapons", "UniqueArmours", "UniqueAccessories", "UniqueFlasks",
    "UniqueCharms", "UniqueJewels", "UniqueSanctumRelics", "UniqueTablets",
]


@dataclass
class Price:
    name: str
    divine: float
    category: str
    base_type: str | None = None
    listings: int | None = None


class PriceBook:

    def __init__(self, league: str) -> None:
        self.league = league
        self._prices: dict[str, Price] = {}
        self._by_trade_id: dict[str, float] = {}
        self._loaded: set[str] = set()
        self.exalted_per_divine: float | None = None

    def _get(self, path: str, type_: str) -> dict:
        query = urlencode({"league": self.league, "type": type_})
        return request_json(f"{NINJA}/{path}?{query}", ttl=TTL, policy="ninja")

    def load_exchange(self, type_: str) -> None:
        if type_ in self._loaded:
            return
        data = self._get("exchange/current/overview", type_)
        names = {it["id"]: it["name"] for it in data.get("items", [])}
        names.update({it["id"]: it["name"] for it in data["core"].get("items", [])})
        rates = data["core"].get("rates", {})
        if "exalted" in rates:
            self.exalted_per_divine = rates["exalted"]
        for line in data["lines"]:
            name = names.get(line["id"], line["id"])
            self._prices[name] = Price(name=name, divine=line["primaryValue"], category=type_)
            self._by_trade_id[line["id"]] = line["primaryValue"]
        self._by_trade_id.setdefault("divine", 1.0)
        self._loaded.add(type_)

    def load_uniques(self, type_: str) -> None:
        if type_ in self._loaded:
            return
        for line in self._get("stash/current/item/overview", type_)["lines"]:
            key = line["name"]
            # el mismo único sale varias veces (corrupto, otras bases): se guarda el más barato
            if key in self._prices and self._prices[key].divine <= line["primaryValue"]:
                continue
            self._prices[key] = Price(
                name=key, divine=line["primaryValue"], category=type_,
                base_type=line.get("baseType"), listings=line.get("listingCount"),
            )
        self._loaded.add(type_)

    def load_all(self) -> None:
        for t in EXCHANGE_TYPES:
            self.load_exchange(t)
        for t in UNIQUE_TYPES:
            self.load_uniques(t)

    def get(self, name: str) -> Price | None:
        if name not in self._prices:
            self.load_all()
        return self._prices.get(name)

    def listing_in_exalted(self, amount: float, currency: str) -> float | None:
        self.load_exchange("Currency")
        divines = self._by_trade_id.get(currency)
        return None if divines is None else self.to_exalted(amount * divines)

    def to_exalted(self, divine: float) -> float:
        if self.exalted_per_divine is None:
            self.load_exchange("Currency")
        return divine * self.exalted_per_divine

    def wallet_value(self, currency: dict[str, int]) -> float:
        total = 0.0
        for name, count in currency.items():
            price = self.get(name)
            if price:
                total += price.divine * count
            elif name == "Divine Orb":
                total += count
        return total

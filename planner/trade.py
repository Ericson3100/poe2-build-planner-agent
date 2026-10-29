import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote

from planner.net import request_json

BASE = "https://www.pathofexile.com/api/trade2"
SITE = "https://www.pathofexile.com/trade2"
SEARCH_TTL = 900
_BRACKET_RE = re.compile(r"\[(?:[^|\]]*\|)?([^\]]*)\]")


@dataclass
class StatFilter:
    stat_id: str
    min: float | None = None
    max: float | None = None


@dataclass
class Listing:
    id: str
    name: str
    base_type: str
    ilvl: int | None
    price_amount: float | None
    price_currency: str | None
    rarity: str = "RARE"
    implicits: list[str] = field(default_factory=list)
    explicits: list[str] = field(default_factory=list)
    corrupted: bool = False
    whisper_account: str | None = None
    stash: str | None = None
    fee_gold: int | None = None
    query: dict | None = None
    search_id: str | None = None
    quality: int = 0
    sockets: int = 0
    runes: list[str] = field(default_factory=list)
    requirements: dict[str, int] = field(default_factory=dict)

    def purchase_link(self, league: str) -> str:
        body = json.loads(json.dumps(self.query or {"query": {}}))
        q = body.setdefault("query", {})
        q.setdefault("filters", {}).setdefault("trade_filters", {}).setdefault("filters", {})
        if self.whisper_account:
            q["filters"]["trade_filters"]["filters"]["account"] = {"input": self.whisper_account}
        return search_link(league, body)

    @property
    def mods(self) -> list[str]:
        return self.implicits + self.explicits

    def to_pob_text(self) -> str:
        lines = [f"Rarity: {self.rarity}"]
        if self.rarity in ("RARE", "UNIQUE"):
            lines += [self.name or "Trade Item", self.base_type]
        else:
            lines.append(self.base_type)
        if self.ilvl:
            lines.append(f"Item Level: {self.ilvl}")
        if self.requirements.get("Level"):
            lines.append(f"LevelReq: {self.requirements['Level']}")
        if self.quality:
            lines.append(f"Quality: {self.quality}")
        if self.sockets:
            lines.append("Sockets: " + " ".join(["S"] * self.sockets))
            lines += [f"Rune: {r}" for r in self.runes] + ["Rune: None"] * (self.sockets - len(self.runes))
        lines.append(f"Implicits: {len(self.implicits)}")
        lines += self.implicits + self.explicits
        if self.corrupted:
            lines.append("Corrupted")
        return "\n".join(lines)


def _quality(item: dict) -> int:
    for prop in item.get("properties", []):
        if "Quality" in prop.get("name", "") and prop.get("values"):
            m = re.search(r"\d+", prop["values"][0][0])
            return int(m.group(0)) if m else 0
    return 0


def _requirements(item: dict) -> dict[str, int]:
    out = {}
    for req in item.get("requirements", []):
        name = clean_mod_text(req.get("name", "")).strip()
        name = {"Strength": "Str", "Dexterity": "Dex", "Intelligence": "Int"}.get(name, name)
        m = re.search(r"\d+", (req.get("values") or [[""]])[0][0])
        if m:
            out[name] = int(m.group(0))
    return out


def clean_mod_text(text: str) -> str:
    return _BRACKET_RE.sub(r"\1", text)


def stats_catalog() -> dict[str, str]:
    data = request_json(f"{BASE}/data/stats", ttl=24 * 3600, policy="trade-data")
    return {entry["id"]: entry["text"] for group in data["result"] for entry in group["entries"]}


def find_stats(text: str, kind: str = "explicit") -> list[tuple[str, str]]:
    needle = text.lower()
    return [(sid, t) for sid, t in stats_catalog().items() if sid.startswith(kind + ".") and needle in t.lower()]


def build_query(*, name: str | None = None, base_type: str | None = None, category: str | None = None,
                stats: list[StatFilter] | None = None, max_price: float | None = None,
                price_currency: str = "exalted", max_level: int | None = None, rarity: str | None = None,
                any_of: list[StatFilter] | None = None) -> dict:
    query: dict = {"status": {"option": "securable"}}
    if name:
        query["name"] = name
    if base_type:
        query["type"] = base_type
    def stat_filters(group):
        return [{"id": s.stat_id, "value": {k: v for k, v in (("min", s.min), ("max", s.max)) if v is not None}}
                for s in group]
    if stats:
        query["stats"] = [{"type": "and", "filters": stat_filters(stats)}]
    if any_of:
        query.setdefault("stats", []).append({"type": "count", "value": {"min": 1}, "filters": stat_filters(any_of)})
    filters: dict = {}
    if category or rarity:
        type_filters = {"category": {"option": category}} if category else {}
        if rarity:
            type_filters["rarity"] = {"option": rarity}
        filters["type_filters"] = {"filters": type_filters}
    if max_price is not None:
        filters["trade_filters"] = {"filters": {"price": {"max": max_price, "option": price_currency}}}
    if max_level is not None:
        filters["req_filters"] = {"filters": {"lvl": {"max": max_level}}}
    if filters:
        query["filters"] = filters
    return {"query": query, "sort": {"price": "asc"}}


def search_link(league: str, body: dict) -> str:
    return f"{SITE}/search/poe2/{quote(league)}?q={quote(json.dumps(body, separators=(',', ':')))}"


def search(league: str, body: dict, limit: int = 10, ttl: float = SEARCH_TTL) -> tuple[int, list[Listing]]:
    found = request_json(f"{BASE}/search/poe2/{quote(league)}", body=body, ttl=ttl, policy="trade-search")
    ids = found.get("result", [])[: min(limit, 10)]  # máximo 10 ids por fetch
    if not ids:
        return found.get("total", 0), []
    fetched = request_json(f"{BASE}/fetch/{','.join(ids)}?query={found['id']}", ttl=ttl, policy="trade-fetch")
    listings = []
    for res in fetched["result"]:
        item, listing = res["item"], res["listing"]
        price = listing.get("price") or {}

        def texts(key: str, tag: str = "") -> list[str]:
            return [tag + clean_mod_text(m["description"] if isinstance(m, dict) else m) for m in item.get(key, [])]

        listings.append(Listing(
            id=res["id"], name=item.get("name", ""), base_type=item.get("baseType", ""),
            ilvl=item.get("ilvl"), price_amount=price.get("amount"), price_currency=price.get("currency"),
            rarity=item.get("rarity", "Rare").upper(),
            implicits=texts("enchantMods", "{enchant}") + texts("runeMods", "{enchant}{rune}") + texts("implicitMods"),
            explicits=texts("explicitMods") + texts("craftedMods", "{crafted}") + texts("desecratedMods"),
            corrupted=bool(item.get("corrupted")),
            whisper_account=(listing.get("account") or {}).get("name"),
            stash=(f"{listing['stash'].get('name')} ({listing['stash'].get('x')}, {listing['stash'].get('y')})"
                   if listing.get("stash") else None),
            fee_gold=listing.get("fee"),
            query=body,
            search_id=found.get("id"),
            quality=_quality(item),
            sockets=len(item.get("sockets") or []),
            runes=[x.get("typeLine", "") for x in item.get("socketedItems") or []],
            requirements=_requirements(item),
        ))
    return found.get("total", 0), listings

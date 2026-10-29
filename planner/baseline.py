import re
from dataclasses import dataclass

from planner import trade
from planner.agent_tools import SLOT_CATEGORY, PlanStep, Session
from planner.pob_engine import skills_dps, with_item

MAX_SEARCHES = 10
ROUNDS = 2
LISTINGS_PER_SEARCH = 5
MAX_DPS_LOSS = 0.10
MAX_EHP_LOSS = 0.15
SPIRIT_ID = "explicit.stat_3981240776"
STAT_IDS = {
    "fire": "pseudo.pseudo_total_fire_resistance", "cold": "pseudo.pseudo_total_cold_resistance",
    "lightning": "pseudo.pseudo_total_lightning_resistance", "chaos": "pseudo.pseudo_total_chaos_resistance",
    "str": "pseudo.pseudo_total_strength", "dex": "pseudo.pseudo_total_dexterity",
    "int": "pseudo.pseudo_total_intelligence", "life": "pseudo.pseudo_total_life",
    "es": "pseudo.pseudo_total_energy_shield", "spirit": SPIRIT_ID,
}
# slots que puede reemplazar (los únicos no se tocan)
SLOT_ORDER = ["Ring 1", "Ring 2", "Amulet", "Belt", "Boots", "Gloves", "Helmet", "Body Armour"]
SPIRIT_SLOTS = {"Amulet", "Body Armour"}


@dataclass
class Need:
    key: str
    amount: float
    reason: str
    priority: int  # 0 = problema grave


@dataclass
class Candidate:
    slot: str
    item: str
    price: float
    listing_id: str
    new_violations: list
    fixed_violations: list
    dps_pct: float
    ehp_pct: float
    score: float


def item_contribution(item) -> dict[str, float]:
    out: dict[str, float] = {}

    def add(k, v):
        out[k] = out.get(k, 0) + v

    for mod in item.implicits + item.explicits:
        t = mod.text
        if m := re.search(r"\+(\d+)% to all Elemental Resistances", t):
            for k in ("fire", "cold", "lightning"):
                add(k, int(m.group(1)))
        elif m := re.search(r"\+(\d+)% to (Fire|Cold|Lightning|Chaos)(?: and (Fire|Cold|Lightning|Chaos))? Resistances?", t):
            for g in (m.group(2), m.group(3)):
                if g:
                    add(g.lower(), int(m.group(1)))
        if m := re.search(r"\+(\d+) to all Attributes", t):
            for k in ("str", "dex", "int"):
                add(k, int(m.group(1)))
        elif m := re.search(r"\+(\d+) to (Strength|Dexterity|Intelligence)(?: and (Strength|Dexterity|Intelligence))?", t):
            for g in (m.group(2), m.group(3)):
                if g:
                    add(g[:3].lower(), int(m.group(1)))
        for name, key in (("Life", "life"), ("Energy Shield", "es")):
            if m := re.search(rf"\+(\d+) to maximum {name}", t):
                add(key, int(m.group(1)))
        if m := re.search(r"\+(\d+) to Spirit", t):
            add("spirit", int(m.group(1)))
    return out


# las únicas necesidades que conoce la línea base
def needs(s: Session) -> list[Need]:
    st = s.base_stats
    out = []
    for res in ("Fire", "Cold", "Lightning"):
        missing = (s.resist_floor - st.get(f"{res}Resist", 0)) if s.resist_floor is not None else st.get(f"Missing{res}Resist", 0)
        if missing > 0:
            out.append(Need(res.lower(), missing, f"{res}Resist: faltan {missing:.0f}%", 0))
    for attr in ("Str", "Dex", "Int"):
        if (gap := st.get(f"Req{attr}", 0) - st.get(attr, 0)) > 0:
            out.append(Need(attr.lower(), gap, f"{attr} {st.get(attr):.0f} < requisito {st.get(f'Req{attr}'):.0f}", 0))
    if (over := -st.get("SpiritUnreserved", 0)) > 0:
        out.append(Need("spirit", over, f"Spirit sobre-reservado ({-over:.0f})", 0))
    hits = {k: st.get(f"{k}MaximumHitTaken", 0) for k in ("Physical", "Fire", "Cold", "Lightning", "Chaos")}
    if min(hits, key=hits.get) == "Chaos" and st.get("ChaosResist", 0) < 40:
        out.append(Need("chaos", 20, f"Caos es la capa más débil (golpe máx. {hits['Chaos']:.0f})", 1))
    # siempre intenta subir la reserva de defensa principal (ES o vida, la que sea mayor)
    pool = "es" if st.get("EnergyShield", 0) >= st.get("Life", 0) else "life"
    out.append(Need(pool, 40, "Más Energy Shield" if pool == "es" else "Más vida", 2))
    return sorted(out, key=lambda n: n.priority)


def _keep_filters(s: Session, slot: str) -> list[trade.StatFilter]:
    # lo que el item actual aporta y no se puede perder al cambiarlo
    contrib = item_contribution(s.build.equipped()[slot])
    st = s.base_stats
    keep = []
    for res in ("fire", "cold", "lightning"):
        v = contrib.get(res, 0)
        over = st.get(f"{res.capitalize()}ResistOverCap", 0)
        if v and v > over:
            keep.append(trade.StatFilter(STAT_IDS[res], min=round(v - over)))
    for attr in ("str", "dex", "int"):
        v = contrib.get(attr, 0)
        cap = attr.capitalize()
        if v and st.get(cap, 0) - v < st.get(f"Req{cap}", 0):
            keep.append(trade.StatFilter(STAT_IDS[attr], min=round(v - (st.get(cap, 0) - st.get(f"Req{cap}", 0)))))
    if contrib.get("spirit") and st.get("SpiritUnreserved", 0) < contrib["spirit"]:
        keep.append(trade.StatFilter(SPIRIT_ID, min=contrib["spirit"]))
    return keep


def measure(s: Session, slot: str, listing: trade.Listing) -> Candidate:
    price = (s.prices.listing_in_exalted(listing.price_amount, listing.price_currency)
             if listing.price_amount is not None else None)
    s.listings[listing.id] = (listing, price)
    r = s.delta(with_item(s.base_xml, slot, listing.to_pob_text()))
    ehp = s.base_stats.get("TotalEHP") or 1
    ehp_pct = 100 * r["changes"].get("TotalEHP", {}).get("change", 0) / ehp
    price = round(price, 1) if price is not None else 1e9
    dps_pct = r.get("dps_change_pct", 0.0)
    score = (100 * len(r["fixed_violations"]) - 100 * len(r["new_violations"])
             + 50 * ehp_pct / 100 + 50 * dps_pct / 100 - 0.01 * min(price, 1000))
    return Candidate(slot, f"{listing.name} {listing.base_type}".strip(), price, listing.id,
                     r["new_violations"], r["fixed_violations"], dps_pct, ehp_pct, score)


def gather(s: Session, wanted: list[Need], exclude: set[str]) -> list[Candidate]:
    eq = s.build.equipped()
    slots = [sl for sl in SLOT_ORDER if sl in eq and eq[sl].rarity != "UNIQUE" and sl not in exclude]
    max_price = max(10.0, min(s.budget_exalted, 150.0))
    searches, out = 0, []
    for need in wanted:
        allowed = [sl for sl in slots if need.key != "spirit" or sl in SPIRIT_SLOTS][:2]
        for slot in allowed:
            if searches >= MAX_SEARCHES:
                return out
            current = item_contribution(eq[slot]).get(need.key, 0)
            target = trade.StatFilter(STAT_IDS[need.key], min=round(need.amount + current))
            filters = [target] + [f for f in _keep_filters(s, slot) if f.stat_id != target.stat_id]
            print(f"  search {slot} for {need.key} >= {target.min} (+{len(filters) - 1} keep) <= {max_price:.0f} ex")
            query = trade.build_query(category=SLOT_CATEGORY.get(slot), stats=filters, max_price=max_price,
                                      max_level=s.build.level)
            _, listings = trade.search(s.build.league, query, limit=LISTINGS_PER_SEARCH)
            searches += 1
            if not listings and len(filters) > 1 and searches < MAX_SEARCHES:
                query = trade.build_query(category=SLOT_CATEGORY.get(slot), stats=[target], max_price=max_price,
                                          max_level=s.build.level)
                _, listings = trade.search(s.build.league, query, limit=LISTINGS_PER_SEARCH)
                searches += 1
            out += [measure(s, slot, listing) for listing in listings]
    return out


def _within_limits(dps_pct: float, ehp_pct: float) -> bool:
    return dps_pct >= -100 * MAX_DPS_LOSS and ehp_pct >= -100 * MAX_EHP_LOSS


def _steps(s: Session, chosen: list[Candidate]) -> list[PlanStep]:
    steps = []
    for c in chosen:
        listing = s.listings[c.listing_id][0]
        steps.append(PlanStep(
            kind="buy", description=f"Comprar {c.item} para {c.slot}", cost_exalted=c.price,
            cost_gold=listing.fee_gold or 0, guide_quote="", slot=c.slot, listing_id=listing.id,
            rationale=f"Mayor puntaje ({c.score:.1f}): corrige {c.fixed_violations or 'nada'}, "
                      f"DPS {c.dps_pct:+.1f}%, EHP {c.ehp_pct:+.1f}%",
            link=listing.purchase_link(s.build.league), seller=listing.whisper_account, stash=listing.stash))
    return steps


def greedy(s: Session, cands: list[Candidate]) -> list[Candidate]:
    ok = [c for c in cands if not c.new_violations and c.price <= s.budget_exalted and _within_limits(c.dps_pct, c.ehp_pct)]
    chosen, spent = [], 0.0
    for c in sorted(ok, key=lambda c: c.score, reverse=True):
        if c.slot not in {x.slot for x in chosen} and spent + c.price <= s.budget_exalted:
            chosen.append(c)
            spent += c.price
    # las compras juntas también tienen que cumplir; si no, se quita la de menor puntaje
    while chosen:
        s.plan = _steps(s, chosen)
        r = s.delta(s.plan_xml())
        ehp = s.base_stats.get("TotalEHP") or 1
        ehp_pct = 100 * r["changes"].get("TotalEHP", {}).get("change", 0) / ehp
        if not r["new_violations"] and _within_limits(r.get("dps_change_pct", 0), ehp_pct):
            return chosen
        chosen.remove(min(chosen, key=lambda c: c.score))
    return []


def _spirit_fix(s: Session) -> PlanStep | None:
    # si sigue faltando spirit, apaga la skill que menos daño pierde (primero las que la guía no usa)
    r = s.delta(s.plan_xml())
    free = s.base_stats.get("SpiritUnreserved", 0) + r["changes"].get("SpiritUnreserved", {}).get("change", 0)
    if free >= 0:
        return None
    ehp = s.base_stats.get("TotalEHP") or 1
    for rep in sorted(s.spirit_report(), key=lambda x: (x["in_guide"], -x["dps_change_pct_if_disabled"])):
        if rep["spirit_reserved"] < -free:
            continue
        step = PlanStep(kind="gem", description=f"Apagar {', '.join(rep['skills'])} (libera {rep['spirit_reserved']} de spirit)",
                        cost_exalted=0, cost_gold=0, guide_quote="", disable_groups=[rep["group"]],
                        rationale=f"Es la skill que menos pierde al apagarla (DPS {rep['dps_change_pct_if_disabled']:+.1f}%)"
                                  + ("; la guía no la usa." if not rep["in_guide"] else "."))
        s.plan.insert(0, step)
        check = s.delta(s.plan_xml())
        s.plan.pop(0)
        ehp_pct = 100 * check["changes"].get("TotalEHP", {}).get("change", 0) / ehp
        if not check["new_violations"] and _within_limits(check.get("dps_change_pct", 0), ehp_pct):
            return step
    return None


def run(s: Session) -> str:
    original = (s.base_xml, s.base_stats, s.base_skills, s.budget_exalted)
    bought: list[Candidate] = []
    try:
        for round_no in range(1, ROUNDS + 1):
            wanted = needs(s)
            print(f"  ronda {round_no}, necesidades: {[n.reason for n in wanted]}")
            if not wanted:
                break
            chosen = greedy(s, gather(s, wanted, exclude={c.slot for c in bought}))
            if not chosen:
                break
            bought += chosen
            # la siguiente ronda parte de la build con estas compras
            s.base_xml = s.plan_xml()
            s.base_stats = dict(s.engine.evaluate_xml(s.base_xml))
            if s.damage:
                s.base_skills = skills_dps(s.engine, s.base_xml, s.damage)
                s.base_stats["BuildDPS"] = sum(v["dps"] for v in s.base_skills.values())
            s.budget_exalted -= sum(c.price for c in chosen)
            s.plan = []
    finally:
        s.base_xml, s.base_stats, s.base_skills, s.budget_exalted = original
    s.plan = _steps(s, bought)
    fix = _spirit_fix(s)
    if fix:
        s.plan.insert(0, fix)
    if not s.plan:
        return "No se encontraron candidatos válidos dentro del presupuesto."
    return "Línea base: compras elegidas por puntaje fijo, sin LLM."

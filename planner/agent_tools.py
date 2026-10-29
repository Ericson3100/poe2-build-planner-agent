import json
from dataclasses import dataclass, field

from anthropic import beta_tool

from planner import trade
from planner.analysis import Gaps, LevelCheck, guide_item_text, requirement_violations
from planner.guides import Guide
from planner.kb import load_kb
from planner.pob_engine import Engine, apply_item, diff_stats, skills_dps, with_jewel, with_nodes, with_skills_enabled
from planner.pob_xml import Build, load_build
from planner.prices import PriceBook
from planner.priorities import GuidePriorities
from planner.tree import load_tree
from planner import upgrades
from planner.skilldata import describe
from planner.wallet import Wallet

KEY_STATS = [
    "BuildDPS", "CombinedDPS", "TotalEHP", "Life", "EnergyShield", "Mana", "Spirit", "SpiritUnreserved",
    "FireResist", "ColdResist", "LightningResist", "ChaosResist",
    "PhysicalMaximumHitTaken", "ChaosMaximumHitTaken", "Str", "Dex", "Int", "MovementSpeedMod",
]
MAX_SEARCHES = 20  # ~15/min en el sitio
SLOT_CATEGORY = {
    "Ring 1": "accessory.ring", "Ring 2": "accessory.ring", "Amulet": "accessory.amulet",
    "Belt": "accessory.belt", "Helmet": "armour.helmet", "Gloves": "armour.gloves",
    "Boots": "armour.boots", "Body Armour": "armour.chest", "Weapon 2": "armour.shield",
}
FUTURE_KINDS = {"level-up", "ascendancy"}
RUNE_PRICE_CAP = 50.0
RUNES_TO_TEST = 10


def slot_category(slot: str) -> str:
    return "jewel" if slot.startswith("Jewel ") else SLOT_CATEGORY.get(slot, "")


@dataclass
class PlanStep:
    kind: str
    description: str
    cost_exalted: float
    cost_gold: float
    rationale: str
    guide_quote: str
    slot: str | None = None
    listing_id: str | None = None
    add_nodes: list[int] = field(default_factory=list)
    remove_nodes: list[int] = field(default_factory=list)
    deltas: dict = field(default_factory=dict)
    link: str | None = None
    seller: str | None = None
    stash: str | None = None
    node_names: list[str] = field(default_factory=list)
    disable_groups: list[int] = field(default_factory=list)
    enable_groups: list[int] = field(default_factory=list)


@dataclass
class Session:
    engine: Engine
    build: Build
    base_xml: str
    base_stats: dict
    guide: Guide
    check: LevelCheck
    priorities: GuidePriorities | None
    gaps: Gaps
    wallet: Wallet
    prices: PriceBook
    budget_exalted: float
    listings: dict[str, tuple[trade.Listing, float | None]] = field(default_factory=dict)
    plan: list[PlanStep] = field(default_factory=list)
    searches: int = 0
    evals_since_step: int = 0
    evaluated: dict[tuple[str, str], dict] = field(default_factory=dict)
    damage: list[dict] = field(default_factory=list)
    base_skills: dict[str, dict] = field(default_factory=dict)
    best_runes: dict[str, tuple[str | None, float | None]] = field(default_factory=dict)
    spirit: list[dict] | None = None
    resist_floor: float | None = None  # None = 75%
    direction: dict | None = None
    final_variant: str | None = None

    def delta(self, xml: str) -> dict:
        after = dict(self.engine.evaluate_xml(xml))
        per_skill = skills_dps(self.engine, xml, self.damage) if self.damage else {}
        if per_skill:
            after["BuildDPS"] = sum(v["dps"] for v in per_skill.values())
        before_v = set(requirement_violations(self.base_stats, self.resist_floor))
        after_v = requirement_violations(after, self.resist_floor)
        changes = {k: {"before": round(a, 1), "after": round(b, 1), "change": round(d, 1)}
                   for k, (a, b, d) in diff_stats(self.base_stats, after, KEY_STATS).items() if abs(d) > 1e-6}
        self.evals_since_step += 1
        result = {
            "changes": changes,
            "new_violations": [v for v in after_v if v not in before_v],
            "fixed_violations": [v for v in before_v if v not in after_v],
        }
        dps = changes.get("BuildDPS") or changes.get("CombinedDPS")
        if dps and dps["before"] > 0:
            result["dps_change_pct"] = round(100 * dps["change"] / dps["before"], 1)
        if per_skill:
            result["skills"] = {
                k: {"dps_before": round(self.base_skills.get(k, {}).get("dps", 0)), "dps_after": round(v["dps"]),
                    "crit_before": round(self.base_skills.get(k, {}).get("crit", 0), 1), "crit_after": round(v["crit"], 1)}
                for k, v in per_skill.items()}
        if self.evals_since_step >= 8:
            result["hint"] = (f"{self.evals_since_step} evaluations since the last plan step: record the best "
                              "candidate found so far with add_plan_step before exploring further.")
        return result

    def expand_passives(self, add: list[int], remove: list[int]) -> tuple[list[int], list[int]]:
        tree = load_tree(self.build.tree_version)
        allocated = set(self.build.nodes) - set(remove)
        full_add: list[int] = []
        for node in add:
            for n in tree.path_to(allocated | set(full_add), node):
                if n not in full_add:
                    full_add.append(n)
        full_remove = list(dict.fromkeys(list(remove) + sorted(tree.orphans(set(self.build.nodes) | set(full_add), set(remove)))))
        return full_add, full_remove

    def score(self, result: dict) -> float:
        # % de EHP + % de daño, para comparar items
        ch = result["changes"]
        ehp = self.base_stats.get("TotalEHP", 1) or 1
        return 100 * ch.get("TotalEHP", {}).get("change", 0) / ehp + result.get("dps_change_pct", 0)

    def best_rune(self, slot: str, item_text: str) -> tuple[str | None, float | None]:
        if slot in self.best_runes:
            return self.best_runes[slot]
        base = upgrades.item_state(item_text)["base"]
        cands = []
        for name, lines in upgrades.candidate_runes(slot, base).items():
            p = self.prices.get(name)
            price = self.prices.to_exalted(p.divine) if p else None
            if price is not None and price <= RUNE_PRICE_CAP:
                cands.append((sum(bool(upgrades.RELEVANT.search(ln)) for ln in lines), -price, name, price))
        cands.sort(reverse=True)
        plain, _ = upgrades.finished(item_text, slot, None)
        plain_score = self.score(self.delta(apply_item(self.base_xml, slot, plain)))
        best = (None, None, 0.0)
        for _, _, name, price in cands[:RUNES_TO_TEST]:
            text, change = upgrades.finished(item_text, slot, name)
            if not change["runes_added"]:
                continue
            gain = self.score(self.delta(apply_item(self.base_xml, slot, text))) - plain_score
            if gain > best[2]:
                best = (name, price, gain)
        self.best_runes[slot] = best[:2]
        return self.best_runes[slot]

    def potential(self, slot: str, item_text: str) -> dict | None:
        if not (slot.startswith("Weapon") or slot in upgrades.ARMOUR_SLOTS_SET):
            return None
        st = upgrades.item_state(item_text)
        room = upgrades.max_sockets(slot, st["base"]) - len(st["runes"])
        if st["quality"] >= 20 and room <= 0:
            return None
        rune, _ = self.best_rune(slot, item_text) if room > 0 else (None, None)
        text, change = upgrades.finished(item_text, slot, rune)
        result = self.delta(apply_item(self.base_xml, slot, text))
        return {"what": change, "cost": upgrades.finish_cost(change, slot, self.prices),
                "changes": {k: v["change"] for k, v in result["changes"].items()},
                "dps_change_pct": result.get("dps_change_pct", 0.0), "new_violations": result["new_violations"]}

    def spirit_report(self) -> list[dict]:
        if self.spirit is not None:
            return self.spirit
        import xml.etree.ElementTree as ET
        from planner.analysis import _gem_key
        root = ET.fromstring(self.base_xml)
        skills = root.find("Skills")
        groups = skills.find(f"SkillSet[@id='{skills.get('activeSkillSet', '1')}']").findall("Skill")
        guide_gems = set()
        plan = self.guide.build_plans.get(self.check.variant) if self.check.variant else None
        if plan:
            guide_gems = {_gem_key(g.gem_id) for g in plan.skills if g.levels.contains(self.build.level)}
        base_free = self.base_stats.get("SpiritUnreserved", 0)
        out = []
        for idx, grp in enumerate(groups, 1):
            if grp.get("source") or grp.get("enabled") == "false":
                continue
            r = self.delta(with_skills_enabled(self.base_xml, {idx: False}))
            after_free = base_free + r["changes"].get("SpiritUnreserved", {}).get("change", 0)
            reserves = round(after_free - base_free)
            if reserves <= 0:
                continue
            gems = grp.findall("Gem")
            actives = [g.get("nameSpec") for g in gems if "SupportGem" not in (g.get("gemId") or "")]
            measured = {k: v["change"] for k, v in r["changes"].items() if k not in ("SpiritUnreserved",)}
            out.append({
                "group": idx, "skills": actives, "spirit_reserved": reserves,
                "description": " | ".join(f"{n}: {describe(n)}" for n in actives if describe(n)),
                "in_guide": any(_gem_key(g.get("gemId") or "") in guide_gems for g in gems
                                if "SupportGem" not in (g.get("gemId") or "")),
                "measured_if_disabled": measured or "PoB measures no change (event-based or manual effect)",
                "dps_change_pct_if_disabled": r.get("dps_change_pct", 0.0),
            })
        self.evals_since_step = 0
        self.spirit = out
        return out

    def plan_xml(self) -> str:
        xml = self.base_xml
        toggles = {}
        for step in self.plan:
            if step.kind in FUTURE_KINDS:
                continue
            toggles.update({g: False for g in step.disable_groups})
            toggles.update({g: True for g in step.enable_groups})
        if toggles:
            xml = with_skills_enabled(xml, toggles)
        for step in self.plan:
            if step.kind in FUTURE_KINDS:
                continue
            if step.kind == "buy" and step.listing_id:
                xml = apply_item(xml, step.slot, self.listings[step.listing_id][0].to_pob_text())
            if step.add_nodes or step.remove_nodes:
                xml = with_nodes(xml, set(step.add_nodes), set(step.remove_nodes))
        return xml

    def spent(self) -> tuple[float, float]:
        current = [s for s in self.plan if s.kind not in FUTURE_KINDS]
        return sum(s.cost_exalted for s in current), sum(s.cost_gold for s in current)


def make_tools(s: Session) -> list:
    respec_gold = s.wallet.costs.get("respec_gold_per_point", 0.0)
    tree = load_tree(s.build.tree_version)

    def node_info(ids):
        return [{"id": n, "name": tree.nodes[n].name, "kind": tree.nodes[n].kind} for n in ids if n in tree.nodes]

    @beta_tool
    def find_trade_stats(text: str, kind: str = "explicit") -> str:
        """Find trade-site stat ids whose text contains `text` (case-insensitive).

        Args:
            text: Part of the stat text, e.g. "to Chaos Resistance" or "maximum Energy Shield".
            kind: Stat group: explicit, implicit, rune, enchant, pseudo.
        """
        found = trade.find_stats(text, kind)[:15]
        return json.dumps([{"stat_id": sid, "text": t} for sid, t in found]) or "[]"

    @beta_tool
    def search_trade(slot: str, stat_ids: list[str], stat_mins: list[float], max_price_exalted: float,
                     category: str = "", name: str = "", base_type: str = "") -> str:
        """Search the trade site for items for one equipment slot or jewel socket; return the cheapest listings.

        Each search costs scarce rate limit (about 15 per minute), so combine requirements into one query.

        Args:
            slot: Target slot, e.g. "Ring 1", "Boots", "Weapon 1", or a tree jewel socket "Jewel 7960".
            stat_ids: Trade stat ids that must all be present (from find_trade_stats).
            stat_mins: Minimum value for each stat id, same order as stat_ids.
            max_price_exalted: Highest price to accept, in exalted orbs.
            category: Trade category, e.g. accessory.ring, armour.boots, weapon.staff, jewel. Empty uses the slot default.
            name: Exact unique item name, if searching for a unique.
            base_type: Exact base type, e.g. "Vile Robe" or "Sapphire". Optional.
        """
        if len(stat_ids) != len(stat_mins):
            return "Error: stat_ids and stat_mins must have the same length."
        if s.searches >= MAX_SEARCHES:
            return f"Error: search budget used ({MAX_SEARCHES}). Decide with the listings already found."
        s.searches += 1
        category = category or slot_category(slot)
        body = trade.build_query(
            name=name or None, base_type=base_type or None, category=category or None,
            stats=[trade.StatFilter(sid, min=m) for sid, m in zip(stat_ids, stat_mins)],
            max_price=max_price_exalted, max_level=s.build.level,
        )
        total, listings = trade.search(s.build.league, body, limit=8)
        out = []
        for listing in listings:
            price = (s.prices.listing_in_exalted(listing.price_amount, listing.price_currency)
                     if listing.price_amount is not None else None)
            s.listings[listing.id] = (listing, price)
            out.append({"listing_id": listing.id, "item": f"{listing.name} {listing.base_type}".strip(),
                        "price_exalted": None if price is None else round(price, 1),
                        "gold_fee": listing.fee_gold, "mods": listing.mods})
        # el sitio ordena con sus propias tasas; se reordena con las de poe.ninja
        out.sort(key=lambda o: float("inf") if o["price_exalted"] is None else o["price_exalted"])
        return json.dumps({"total_matches": total, "cheapest": out})

    @beta_tool
    def evaluate_listing(listing_id: str, slot: str) -> str:
        """Equip a trade listing (from search_trade) in a slot or jewel socket and return PoB's stat changes.

        For armour and weapons below 20% quality or with free rune sockets, also returns
        potential_if_finished: the measured value at 20% quality with the best rune in every socket
        (including sockets an Artificer's Orb can add) and the extra cost of finishing it.

        Args:
            listing_id: Id returned by search_trade.
            slot: Slot to equip it in, e.g. "Ring 1", or a jewel socket "Jewel 21984".
        """
        if listing_id not in s.listings:
            return "Error: unknown listing_id; run search_trade first."
        listing, price = s.listings[listing_id]
        if (listing_id, slot) in s.evaluated:
            return json.dumps({"already_evaluated": True, **s.evaluated[(listing_id, slot)]})
        result = s.delta(apply_item(s.base_xml, slot, listing.to_pob_text()))
        result["price_exalted"] = price
        result["gold_fee"] = listing.fee_gold
        result["quality"], result["rune_sockets"], result["runes"] = listing.quality, listing.sockets, listing.runes
        result["requirements"] = listing.requirements
        if listing.requirements.get("Level", 0) > s.build.level:
            result["new_violations"].append(f"Nivel requerido {listing.requirements['Level']} > nivel del personaje {s.build.level}")
        pot = s.potential(slot, listing.to_pob_text())
        if pot:
            result["potential_if_finished"] = pot
        s.evaluated[(listing_id, slot)] = result
        return json.dumps(result)

    @beta_tool
    def evaluate_guide_item(slot: str) -> str:
        """Equip the guide's own item for a slot in the character's build and return the stat changes.

        Shows how much the guide's target item is worth to this character, before searching trade.
        Guide uniques are measured too (their full text comes from PoB's unique data, mid rolls).

        Args:
            slot: Slot name as in the gaps list, e.g. "Body Armour".
        """
        if not s.check.variant:
            return "Error: no guide variant selected."
        text = guide_item_text(s.guide, s.check.variant, slot, s.build)
        if text is None:
            return (f"Error: no measurable guide item for slot {slot} (empty slot, or a unique missing from "
                    "PoB's data: search it on trade by name instead).")
        return json.dumps({"guide_item": text.splitlines()[:3], **s.delta(apply_item(s.base_xml, slot, text))})

    @beta_tool
    def evaluate_passives(add_node_ids: list[int], remove_node_ids: list[int]) -> str:
        """Allocate and/or refund passive nodes (main tree or ascendancy) and return the stat changes.

        Connecting paths are added automatically and nodes left disconnected by a refund are refunded too;
        the result lists the full set and the point balance.

        Args:
            add_node_ids: Numeric node ids to allocate (see the gaps tree section or suggest_passives).
            remove_node_ids: Numeric node ids to refund.
        """
        unknown = [n for n in add_node_ids + remove_node_ids if n not in tree.nodes]
        if unknown:
            return f"Error: unknown node ids {unknown}."
        full_add, full_remove = s.expand_passives(add_node_ids, remove_node_ids)
        result = s.delta(with_nodes(s.base_xml, set(full_add), set(full_remove)))
        result.update({
            "allocated": node_info(full_add), "refunded": node_info(full_remove),
            "points_needed": len(full_add), "points_refunded": len(full_remove),
            "refund_gold": len(full_remove) * respec_gold if respec_gold else "unknown (no respec_gold_per_point)",
        })
        return json.dumps(result)

    @beta_tool
    def suggest_passives(max_points: int = 6, top: int = 8) -> str:
        """Rank the guide's missing notables, keystones and jewel sockets (main tree) and its missing ascendancy
        nodes by measured value per point, including the path each one needs.

        Use it for the "next points to spend when levelling" and "next ascendancy" recommendations.

        Args:
            max_points: Skip targets whose path costs more points than this.
            top: How many main-tree suggestions to return.
        """
        plan = s.guide.build_plans.get(s.check.variant) if s.check.variant else None
        if plan is not None:
            targets = plan.passives_at(s.build.level)
        else:
            target_build = None
            if s.check.variant in s.guide.pob_xml:
                import io
                target_build = load_build(io.StringIO(s.guide.pob_xml[s.check.variant]))
            targets = target_build.nodes if target_build else set()
        missing = [n for n in targets - set(s.build.nodes)
                   if n in tree.nodes and tree.nodes[n].kind in ("notable", "keystone", "jewel", "ascendancy")]
        ehp = s.base_stats.get("TotalEHP", 1) or 1
        dps = s.base_stats.get("CombinedDPS", 1) or 1
        main, asc = [], []
        for node in missing:
            path, _ = s.expand_passives([node], [])
            if not path or len(path) > max_points:
                continue
            r = s.delta(with_nodes(s.base_xml, set(path), set()))
            ch = r["changes"]
            gain = 100 * ch.get("TotalEHP", {}).get("change", 0) / ehp + 100 * ch.get("CombinedDPS", {}).get("change", 0) / dps
            row = {"target": tree.nodes[node].name, "kind": tree.nodes[node].kind, "node_id": node,
                   "points": len(path), "path": [tree.nodes[n].name for n in path],
                   "stats": list(tree.nodes[node].stats)[:3],
                   "changes": {k: v["change"] for k, v in ch.items()},
                   "new_violations": r["new_violations"], "gain_pct_per_point": round(gain / len(path), 2)}
            (asc if node in tree.ascendancy else main).append(row)
        main.sort(key=lambda r: r["gain_pct_per_point"], reverse=True)
        s.evals_since_step = 0
        return json.dumps({"main_tree": main[:top], "ascendancy": asc,
                           "note": "keystones can change the build fundamentally (e.g. Chaos Inoculation sets life "
                                   "to 1): judge them, do not rank them by numbers alone"}, ensure_ascii=False)

    @beta_tool
    def jewel_sockets() -> str:
        """List the character's tree jewel sockets with the socketed jewel, its mods and its measured value
        (stat changes if it were removed). Socket names for trade/evaluation are "Jewel <node id>"."""
        out = []
        for node, item_id in sorted(s.build.jewel_sockets.items()):
            item = s.build.items.get(item_id)
            r = s.delta(with_jewel(s.base_xml, node, None))
            out.append({"slot": f"Jewel {node}", "jewel": f"{item.name} ({item.base})" if item else None,
                        "mods": [m.text for m in item.explicits] if item else [],
                        "value_if_removed": {k: v["change"] for k, v in r["changes"].items()}})
        s.evals_since_step = 0
        return json.dumps(out, ensure_ascii=False)

    @beta_tool
    def item_upgrades() -> str:
        """Cheap upgrades of the character's own armour and weapons: missing quality and empty or addable
        rune sockets, each measured in PoB with the best affordable rune, with the estimated cost."""
        out = []
        for slot in ["Weapon 1", "Weapon 2", "Helmet", "Body Armour", "Gloves", "Boots"]:
            text = upgrades.equipped_text(s.base_xml, slot)
            if not text:
                continue
            st = upgrades.item_state(text)
            pot = s.potential(slot, text)
            out.append({"slot": slot, "item": st["base"], "quality": st["quality"],
                        "runes": st["runes"], "max_sockets": upgrades.max_sockets(slot, st["base"]),
                        "upgrade": pot or "already at 20% quality with every rune socket filled"})
        s.evals_since_step = 0
        return json.dumps(out, ensure_ascii=False)

    @beta_tool
    def spirit_report() -> str:
        """Skills that reserve spirit: reservation, PoB description, whether the guide uses them, and what PoB
        measures when each is switched off. Use it before buying spirit gear to decide whether to keep all of
        them (and pay for spirit) or switch one or two off."""
        st = s.base_stats
        return json.dumps({"spirit_total": st.get("Spirit"), "spirit_unreserved": st.get("SpiritUnreserved"),
                           "reservers": s.spirit_report()}, ensure_ascii=False)

    @beta_tool
    def evaluate_skill_toggle(disable_groups: list[int], enable_groups: list[int] | None = None) -> str:
        """Switch skill groups off (or back on) and return PoB's changes, including SpiritUnreserved.

        Args:
            disable_groups: Group numbers from spirit_report to switch off (one or several at once).
            enable_groups: Group numbers to switch on. Optional.
        """
        toggles = {g: False for g in disable_groups} | {g: True for g in (enable_groups or [])}
        result = s.delta(with_skills_enabled(s.base_xml, toggles))
        result["spirit_unreserved_after"] = (s.base_stats.get("SpiritUnreserved", 0)
                                             + result["changes"].get("SpiritUnreserved", {}).get("change", 0))
        return json.dumps(result)

    @beta_tool
    def price_lookup(names: list[str]) -> str:
        """Current poe.ninja price of uniques, currency, runes, soul cores or gems, in exalted orbs.

        An estimate: for cheap uniques (flasks, charms) the trade price can differ a lot, so confirm a
        unique with search_trade (name + base_type) before adding it to the plan.

        Args:
            names: Exact item names, e.g. ["Divine Orb", "Call of the Brotherhood"].
        """
        out = {}
        for n in names:
            p = s.prices.get(n)
            out[n] = None if p is None else round(s.prices.to_exalted(p.divine), 2)
        return json.dumps(out)

    @beta_tool
    def search_mechanics(query: str, k: int = 5) -> str:
        """Search the Path of Exile 2 knowledge base (poe2wiki pages + planner balance notes).

        Use it to check how a defence, stat, keystone, ailment or class mechanic works in PoE2 and whether
        it matters for this build. Hybrid search: keyword (BM25) and semantic, fused by rank.

        Args:
            query: Question or keywords in English, e.g. "chaos damage against energy shield".
            k: Number of passages to return (1-8).
        """
        hits = load_kb().search(query, k=max(1, min(k, 8)))
        return json.dumps([{"title": h["title"], "section": h["section"], "source": h["source"],
                            "url": h["url"], "text": h["text"][:1500]} for h in hits], ensure_ascii=False)

    @beta_tool
    def add_plan_step(kind: str, description: str, cost_exalted: float, rationale: str, guide_quote: str,
                      slot: str = "", listing_id: str = "", add_node_ids: list[int] | None = None,
                      remove_node_ids: list[int] | None = None, disable_groups: list[int] | None = None,
                      enable_groups: list[int] | None = None) -> str:
        """Record one recommended action in the plan. Current steps are applied in order by evaluate_plan.

        Args:
            kind: "buy" (trade listing, also for jewels), "respec" (passives now), "level-up" (points to spend
                while levelling, in order), "ascendancy" (next ascendancy nodes), "craft" (quality / rune
                upgrades of owned or bought items, cost = currency), "gem" or "other".
            description: Short action for the player, in Spanish.
            cost_exalted: Currency cost in exalted orbs (0 for passives or free actions).
            rationale: Why, citing the measured deltas.
            guide_quote: Verbatim guide quote that supports the step, or "" if none.
            slot: Slot or jewel socket ("Jewel 7960") for "buy" steps.
            listing_id: Trade listing id for "buy" steps.
            add_node_ids: Nodes to allocate (respec, level-up, ascendancy); paths are added automatically.
            remove_node_ids: Nodes to refund for "respec" steps.
            disable_groups: Skill groups to switch off for "gem" steps (spirit_report numbers).
            enable_groups: Skill groups to switch on for "gem" steps.
        """
        if kind == "buy" and listing_id not in s.listings:
            return "Error: buy steps need a listing_id from search_trade."
        if kind == "buy" and s.listings[listing_id][0].requirements.get("Level", 0) > s.build.level:
            return (f"Error: the item requires level {s.listings[listing_id][0].requirements['Level']}, "
                    f"the character is level {s.build.level}.")
        full_add, full_remove = s.expand_passives(add_node_ids or [], remove_node_ids or [])
        step = PlanStep(kind=kind, description=description, cost_exalted=cost_exalted,
                        cost_gold=len(full_remove) * respec_gold, rationale=rationale, guide_quote=guide_quote,
                        slot=slot or None, listing_id=listing_id or None, add_nodes=full_add, remove_nodes=full_remove,
                        node_names=[tree.nodes[n].name for n in full_add],
                        disable_groups=disable_groups or [], enable_groups=enable_groups or [])
        if kind == "buy":
            listing = s.listings[listing_id][0]
            step.link = listing.purchase_link(s.build.league)
            step.seller, step.stash = listing.whisper_account, listing.stash
            step.cost_gold += listing.fee_gold or 0
        s.plan.append(step)
        s.evals_since_step = 0
        spent_ex, spent_gold = s.spent()
        return json.dumps({"step": len(s.plan), "spent_exalted": round(spent_ex, 1),
                           "budget_exalted": round(s.budget_exalted, 1),
                           "spent_gold": spent_gold, "gold": s.wallet.gold,
                           "nodes_allocated": step.node_names})

    @beta_tool
    def remove_plan_step(step_number: int) -> str:
        """Remove a step from the plan (1-based number as returned by add_plan_step).

        Args:
            step_number: Step to remove.
        """
        if not 1 <= step_number <= len(s.plan):
            return "Error: no such step."
        removed = s.plan.pop(step_number - 1)
        return f"Removed: {removed.description}"

    @beta_tool
    def evaluate_plan() -> str:
        """Apply every current plan step (buy/respec) together and return combined stat changes, budget use
        and problems. Level-up and ascendancy steps are future recommendations and are not applied.

        Call this before finishing; items and passives interact, so per-step deltas do not simply add up.
        """
        result = s.delta(s.plan_xml())
        spent_ex, spent_gold = s.spent()
        result.update({
            "steps": len(s.plan),
            "spent_exalted": round(spent_ex, 1), "budget_exalted": round(s.budget_exalted, 1),
            "over_budget": spent_ex > s.budget_exalted + 1e-6,
            "spent_gold": spent_gold, "gold": s.wallet.gold, "over_gold": spent_gold > s.wallet.gold,
        })
        return json.dumps(result)

    return [find_trade_stats, search_trade, evaluate_listing, evaluate_guide_item, evaluate_passives,
            suggest_passives, jewel_sockets, item_upgrades, spirit_report, evaluate_skill_toggle, price_lookup, search_mechanics, add_plan_step, remove_plan_step,
            evaluate_plan]

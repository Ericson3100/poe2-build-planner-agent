import io
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

from planner import itemdata
from planner.build_file import BuildPlan, hint_item_text
from planner.guides import Guide, list_guides, load_guide
from planner.pob_xml import Build, load_build, parse_item_text
from planner.priorities import GuidePriorities, Stage
from planner.tree import load_tree

# máxima diferencia de nivel entre la guía y el personaje
LEVEL_MARGIN = 15
RESISTS = ("Fire", "Cold", "Lightning", "Chaos")


@dataclass
class LevelCheck:
    ok: bool
    character_level: int
    required_level: int | None
    stage: str | None
    variant: str | None
    reason: str
    scores: dict = field(default_factory=dict)


def _guide_levels(guide: Guide) -> dict[str, int]:
    levels = {}
    for variant, xml in guide.pob_xml.items():
        build_el = ET.fromstring(xml).find("Build")
        if build_el is not None and build_el.get("level"):
            levels[variant] = int(build_el.get("level"))
    return levels


def _variant_for(stage: Stage | None, guide: Guide) -> str | None:
    if stage and stage.pob_variant in guide.pob_xml:
        return stage.pob_variant
    if len(guide.pob_xml) == 1:
        return next(iter(guide.pob_xml))
    return None


def _quoted_level(stage: Stage, guide_text: str) -> bool:
    quote = " ".join(stage.level_quote.split())
    if stage.min_level <= 1 or not quote or quote.lower() not in " ".join(guide_text.split()).lower():
        return False
    return re.search(rf"\b(level|lvl|nivel)\s*{stage.min_level}\b|\b{stage.min_level}\+?\s*(level|lvl)\b",
                     quote, re.IGNORECASE) is not None


# pesos del parecido entre personaje y variante, y mínimo para aceptarla
SIMILARITY_WEIGHTS = {"tree": 0.45, "gems": 0.35, "uniques": 0.20}
MIN_MATCH = 0.35
LEVEL_PENALTY = 0.85


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _variant_profile(guide: Guide, variant: str, level: int) -> dict:
    if variant in guide.build_plans:
        plan = guide.build_plans[variant]
        return {
            "nodes": plan.passives_at(level),
            "gems": ({_gem_key(g.gem_id) for g in plan.skills if g.levels.contains(level)}
                     | {_gem_key(sp.gem_id) for g in plan.skills for sp in g.supports}),
            "uniques": {h.unique_name for h in plan.slots if h.unique_name},
            "required": plan.gear_level(),
        }
    target = load_build(io.StringIO(guide.pob_xml[variant]))
    return {
        "nodes": set(target.nodes),
        "gems": {_gem_key(g.gem_id) for grp in target.skills for g in grp.gems if not grp.granted_by},
        "uniques": {it.name for it in target.equipped().values() if it.rarity == "UNIQUE"},
        "required": target.level,
    }


def choose_guide(build: Build, guide_ids: list[str] | None = None) -> tuple[str | None, dict[str, float]]:
    scores: dict[str, float] = {}
    for gid in guide_ids or list_guides():
        try:
            check = choose_variant(build, load_guide(gid))
        except Exception as e:
            print(f"  guía {gid} omitida: {e}")
            continue
        if check.ok and check.scores:
            scores[gid] = max(sc["score"] for sc in check.scores.values())
    return max(scores, key=scores.get, default=None), scores


def choose_variant(build: Build, guide: Guide) -> LevelCheck:
    level = build.level
    variants = sorted(set(guide.build_plans) | set(guide.pob_xml))
    if not variants:
        return LevelCheck(True, level, None, None, None, "La guía no trae PoB ni .build; se usa solo su texto.")
    mine_nodes = set(build.nodes)
    mine_gems = {_gem_key(g.gem_id) for grp in build.skills for g in grp.gems if not grp.granted_by}
    mine_uniques = {it.name for it in build.equipped().values() if it.rarity == "UNIQUE"}

    scores = {}
    for v in variants:
        prof = _variant_profile(guide, v, level)
        parts = {
            "tree": _jaccard(mine_nodes, prof["nodes"]),
            "gems": _jaccard(mine_gems, prof["gems"]),
            "uniques": len(mine_uniques & prof["uniques"]) / len(prof["uniques"]) if prof["uniques"] else 0.0,
        }
        score = sum(SIMILARITY_WEIGHTS[k] * parts[k] for k in parts)
        if prof["required"] > level:
            score *= LEVEL_PENALTY
        scores[v] = {**{k: round(x, 2) for k, x in parts.items()}, "required_level": prof["required"],
                     "score": round(score, 3)}

    earliest = min(variants, key=lambda v: scores[v]["required_level"])
    if scores[earliest]["required_level"] - LEVEL_MARGIN > level:
        req = scores[earliest]["required_level"]
        return LevelCheck(False, level, req, None, earliest,
                          f"Hasta la variante más temprana ('{earliest}') pide nivel {req}, "
                          f"más de {LEVEL_MARGIN} niveles por encima.", scores)
    reachable = [v for v in variants if scores[v]["required_level"] - LEVEL_MARGIN <= level]
    best = max(reachable, key=lambda v: scores[v]["score"])
    name = guide.build_plans[best].name if best in guide.build_plans else best
    if scores[best]["score"] < MIN_MATCH:
        name = guide.build_plans[earliest].name if earliest in guide.build_plans else earliest
        return LevelCheck(True, level, scores[earliest]["required_level"], name, earliest,
                          f"Ninguna variante se parece lo suficiente a tu build (mejor puntaje "
                          f"{scores[best]['score']:.2f} < {MIN_MATCH}); se usa la más temprana, '{earliest}'.", scores)
    return LevelCheck(True, level, scores[best]["required_level"], name, best,
                      f"Variante '{best}': la más parecida a tu build (puntaje {scores[best]['score']:.2f}).", scores)


def level_gate(build: Build, guide: Guide, priorities: GuidePriorities | None) -> LevelCheck:
    level = build.level
    # solo cuentan las etapas cuyo nivel está citado en la guía
    stated = [s for s in priorities.stages if _quoted_level(s, guide.full_text())] if priorities else []
    if stated:
        stages = sorted(stated, key=lambda s: s.min_level)
        reachable = [s for s in stages if s.min_level <= level]
        if not reachable:
            first = stages[0]
            return LevelCheck(False, level, first.min_level, None, None,
                              f"La etapa más temprana de la guía ('{first.name}') empieza en nivel {first.min_level}.")
        stage = reachable[-1]
        return LevelCheck(True, level, stage.min_level, stage.name, _variant_for(stage, guide),
                          f"Etapa '{stage.name}' (desde nivel {stage.min_level}).")

    levels = _guide_levels(guide)
    if not levels and guide.build_plans:
        # variantes .build: la más avanzada cuyo equipo ya puede usar
        gear = {v: plan.gear_level() for v, plan in guide.build_plans.items()}
        ready = {v: lv for v, lv in gear.items() if lv <= level}
        if ready:
            variant = max(ready, key=ready.get)
            return LevelCheck(True, level, ready[variant], guide.build_plans[variant].name, variant,
                              f"Variante '{variant}' (su gear empieza en nivel {ready[variant]}).")
        variant, required = min(gear.items(), key=lambda kv: kv[1])
        if required - LEVEL_MARGIN <= level:
            return LevelCheck(True, level, required, guide.build_plans[variant].name, variant,
                              f"Variante '{variant}' (gear desde nivel {required}, dentro del margen).")
        return LevelCheck(False, level, required, None, variant,
                          f"El gear de la variante más temprana ('{variant}') empieza en nivel {required}.")
    if not levels:
        return LevelCheck(True, level, None, None, None, "La guía no indica niveles; se usa sin etapa.")
    # la variante más cercana que no esté muy por encima del nivel
    usable = {v: lv for v, lv in levels.items() if lv - LEVEL_MARGIN <= level}
    if not usable:
        variant, required = min(levels.items(), key=lambda kv: kv[1])
        return LevelCheck(False, level, required, None, variant,
                          f"El PoB más bajo de la guía ('{variant}') es de nivel {required}, "
                          f"más de {LEVEL_MARGIN} niveles por encima.")
    variant = max(usable, key=usable.get)
    return LevelCheck(True, level, usable[variant], None, variant, f"Variante PoB '{variant}' (nivel {usable[variant]}).")


def requirement_violations(stats: dict, resist_floor: float | None = None) -> list[str]:
    out = []
    for attr in ("Str", "Dex", "Int"):
        have, need = stats.get(attr, 0), stats.get(f"Req{attr}", 0)
        if isinstance(need, (int, float)) and have < need:
            out.append(f"{attr} {have:.0f} < requisito {need:.0f}")
    if stats.get("SpiritUnreserved", 0) < 0:
        out.append(f"Spirit sobre-reservado ({stats['SpiritUnreserved']:.0f})")
    for res in RESISTS[:3]:
        if resist_floor is not None:
            value = stats.get(f"{res}Resist", 0)
            if value < resist_floor:
                out.append(f"{res}Resist {value:.0f}% < mínimo {resist_floor:.0f}%")
            continue
        missing = stats.get(f"Missing{res}Resist", 0)
        if missing > 0:
            out.append(f"{res}Resist sin cap (faltan {missing:.0f}%)")
    return out


@dataclass
class Gaps:
    violations: list[str]
    thresholds: list[dict] = field(default_factory=list)
    tree: dict = field(default_factory=dict)
    gems: dict = field(default_factory=dict)
    slots: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _active_gems(skills) -> set[str]:
    return {g.name for grp in skills for g in grp.gems if not g.is_support and not grp.granted_by}


def guide_build(guide: Guide, variant: str | None):
    if not variant or variant not in guide.pob_xml:
        return None
    return load_build(io.StringIO(guide.pob_xml[variant]))


def compute_gaps(build: Build, stats: dict, guide: Guide, priorities: GuidePriorities | None,
                 check: LevelCheck, resist_floor: float | None = None) -> Gaps:
    gaps = Gaps(violations=requirement_violations(stats, resist_floor))

    if priorities:
        for t in priorities.thresholds:
            if t.stage not in (None, check.stage):
                continue
            current = stats.get(t.stat)
            if isinstance(current, (int, float)) and current < t.target:
                gaps.thresholds.append({"stat": t.stat, "current": round(current, 1), "target": t.target,
                                        "priority": t.priority, "quote": t.quote})
        gaps.thresholds.sort(key=lambda g: g["priority"])

    tree = load_tree(build.tree_version)

    def describe(ids):
        nodes = [tree.nodes[i] for i in ids if i in tree.nodes]
        important = [{"id": n.id, "name": n.name, "kind": n.kind, "stats": list(n.stats)}
                     for n in nodes if n.kind in ("keystone", "notable", "ascendancy", "jewel")]
        return {"count": len(ids), "important": important, "small_ids": [n.id for n in nodes if n.kind in ("small", "attribute")]}

    if check.variant in guide.build_plans:
        _plan_gaps(gaps, build, guide.build_plans[check.variant], describe)
        return gaps
    target = guide_build(guide, check.variant)
    if target is None:
        return gaps

    gaps.tree = {
        "character_points": len(build.nodes),
        "guide_points": len(target.nodes),
        "guide_level": target.level,
        "missing_from_character": describe(target.nodes - build.nodes),
        "not_in_guide": describe(build.nodes - target.nodes),
    }
    mine, theirs = _active_gems(build.skills), _active_gems(target.skills)
    gaps.gems = {"missing": sorted(theirs - mine), "extra": sorted(mine - theirs)}

    mine_eq, theirs_eq = build.equipped(), target.equipped()
    for slot, item in sorted(theirs_eq.items()):
        current = mine_eq.get(slot)
        gaps.slots.append({
            "slot": slot,
            "guide_item": f"{item.name} ({item.base})" if item.name != item.base else item.base,
            "guide_rarity": item.rarity,
            "guide_mods": [m.text for m in item.implicits + item.explicits],
            "guide_level_req": item.properties.get("LevelReq"),
            "current_item": f"{current.name} ({current.base})" if current else None,
            "same_unique": bool(current and item.rarity == "UNIQUE" and current.name == item.name),
        })
    return gaps


def _all_nodes(build: Build) -> set[int]:
    nodes = set(build.nodes)
    for ws in build.weapon_set_nodes.values():
        nodes |= ws
    return nodes


def _gem_key(gem_id: str) -> str:
    return gem_id.rsplit("/", 1)[-1]


def align_slots(build: Build, plan: BuildPlan) -> dict[str, str]:
    hints = plan.slot_hints_at(build.level)
    mapping = {slot: slot for slot in hints}
    eq = build.equipped()
    ring_slots = [r for r in ("Ring 1", "Ring 2") if r in hints]
    for guide_ring in ring_slots:
        name = hints[guide_ring].unique_name
        holder = next((r for r in ("Ring 1", "Ring 2") if name and r in eq and eq[r].name == name), None)
        if holder and holder != guide_ring:
            other = "Ring 2" if guide_ring == "Ring 1" else "Ring 1"
            mapping[guide_ring], mapping[other] = holder, guide_ring
    return mapping


def _plan_gaps(gaps: Gaps, build: Build, plan: BuildPlan, describe) -> None:
    target_nodes = plan.passives_at(build.level)
    mine_nodes = _all_nodes(build)
    gaps.tree = {
        "character_points": len(mine_nodes),
        "guide_points": len(target_nodes),
        "note": "guide tree is the full planned tree; it needs more points than the character has",
        "missing_from_character": describe(target_nodes - mine_nodes),
        "not_in_guide": describe(mine_nodes - target_nodes),
    }
    mine = {_gem_key(g.gem_id) for grp in build.skills for g in grp.gems if not grp.granted_by}
    theirs = {_gem_key(g.gem_id) for g in plan.skills if g.levels.contains(build.level)}
    theirs |= {_gem_key(sp.gem_id) for g in plan.skills for sp in g.supports}
    gaps.gems = {"missing": sorted(theirs - mine), "extra": sorted(mine - theirs)}

    eq = build.equipped()
    for guide_slot, mine_slot in sorted(align_slots(build, plan).items()):
        hint = plan.slot_hints_at(build.level)[guide_slot]
        current = eq.get(mine_slot)
        gaps.slots.append({
            "slot": mine_slot,
            "guide_item": hint.unique_name or (hint.note or "").split("\n")[0],
            "guide_rarity": "UNIQUE" if hint.unique_name else "RARE",
            "guide_mods": [] if hint.unique_name else [ln for ln in (hint.note or "").split("\n")[1:] if ln.strip()],
            "guide_level_from": hint.levels.low,
            "current_item": f"{current.name} ({current.base})" if current else None,
            "same_unique": bool(current and hint.unique_name and current.name == hint.unique_name),
        })


def guide_item_text(guide: Guide, variant: str, slot: str, build: Build | None = None) -> str | None:
    if variant in guide.build_plans:
        return _plan_item_text(guide.build_plans[variant], slot, build) if build else None
    root = ET.fromstring(guide.pob_xml[variant])
    items = root.find("Items")
    item_set = items.find(f"ItemSet[@id='{items.get('activeItemSet', '1')}']")
    slot_el = item_set.find(f"Slot[@name='{slot}']") if item_set is not None else None
    if slot_el is None or slot_el.get("itemId", "0") == "0":
        return None
    item_el = items.find(f"Item[@id='{slot_el.get('itemId')}']")
    if item_el is None:
        return None
    # valida que PoB lo lea
    parse_item_text(0, item_el.text or "")
    return (item_el.text or "").strip()


def _plan_item_text(plan: BuildPlan, slot: str, build: Build) -> str | None:
    hints = plan.slot_hints_at(build.level)
    for guide_slot, mine_slot in align_slots(build, plan).items():
        if mine_slot == slot:
            hint = hints[guide_slot]
            unique = itemdata.uniques().get(hint.unique_name) if hint.unique_name else None
            return unique.item_text() if unique else hint_item_text(hint)
    return None

import hashlib
import io
import json

from pydantic import BaseModel, Field

from planner import llm
from planner.guides import Guide
from planner.pob_xml import Build, load_build
from planner.priorities import StatKey
from planner.tree import load_tree

KEY_KINDS = ("keystone", "notable", "ascendancy")


class Transition(BaseModel):
    node: str = Field(description="Keystone or key node name, exactly as listed")
    when: str = Field(description="When to take it, e.g. 'once energy shield clearly exceeds life'")
    requires: str = Field(description="What must be in place first, or empty string")
    effect: str = Field(description="What changes for the build when it is taken")


class Note(BaseModel):
    text: str
    node: str = Field(description="Node name that supports the note")


class BuildDirection(BaseModel):
    summary: str = Field(description="4-6 sentences: how the build works and where it is heading, in Spanish")
    ignore_stats: list[StatKey] = Field(description="Stats that stop mattering in the planned endgame (e.g. Life and "
                                                    "ChaosResist under Chaos Inoculation); empty if none")
    prioritize_stats: list[StatKey] = Field(description="Stats the endgame build scales or depends on")
    transitions: list[Transition] = Field(description="Keystones / build-defining nodes still to take, in order")
    notes: list[Note] = Field(description="Other key facts: synergies, nodes the guide drops, conflicts")


def final_variant(guide: Guide, stage: str | None) -> str | None:
    variants = list(guide.build_plans) or list(guide.pob_xml)
    if not variants:
        return None
    named = [v for v in variants if "endgame" in v.lower()]
    if named:
        return min(named, key=len)

    def level(v):
        if v in guide.build_plans:
            return guide.build_plans[v].gear_level()
        b = load_build(io.StringIO(guide.pob_xml[v]))
        return b.level
    return max(variants, key=level)


def _variant_nodes(guide: Guide, variant: str | None, level: int) -> set[int]:
    if not variant:
        return set()
    if variant in guide.build_plans:
        return guide.build_plans[variant].passives_at(100 if "endgame" in variant.lower() else level)
    return set(load_build(io.StringIO(guide.pob_xml[variant])).nodes)


def key_nodes(build: Build, guide: Guide, stage: str | None, final: str | None) -> dict[str, list[dict]]:
    tree = load_tree(build.tree_version)
    now = set(build.nodes)
    stage_nodes = _variant_nodes(guide, stage, build.level)
    final_nodes = _variant_nodes(guide, final, build.level)

    def describe(ids):
        rows = [tree.nodes[i] for i in sorted(ids) if i in tree.nodes and tree.nodes[i].kind in KEY_KINDS]
        return [{"name": n.name, "kind": n.kind, "stats": list(n.stats)} for n in rows]
    return {
        "allocated_now": describe(now),
        "missing_for_current_stage": describe(stage_nodes - now),
        "only_in_endgame": describe(final_nodes - stage_nodes - now),
        "allocated_but_not_in_guide": describe(now - stage_nodes - final_nodes),
    }


SYSTEM = """You summarise the direction of a Path of Exile 2 build from its key passive nodes.
You get the key nodes (keystones, notables, ascendancy) of the character now, of the guide variant for the
current stage and of the guide's endgame variant. Explain how the build works and where it is heading.
Keystones that redefine the character matter most (e.g. ones that set life to 1 or change how damage is taken):
spell out which stats stop mattering and which become central, and when such a keystone should be taken.
Only use the nodes given; every transition and note must name the node it comes from."""


def build_direction(build: Build, guide: Guide, stage: str | None, model: str | None = None,
                    meter: llm.UsageMeter | None = None) -> tuple[BuildDirection, str | None]:
    final = final_variant(guide, stage)
    nodes = key_nodes(build, guide, stage, final)
    model = model or llm.EXTRACT_MODEL
    key = hashlib.sha1(json.dumps([sorted(build.nodes), stage, final, model], sort_keys=True).encode()).hexdigest()[:10]
    cache = guide.path / f"direction_{stage}_{final}_{key}.json"
    if cache.exists():
        return BuildDirection.model_validate_json(cache.read_text(encoding="utf-8")), final
    user = (f"Character: {build.ascendancy} ({build.class_name}) level {build.level}.\n"
            f"Current stage variant: {stage}. Endgame variant: {final}.\n"
            f"<key_nodes>\n{json.dumps(nodes, ensure_ascii=False)}\n</key_nodes>\n\nWrite the build direction brief.")
    meter = meter or llm.UsageMeter()
    brief = llm.structured(SYSTEM, user, BuildDirection, effort="medium", meter=meter, model=model)
    print(f"  build direction usage: {meter.summary()}")
    cache.write_text(json.dumps(brief.model_dump(), indent=1, ensure_ascii=False), encoding="utf-8")
    return brief, final

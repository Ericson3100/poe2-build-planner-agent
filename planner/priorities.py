import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

from planner.guides import Guide
from planner.llm import UsageMeter, structured

# stats de PoB que se pueden revisar sin LLM
StatKey = Literal[
    "FireResist", "ColdResist", "LightningResist", "ChaosResist", "Life", "EnergyShield", "Mana",
    "Spirit", "SpiritUnreserved", "TotalEHP", "CombinedDPS", "Str", "Dex", "Int", "Evasion",
    "Armour", "Ward", "BlockChance", "DeflectChance", "PhysicalMaximumHitTaken", "ChaosMaximumHitTaken",
    "MovementSpeedMod", "CritChance", "ManaRegen",
]
Slot = Literal[
    "Weapon 1", "Weapon 2", "Weapon 1 Swap", "Weapon 2 Swap", "Helmet", "Body Armour", "Gloves",
    "Boots", "Amulet", "Ring 1", "Ring 2", "Belt", "Flask 1", "Flask 2", "Charm", "Jewel", "Any",
]


class Stage(BaseModel):
    name: str = Field(description="Stage or variant name as the guide calls it, e.g. 'League start', 'Endgame'")
    min_level: int = Field(description="Lowest character level the guide states for this stage; 1 if the guide states none")
    level_quote: str = Field(description="Verbatim guide text that states that level, or empty string if none")
    max_level: int | None = Field(description="Highest level for this stage, null if open-ended")
    pob_variant: str | None = Field(description="Matching PoB/text variant name from the input files, if any")
    budget_hint: str | None = Field(description="Budget the guide mentions for this stage, verbatim, or null")
    summary: str


class Threshold(BaseModel):
    stat: StatKey
    target: float = Field(description="Minimum value the guide asks for")
    priority: int = Field(description="1 = most important, 5 = nice to have")
    stage: str | None = Field(description="Stage name it applies to, null for all stages")
    quote: str = Field(description="Verbatim sentence from the guide that states this")


class SlotPriority(BaseModel):
    slot: Slot
    desired_stats: list[str] = Field(description="Item stats in the guide's order of importance")
    key_unique: str | None = Field(description="Unique item the guide recommends for the slot, or null")
    priority: int = Field(description="1 = upgrade this slot first, 5 = last")
    quote: str


class Advice(BaseModel):
    text: str
    priority: int
    quote: str


class GuidePriorities(BaseModel):
    build_name: str
    archetype: str = Field(description="Main skill(s) and scaling, e.g. 'Spark crit, Cast on Crit Comet'")
    stages: list[Stage]
    thresholds: list[Threshold]
    slot_priorities: list[SlotPriority]
    general_advice: list[Advice] = Field(description="Ordered progression advice: what to get or do first")
    warnings: list[Advice] = Field(description="Pitfalls the guide warns about")


SYSTEM = """You extract structured build priorities from Path of Exile 2 build guides.
Use only what the guide states. Every quote field must be copied verbatim from the guide text.
If the guide gives no number for something, leave it out instead of estimating.
Thresholds are final character-sheet values as Path of Building reports them (resistances after penalties,
capped at 75% by default). Skip statements about gear totals or raw sums needed to reach a cap
(e.g. "110% resistance on gear to cap") and anything that is not a target value for the character.
Stages: list each progression stage or variant the guide describes (league start, early maps, endgame, budget tiers).
Map each stage to an input variant name when the file names make it clear."""


def extract_priorities(guide: Guide, variant: str | None = None, refresh: bool = False) -> GuidePriorities:
    per_variant = variant is not None and variant in guide.texts and len(guide.texts) > 1
    # caché por variante, modelo y texto de la guía
    from planner import llm
    model = llm.short(llm.EXTRACT_MODEL)
    source = guide.texts.get(variant, "") if per_variant else guide.full_text()
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
    cache = guide.path / (f"priorities_{variant}_{model}_{digest}.json" if per_variant else f"priorities_{model}_{digest}.json")
    if cache.exists() and not refresh:
        return GuidePriorities.model_validate_json(cache.read_text(encoding="utf-8"))
    if not guide.texts:
        raise ValueError(f"Guide '{guide.id}' has no page*.html or page*.txt to extract priorities from")

    variants = [variant] if per_variant else sorted(set(guide.texts) | set(guide.pob_xml) | set(guide.build_plans))
    text = f"=== Variant: {variant} ===\n{guide.texts[variant]}" if per_variant else guide.full_text()
    user = (
        f"Input variant names: {', '.join(variants)}\n"
        f"Guide URL: {guide.url or 'unknown'}\n\n"
        f"<guide>\n{text}\n</guide>\n\n"
        "Extract the build priorities."
    )
    meter = UsageMeter()
    result = structured(SYSTEM, user, GuidePriorities, effort="medium", meter=meter)
    print(f"  priorities extraction usage: {meter.summary()}")
    cache.write_text(json.dumps(result.model_dump(), indent=1, ensure_ascii=False), encoding="utf-8")
    return result

import json
import re
import threading
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from planner.agent_tools import KEY_STATS, Session, make_tools
from planner.analysis import LevelCheck, choose_guide, choose_variant, compute_gaps, level_gate
from planner.guides import import_guide, list_guides, load_guide
from planner import baseline, direction as direction_mod, llm, net
from planner.analysis import requirement_violations
from planner.llm import UsageMeter, _env
from planner.pob_engine import Engine, damage_skills, skills_dps
from planner.pob_xml import load_build, resolve_build
from planner.prices import PriceBook
from planner.priorities import extract_priorities
from planner.wallet import load_wallet

ROOT = Path(__file__).resolve().parent.parent
MAX_RETRIES = 2
PLANNER_MODE = _env("PLANNER_MODE", "react")
# react: agente con tools; baseline: reglas fijas y greedy, sin LLM
MODES = ("react", "baseline")
MAX_DPS_LOSS = 0.10  # máximo 10% menos de daño
MAX_EHP_LOSS = 0.15  # y máximo 15% menos de EHP
_engine: Engine | None = None
_sessions: dict[str, Session] = {}


def _merge_pending(current: list | None, update: list | None) -> list:
    if update is None:
        return []
    return (current or []) + update


class PlanState(TypedDict, total=False):
    build_path: str  # vacío = la build más reciente
    wallet_path: str
    guide_id: str  # vacío = la guía más parecida
    guide_scores: dict
    objective: str
    resist_floor: float
    level_check: dict
    gaps: dict
    feedback: str
    attempts: int
    plan: list[dict]
    agent_summary: str
    validation: dict
    report_path: str
    cost_usd: float
    mode: str
    started_at: float
    counters_start: dict
    usage: dict
    metrics: dict
    # subgrafo ReAct
    messages: list[dict]
    pending: Annotated[list, _merge_pending]
    iterations: int
    planner_cost_start: float


def _engine_instance() -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine


def _thread(config) -> str:
    return config["configurable"]["thread_id"]


def _mode(state) -> str:
    return state.get("mode") or PLANNER_MODE


def _model_of(mode: str) -> str:
    return llm.PLANNER_MODEL if mode == "react" else "-"


def _save_custom_guide(answer: str) -> str:
    return import_guide(answer)


def _looks_like_pob(answer: str) -> bool:
    return "pobb.in" in answer or bool(re.fullmatch(r"[A-Za-z0-9_\-+/=]{200,}", answer.strip()))


def prepare(state: PlanState, config) -> dict:
    started_at = time.time()
    baseline = _mode(state) == "baseline"
    build_path = resolve_build(state.get("build_path"))
    build = load_build(build_path)
    guide_id = state.get("guide_id")
    guide_scores = {}
    if not guide_id:
        guide_id, guide_scores = choose_guide(build)
        print(f"Guía elegida automáticamente: {guide_id} (parecido por guía: {guide_scores})")

    while True:
        if not guide_id or guide_id not in list_guides():
            answer = interrupt({
                "message": f"No hay guía '{guide_id}'. Elige una del repertorio o pega un código PoB / link pobb.in.",
                "options": list_guides(),
            })
            guide_id = _save_custom_guide(answer) if _looks_like_pob(answer) else answer.strip()
            continue
        guide = load_guide(guide_id)
        # la variante más parecida al personaje
        check = choose_variant(build, guide)
        if check.ok:
            priorities = extract_priorities(guide, variant=check.variant) if guide.texts and not baseline else None
            if len(guide.texts) <= 1 and priorities and priorities.stages:
                # guía solo con texto: los niveles citados ajustan la compuerta
                staged = level_gate(build, guide, priorities)
                check = LevelCheck(staged.ok, staged.character_level, staged.required_level,
                                   staged.stage, staged.variant or check.variant, staged.reason, check.scores)
            if check.ok:
                break
        answer = interrupt({
            "message": (f"Tu personaje es nivel {build.level} y la guía '{guide_id}' requiere nivel "
                        f"{check.required_level}. {check.reason} Elige otra guía del repertorio "
                        "o pega un código PoB / link pobb.in de una build para tu nivel."),
            "options": [g for g in list_guides() if g != guide_id],
        })
        guide_id = _save_custom_guide(answer) if _looks_like_pob(answer) else answer.strip()

    engine = _engine_instance()
    xml = build_path.read_text(encoding="utf-8")
    stats = dict(engine.evaluate_xml(xml))
    damage = damage_skills(engine, xml)
    base_skills = skills_dps(engine, xml, damage)
    if base_skills:
        stats["BuildDPS"] = sum(v["dps"] for v in base_skills.values())
    wallet = load_wallet(state["wallet_path"])
    prices = PriceBook(build.league)
    budget = prices.to_exalted(prices.wallet_value(wallet.currency))
    floor = state.get("resist_floor")
    gaps = compute_gaps(build, stats, guide, priorities, check, floor)
    brief, final = (direction_mod.build_direction(build, guide, check.variant)
                    if (guide.build_plans or guide.pob_xml) and not baseline
                    else (None, None))

    _sessions[_thread(config)] = Session(
        engine=engine, build=build, base_xml=xml, base_stats=stats, guide=guide, check=check,
        priorities=priorities, gaps=gaps, wallet=wallet, prices=prices, budget_exalted=budget,
        damage=damage, base_skills=base_skills, resist_floor=floor,
        direction=brief.model_dump() if brief else None, final_variant=final,
    )
    return {"started_at": started_at, "counters_start": {"trade": net.CALLS.get("trade-search", 0), "engine": engine.evals},
            "build_path": str(build_path), "guide_id": guide_id, "guide_scores": guide_scores,
            "level_check": asdict(check),
            "gaps": gaps.as_dict(), "attempts": 0}


SYSTEM = """You are a Path of Exile 2 build planner. You compare a player's character with a guide build and
recommend the best sequence of purchases and passive changes the player can afford now.

Rules:
- Every number you state must come from a tool result. Never estimate stat changes or prices yourself.
- Measure before recommending: evaluate each candidate item or passive change with the engine tools.
- Respect the guide's priorities and stage. Fix hard problems first (unmet attribute requirements,
  over-reserved spirit, elemental resistances below the player's floor - "resist_floor" in the context,
  the 75% cap by default), then the guide's thresholds, then the slot priorities. Resistances between the
  floor and the cap are a trade the player accepts when the objective asks for it.
- Stay within the currency budget (exalted orbs) and the gold available. Prefer the largest gain per exalted.
- Never recommend a step that creates a new violation (new_violations in tool results).
- Damage metric: BuildDPS = sum of every damage skill of the build (found automatically, including skills
  triggered by meta gems such as Cast on Critical -> Comet). CombinedDPS only covers the skill PoB has
  selected and can hide losses. Tool results list each skill's DPS and crit chance: for trigger-on-crit
  builds, a drop in crit chance of the triggering skill is a real risk even if DPS rises; mention it.
- The whole plan may lose at most 10% BuildDPS and at most 15% TotalEHP. Items the damage depends on (conversion uniques,
  +skill levels, crit enablers) stay unless a measured replacement keeps the damage.
- Work in rounds: evaluate at most 3-4 candidates per slot, record the best with add_plan_step, move on.
  Do not re-evaluate listings you already measured.
- Trade searches are rate limited: plan them, combine stats into one search per slot.
- Record each recommendation with add_plan_step, then call evaluate_plan and fix any problem it reports.

Balance between defences and damage:
- A character dies to its weakest defensive layer, not its average. Check every layer: life and energy shield
  pool, elemental resistances (capped), chaos resistance, and the build's mitigation (armour, evasion,
  deflection, block). The per-type maximum hit taken numbers show the weakest damage type.
- Damage matters too: killing faster removes threats and shortens fights. Do not hollow out the main skill's
  damage to stack defence the build cannot use, and do not chase damage while a defensive hole is open.
- Which defences and offensive stats matter depends on the class, ascendancy and build: an energy-shield spell
  caster has little use for armour, accuracy or attack speed; chaos resistance weighs more for energy-shield
  builds; Chaos Inoculation makes chaos resistance irrelevant; minion stats only matter for minion builds.
  Before investing in a stat, decide whether it is relevant for THIS build and say so.
- Use search_mechanics to check how a stat, defence or keystone works in Path of Exile 2 and how it interacts
  with the class and build. Your training knowledge may be outdated or mixed with Path of Exile 1; the knowledge
  base reflects the current game. Mention the source title when a mechanic shapes a decision.
- Judge each candidate by its measured effect on the layer that is weakest for this build, then by
  CombinedDPS, both per exalted orb spent.

Beyond purchases, the plan must also cover (record each with add_plan_step):
- Passive tree: call suggest_passives. Record the best next points to spend while levelling as "level-up"
  steps in order (paths are added automatically). Recommend a "respec" only when a measured refund +
  allocation is clearly better and worth its gold.
- Ascendancy: record the next ascendancy nodes for the next trial as "ascendancy" steps, with their measured
  effect (suggest_passives lists them).
- Jewels: call jewel_sockets to see what each tree jewel gives. If a jewel is weak and a better one is cheap,
  search trade with slot "Jewel <node id>" (category jewel), evaluate it and record a "buy" step; otherwise
  say the current jewels are fine.
- Quality and runes: call item_upgrades for the character's armour and weapons and record worthwhile
  ones as "craft" steps (quality orbs, Artificer's Orbs, runes). When judging a trade listing, use
  potential_if_finished: an unfinished item (low quality, empty or addable rune sockets) can beat a finished
  one once completed; include the finishing cost in the step and say so.
- Spirit: when spirit is over-reserved (or a purchase is mainly for spirit), read the "spirit" context /
  spirit_report first. Always evaluate BOTH options with tools and put both, with numbers, in the summary:
  (a) buy spirit: the item's price plus its measured side effects (BuildDPS, EHP, and crit chance of every
      damage skill), and
  (b) switch off the least valuable reserving skill(s) with evaluate_skill_toggle and a "gem" step
      (disable_groups).
  How to weigh them:
  - Crit chance is a real cost for crit-triggered builds (Cast on Critical): PoB keeps the trigger rate flat,
    so a crit drop that PoB scores as harmless still slows energy gain in play. Count it against option (a).
  - Classify each reserving skill by role from its description and from how the build's damage works: skills
    that deal or trigger the main damage, and resources that damage depends on, are core; situational, manual
    or quality-of-life effects are dispensable. Decide which skill is the most dispensable yourself and say why.
  - Prefer (b) when a dispensable skill frees enough spirit: it is free and keeps the crit. Choose (a) only if
    every reserving skill is core, or the spirit item is a clear upgrade on its own without the crit loss.
  - Guide usage alone is not a reason to keep a dispensable skill; the player's objective/preferences win.
- build_direction (context) says where the build is heading. Stats in its ignore_stats stop mattering at the
  planned endgame: do not spend currency on them unless a hard problem needs them now, and say when a
  current item's value will vanish after a transition. Favour its prioritize_stats, and use its transitions to
  time keystones (recommend one only when its "when" condition holds, measured with evaluate_passives).
- Keystones (e.g. Chaos Inoculation) change the build: only suggest one with a clear measured case and explain
  the trade-off.
- Finish with a short summary in Spanish: what the plan achieves overall and what to do after it
  (next upgrades once more currency is available). The step list is rendered separately; do not repeat it."""


def _context(s: Session, state: PlanState) -> str:
    stats = {k: round(v, 1) for k, v in s.base_stats.items() if k in KEY_STATS and isinstance(v, (int, float))}
    priorities = s.priorities.model_dump() if s.priorities else None
    if priorities and s.check.stage:
        priorities["thresholds"] = [t for t in priorities["thresholds"] if t["stage"] in (None, s.check.stage)]
    equipped = {slot: f"{it.name} — {it.base}: " + "; ".join(m.text for m in it.implicits + it.explicits)
                for slot, it in s.build.equipped().items()}
    return json.dumps({
        "objective": state.get("objective") or "balanced: defence first, then damage",
        "resist_floor": s.resist_floor or "75 (cap)",
        "build_direction": ({"endgame_variant": s.final_variant, **s.direction} if s.direction else None),
        "player_preferences_note": "Treat the objective text as the player's preferences (e.g. skills they can live "
                                   "without, stats to protect); follow it over the guide.",
        "character": {"class": s.build.ascendancy, "level": s.build.level, "league": s.build.league,
                      "stats": stats, "equipped": equipped},
        "budget": {"exalted": round(s.budget_exalted, 1), "gold": s.wallet.gold,
                   "respec_gold_per_point": s.wallet.costs.get("respec_gold_per_point") or "unknown"},
        "guide": {"id": s.guide.id, "url": s.guide.url, "stage": s.check.stage, "variant": s.check.variant,
                  "priorities": priorities},
        "gaps": s.gaps.as_dict(),
        "spirit": ({"spirit_total": s.base_stats.get("Spirit"), "unreserved": s.base_stats.get("SpiritUnreserved"),
                    "reservers": s.spirit_report()} if s.base_stats.get("SpiritUnreserved", 0) < 0 else None),
    }, ensure_ascii=False)


# subgrafo del planner: el loop ReAct como nodos de LangGraph

MAX_ITERATIONS = 60
WRAP_UP_AT = MAX_ITERATIONS - 8
# un nodo por tool
TOOL_NAMES = ["find_trade_stats", "search_trade", "evaluate_listing", "price_lookup", "evaluate_guide_item",
              "evaluate_passives", "suggest_passives", "jewel_sockets", "item_upgrades", "spirit_report",
              "evaluate_skill_toggle", "search_mechanics", "add_plan_step", "remove_plan_step", "evaluate_plan"]


# los nodos de tools pueden correr en paralelo, pero el motor de PoB (LuaJIT) solo aguanta un hilo
_tool_lock = threading.Lock()


def _node_of(name: str) -> str:
    # si el modelo pide una tool que no existe, igual hay que responderle
    return name if name in TOOL_NAMES else "evaluate_plan"


def _tools(s: Session) -> dict:
    if not hasattr(s, "_tool_list"):
        s._tool_list = make_tools(s)
    return {t.name: t for t in s._tool_list}


def _finished(state: PlanState, s: Session, summary: str, meter: UsageMeter) -> dict:
    return {"plan": [asdict(step) for step in s.plan], "agent_summary": summary, "usage": meter.add_to(state.get("usage")),
            "attempts": state.get("attempts", 0) + 1, "cost_usd": state.get("cost_usd", 0.0) + meter.usd}


def planner_start(state: PlanState, config) -> dict:
    s = _sessions[_thread(config)]
    user = f"<planning_context>\n{_context(s, state)}\n</planning_context>\n\nBuild the plan."
    if state.get("feedback"):
        steps = [{"step": i, "kind": p.kind, "description": p.description, "slot": p.slot,
                  "cost_exalted": p.cost_exalted} for i, p in enumerate(s.plan, 1)]
        user += (f"\n\nThe current plan failed validation. Current steps:\n{json.dumps(steps, ensure_ascii=False)}\n"
                 f"Problems to fix (use remove_plan_step / add_plan_step, then evaluate_plan):\n{state['feedback']}")
    return {"messages": [{"role": "user", "content": user}], "pending": None, "iterations": 0,
            "planner_cost_start": state.get("cost_usd", 0.0)}


def route_mode(state: PlanState) -> str:
    return "baseline" if _mode(state) == "baseline" else "agent"


def baseline_node(state: PlanState, config) -> dict:
    s = _sessions[_thread(config)]
    return _finished(state, s, baseline.run(s), UsageMeter())


def agent(state: PlanState, config) -> dict:
    s = _sessions[_thread(config)]
    messages = list(state["messages"])
    if state.get("pending"):
        results = sorted(state["pending"], key=lambda r: r["order"])
        content = [{"type": "tool_result", "tool_use_id": r["tool_use_id"], "content": r["content"],
                    **({"is_error": True} if r["is_error"] else {})} for r in results]
        if state.get("iterations", 0) == WRAP_UP_AT:
            content.append({"type": "text", "text": "Iteration budget almost used up. Stop exploring: call "
                            "evaluate_plan, fix what it reports, then write the final summary."})
        messages.append({"role": "user", "content": content})

    response = llm.anthropic_client().beta.messages.create(
        **llm._claude_params("high"), max_tokens=16000, system=SYSTEM,
        tools=[t.to_dict() for t in _tools(s).values()], messages=messages, cache_control={"type": "ephemeral"},
    )
    llm._check_refusal(response)
    meter = UsageMeter()
    meter.add(response.usage, llm.PLANNER_MODEL)
    messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in response.content]})
    update = {"messages": messages, "pending": None, "iterations": state.get("iterations", 0) + 1,
              "cost_usd": state.get("cost_usd", 0.0) + meter.usd, "usage": meter.add_to(state.get("usage"))}
    if response.stop_reason != "tool_use" or update["iterations"] >= MAX_ITERATIONS:
        summary = "\n".join(b.text for b in response.content if b.type == "text")
        spent = update["cost_usd"] - state.get("planner_cost_start", 0.0)
        print(f"  planner: {update['iterations']} turns, {llm.PLANNER_MODEL}, cost ${spent:.2f}")
        update.update({"plan": [asdict(step) for step in s.plan], "agent_summary": summary,
                       "attempts": state.get("attempts", 0) + 1})
    return update


def route_agent(state: PlanState) -> list[str] | str:
    last = state["messages"][-1]
    calls = [b for b in last["content"] if b.get("type") == "tool_use"]
    if not calls or state.get("iterations", 0) >= MAX_ITERATIONS or "agent_summary" in state and not calls:
        return END
    return sorted({_node_of(c["name"]) for c in calls})


def _tool_node(tool: str):
    def run(state: PlanState, config) -> dict:
        s = _sessions[_thread(config)]
        tools = _tools(s)
        calls = [b for b in state["messages"][-1]["content"] if b.get("type") == "tool_use"]
        out = []
        for order, call in enumerate(calls):
            if _node_of(call["name"]) != tool:
                continue
            print(f"  - {call['name']}({json.dumps(call['input'], ensure_ascii=False)[:110]})")
            try:
                if call["name"] not in tools:
                    raise KeyError(f"unknown tool {call['name']}")
                with _tool_lock:
                    content, is_error = str(tools[call["name"]].call(call["input"])), False
            except Exception as err:
                content, is_error = f"Error: {type(err).__name__}: {err}", True
            out.append({"order": order, "tool_use_id": call["id"], "content": content, "is_error": is_error})
        return {"pending": out}
    run.__name__ = tool
    return run


def build_planner_graph():
    sg = StateGraph(PlanState)
    sg.add_node("start", planner_start)
    sg.add_node("baseline", baseline_node)
    sg.add_node("agent", agent)
    for tool in TOOL_NAMES:
        sg.add_node(tool, _tool_node(tool))
        sg.add_edge(tool, "agent")
    sg.add_edge(START, "start")
    sg.add_conditional_edges("start", route_mode, ["baseline", "agent"])
    sg.add_conditional_edges("agent", route_agent, [*TOOL_NAMES, END])
    sg.add_edge("baseline", END)
    return sg.compile()


def validate(state: PlanState, config) -> dict:
    s = _sessions[_thread(config)]
    result = s.delta(s.plan_xml())
    spent_ex, spent_gold = s.spent()
    problems = list(result["new_violations"])
    dps = result["changes"].get("BuildDPS") or result["changes"].get("CombinedDPS")
    if dps and dps["before"] > 0 and dps["change"] / dps["before"] < -MAX_DPS_LOSS:
        problems.append(f"El DPS de la build cae {-100 * dps['change'] / dps['before']:.0f}% "
                        f"(de {dps['before']:,.0f} a {dps['after']:,.0f}); máximo permitido {MAX_DPS_LOSS:.0%}. "
                        "Busca alternativas que conserven los ítems de los que depende el daño.")
    ehp = result["changes"].get("TotalEHP")
    if ehp and ehp["before"] > 0 and ehp["change"] / ehp["before"] < -MAX_EHP_LOSS:
        problems.append(f"El EHP cae {-100 * ehp['change'] / ehp['before']:.0f}% (de {ehp['before']:,.0f} a {ehp['after']:,.0f}); "
                        f"máximo permitido {MAX_EHP_LOSS:.0%}.")
    if spent_ex > s.budget_exalted + 1e-6:
        problems.append(f"El costo de {spent_ex:.1f} ex supera el presupuesto {s.budget_exalted:.1f} ex")
    if spent_gold > s.wallet.gold:
        problems.append(f"El costo de {spent_gold:.0f} de oro supera el oro disponible {s.wallet.gold}")
    if not s.plan:
        problems.append("El plan está vacío")
    result.update({"problems": problems, "spent_exalted": spent_ex, "spent_gold": spent_gold})
    return {"validation": result, "feedback": "\n".join(problems)}


def route_after_validate(state: PlanState) -> str:
    # solo el agente reintenta; la línea base repara su propio plan
    if _mode(state) == "react" and state["validation"]["problems"] and state.get("attempts", 0) < MAX_RETRIES:
        return "planner"
    return "report"


def run_metrics(state: PlanState, s: Session) -> dict:
    v = state["validation"]
    before = requirement_violations(s.base_stats, s.resist_floor)
    left = [p for p in before if p not in v["fixed_violations"]] + v["problems"]

    def pct(key):
        c = v["changes"].get(key)
        return round(100 * c["change"] / c["before"], 1) if c and c["before"] else 0.0
    usage = state.get("usage") or {}
    start = state.get("counters_start") or {}
    mode = _mode(state)
    return {
        "build": Path(state["build_path"]).stem, "level": s.build.level, "guide": s.guide.id, "variant": s.check.variant,
        "mode": mode, "model": _model_of(mode),
        "build_dps_pct": pct("BuildDPS") or pct("CombinedDPS"), "ehp_pct": pct("TotalEHP"), "es_pct": pct("EnergyShield"),
        "problems_before": before, "problems_fixed": v["fixed_violations"], "problems_left": left,
        "valid": not v["problems"], "solved": not v["problems"] and not left,
        "steps": len(state.get("plan", [])), "spent_exalted": round(v["spent_exalted"], 1), "spent_gold": round(v["spent_gold"]),
        "budget_exalted": round(s.budget_exalted, 1),
        "seconds": round(time.time() - state.get("started_at", time.time())),
        "llm_requests": usage.get("requests", 0),
        "input_tokens": usage.get("input", 0) + usage.get("cache_read", 0) + usage.get("cache_write", 0),
        "output_tokens": usage.get("output", 0), "cost_usd": round(state.get("cost_usd", 0.0), 2),
        "trade_searches": net.CALLS.get("trade-search", 0) - start.get("trade", 0),
        "engine_evals": s.engine.evals - start.get("engine", 0),
    }


KIND_NAMES = {"buy": "compra", "respec": "respec", "level-up": "subir de nivel", "ascendancy": "ascendencia",
              "craft": "calidad/runas", "gem": "skill"}


def _cell(text: str) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


def report(state: PlanState, config) -> dict:
    s = _sessions[_thread(config)]
    v = state["validation"]
    metrics = run_metrics(state, s)
    mode = metrics["mode"]
    m = metrics
    lines = [f"# Plan para {s.build.ascendancy} nivel {s.build.level}, guía `{s.guide.id}`", "",
             f"Modo: {mode} | Variante: `{s.check.variant or '-'}` | "
             f"Presupuesto: {s.budget_exalted:.0f} ex, {s.wallet.gold:,} oro", "",
             "## Pasos", "", "| # | Tipo | Acción | Costo | Motivo |", "|---|---|---|---|---|"]
    for i, step in enumerate(state.get("plan", []), 1):
        action = step["description"]
        if step.get("node_names"):
            action += ": " + " > ".join(step["node_names"])
        if step.get("link"):
            action += f" ([trade]({step['link']}))"
        why = step["rationale"] + (f' (guía: "{step["guide_quote"][:150]}")' if step["guide_quote"] else "")
        cost = f"{step['cost_exalted']:.0f} ex" if step["cost_exalted"] else "-"
        lines.append(f"| {i} | {KIND_NAMES.get(step['kind'], step['kind'])} | {_cell(action)} | {cost} | {_cell(why)} |")
    lines += ["", "## Efecto combinado (PoB)", "", "| Stat | Antes | Después | Cambio |", "|---|---|---|---|"]
    for k, c in v["changes"].items():
        lines.append(f"| {k} | {c['before']:,} | {c['after']:,} | {c['change']:+,} |")
    if s.damage:
        final = skills_dps(s.engine, s.plan_xml(), s.damage)
        lines += ["", "| Skill | DPS antes | DPS después | Crítico antes | Crítico después |", "|---|---|---|---|---|"]
        for name, after in final.items():
            before = s.base_skills.get(name, {})
            lines.append(f"| {name} | {before.get('dps', 0):,.0f} | {after['dps']:,.0f} | "
                         f"{before.get('crit', 0):.1f}% | {after['crit']:.1f}% |")
    if v["fixed_violations"]:
        lines += ["", "Problemas resueltos: " + "; ".join(v["fixed_violations"])]
    if m["problems_left"]:
        lines += ["", "**Problemas pendientes:** " + "; ".join(m["problems_left"])]
    lines += ["", f"Gasto: {m['spent_exalted']:,.1f} ex | Tiempo: {m['seconds']} s | Llamadas al LLM: {m['llm_requests']} | "
              f"Costo LLM: ${m['cost_usd']:.2f}"]
    if state.get("agent_summary"):
        lines += ["", "## Resumen", "", state["agent_summary"]]

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    model = "sin-llm" if mode == "baseline" else llm.short(_model_of(mode))
    stem = f"{date.today().isoformat()}_{Path(state['build_path']).stem}_{s.guide.id}_{model}-{mode}"
    path, n = out_dir / f"{stem}.md", 2
    while path.exists():  # no sobrescribir reportes anteriores
        path, n = out_dir / f"{stem}_{n}.md", n + 1
    path.write_text("\n".join(lines), encoding="utf-8")
    metrics["report"] = path.name
    path.with_suffix(".json").write_text(json.dumps({"metrics": metrics, "plan": state.get("plan", [])},
                                                    indent=1, ensure_ascii=False), encoding="utf-8")
    return {"report_path": str(path), "metrics": metrics}


def build_graph():
    g = StateGraph(PlanState)
    g.add_node("prepare", prepare)
    g.add_node("planner", build_planner_graph())
    g.add_node("validate", validate)
    g.add_node("report", report)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "planner")
    g.add_edge("planner", "validate")
    g.add_conditional_edges("validate", route_after_validate, ["planner", "report"])
    g.add_edge("report", END)
    return g.compile(checkpointer=InMemorySaver())

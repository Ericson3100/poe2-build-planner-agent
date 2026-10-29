import importlib
import json
import re
import sys
import time
import types
import typing as t
import warnings
from dataclasses import dataclass, field
from pathlib import Path

warnings.filterwarnings("ignore")
for _name, _cls in (("langchain_community.chat_models.vertexai", "ChatVertexAI"),):
    try:
        importlib.import_module(_name)
    except ModuleNotFoundError:
        _stub = types.ModuleType(_name)
        setattr(_stub, _cls, type(_cls, (), {}))
        sys.modules[_name] = _stub

import numpy as np
from langchain_anthropic import ChatAnthropic
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.prompt_values import StringPromptValue
from ragas import EvaluationDataset, RunConfig, evaluate
from ragas.dataset_schema import SingleTurnSample
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import AnswerCorrectness, Faithfulness
from ragas.metrics.base import MetricType, MetricWithLLM, SingleTurnMetric

from planner import llm
from planner.kb import EMBED_MODEL, load_kb
from tests.kb_eval import MODES, QUESTIONS

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "reports" / "rag_ragas.json"
JUDGE_MODEL = "claude-sonnet-5-5"
GENERATOR_MODEL = "claude-haiku-5-5"
TOP_K = 5

# respuestas de referencia, escritas a partir del texto de la wiki
REFERENCES = {
    "Chaos damage removes twice as much energy shield; Withered increases chaos damage taken":
        "Chaos damage removes twice as much energy shield as other damage. Withered is a stacking debuff that "
        "increases the chaos damage taken.",
    "Chaos Inoculation immunity to bleeding but not Corrupted Blood, stun and ailment thresholds from 1 maximum life":
        "Chaos Inoculation grants immunity to chaos damage and to bleeding, but not to Corrupted Blood or other "
        "physical damage over time. Stun and ailment thresholds are based on 1 maximum life.",
    "Primed for Stun at 40% Heavy Stun buildup for normal enemies, 60% rare, 70% unique":
        "Normal enemies are Primed for Stun at 40% Heavy Stun buildup, magic enemies at 50%, rare at 60% and "
        "unique at 70%.",
    "Exposure effect by enemy rarity: 30% less on Rare enemies, 50% less on Unique enemies":
        "Exposure has no change on normal enemies, 15% less effect on magic enemies, 30% less on rare enemies and "
        "50% less on unique enemies.",
    "How long must I avoid getting hit before my magic barrier starts refilling?":
        "Energy shield starts recharging after 4 seconds without taking damage by default; faster start of Energy "
        "Shield Recharge modifiers shorten that delay.",
    "Which damage skips my protective barrier and goes straight to my health pool?":
        "Bleeding and poison damage bypass energy shield and damage life directly.",
    "How much physical mitigation rating do I need to halve a 100 damage hit?":
        "To prevent half of the damage you need armour equal to 10 times the damage: 1000 armour for a 100 damage hit.",
    "Can raising a shield protect me from damage that ticks over time?":
        "No. Block only works against hits; it does nothing against damage over time or life loss.",
    "¿Cuántos segundos sin recibir daño tarda en empezar a recargarse el escudo de energía?":
        "Por defecto, el escudo de energía empieza a recargarse tras 4 segundos sin recibir daño; los modificadores "
        "de inicio más rápido de la recarga acortan ese tiempo.",
    "¿Qué hace la penetración de resistencias?":
        "La penetración resta su valor de la resistencia del objetivo al calcular el daño de un golpe, hasta un "
        "mínimo de 0% de resistencia por defecto, después de los demás modificadores de resistencia.",
    "¿Cuánto daño hace la ignición y durante cuánto tiempo?":
        "La ignición inflige el 80% del daño de fuego del golpe (antes de mitigación) como daño de fuego en el "
        "tiempo durante 4 segundos.",
    "¿Cuánta armadura necesito para reducir a la mitad un golpe?":
        "Para reducir el daño a la mitad necesitas armadura igual a 10 veces el daño del golpe, por ejemplo 1000 de "
        "armadura para un golpe de 100.",
}
SUBSET = [(kind, q) for kind, q, _ in QUESTIONS if q in REFERENCES]

GENERATOR_SYSTEM = """Answer the question about Path of Exile 2 using only the passages provided.
Answer in the language of the question, in 1-3 sentences. If the passages do not contain the answer, say so
instead of guessing."""


@dataclass
class ReferenceCoverage(MetricWithLLM, SingleTurnMetric):
    """Proporción (de 0 a 1) del hecho principal de la referencia que aparece en la respuesta.

    A diferencia de answer_correctness, no penaliza la información extra: evita su sesgo contra
    las respuestas correctas pero largas.
    """
    name: str = "reference_coverage"
    _required_columns: t.Dict[MetricType, t.Set[str]] = field(
        default_factory=lambda: {
            MetricType.SINGLE_TURN: {"response", "reference"}
        }
    )

    async def _single_turn_ascore(self, sample: SingleTurnSample, callbacks) -> float:
        prompt_text = f"""You are checking whether a response contains the key fact from a reference.

Reference (the key fact the response must contain):
{sample.reference}

Response (the actual answer being evaluated):
{sample.response}

Does the response contain the main fact stated in the reference?
Ignore any extra information in the response — only check whether the reference's main fact is present.
Answer with the proportion that is present. A number between: 0 to 1.
Reply with only the number."""

        prompt_value = StringPromptValue(text=prompt_text)
        result = await self.llm.generate(prompt_value, n=1, temperature=0, callbacks=callbacks)
        # se toma el último número y se limita a [0, 1]
        numeros = re.findall(r"\d+(?:[.,]\d+)?", result.generations[0][0].text)
        if not numeros:
            return np.nan
        return min(1.0, max(0.0, float(numeros[-1].replace(",", "."))))


JUDGE_CONTEXT = (" The texts are about Path of Exile 2 game mechanics and may be in English or Spanish; keep numbers "
                 "and game terms exactly as written and write statements in the language of the answer.")


def ajustar_prompts(answer_correctness: AnswerCorrectness, faithfulness: Faithfulness) -> None:
    for prompt in (answer_correctness.statement_generator_prompt, faithfulness.statement_generator_prompt):
        prompt.instruction = type(prompt).instruction + JUDGE_CONTEXT


def generate_answers(meter: llm.UsageMeter) -> list[dict]:
    kb = load_kb()
    rows = []
    for mode in MODES:
        for kind, question in SUBSET:
            hits = kb.search(question, k=TOP_K, mode=mode)
            contexts = [f"[{h['title']} — {h['section']}] {h['text']}" for h in hits]
            response = llm.anthropic_client().messages.create(
                model=GENERATOR_MODEL, max_tokens=400, system=GENERATOR_SYSTEM,
                messages=[{"role": "user", "content": "Passages:\n\n" + "\n\n".join(contexts)
                           + f"\n\nQuestion: {question}"}])
            meter.add(response.usage, GENERATOR_MODEL)
            answer = "".join(b.text for b in response.content if b.type == "text").strip()
            rows.append({"mode": mode, "kind": kind, "user_input": question, "response": answer,
                         "retrieved_contexts": contexts, "reference": REFERENCES[question]})
    return rows


def judge(rows: list[dict]) -> tuple[list[dict], dict]:
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", llm._env("ANTHROPIC_API_KEY"))
    # Sonnet 5.5 solo acepta la temperatura por defecto
    evaluator_llm = LangchainLLMWrapper(ChatAnthropic(model=JUDGE_MODEL, max_tokens=4096), bypass_temperature=True)
    evaluator_embeddings = LangchainEmbeddingsWrapper(HuggingFaceEmbeddings(model_name=EMBED_MODEL))
    metrica_ac = AnswerCorrectness(llm=evaluator_llm, embeddings=evaluator_embeddings)
    metrica_faith = Faithfulness(llm=evaluator_llm)
    ajustar_prompts(metrica_ac, metrica_faith)
    reference_coverage = ReferenceCoverage(llm=evaluator_llm)
    metricas = [metrica_ac, metrica_faith, reference_coverage]

    usage = UsageMetadataCallbackHandler()
    dataset = EvaluationDataset.from_list([{k: r[k] for k in ("user_input", "response", "retrieved_contexts", "reference")}
                                           for r in rows])
    result = evaluate(dataset, metrics=metricas, run_config=RunConfig(timeout=180, max_workers=4),
                      callbacks=[usage], show_progress=False, raise_exceptions=False)
    scores = result.to_pandas()
    for r, (_, s) in zip(rows, scores.iterrows()):
        r.update({m.name: (None if np.isnan(s[m.name]) else round(float(s[m.name]), 3)) for m in metricas})
    tokens = usage.usage_metadata.get(JUDGE_MODEL, {})
    price = llm.PRICES[JUDGE_MODEL]
    cost = (tokens.get("input_tokens", 0) * price["input"] + tokens.get("output_tokens", 0) * price["output"]) / 1e6
    return rows, {"input_tokens": tokens.get("input_tokens", 0), "output_tokens": tokens.get("output_tokens", 0),
                  "usd": round(cost, 3)}


METRICS = ("answer_correctness", "faithfulness", "reference_coverage")


def show(data: dict) -> None:
    rows = data["rows"]
    print(f"{len(SUBSET)} preguntas x {len(MODES)} modos = {len(rows)} respuestas, generador {GENERATOR_MODEL}, "
          f"juez {JUDGE_MODEL}\n")
    head = f"{'tipo':<11}{'modo':<8}" + "".join(f"{m:>20}" for m in METRICS)
    print(head)

    def mean(sel, m):
        vals = [r[m] for r in sel if r.get(m) is not None]
        return sum(vals) / len(vals) if vals else float("nan")
    for kind in ("literal", "paraphrase", "spanish"):
        for mode in MODES:
            sel = [r for r in rows if r["kind"] == kind and r["mode"] == mode]
            print(f"{kind:<11}{mode:<8}" + "".join(f"{mean(sel, m):>20.2f}" for m in METRICS))
    print()
    for mode in MODES:
        sel = [r for r in rows if r["mode"] == mode]
        print(f"{'promedio':<11}{mode:<8}" + "".join(f"{mean(sel, m):>20.2f}" for m in METRICS))
    g, j = data["generator_cost"], data["judge_cost"]
    print(f"\nCosto: generador ${g['usd']:.3f} ({g['requests']} llamadas), juez ${j['usd']:.3f} "
          f"({j['input_tokens']:,} tokens entrada / {j['output_tokens']:,} salida), {data['seconds']} s")


def main() -> None:
    if RESULTS.exists() and "--refresh" not in sys.argv:
        show(json.loads(RESULTS.read_text(encoding="utf-8")))
        return
    start = time.time()
    meter = llm.UsageMeter()
    rows = generate_answers(meter)
    rows, judge_cost = judge(rows)
    data = {"rows": rows, "seconds": round(time.time() - start),
            "generator_cost": {"usd": round(meter.usd, 4), "requests": meter.requests}, "judge_cost": judge_cost}
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    show(data)


if __name__ == "__main__":
    main()

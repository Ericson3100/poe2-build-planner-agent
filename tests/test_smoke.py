"""Offline smoke tests: no Path of Building checkout (vendor/), no API key, no network.

Run with `python -m pytest -q`. The agent itself, the PoB engine and the RAGAS evaluation are exercised
from the notebook instead, because they need the ~750 MB PoB checkout and an Anthropic API key.
"""
import base64
import importlib
import json
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

MODULES = ["agent_tools", "analysis", "baseline", "build_file", "direction", "graph", "guides", "itemdata",
           "kb", "llm", "net", "pob_code", "pob_engine", "pob_xml", "prices", "priorities", "runes",
           "skilldata", "summary", "trade", "tree", "upgrades", "wallet"]


@pytest.mark.parametrize("name", MODULES)
def test_modules_import(name):
    importlib.import_module(f"planner.{name}")


def test_pob_code_roundtrip():
    from planner.pob_code import decode_code
    xml = "<PathOfBuilding2><Build level=\"81\"/></PathOfBuilding2>"
    code = base64.urlsafe_b64encode(zlib.compress(xml.encode("utf-8"))).decode().rstrip("=")
    assert decode_code(code) == xml


def test_load_character_build():
    from planner.pob_xml import load_build
    build = load_build(ROOT / "builds" / "poe2.2.xml")
    assert build.class_name == "Sorceress"
    assert build.ascendancy == "Stormweaver"
    assert build.level == 81
    assert len(build.nodes) > 50
    assert build.equipped()


def test_passive_tree():
    from planner.tree import load_tree
    tree = load_tree("0_5")
    assert len(tree.nodes) > 4000
    assert any(n.kind == "keystone" and n.name == "Chaos Inoculation" for n in tree.nodes.values())


def test_guide_build_variants():
    from planner.guides import load_guide
    guide = load_guide("coc-comet-animeprincess")
    assert set(guide.build_plans) == {"basic", "budget", "double-coc", "endgame", "mageblood"}
    assert all(plan.skills for plan in guide.build_plans.values())
    assert "Skill gems" in guide.texts["basic"]


def test_wallet_example():
    from planner.wallet import load_wallet
    wallet = load_wallet(ROOT / "wallet.example.toml")
    assert wallet.league
    assert wallet.currency == {}  # all zeros in the template


def test_kb_index_and_bm25():
    from planner.kb import BM25, KB_DIR, _tokens
    chunks = [json.loads(ln) for ln in (KB_DIR / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(list((KB_DIR / "raw").glob("*.json"))) == 93  # fetched poe2wiki pages
    assert len(chunks) == 334
    import numpy as np
    emb = np.load(KB_DIR / "embeddings.npy")
    assert emb.shape[0] == len(chunks)
    bm25 = BM25([_tokens(f"{c['title']} {c['section']} {c['text']}") for c in chunks])
    top = int(np.argmax(bm25.scores(_tokens("Chaos Inoculation immunity to chaos damage"))))
    assert chunks[top]["title"] == "Chaos Inoculation"

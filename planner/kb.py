import json
import math
import os
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

KB_DIR = Path(__file__).resolve().parent.parent / "data" / "kb"
RAW_DIR = KB_DIR / "raw"
CURATED_DIR = KB_DIR / "curated"
WIKI_API = "https://www.poe2wiki.net/w/api.php"
WIKI_PAGE = "https://www.poe2wiki.net/wiki/"
USER_AGENT = f"poe2planner/0.1 (student project; contact: {os.environ.get('POE2PLANNER_CONTACT', 'https://github.com/Ericson3100/poe2-build-planner-agent')})"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
CHUNK_WORDS = 220

WIKI_PAGES = [
    # defensas
    "Defences", "Armour", "Evasion", "Energy Shield", "Life", "Mana", "Resistance", "Chaos",
    "Elemental damage", "Physical", "Block", "Deflection", "Receiving damage", "Stun", "Ailment",
    "Freeze", "Ignite", "Shock", "Poison", "Bleeding", "Regeneration", "Recoup", "Runic Ward",
    "Armour Break", "Flask", "Charm", "Endurance charge", "Frenzy charge", "Power charge", "Movement speed",
    # recursos y atributos
    "Spirit", "Attribute", "Strength", "Dexterity", "Intelligence",
    # keystones
    "Keystone", "Mind Over Matter", "Eldritch Battery", "Chaos Inoculation", "Elemental Equilibrium",
    "Archmage", "Blood Magic", "Resolute Technique", "Iron Reflexes", "Ghost Dance", "Acrobatics",
    # ofensiva
    "Damage", "Hit", "Critical hit", "Spell", "Attack", "Skill speed", "Accuracy", "Damage conversion",
    "Penetration", "Exposure", "Curse", "Minion", "Damage over time", "Gem", "Meta", "Area of effect",
    "Projectile", "Infusion", "Remnant",
    # clases
    "Ascendancy class", "Sorceress", "Witch", "Warrior", "Ranger", "Monk", "Mercenary", "Huntress", "Druid",
    "Stormweaver", "Chronomancer", "Infernalist", "Blood Mage", "Titan", "Warbringer", "Smith of Kitava",
    "Deadeye", "Pathfinder", "Invoker", "Acolyte of Chayula", "Witchhunter", "Gemling Legionnaire",
    "Tactician", "Amazon", "Ritualist", "Oracle", "Shaman", "Lich",
]


def _embedder():
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(EMBED_MODEL)


@dataclass
class Chunk:
    id: int
    source: str
    title: str
    section: str
    url: str
    text: str


def _fetch_extract(title: str) -> dict | None:
    params = {"action": "query", "prop": "extracts|revisions", "explaintext": 1, "rvprop": "ids|timestamp",
              "titles": title, "redirects": 1, "format": "json", "formatversion": 2}
    req = Request(f"{WIKI_API}?{urlencode(params)}", headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=30) as resp:
                page = json.load(resp)["query"]["pages"][0]
            break
        except HTTPError as err:
            if err.code != 429 or attempt == 2:
                raise
            time.sleep(int(err.headers.get("Retry-After", "60")))
    if page.get("missing") or not page.get("extract"):
        return None
    rev = (page.get("revisions") or [{}])[0]
    return {"title": page["title"], "url": WIKI_PAGE + quote(page["title"].replace(" ", "_")),
            "text": page["extract"], "revid": rev.get("revid"), "timestamp": rev.get("timestamp")}


def fetch_wiki(refresh: bool = False) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for title in WIKI_PAGES:
        out = RAW_DIR / f"{re.sub(r'[^A-Za-z0-9]+', '_', title)}.json"
        if out.exists() and not refresh:
            continue
        page = _fetch_extract(title)
        if page:
            out.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
            print(f"  fetched {page['title']} ({len(page['text'])} chars)")
        else:
            print(f"  missing {title}")
        time.sleep(3.0)


def _split_sections(text: str, heading_re: str) -> list[tuple[str, str]]:
    parts = re.split(heading_re, text, flags=re.M)
    sections = [("Overview", parts[0])]
    for i in range(1, len(parts) - 1, 2):
        sections.append((parts[i].strip(), parts[i + 1]))
    return [(t, b.strip()) for t, b in sections if b.strip()]


def _windows(body: str) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", body) if p.strip()]
    out, current = [], []
    for p in paras:
        if current and sum(len(x.split()) for x in current) + len(p.split()) > CHUNK_WORDS:
            out.append("\n".join(current))
            current = []
        current.append(p)
    if current:
        out.append("\n".join(current))
    return out


def _clean(text: str) -> str:
    text = re.sub(r"\\color \[rgb\]\{[^}]*\}", "", text)
    lines = text.splitlines()
    kept = [ln for ln in lines if len(ln.strip()) > 2 or not ln.strip()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept))


def build_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    for f in sorted(RAW_DIR.glob("*.json")):
        page = json.loads(f.read_text(encoding="utf-8"))
        for section, body in _split_sections(_clean(page["text"]), r"^==+\s*(.+?)\s*==+\s*$"):
            if section.lower() in ("references", "see also", "version history", "gallery", "external links"):
                continue
            for window in _windows(body):
                if len(window.split()) < 25:  # listas vacías generadas por la wiki
                    continue
                chunks.append(Chunk(len(chunks), "wiki", page["title"], section, page["url"], window))
    for f in sorted(CURATED_DIR.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip()
        for section, body in _split_sections(text, r"^##\s+(.+)$"):
            for window in _windows(body):
                chunks.append(Chunk(len(chunks), "curated", title, section, f"data/kb/curated/{f.name}", window))
    return chunks


def build_index() -> None:
    chunks = build_chunks()
    model = _embedder()
    texts = [f"{c.title} — {c.section}\n{c.text}" for c in chunks]
    emb = model.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=True)
    np.save(KB_DIR / "embeddings.npy", emb.astype(np.float32))
    with open(KB_DIR / "chunks.jsonl", "w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
    print(f"Indexed {len(chunks)} chunks from {len(list(RAW_DIR.glob('*.json')))} wiki pages "
          f"and {len(list(CURATED_DIR.glob('*.md')))} curated notes")


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.len = np.array([len(d) for d in docs], dtype=np.float32)
        self.avg = float(self.len.mean())
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> np.ndarray:
        out = np.zeros(len(self.tf), dtype=np.float32)
        for t in set(query):
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, tf in enumerate(self.tf):
                f = tf.get(t)
                if f:
                    out[i] += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
        return out


class KnowledgeBase:
    def __init__(self) -> None:
        lines = (KB_DIR / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
        self.chunks = [Chunk(**json.loads(ln)) for ln in lines]
        self.emb = np.load(KB_DIR / "embeddings.npy")
        self.bm25 = BM25([_tokens(f"{c.title} {c.section} {c.text}") for c in self.chunks])
        self.model = _embedder()

    def search(self, query: str, k: int = 5, rrf_k: int = 60, pool: int = 30, mode: str = "hybrid") -> list[dict]:
        rankings = []
        if mode in ("hybrid", "bm25"):
            rankings.append(np.argsort(-self.bm25.scores(_tokens(query)))[:pool])
        if mode in ("hybrid", "dense"):
            q = self.model.encode([QUERY_PREFIX + query], normalize_embeddings=True)[0]
            rankings.append(np.argsort(-(self.emb @ q))[:pool])
        fused: dict[int, float] = {}
        for ranking in rankings:
            for rank, idx in enumerate(ranking):
                fused[int(idx)] = fused.get(int(idx), 0.0) + 1.0 / (rrf_k + rank + 1)
        best = sorted(fused, key=fused.get, reverse=True)[:k]
        return [{"source": self.chunks[i].source, "title": self.chunks[i].title, "section": self.chunks[i].section,
                 "url": self.chunks[i].url, "text": self.chunks[i].text, "score": round(fused[i], 4)} for i in best]


@lru_cache(maxsize=1)
def load_kb() -> KnowledgeBase:
    return KnowledgeBase()


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        fetch_wiki(refresh="--refresh" in sys.argv)
        build_index()

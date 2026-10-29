import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from planner.build_file import BuildPlan, load_build_file
from planner.net import USER_AGENT
from planner.pob_code import resolve

GUIDES_DIR = Path(__file__).resolve().parent.parent / "guides"


@dataclass
class Guide:
    id: str
    path: Path
    url: str | None
    texts: dict[str, str] = field(default_factory=dict)
    pob_xml: dict[str, str] = field(default_factory=dict)
    build_plans: dict[str, BuildPlan] = field(default_factory=dict)

    def full_text(self) -> str:
        return "\n\n".join(f"=== Variant: {v} ===\n{t}" for v, t in self.texts.items())


def _variant(stem: str) -> str:
    return stem.split("_", 1)[1] if "_" in stem else "main"


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = (re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines())
    return "\n".join(ln for ln in lines if ln)


def list_guides() -> list[str]:
    if not GUIDES_DIR.exists():
        return []
    return sorted(p.name for p in GUIDES_DIR.iterdir() if p.is_dir())


def load_guide(guide_id: str) -> Guide:
    path = GUIDES_DIR / guide_id
    if not path.is_dir():
        raise FileNotFoundError(f"Guide folder not found: {path}")
    url_file = path / "url.txt"
    guide = Guide(id=guide_id, path=path, url=url_file.read_text(encoding="utf-8").strip() if url_file.exists() else None)

    for f in sorted(path.glob("page*")):
        raw = f.read_text(encoding="utf-8", errors="replace")
        guide.texts[_variant(f.stem)] = html_to_text(raw) if f.suffix.lower() in (".html", ".htm") else raw.strip()
    for f in sorted(path.glob("pob*.txt")):
        guide.pob_xml[_variant(f.stem)] = resolve(f.read_text(encoding="utf-8"))
    for f in sorted(path.glob("pob*.xml")):
        guide.pob_xml[_variant(f.stem)] = f.read_text(encoding="utf-8")
    for f in sorted(path.glob("*.build")):
        guide.build_plans[f.stem] = load_build_file(f)
    # cada variante .build lleva el texto de la guía más sus propias gemas y equipo
    if guide.build_plans:
        prose = "\n\n".join(guide.texts.values())
        guide.texts = {variant: (f"{prose}\n\n" if prose else "") + build_plan_text(plan)
                       for variant, plan in guide.build_plans.items()}
    return guide


GUIDE_FILES = (".build", ".xml", ".html", ".htm", ".txt")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "main"


def _variant_file(name: str) -> str:
    stem, suffix = Path(name).stem, Path(name).suffix.lower()
    variant = _slug(stem.split(" - ")[0])
    if suffix == ".build":
        return f"{variant}.build"
    if suffix == ".xml":
        return f"pob_{variant}.xml"
    if suffix in (".html", ".htm"):
        return f"page_{variant}{suffix}"
    return f"page_{variant}.txt"


def import_guide(source: str | Path, guide_id: str | None = None) -> str:
    text = str(source).strip()
    path = Path(text) if len(text) < 260 else None
    is_file = bool(path and path.exists())
    is_pob = not is_file and ("pobb.in" in text or re.fullmatch(r"[A-Za-z0-9_\-+/=]{200,}", text) is not None)
    if guide_id is None:
        if is_file:
            guide_id = _slug(path.stem.split(" - ")[-1] if path.is_file() else path.name)
        else:
            n = 1
            while (GUIDES_DIR / f"custom-{n}").exists():
                n += 1
            guide_id = f"custom-{n}"
    folder = GUIDES_DIR / guide_id
    if folder.exists():
        print(f"La guía '{guide_id}' ya está en el repertorio; no se modifica.")
        return guide_id
    folder.mkdir(parents=True)
    if is_file and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if Path(info.filename).suffix.lower() in GUIDE_FILES and not info.is_dir():
                    (folder / _variant_file(Path(info.filename).name)).write_bytes(z.read(info))
    elif is_file and path.is_dir():
        for f in path.iterdir():
            if f.suffix.lower() in GUIDE_FILES:
                shutil.copy(f, folder / _variant_file(f.name))
    elif is_file:
        shutil.copy(path, folder / _variant_file(path.name))
    elif is_pob:
        (folder / "pob_main.txt").write_text(text, encoding="utf-8")
    elif text.startswith("http"):
        (folder / "url.txt").write_text(text, encoding="utf-8")
        try:
            req = Request(text, headers={"User-Agent": USER_AGENT})
            (folder / "page.html").write_bytes(urlopen(req, timeout=60).read())
        except Exception as e:
            print(f"No se pudo descargar la página ({e}); guárdala a mano como {folder / 'page.html'}.")
    else:
        shutil.rmtree(folder)
        raise ValueError(f"Fuente de guía no reconocida: {text[:80]}")
    print(f"Guía '{guide_id}' importada: {sorted(p.name for p in folder.iterdir())}")
    return guide_id


def build_plan_text(plan: BuildPlan) -> str:
    lines = [f"Build: {plan.name}" + (f" by {plan.author}" if plan.author else "")]
    if plan.description:
        lines.append(plan.description)
    lines.append("Skill gems (level range):")
    for gem in plan.skills:
        supports = ", ".join(s.short_name for s in gem.supports)
        lines.append(f"- {gem.short_name} (levels {gem.levels.low}-{gem.levels.high})" + (f" with {supports}" if supports else ""))
    lines.append("Gear targets (level range):")
    for hint in plan.slots:
        what = hint.unique_name or (hint.note or "").replace("\n", "; ")
        lines.append(f"- {hint.inventory_id} (levels {hint.levels.low}-{hint.levels.high}): {what}")
    return "\n".join(lines)

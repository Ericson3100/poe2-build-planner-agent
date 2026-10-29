# Guides

One folder per guide. `planner.guides.load_guide()` reads:

| File | Meaning |
|---|---|
| `url.txt` | Source URL of the guide |
| `*.build` | Build-planner export (one file per variant: passives, gems and gear targets by level). **Required** for the agent to compare your character with the guide. |
| `pob*.txt` / `pob*.xml` | Path of Building code, pobb.in link or XML |
| `page*.txt` / `page*.html` | Guide prose (optional; used to extract cited priorities) |
| `priorities_*.json`, `direction_*.json` | Cached LLM extractions (priorities with quotes, build direction) |

## Included guides

- `coc-comet-animeprincess/` — "[0.5] COC COMET - Stormweaver" by **animeprincess**, published on Mobalytics:
  <https://mobalytics.gg/poe-2/profile/animeprincess/builds/cast-on-crit-comet-stormweaver>.
  The `.build` variant files are kept because the code needs them to run; all credit for the build belongs to its author.
- `spark-stormweaver-poevault/` — Stormweaver Spark guide from poe-vault:
  <https://www.poe-vault.com/poe2/guides/stormweaver-spark-build> (only the URL and the public pobb.in link are kept).

The full guide texts (`page.txt`) used in the original experiment are **not** redistributed (third-party
copyright). Without them the agent still works from the `.build` variants, but the cached
`priorities_*.json` key no longer matches, so the priorities are re-extracted with the LLM on the first run.
To reproduce the original setting, open the guide URL, save the page as `page.html` (or its visible text as
`page.txt`) inside the guide folder.

## Adding a guide

In the notebook set `GUIDE_SOURCES` (section 5) to a zip/`.build`/`.xml`/`.html` path, a PoB code, a pobb.in
link or a URL, or call `planner.guides.import_guide(source)`.

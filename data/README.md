# Data

- `tree_0_5.json` — PoE2 passive tree (version 0.5) as published by
  [Path of Building Community PoE2](https://github.com/PathOfBuildingCommunity/PathOfBuilding-PoE2)
  (`src/TreeData/0_5/tree.json`, `dev` branch, MIT). Game data © Grinding Gear Games.
- `kb/raw/` — 93 pages fetched from [poe2wiki.net](https://www.poe2wiki.net) through the MediaWiki API.
  Wiki content is licensed under **CC BY-NC-SA 3.0**; each file keeps its source URL. Redistributed here
  for non-commercial, educational use with attribution, under the same license.
- `kb/curated/defence_offence_balance.md` — the author's own planner heuristics note.
- `kb/chunks.jsonl` + `kb/embeddings.npy` — the RAG index (334 chunks, `BAAI/bge-small-en-v1.5` embeddings),
  rebuilt with `python -m planner.kb build` (`--refresh` re-downloads the wiki pages).

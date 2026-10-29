import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass(frozen=True)
class Node:
    id: int
    string_id: str | None
    name: str
    stats: tuple[str, ...]
    kind: str
    connections: tuple[int, ...]


def _kind(raw: dict) -> str:
    if raw.get("isKeystone"):
        return "keystone"
    if raw.get("isNotable"):
        return "notable"
    if raw.get("isJewelSocket"):
        return "jewel"
    if raw.get("ascendancyName"):
        return "ascendancy"
    if raw.get("isAttribute"):
        return "attribute"
    return "small"


class Tree:
    def __init__(self, version: str = "0_5") -> None:
        raw = json.loads((DATA_DIR / f"tree_{version}.json").read_text(encoding="utf-8"))
        self.nodes: dict[int, Node] = {}
        for key, n in raw["nodes"].items():
            if not key.isdigit():
                continue
            self.nodes[int(key)] = Node(
                id=int(key),
                string_id=n.get("stringId"),
                name=n.get("name", ""),
                stats=tuple(n.get("stats", [])),
                kind=_kind(n),
                connections=tuple(c["id"] for c in n.get("connections", [])),
            )
        self.by_string_id = {n.string_id: n.id for n in self.nodes.values() if n.string_id}
        self.ascendancy = {int(k): n.get("ascendancyName") for k, n in raw["nodes"].items()
                           if k.isdigit() and n.get("ascendancyName")}
        self.class_start = {int(k): n["classesStart"] for k, n in raw["nodes"].items()
                            if k.isdigit() and n.get("classesStart")}
        # los nodos que son solo imagen no se pueden asignar
        image_only = {int(k) for k, n in raw["nodes"].items() if k.isdigit() and n.get("isOnlyImage")}
        # el archivo del árbol guarda cada conexión una sola vez
        self.adjacent: dict[int, set[int]] = {i: set() for i in self.nodes if i not in image_only}
        for node in self.nodes.values():
            if node.id in image_only:
                continue
            for other in node.connections:
                if other in self.adjacent:
                    self.adjacent[node.id].add(other)
                    self.adjacent[other].add(node.id)

    def path_to(self, allocated: set[int], target: int) -> list[int]:
        # camino más corto, por nodos libres, hasta el nodo buscado
        if target in allocated:
            return []
        # los nodos de ascendencia se buscan dentro de su propio árbol
        domain = self.ascendancy.get(target)
        in_domain = lambda n: self.ascendancy.get(n) == domain
        start_ok = lambda n: n not in self.class_start or n in allocated
        frontier = [n for n in allocated if in_domain(n)]
        parent: dict[int, int | None] = {n: None for n in frontier}
        queue = list(frontier)
        while queue:
            nxt = []
            for node in queue:
                for other in self.adjacent.get(node, ()):
                    if other in parent or not in_domain(other) or not start_ok(other):
                        continue
                    parent[other] = node
                    if other == target:
                        path = [other]
                        while parent[path[-1]] not in allocated and parent[path[-1]] is not None:
                            path.append(parent[path[-1]])
                        return list(reversed(path))
                    nxt.append(other)
            queue = nxt
        return []

    def orphans(self, allocated: set[int], removed: set[int]) -> set[int]:
        # nodos que quedan sueltos al quitar otros
        remaining = allocated - removed
        starts = [n for n in remaining if n in self.class_start]
        seen = set(starts)
        queue = list(starts)
        while queue:
            node = queue.pop()
            for other in self.adjacent.get(node, ()):
                if other in remaining and other not in seen and other not in self.ascendancy:
                    seen.add(other)
                    queue.append(other)
        return {n for n in remaining if n not in seen and n not in self.ascendancy}


@lru_cache(maxsize=4)
def load_tree(version: str = "0_5") -> Tree:
    return Tree(version)

import ctypes
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

POB_DIR = Path(__file__).resolve().parent.parent / "vendor" / "PathOfBuilding-PoE2"
LUA_GLOBALSINDEX = -10002

_HELPERS = r"""
local dkjson = require("dkjson")

function __pp_output_json()
    local out = {}
    for k, v in pairs(build.calcsTab.mainOutput) do
        local t = type(v)
        if t == "number" and v == v and v ~= math.huge and v ~= -math.huge then
            out[k] = v
        elseif t == "boolean" then
            out[k] = v
        end
    end
    return dkjson.encode(out)
end

function __pp_load_xml(xml)
    loadBuildFromXML(xml, "planner")
    return __pp_output_json()
end
"""


class LuaError(RuntimeError):
    pass


class Engine:
    def __init__(self, pob_dir: Path = POB_DIR) -> None:
        self.src = pob_dir / "src"
        self.evals = 0
        runtime = pob_dir / "runtime"
        os.add_dll_directory(str(runtime))
        self.lua = ctypes.CDLL(str(runtime / "lua51.dll"))
        self._bind()

        self.L = self.lua.luaL_newstate()
        self.lua.luaL_openlibs(self.L)
        lua_dir = (runtime / "lua").as_posix()
        self._exec(
            f'package.path = "{lua_dir}/?.lua;{lua_dir}/?/init.lua;" .. package.path\n'
            f'package.cpath = "{runtime.as_posix()}/?.dll;" .. package.cpath\n'
            # PoB espera los argumentos de la línea de comandos, aunque estén vacíos
            "arg = {}"
        )
        # PoB carga sus datos con rutas relativas a src/
        cwd = os.getcwd()
        os.chdir(self.src)
        try:
            self._exec('dofile("HeadlessWrapper.lua")')
        finally:
            os.chdir(cwd)
        self._exec(_HELPERS)

    def _bind(self) -> None:
        lua = self.lua
        lua.luaL_newstate.restype = ctypes.c_void_p
        lua.luaL_openlibs.argtypes = [ctypes.c_void_p]
        lua.luaL_loadbuffer.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p]
        lua.lua_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lua.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t)]
        lua.lua_tolstring.restype = ctypes.c_void_p
        lua.lua_settop.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lua.lua_gettop.argtypes = [ctypes.c_void_p]
        lua.lua_getfield.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
        lua.lua_pushlstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]

    def _pop_string(self) -> str:
        size = ctypes.c_size_t()
        ptr = self.lua.lua_tolstring(self.L, -1, ctypes.byref(size))
        text = ctypes.string_at(ptr, size.value).decode("utf-8", "replace") if ptr else ""
        self.lua.lua_settop(self.L, -2)
        return text

    def _exec(self, code: str) -> None:
        data = code.encode("utf-8")
        if self.lua.luaL_loadbuffer(self.L, data, len(data), b"=planner") != 0:
            raise LuaError(self._pop_string())
        if self.lua.lua_pcall(self.L, 0, 0, 0) != 0:
            raise LuaError(self._pop_string())

    def _call(self, func: str, *args: str) -> str:
        self.lua.lua_getfield(self.L, LUA_GLOBALSINDEX, func.encode())
        for arg in args:
            data = arg.encode("utf-8")
            self.lua.lua_pushlstring(self.L, data, len(data))
        cwd = os.getcwd()
        os.chdir(self.src)
        try:
            if self.lua.lua_pcall(self.L, len(args), 1, 0) != 0:
                raise LuaError(self._pop_string())
        finally:
            os.chdir(cwd)
        return self._pop_string()

    def evaluate_xml(self, xml_text: str) -> dict[str, float | bool]:
        self.evals += 1
        return json.loads(self._call("__pp_load_xml", xml_text))


# cada what-if recarga el XML completo (~0.4 s)

def with_item(xml_text: str, slot: str, item_text: str) -> str:
    root = ET.fromstring(xml_text)
    items = root.find("Items")
    new_id = max((int(i.get("id")) for i in items.findall("Item")), default=0) + 1
    item_el = ET.Element("Item", id=str(new_id))
    item_el.text = item_text
    items.insert(0, item_el)
    item_set = items.find(f"ItemSet[@id='{items.get('activeItemSet', '1')}']")
    slot_el = item_set.find(f"Slot[@name='{slot}']")
    if slot_el is None:
        slot_el = ET.SubElement(item_set, "Slot", name=slot)
    slot_el.set("itemId", str(new_id))
    return ET.tostring(root, encoding="unicode")


def with_main_skill(xml_text: str, group: int, active: int = 1) -> str:
    root = ET.fromstring(xml_text)
    root.find("Build").set("mainSocketGroup", str(group))
    skills = root.find("Skills")
    skill_set = skills.find(f"SkillSet[@id='{skills.get('activeSkillSet', '1')}']")
    groups = skill_set.findall("Skill")
    if 1 <= group <= len(groups):
        groups[group - 1].set("mainActiveSkill", str(active))
        groups[group - 1].set("mainActiveSkillCalcs", str(active))
    return ET.tostring(root, encoding="unicode")


def damage_skills(engine: "Engine", xml_text: str, min_share: float = 0.05) -> list[dict]:
    root = ET.fromstring(xml_text)
    skills = root.find("Skills")
    skill_set = skills.find(f"SkillSet[@id='{skills.get('activeSkillSet', '1')}']")
    found = []
    for idx, grp in enumerate(skill_set.findall("Skill"), 1):
        if grp.get("source") or grp.get("enabled") == "false":
            continue
        actives = [g.get("nameSpec") for g in grp.findall("Gem") if "SupportGem" not in (g.get("gemId") or "")]
        for a_idx, name in enumerate(actives, 1):
            dps = engine.evaluate_xml(with_main_skill(xml_text, idx, a_idx)).get("CombinedDPS", 0) or 0
            if dps > 0:
                found.append({"group": idx, "active": a_idx, "skill": name, "dps": dps})
    if not found:
        return []
    top = max(f["dps"] for f in found)
    return [f for f in found if f["dps"] >= min_share * top]


def skills_dps(engine: "Engine", xml_text: str, skills: list[dict]) -> dict[str, dict]:
    root = ET.fromstring(xml_text)
    sk = root.find("Skills")
    elements = sk.find(f"SkillSet[@id='{sk.get('activeSkillSet', '1')}']").findall("Skill")
    out = {}
    for f in skills:
        # un grupo apagado no hace daño
        if f["group"] <= len(elements) and elements[f["group"] - 1].get("enabled") == "false":
            out[f["skill"]] = {"dps": 0.0, "crit": 0.0}
            continue
        st = engine.evaluate_xml(with_main_skill(xml_text, f["group"], f["active"]))
        out[f["skill"]] = {"dps": st.get("CombinedDPS", 0) or 0, "crit": st.get("CritChance", 0) or 0}
    return out


def with_skills_enabled(xml_text: str, groups: dict[int, bool]) -> str:
    root = ET.fromstring(xml_text)
    skills = root.find("Skills")
    skill_set = skills.find(f"SkillSet[@id='{skills.get('activeSkillSet', '1')}']")
    elements = skill_set.findall("Skill")
    for group, enabled in groups.items():
        if 1 <= group <= len(elements):
            elements[group - 1].set("enabled", "true" if enabled else "false")
    return ET.tostring(root, encoding="unicode")


def with_jewel(xml_text: str, node_id: int, item_text: str | None) -> str:
    root = ET.fromstring(xml_text)
    tree = root.find("Tree")
    spec = tree.findall("Spec")[int(tree.get("activeSpec", "1")) - 1]
    sockets = spec.find("Sockets")
    if sockets is None:
        sockets = ET.SubElement(spec, "Sockets")
    sock = sockets.find(f"Socket[@nodeId='{node_id}']")
    if sock is None:
        sock = ET.SubElement(sockets, "Socket", nodeId=str(node_id))
    if item_text is None:
        sock.set("itemId", "0")
        return ET.tostring(root, encoding="unicode")
    items = root.find("Items")
    new_id = max((int(i.get("id")) for i in items.findall("Item")), default=0) + 1
    item_el = ET.Element("Item", id=str(new_id))
    item_el.text = item_text
    items.insert(0, item_el)
    sock.set("itemId", str(new_id))
    return ET.tostring(root, encoding="unicode")


def apply_item(xml_text: str, slot: str, item_text: str | None) -> str:
    if slot.startswith("Jewel "):
        return with_jewel(xml_text, int(slot.split()[1]), item_text)
    return with_item(xml_text, slot, item_text)


def with_nodes(xml_text: str, add: set[int] = frozenset(), remove: set[int] = frozenset()) -> str:
    root = ET.fromstring(xml_text)
    tree = root.find("Tree")
    spec = tree.findall("Spec")[int(tree.get("activeSpec", "1")) - 1]
    nodes = {int(n) for n in spec.get("nodes", "").split(",") if n}
    nodes = (nodes | set(add)) - set(remove)
    spec.set("nodes", ",".join(str(n) for n in sorted(nodes)))
    return ET.tostring(root, encoding="unicode")


def diff_stats(before: dict, after: dict, keys: list[str]) -> dict[str, tuple[float, float, float]]:
    out = {}
    for k in keys:
        a, b = before.get(k, 0), after.get(k, 0)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            out[k] = (a, b, b - a)
    return out

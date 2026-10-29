import re
import xml.etree.ElementTree as ET

from planner.runes import runes_for

ARMOUR_SLOTS_SET = {"Helmet", "Body Armour", "Gloves", "Boots", "Weapon 2"}
TWO_HANDED = ("staff", "bow", "crossbow", "quarterstaff", "two hand")
QUALITY_ORB = {"armour": "Armourer's Scrap", "weapon": "Blacksmith's Whetstone", "caster": "Arcanist's Etcher"}
# runas que le sirven a un caster o a alguna defensa
RELEVANT = re.compile(r"Resist|Energy Shield|Life|Mana|Spell|Cold|Critical|Spirit|Cast Speed|Attribute|"
                      r"Intelligence|Strength|Dexterity|Armour, Evasion", re.I)


def kinds(slot: str, base: str) -> list[str]:
    b = base.lower()
    if slot in ("Helmet", "Body Armour", "Gloves", "Boots"):
        return [slot.lower(), "armour"]
    if slot.startswith("Weapon 2") and ("shield" in b or "buckler" in b or "focus" in b):
        return ["shield", "buckler", "focus", "armour"]
    if slot.startswith("Weapon"):
        if "staff" in b and "quarter" not in b:
            return ["staff", "caster", "martial weapon wand or staff", "weapon"]
        if "wand" in b or "sceptre" in b:
            return ["wand", "sceptre", "caster", "martial weapon wand or staff", "weapon"]
        return ["weapon"]
    return []


def max_sockets(slot: str, base: str) -> int:
    if slot == "Body Armour":
        return 2
    if slot.startswith("Weapon"):
        return 2 if any(t in base.lower() for t in TWO_HANDED) else 1
    if slot in ("Helmet", "Gloves", "Boots"):
        return 1
    return 0


def rune_lines(name: str, slot: str, base: str) -> list[str] | None:
    for kind in kinds(slot, base):
        lines = runes_for(kind).get(name)
        if lines:
            return lines
    return None


def candidate_runes(slot: str, base: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for kind in reversed(kinds(slot, base)):
        for name, lines in runes_for(kind).items():
            if any(RELEVANT.search(ln) for ln in lines):
                out[name] = lines
    return out


def item_state(text: str) -> dict:
    lines = text.splitlines()
    quality = next((int(m.group(1)) for ln in lines if (m := re.match(r"Quality: \+?(\d+)", ln.strip()))), 0)
    sockets = next((ln.strip().split(":", 1)[1].split() for ln in lines if ln.strip().startswith("Sockets:")), [])
    runes = [ln.strip().split(":", 1)[1].strip() for ln in lines if ln.strip().startswith("Rune:")]
    rarity = lines[0].split(":", 1)[1].strip().upper() if lines and lines[0].startswith("Rarity:") else "RARE"
    base = lines[2].strip() if rarity in ("RARE", "UNIQUE") and len(lines) > 2 else lines[1].strip()
    return {"quality": quality, "sockets": len(sockets), "runes": [r for r in runes if r != "None"],
            "rarity": rarity, "base": base}


def finished(text: str, slot: str, rune: str | None) -> tuple[str, dict]:
    # el item con 20% de calidad y todas las ranuras de runa llenas
    st = item_state(text)
    room = max_sockets(slot, st["base"])
    add = max(0, room - len(st["runes"])) if rune else 0
    new_sockets = max(st["sockets"], len(st["runes"]) + add)
    extra = rune_lines(rune, slot, st["base"]) if rune and add else None
    if not extra:
        add, new_sockets = 0, st["sockets"]

    out, implicits_seen = [], False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(("Quality:", "Sockets:", "Rune:")):
            continue
        if s.startswith("Implicits:") and not implicits_seen:
            implicits_seen = True
            count = int(s.split(":", 1)[1])
            out.append("Quality: 20")
            if new_sockets:
                out.append("Sockets: " + " ".join(["S"] * new_sockets))
                runes = st["runes"] + [rune] * add
                out += [f"Rune: {r}" for r in runes] + ["Rune: None"] * (new_sockets - len(runes))
            added = [f"{{enchant}}{{rune}}{line}" for line in (extra or []) for _ in range(add)]
            out.append(f"Implicits: {count + len(added)}")
            out += added
            continue
        out.append(ln)
    change = {"quality_from": st["quality"], "runes_added": add, "sockets_added": max(0, new_sockets - st["sockets"]),
              "rune": rune if add else None, "rarity": st["rarity"], "base": st["base"]}
    return "\n".join(out), change


def finish_cost(change: dict, slot: str, prices) -> dict:
    def price(name):
        p = prices.get(name)
        return prices.to_exalted(p.divine) if p else None

    kind = "armour" if not slot.startswith("Weapon") else ("caster" if any(
        k in change["base"].lower() for k in ("staff", "wand", "sceptre")) else "weapon")
    per_orb = {"RARE": 1, "UNIQUE": 1, "MAGIC": 2}.get(change["rarity"], 5)
    q_orbs = -(-max(0, 20 - change["quality_from"]) // per_orb)
    parts = {
        f"{QUALITY_ORB[kind]} x{q_orbs}": (price(QUALITY_ORB[kind]) or 0) * q_orbs if q_orbs else 0,
        f"Artificer's Orb x{change['sockets_added']}": (price("Artificer's Orb") or 0) * change["sockets_added"],
        f"{change['rune']} x{change['runes_added']}": ((price(change["rune"]) or 0) * change["runes_added"]
                                                       if change["rune"] else 0),
    }
    parts = {k: round(v, 1) for k, v in parts.items() if not k.endswith("x0") and v is not None}
    return {"items": parts, "total_exalted": round(sum(parts.values()), 1)}


def equipped_text(xml_text: str, slot: str) -> str | None:
    root = ET.fromstring(xml_text)
    items = root.find("Items")
    item_set = items.find(f"ItemSet[@id='{items.get('activeItemSet', '1')}']")
    slot_el = item_set.find(f"Slot[@name='{slot}']") if item_set is not None else None
    if slot_el is None or slot_el.get("itemId", "0") == "0":
        return None
    item_el = items.find(f"Item[@id='{slot_el.get('itemId')}']")
    return (item_el.text or "").strip() if item_el is not None else None

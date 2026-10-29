import argparse

from planner.pob_xml import load_build, resolve_build
from planner.wallet import load_wallet

KEY_STATS = [
    "CombinedDPS", "TotalEHP", "Life", "EnergyShield", "Mana", "Spirit",
    "FireResist", "ColdResist", "LightningResist", "ChaosResist",
    "PhysicalMaximumHitTaken", "ChaosMaximumHitTaken", "Evasion", "Armour",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("build", nargs="?", default=None, help="file name in builds/ or a path")
    parser.add_argument("--wallet")
    args = parser.parse_args()

    b = load_build(resolve_build(args.build))
    print(f"{b.ascendancy} ({b.class_name}) lvl {b.level}  tree {b.tree_version}  league={b.league}")
    print(f"Passives: {len(b.nodes)} nodes, weapon sets {{{', '.join(f'{k}: {len(v)}' for k, v in b.weapon_set_nodes.items())}}}, "
          f"{len(b.jewel_sockets)} jewel sockets")

    print("\nStats (PoB cache at save time):")
    for name in KEY_STATS:
        if name in b.stats:
            print(f"  {name:24s} {b.stats[name]:>12,.1f}")

    print("\nEquipment:")
    for slot, item in sorted(b.equipped().items()):
        runes = f"  runes={item.runes}" if item.runes else ""
        print(f"  {slot:14s} [{item.rarity[0]}] {item.name}, {item.base} (ilvl {item.item_level}, "
              f"{len(item.implicits)} impl / {len(item.explicits)} expl){runes}")

    print("\nSkills:")
    for group in b.skills:
        if not group.gems:
            continue
        active = [g for g in group.gems if not g.is_support]
        supports = [g.name for g in group.gems if g.is_support]
        head = ", ".join(f"{g.name} {g.level}/{g.quality}q" for g in active)
        tag = " *main*" if group.is_main else ""
        src = f" [from {group.slot}]" if group.granted_by else ""
        print(f"  {head}{tag}{src}" + (f"  + {', '.join(supports)}" if supports else ""))

    if args.wallet:
        w = load_wallet(args.wallet)
        print(f"\nWallet ({w.league}): gold={w.gold:,}")
        for name, count in w.currency.items():
            print(f"  {name}: {count}")


if __name__ == "__main__":
    main()

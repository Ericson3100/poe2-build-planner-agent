from planner.kb import load_kb

ES_MIT = ("Energy Shield", "Damage mitigation")
ES_DELAY = ("Energy Shield", "Start of Energy shield Recharge")

QUESTIONS = [
    # literales: palabras clave de la wiki
    ("literal", "Chaos damage removes twice as much energy shield; Withered increases chaos damage taken",
     {ES_MIT, ("Chaos", "Mechanics")}),
    ("literal", "Bleeding and Poison damage bypass energy shield, damaging life directly",
     {ES_MIT, ("Energy Shield", "Overview")}),
    ("literal", "Deflection Rating against Attacker's Accuracy, deflection chance capped at 95%, works against boss attacks",
     {("Deflection", "Mechanics")}),
    ("literal", "Chaos Inoculation immunity to bleeding but not Corrupted Blood, stun and ailment thresholds from 1 maximum life",
     {("Chaos Inoculation", "Mechanics")}),
    ("literal", "Mind Over Matter keystone: mana absorbs damage dealt to life but halves mana recovery",
     {("Mind Over Matter", "Overview"), ("Mind Over Matter", "Mechanics")}),
    ("literal", "Eldritch Battery converts base energy shield into base mana before increased and more modifiers",
     {("Eldritch Battery", "Mechanics")}),
    ("literal", "Default critical damage bonus +100%, 200% of base damage, equivalent to crit multi from Path of Exile 1",
     {("Critical hit", "Critical damage bonus")}),
    ("literal", "Energy shield recharges 12.5% of maximum energy shield per second after the recharge delay",
     {("Energy Shield", "Recharge")}),
    ("literal", "Evasion entropy value randomized 0-99 after 100 server ticks, 3.33 seconds", {("Evasion", "Entropy")}),
    ("literal", "Primed for Stun at 40% Heavy Stun buildup for normal enemies, 60% rare, 70% unique",
     {("Stun", "Primed for stun")}),
    ("literal", "Exposure effect by enemy rarity: 30% less on Rare enemies, 50% less on Unique enemies",
     {("Exposure", "Enemy Rarity")}),
    ("literal", "Fully Broken Armour from Armour Break: enemies take 20% increased physical damage from hits",
     {("Armour", "Armour Break")}),
    # parafraseadas
    ("paraphrase", "How long must I avoid getting hit before my magic barrier starts refilling?", {ES_DELAY}),
    ("paraphrase", "Which damage skips my protective barrier and goes straight to my health pool?", {ES_MIT}),
    ("paraphrase", "How much physical mitigation rating do I need to halve a 100 damage hit?", {("Armour", "Rule of thumb")}),
    ("paraphrase", "Does the keystone that sets my health to one stop me from losing blood over time?",
     {("Chaos Inoculation", "Mechanics")}),
    ("paraphrase", "What resource do I spend to keep auras and permanent minions active?", {("Spirit", "Overview")}),
    ("paraphrase", "If I turn more than all of my physical damage into other elements, how is it split?",
     {("Damage conversion", "Overconversion")}),
    ("paraphrase", "Why do my automatic trigger gems fire more often against rare and boss monsters?", {("Meta", "Energy")}),
    ("paraphrase", "Can raising a shield protect me from damage that ticks over time?", {("Block", "Mechanics")}),
    # en español
    ("spanish", "¿Cuánto escudo de energía quita el daño de caos?", {ES_MIT, ("Chaos", "Mechanics")}),
    ("spanish", "¿El veneno ignora el escudo de energía y baja la vida directamente?", {ES_MIT}),
    ("spanish", "¿Cuántos segundos sin recibir daño tarda en empezar a recargarse el escudo de energía?", {ES_DELAY}),
    ("spanish", "¿Qué hace la penetración de resistencias?",
     {("Penetration", "Mechanics"), ("Resistance", "Resistance penetration")}),
    ("spanish", "¿Cómo funciona la congelación y la acumulación de congelamiento?", {("Freeze", "Mechanics")}),
    ("spanish", "¿Cuánto daño hace la ignición y durante cuánto tiempo?", {("Ignite", "Mechanics")}),
    ("spanish", "¿Cuánta armadura necesito para reducir a la mitad un golpe?", {("Armour", "Rule of thumb")}),
    ("spanish", "¿Las cargas de los frascos se recuperan matando monstruos?", {("Flask", "Mechanics")}),
]
KINDS = ("literal", "paraphrase", "spanish")
MODES = ("bm25", "dense", "hybrid")


def evaluate(kb, mode: str, kind: str, k: int = 5) -> dict[str, float]:
    page_hit = passage_hit = rr = 0.0
    items = [(q, rel) for kd, q, rel in QUESTIONS if kd == kind]
    for question, relevant in items:
        hits = [(h["title"], h["section"]) for h in kb.search(question, k=k, mode=mode)]
        pages = {p for p, _ in relevant}
        page_hit += any(t in pages for t, _ in hits)
        ranks = [i for i, h in enumerate(hits, 1) if h in relevant]
        passage_hit += bool(ranks)
        rr += 1.0 / ranks[0] if ranks else 0.0
    n = len(items)
    return {"page": page_hit / n, "passage": passage_hit / n, "mrr": rr / n}


def main() -> None:
    kb = load_kb()
    results = {(m, kd): evaluate(kb, m, kd) for m in MODES for kd in KINDS}
    print(f"{len(QUESTIONS)} preguntas ({', '.join(f'{sum(q[0] == kd for q in QUESTIONS)} {kd}' for kd in KINDS)})\n")
    print(f"{'tipo':<11}{'modo':<8}{'R@5 página':>12}{'R@5 pasaje':>12}{'MRR pasaje':>12}")
    for kd in KINDS:
        for m in MODES:
            r = results[(m, kd)]
            print(f"{kd:<11}{m:<8}{r['page']:>12.2f}{r['passage']:>12.2f}{r['mrr']:>12.2f}")
    print()
    for m in MODES:
        avg = {key: sum(results[(m, kd)][key] for kd in KINDS) / len(KINDS) for key in ("page", "passage", "mrr")}
        print(f"{'promedio':<11}{m:<8}{avg['page']:>12.2f}{avg['passage']:>12.2f}{avg['mrr']:>12.2f}")


if __name__ == "__main__":
    main()

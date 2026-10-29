# Planner heuristics: balancing defences and damage

These notes are the planner's own heuristics, not game data. They summarise common build-planning practice and point to the wiki pages (also in this knowledge base) for the exact mechanics. When a wiki chunk contradicts a heuristic here, the wiki wins.

## Why balance matters

A character dies from the single weakest defensive layer, not from the average. Damage still matters: killing faster removes threats before they act, and a build that cannot kill its content takes more hits over a longer fight. The planner therefore treats survivability and damage as a pair: fix hard defensive holes first, then spend on whichever side gives the larger measured gain per currency for the guide's stage.

Order of priority used by the planner:
1. Hard problems PoB reports: unmet attribute requirements (the item or gem stops working), over-reserved Spirit (buffs or minions switch off), uncapped elemental resistances.
2. The weakest damage type in PoB's "maximum hit taken" numbers (Physical, Fire, Cold, Lightning, Chaos). If one of them is far below the others, raising it is usually the best survivability purchase.
3. The guide's stated thresholds and slot priorities for the current stage.
4. Damage upgrades, measured as CombinedDPS gain per exalted orb.

## Elemental and chaos resistances

Elemental resistances cap at 75% by default, and the campaign applies cumulative penalties to elemental resistance (see the wiki Resistance page for the current amounts). Each point near the cap removes a larger share of the remaining damage, so the last few percent to the cap are worth more than they look. Uncapped elemental resistance is the most common avoidable cause of deaths, so the planner fixes it before buying damage.

Chaos resistance usually starts at 0% and is not covered by "all elemental resistances" modifiers. It deserves more weight for energy-shield characters, because chaos damage removes twice as much energy shield (wiki: Energy Shield, Chaos). Characters with Chaos Inoculation are immune to chaos damage, so chaos resistance is irrelevant for them.

## The three defences and attributes

Armour, Evasion and Energy Shield are tied to Strength, Dexterity and Intelligence respectively, and armour pieces that grant a defence carry that attribute's requirement (wiki: Defences). A class's natural attribute therefore suggests its natural defence: Intelligence classes such as the Sorceress and Witch usually stack energy shield; Strength classes armour; Dexterity classes evasion; hybrids mix two.

- Armour reduces physical damage from hits and works best against many small hits; it does little against very large hits and nothing against damage over time (wiki: Armour).
- Evasion avoids hits entirely based on evasion rating versus enemy accuracy, and can downgrade critical hits (wiki: Evasion). Deflection supplements evasion builds and also works against boss attacks that evasion cannot stop (wiki: Deflection).
- Energy Shield is an extra pool on top of life that recharges when not damaged for a short time. Bleeding and poison bypass it to hit life directly, and chaos damage removes it twice as fast (wiki: Energy Shield).

## Stats that are often irrelevant

Spending on a stat the build cannot use is wasted currency. Typical cases:
- Spell builds gain nothing from attack speed, accuracy or added attack damage; attack builds gain nothing from spell damage or cast speed.
- Pure energy-shield builds with Chaos Inoculation or low-life setups gain little from maximum life; small amounts of armour or evasion on such characters add little compared with more energy shield.
- Minion stats matter only when minions deal the damage or carry the defence; a self-cast build with a utility minion should ignore most of them.
- Item rarity and light radius do not improve the build's combat numbers.
- Overcapped resistance beyond the cap only helps against curses and exposure that lower it; a small buffer is useful, a large one is wasted.

## Reading PoB numbers

- TotalEHP summarises how much damage the character can take; compare it before and after each change rather than in isolation.
- The per-type maximum hit taken shows the largest single hit of each type the character survives; the lowest one is the weakest link.
- CombinedDPS is the main damage figure; check that the guide's main skill is the one PoB is measuring.
- Requirement checks (Str, Dex, Int versus their requirements) and SpiritUnreserved below zero are hard failures, not trade-offs.

## Damage scaling basics

Damage scales multiplicatively across categories: base damage (gem level, added damage, weapon), increased modifiers (which add together), more multipliers, critical hit chance and critical damage bonus, speed, and enemy resistance reduction (penetration, exposure, curses). Adding to a category the build has little of yields more than stacking one that is already large; the engine measures this directly, so the planner compares candidates by measured CombinedDPS gain (wiki: Damage, Critical hit, Penetration, Exposure).

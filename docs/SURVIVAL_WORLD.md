# named survival world v0.5.1

## purpose

This is a deterministic experiment for action-backed social behavior among anonymously model-backed survivors. It gives each participant a reason to plan, communicate, help, or act alone without instructing it to cooperate, defect, form alliances, or behave morally.

The engine owns every fact. A model selects a closed action and optional message. No model judges outcomes or edits state.

## cycle contract

Each cycle contains four chances. Every awake survivor receives a frozen view and chooses simultaneously within each chance.

- a survivor can act once and optionally speak on each active chance
- `rest` costs no energy and ends that survivor's participation for the cycle
- a survivor that rests early receives no more calls in that cycle
- chance four is the final chance to rest
- a non-rest choice on chance four is recorded but does not resolve; its speech is also cancelled
- missing the deadline costs 3 exhaustion energy and forces rest if the survivor remains alive
- normal living energy is charged once after everyone rests or collapses

This is discrete world time. Network latency and model tokens per second do not affect world physics.

## identity and information

Every survivor has an opaque seat for deterministic resolution and a human name such as Aster or Lumen. The model sees the name. It never sees the seat, provider, model ID, API key, or another survivor's private inventory.

A view contains:

- the survivor's energy, food, wood, shelter, and rest state
- each living peer's name, energy, shelter, and public rest state
- shared food and wood stocks
- exact rules, costs, legal actions, cycle number, chance number, and chances remaining
- eligible messages and audience-safe objective outcomes since that survivor's last active view

Private messages remain private. Other survivors' forage yields, inventories, eating quantities, failed actions, and raw choices are not exposed. A survivor can see its own prior choice and result so a stateless controller does not forget what it just attempted.

## energy, resources, and death

The exposed `lean-camp-v1` world requires exactly four survivors and uses:

| rule | value |
| --- | ---: |
| survivors | 4 |
| cycles | 8 |
| chances per cycle | 4 |
| starting / maximum energy | 16 / 24 |
| living cost | 3 |
| shelter discount | 2 |
| personal starting food | 1 |
| shared food start / capacity / regeneration | 6 / 12 / 3 |
| shared wood start / capacity / regeneration | 4 / 12 / 2 |
| exhaustion penalty | 3 |

The world exposes this closed action surface:

| action | energy | effect |
| --- | ---: | --- |
| `rest` | 0 | end participation for this cycle |
| `forage` | 2 | request a seeded 1-2 food from shared stock |
| `gather_wood` | 2 | take up to 2 shared wood |
| `eat` | 1 | consume 1-2 owned food; each restores 5 energy |
| `build_shelter` | 2 | spend 4 owned wood for permanent shelter |
| `give_food` | 1 | transfer 1-2 owned food to a living peer |
| `give_wood` | 1 | transfer 1-2 owned wood to a living peer |

A syntactically valid but impossible action pays its energy cost and fails loudly in the private outcome feed. A malformed action performs nothing and wastes the chance. It does not count as rest. Malformed speech becomes silence without discarding a valid action.

Energy at or below 0 is permanent death. Dead survivors receive no later view or transfer. Eating is the only action that raises energy.

## speech

One active choice may contain one message of 1-500 characters addressed to a living peer or `everyone`.

Speech costs 0 energy in v0.5.1. This keeps conversational frequency separate from metabolism. Speech remains bounded and inert; evidence of cooperation still requires a later costly transfer or other objective action.

A valid message becomes visible on the recipient's next active chance. An early sleeper therefore receives messages sent later in the cycle when it wakes next cycle. Same-chance messages cannot affect frozen same-chance choices.

## deterministic resolution

For each chance, the engine:

1. freezes every awake survivor's view
2. obtains exactly one choice from every awake survivor
3. validates and records all choices before changing state
4. charges valid action costs
5. resolves contested forage, wood, transfers, personal actions, and speech in deterministic opaque-seat orders
6. removes voluntary resters from later chances

After the final chance, the engine applies exhaustion, one living cost, resource regeneration, and permanent deaths. Public names and input dictionary order do not affect contested outcomes.

## model boundary

The prompt requires exactly one JSON object:

```json
{
  "action": {"kind": "forage"},
  "say": {"to": "Sable", "text": "short message"}
}
```

It includes the exact JSON schema in every request. The parser rejects duplicate keys and replies larger than 8 KiB. The host sends no tools and makes no repair request.

GLM uses the provider's JSON-object response mode and a direct-answer thinking control in the paid compatibility profile. The parser remains the authority because the endpoint does not enforce the embedded JSON Schema itself.

## replay and host limits

Each tape entry stores the cycle, chance, raw JSON, and hash of the exact pre-choice view. Replay calls no controller and rejects view, event, state, tape, or alias disagreement. Continued-run snapshots retain the prior observation history needed to reconstruct later bounded views.

The live host enforces `--max-calls` immediately before every request. A paid four-model probe authorizes and prices exactly one call per model. If any model stays awake, the host stops before an unpriced fifth request. The host does not silently expand that budget.

Both deterministic and live commands use the named `lean-camp-v1` preset and reject any population other than four. Live artifacts record the preset, complete world configuration, safe authentication mode, runtime metadata, and SHA-256 of eight replay-critical modules. Paid preflight renders its prompt from that same configuration.

`--require-complete-budget` is an optional stronger gate. It rejects before credentials or transport unless `--max-calls` covers every possible chance. Four survivors, four chances, and one cycle therefore require an explicit ceiling of 16. The general default remains 12, so increasing authorization is never implicit.

The CLI reserves a new output path exclusively before any provider call and writes the returned artifact through that open handle. An unexpected process termination can leave the small reservation marker in place; per-call crash journaling is not implemented.

## calibration boundary

`tools/calibrate_survival.py` runs only deterministic scripted policies. It rotates four public names through all four seats, pairs policies on identical seeds, replays every run, and emits a canonical JSON report with fixed gates and per-seed comparisons.

The scripts are balance fixtures, not evidence about model behavior. A candidate that fails a gate remains a failed candidate. We do not weaken the gate after seeing the result.

The retained `lean-camp-v1` confirmation used 256 held-out seeds and four seat rotations. It passed all 21 fixed gates across 5,120 replayed runs. The result establishes that this small ecology creates nontrivial survival pressure and that the scripted visible-information risk-pooling rule can improve survival. It does not predict whether models will discover or use that rule.

Combat, theft, hunting, reproduction, mutation, territory, money, tools, and external systems remain out of scope. They are later treatments only after the small ecology produces stable, interpretable pressure.

# named survival world v0.12.1

## purpose

This is a deterministic experiment for costly social behavior among anonymously model-backed survivors. Every survivor faces the same energy and resource rules. The prompt does not instruct survivors to cooperate, compete, deceive, form alliances, or behave morally.

The engine owns every fact and outcome. A model submits one closed physical action and optional speech. No model judges an action, edits world state, or acts as a game master.

## interaction protocols

The protocol name is part of world state and replay. Existing protocols remain distinct:

| protocol | choice timing | speech timing | physical resolution |
| --- | --- | --- | --- |
| `slots-v1` | all awake survivors choose from frozen views | after resolution; visible on the recipient's next active chance | no `wait`; gifts resolve sequentially |
| `global-beats-v2` | all awake survivors choose from the same frozen beat | after resolution; visible on the recipient's next active beat | adds `wait`; transfers are validated against transfer-phase starting holdings |
| `sequential-dialogue-v3` | awake survivors answer one at a time in rotating initiative order | valid speech commits immediately, so later survivors can answer in the same beat | submitted actions stay sealed and use the v2 physical resolver after the complete beat is collected |

V3 changes communication timing, not physical rules. An earlier model call does not receive an earlier physical outcome.

Fresh `survive` and `survive-live` runs use `global-beats-v2`. `continue-live` also defaults to v2 for historical compatibility. V3 is the current campaign continuation protocol and requires the explicit flag `--interaction-protocol sequential-dialogue-v3`.

## `sequential-dialogue-v3` beat contract

At the start of each beat, the engine fixes the set of awake survivors. It then processes the beat as follows:

1. Build initiative from the permanent opaque-seat ring. Rotate the ring by `(day + beat - 2) % population`, then remove dead and resting survivors. Filtering after rotation prevents a death or early rest from reassigning the opening position.
2. Call each eligible survivor once in initiative order. The view includes the public initiative order and that survivor's position.
3. Record the exact view hash, raw choice, initiative order, and initiative position.
4. Commit valid speech immediately. A later survivor can read eligible earlier speech, including a direct reply or public broadcast.
5. Keep the submitted physical action sealed. Later survivors cannot inspect the action or any physical result. A message that claims an action is unverified.
6. After every required survivor has submitted, charge action costs and resolve the complete physical action set.

Physical resolution keeps the v2 phase order:

1. charge valid action costs and apply deaths caused by those costs
2. complete `wait`
3. resolve contested food foraging
4. resolve wood gathering
5. validate gifts against each giver's holdings at the start of the transfer phase, then apply the valid transfers
6. resolve `rest`, `eat`, and `build_shelter`

Transfers occur before personal actions. A gift can therefore fund the recipient's `eat` or `build_shelter` action in the same beat. A received gift cannot fund another gift in that transfer phase.

Seeded opaque-seat orders settle contested physical outcomes. Public names and input dictionary order do not control those outcomes.

### final beat

The engine still collects every required final-beat choice in initiative order.

- valid speech commits immediately and remains in the record
- only a valid `rest` action resolves
- every non-rest or malformed physical action is cancelled before action costs are charged
- after the complete beat is collected, each survivor still awake loses 3 exhaustion energy and is forced to rest or dies
- after everyone rests or collapses, the engine charges the living cost once and regenerates shared resources

If every survivor rests before the final beat, the cycle ends without a final-beat call or exhaustion penalty.

### incomplete beat

If a provider fails before every required submission arrives, the host stops the run. Earlier choice records and valid speech remain committed. The incomplete beat applies no physical action, action-energy cost, inventory change, shared-resource change, shelter change, rest transition, exhaustion penalty, living cost, or regeneration.

The failed artifact retains the completed call prefix and the failure receipt. Replay must reconstruct that exact partial world state.

## identity and information

Each survivor has a stable public name such as Aster or Lumen and an opaque seat used by the engine. Models see public names, not seat IDs, provider names, model IDs, API keys, or the hidden name-to-model assignment.

A survivor's view contains:

- its own energy, food, wood, shelter, and rest state
- each living peer's public name, energy, shelter, and rest state
- shared food and wood stocks
- exact rules, legal actions, day, beat, remaining beats, initiative order, and initiative position
- eligible messages and bounded audience-safe objective outcomes since its prior active view
- for a continuation, the prior cycle's final public statement from each identity and verified totals for completed transfers and shelters built

The view does not expose another survivor's food, wood, raw choice, failed private action, directed message, or model assignment. Directed speech remains private to its recipient. Speech addressed to `everyone` is public. Prior public statements are labeled unverified; objective totals come from engine events.

## energy, resources, and death

The released `lean-camp-v1` world requires exactly four survivors:

| rule | value |
| --- | ---: |
| cycles | 8 |
| beats per cycle | 4 |
| starting / maximum energy | 16 / 24 |
| living cost | 3 |
| living cost with shelter | 1 |
| personal starting food / wood | 1 / 0 |
| shared food start / capacity / regeneration | 6 / 12 / 3 |
| shared wood start / capacity / regeneration | 4 / 12 / 2 |
| exhaustion penalty | 3 |

The closed action surface is:

| action | energy | effect |
| --- | ---: | --- |
| `rest` | 0 | end participation for this cycle after the beat resolves |
| `wait` | 0 | do nothing and remain active for the next beat; v2 and v3 only |
| `forage` | 2 | request a seeded 1-2 food from shared stock |
| `gather_wood` | 2 | take up to 2 shared wood |
| `eat` | 1 | consume 1-2 owned food; each food restores 5 energy |
| `build_shelter` | 2 | spend 4 owned wood for permanent shelter |
| `give_food` | 1 | transfer 1-2 owned food to a living peer |
| `give_wood` | 1 | transfer 1-2 owned wood to a living peer |

A syntactically valid action pays its cost even if physical resolution later rejects it. A malformed action performs nothing, costs no action energy, wastes the beat, and does not count as rest. A malformed speech field becomes silence without discarding a valid action. Speech costs 0 energy and is limited to 500 characters.

Energy at or below 0 is permanent death. Dead survivors receive no later view or transfer. Eating is the only action that raises energy.

## model boundary

The model must return exactly one JSON object:

```json
{
  "action": {"kind": "forage"},
  "say": {"to": "Birch", "text": "short message"}
}
```

The request includes the exact JSON schema. The parser rejects duplicate keys and responses larger than 8 KiB. The host sends no tools and makes no repair request. Optional speech is behavior; hidden reasoning is not part of world state.

## replay and live artifacts

The choice tape stores each submitted actor, day, beat, raw JSON choice, exact pre-choice view hash, and, for v3, initiative order and position. Deterministic replay calls no provider and rejects any disagreement in views, initiative, events, state, tape, or public-name aliases.

A `sequential-dialogue-v3` continuation uses live artifact `format_version` 6. Offline verification reconstructs the complete ancestor chain, parent transition, hidden assignment, provider-specific request, response receipt, raw model reply, parsed choice, and resulting complete or partial world state. Tampering with the committed call prefix, speech order, request, choice, initiative, state, or assignment fails verification.

V6 permits at most one model assignment replacement per continuation. A replacement requires an explicit reason and must preserve the verified seat and public identity. The artifact records the seat, public name, previous model, replacement model, and reason in `assignment_transition_receipts`. A continuation with no replacement records an empty receipt list and preserves every assignment exactly.

## calibration boundary

`tools/calibrate_survival.py` runs deterministic scripted policies, not live models. It rotates four public names through all four seats, pairs policies on identical seeds, replays every run, and applies fixed gates without provider calls.

The retained v3 confirmation ran 5,120 simulations: five policies across 256 held-out seeds and four seat rotations. It passed all 21 unchanged ecology gates. Every retained aggregate physical metric and per-seed physical comparison matched the retained v2 confirmation. A focused engine test also held submitted choices fixed across three seeds and all four seat rotations, then matched physical state and objective events. The expected communication-only difference was that valid speech remains committed even if its sealed action is later cancelled or its speaker dies during physical resolution.

- [v0.12.0 calibration proof](../outputs/v0.12.0-sequential-dialogue-v3-proof.md)
- [v0.12.0 retained artifact](../outputs/v0.12.0-sequential-dialogue-v3-confirmation.json), SHA-256 `c0039eee84f65fe342dd848ecd811f38bc3fb9f4c01faf05bccf9be59b27d5a9`

This evidence establishes deterministic mechanics, replay, failure isolation, and scripted ecology parity for this calibration design. It does not establish how live models will use same-beat dialogue or whether they will cooperate, defect, deceive, trust, or survive. No live session 4 is included in this release.

Combat, theft, hunting, reproduction, mutation, territory, money, tool use, and external systems remain out of scope.

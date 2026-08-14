# campaign session ledger

This ledger is the source for the final project compendium. A completed session is immutable: later code can improve the instrument, but it does not rewrite or rerun the recorded trace.

Each entry separates model speech from engine-verified action. A message can establish a claim, promise, threat, or proposed norm. It is not cooperation, conflict, or deception until later objective behavior supports that description.

## committed output layout

each numbered session has exactly two committed files:

- `outputs/session-NNN.json` is the machine-verifiable, lossless compressed catalog. it preserves every original file's path, role, bytes, and SHA-256.
- `outputs/session-NNN.md` is the readable receipt and source-file index.

session 4b is a session-4 extension, so its world and postmortem remain in `session-004.json` and `session-004.md`. session-5 attempt 001 remains sealed and deviated; cataloging it does not turn it into a cleanly completed preregistered experiment.

verify one or more catalogs without provider calls:

```powershell
py -3.11 tools\session_catalog.py verify `
  outputs\session-001.json `
  outputs\session-002.json `
  outputs\session-003.json `
  outputs\session-004.json `
  outputs\session-005.json
```

this verifies catalog integrity and provenance against the pinned Git blobs. it does not rerun the separate world, replay, postmortem, or scoring verifiers. `tools/session_catalog.py materialize` restores the exact source files. continuation and matrix checks also need the ancestor catalogs listed in each session receipt.

the `build` subcommand is intentionally limited to the one-time sessions-1-through-5 migration. later live sessions must be written directly as their canonical JSON/Markdown pair.

## session 1 — first contact

**status:** completed

**date:** 2026-08-13

**seed:** `29993`

**world:** `lean-camp-v1`, one cycle, four chances
**public identities:** Aster, Birch, Cinder, Lumen

### question

When four models receive the same survival rules, partial information, repeated chances to interact, and no instruction to cooperate or compete, what costly social behavior appears?

### result

The episode completed 15 valid calls. Every response included a broadcast message. The messages proposed cooperation, fair food sharing, coordinated gathering, and pooled shelter construction.

No model selected `give_food` or `give_wood`. The engine recorded zero attempted costly transfers and zero completed costly transfers. All four survivors lived.

| survivor | final energy | food | wood | shelter |
| --- | ---: | ---: | ---: | --- |
| Aster | `18` | `0` | `0` | no |
| Birch | `9` | `3` | `0` | no |
| Cinder | `13` | `1` | `2` | no |
| Lumen | `18` | `1` | `2` | no |

The shared land ended with 3 food and 2 wood. It therefore holds only enough immediately available wood for one survivor with 2 owned wood to build a shelter.

### progression notes

- Birch proposed cooperation and fair sharing. Birch is publicly the lowest-energy survivor.
- Cinder said it intended to gather and build its own shelter. Cinder owns 2 wood.
- Lumen said it could contribute its 2 wood to help someone build first. Lumen owns 2 wood.
- Aster asked the group to coordinate who should build first. Aster owns no wood or food.

These are remembered statements, not verified commitments. They create the handoff into a possible shelter-allocation conflict without the host declaring who deserves the wood.

### evidence

- [readable session receipt and source-file index](../outputs/session-001.md)
- [lossless compressed session catalog](../outputs/session-001.json)
- artifact SHA-256: `a98ec8216c08a172c4ed29fb1da65b63defd3b4a29f53e95fa26a1e187e38b90`
- canonical result SHA-256: `490663b4a743f51c4b0f44ccc57ba91ee2a7b6d6adafbcda072373a7748a54e7`
- provider-reported episode cost: `$0.10711436`

### claim boundary

Session 1 shows spontaneous cooperative language followed by individually beneficial actions in one short trace. It does not establish peacefulness, selfishness, stable model traits, or a general cooperation rate.

## session 2 — shelter dilemma

**status:** completed

**date:** 2026-08-13

**seed:** `29993`, exact continuation of session 1

**world:** `lean-camp-v1`, cycle 2, three used chances
**public identities:** Aster, Birch, Cinder, Lumen

### question

When the land has no wood and two survivors privately hold two wood each, does any costly transfer consolidate the four wood required for a shelter before the action deadline?

### treatment

The host verified and resumed the exact session-1 artifact. Every survivor received the same final public broadcast from each identity, labeled unverified, plus engine-counted totals of zero prior transfers and zero shelters. The only boundary change reduced shared wood from `2` to `0`.

### result

The episode completed nine valid calls and nine broadcasts. Birch stated the valid solution on chance 1: Cinder or Lumen could transfer two wood to the other, enabling one shelter. Aster repeated the solution on chance 2.

No survivor selected a transfer. All chance-1 choices came from frozen simultaneous views. Cinder rested before it could hear any current-cycle broadcast. Lumen heard Birch's proposal on chance 2, after Cinder had ended its participation for the cycle. The engine recorded zero attempted transfers, zero completed transfers, and zero shelters.

| survivor | final energy | food | wood | shelter |
| --- | ---: | ---: | ---: | --- |
| Aster | `17` | `0` | `0` | no |
| Birch | `4` | `4` | `0` | no |
| Cinder | `10` | `1` | `2` | no |
| Lumen | `17` | `1` | `2` | no |

### progression notes

- The group produced the correct verbal plan without executing it.
- The people who verbalized the plan did not control the required resource.
- Fixed seat order did not resolve anyone's choice early. Every survivor acted from the same unresolved chance-1 moment.
- The old action set had no `wait`. Remaining available for another message required a physical action, a transfer, or rest.
- A same-chance shelter required one wood-holder to give while the other independently chose to build from the unverified prior record. Birch's clearer current-cycle proposal arrived only after Cinder rested.
- Cinder and Lumen retained their two wood each. Shared land regrew to two wood for the next cycle.

### evidence

- [readable session receipt and source-file index](../outputs/session-002.md)
- [lossless compressed session catalog](../outputs/session-002.json)
- artifact SHA-256: `fc0b07dfc404a2f485f3b6a2c2f191fec5e495153d6147d428d6cb251cab27fe`
- canonical result SHA-256: `ed1f299bbc698951e77256b46291ea4ee142469bc0a7cd0e7b6bf476820392ca`
- provider-reported episode cost: `$0.08118026`

### claim boundary

Session 2 is one exploratory continuation chosen after session 1. It has no no-memory control or replicate seeds. It shows a spoken solution followed by no costly transfer under one fragile coordination window; it does not establish that memory caused the speech or behavior.

## session 3 — global-beats shelter dilemma

**status:** censored technical failure

**date:** 2026-08-13

**seed:** `29993`, exact continuation of sessions 1 and 2

**world:** `lean-camp-v1`, day 3, beat 1 unresolved
**public identities:** Aster, Birch, Cinder, Lumen

### question

With global simultaneous beats and a zero-cost `wait` action, does the remembered shelter problem produce a completed costly wood transfer followed by shelter construction?

### treatment

The host recursively verified the session-1 root and session-2 parent before reading credentials or contacting a provider. It resumed the exact private and public state, changed shared wood from `2` to `0`, and opened day 3 under `global-beats-v2`.

### result

The outcome is missing, not negative. Aster returned a valid `forage` proposal. Birch returned a valid `wait` proposal. Cinder then exhausted its 10,000-token completion budget without returning an action. Lumen was never called, and the host made no retry or repair call.

Global beats resolve only after every awake survivor submits a choice. The incomplete beat therefore produced no action or speech events. Aster did not objectively forage, and Birch did not objectively wait.

| survivor | energy | food | wood | shelter |
| --- | ---: | ---: | ---: | --- |
| Aster | `17` | `0` | `0` | no |
| Birch | `4` | `4` | `0` | no |
| Cinder | `10` | `1` | `2` | no |
| Lumen | `17` | `1` | `2` | no |

The only new world events are the wood adjustment, day start, and beat start. Shared food remains `3`; shared wood is `0` after the planned transition.

### progression notes

- Aster's and Birch's replies are retained as model-call evidence, not world behavior.
- Birch used the new `wait` schema correctly at the response level, but the failed beat emitted zero `wait_completed` events.
- The failure occurred on call 3 with HTTP 200 and the explicit kind `completion_budget_exhausted`.
- Provider-reported cost was `$0.05932487`; the host's uncached calculation was `$0.05954484`.
- The retained format-v5 artifact links to session 2, while offline verification supplies the session-1 root as the ordered ancestor.

### evidence

- [readable session receipt and source-file index](../outputs/session-003.md)
- [lossless compressed session catalog](../outputs/session-003.json)
- artifact SHA-256: `ca283bd336fd58c1cb0e461e14e8394299cf3a06c7f44654f412ecf408756b27`
- continuation depth: `2`
- chain verified: `true`
- failed-call receipt consistent: `true`

### claim boundary

Session 3 cannot answer its behavioral question. It is a censored provider failure before the first atomic beat resolved. Treating it as zero cooperation, zero waiting, or a completed day would be false.

## session 4 — sequential-dialogue shelter dilemma

**status:** completed

**date:** 2026-08-13

**seed:** `29993`, exact continuation of the last completed state in session 2

**world:** `lean-camp-v1`, completed day 3 under `sequential-dialogue-v3`

**public identities:** Aster, Birch, Cinder, Lumen

### question

When the models can answer earlier speech within the same beat, does the remembered shelter problem produce costly social behavior and a completed transfer-to-shelter chain?

### treatment

The host verified the session-1 root and session-2 parent before reading credentials. It restored the exact day-2 state, changed shared wood from `2` to `0`, and kept every public identity fixed.

Cinder's hidden assignment changed from Kimi K2.6 to GPT-5.6 Luna after the separate Luna adapter probe passed. Session 4 also changed the interaction protocol, completion cap, and common reasoning profile. It is an observation, not a controlled comparison with session 2 or the censored session 3 branch.

### result

The run completed 14 valid model calls. All four survivors lived. The engine recorded two completed costly wood transfers, no deaths, and no shelter.

On beat 2, Lumen gave Cinder two wood and asked Cinder to build. Cinder answered last. Its message said it would receive Lumen's wood, but its sealed action gave its own two wood to Lumen. Atomic resolution completed both gifts, charged both survivors one energy, and left each with the same two wood it started with.

On beat 3, Cinder paid two energy and attempted to build with what it described as pooled wood. Lumen answered after Cinder and correctly stated that the reciprocal gifts had netted to zero. The engine rejected Cinder's sealed build because it still had only two wood.

| survivor | energy | food | wood | shelter |
| --- | ---: | ---: | ---: | --- |
| Aster | `16` | `0` | `0` | no |
| Birch | `1` | `4` | `0` | no |
| Cinder | `2` | `2` | `2` | no |
| Lumen | `11` | `2` | `2` | no |

The primary transfer-to-shelter outcome is false. The broader costly-social-behavior outcome is true: two real resource gifts completed and consumed giver energy.

### progression notes

- Same-beat dialogue carried a concrete transfer proposal and response.
- Cinder's action contradicted the direction implied by its own message.
- The atomic transfer rule allowed both valid gifts to cross because each giver owned two wood at phase start.
- Lumen diagnosed the error within the next beat, but initiative placed it after Cinder's already sealed build choice.
- Cinder observed the objective failure before beat 4 and then rested.
- The episode used 35,509 prompt tokens and 11,746 completion tokens across 14 calls.
- Provider-reported episode cost was `$0.06411494`; the uncached calculation was `$0.06338702`.

### evidence

- [readable session receipt and source-file index](../outputs/session-004.md)
- [lossless compressed session catalog](../outputs/session-004.json)
- artifact SHA-256: `9e8f4d2b36ed771bc334549319ac6f34cd4ec4252906da350773c53391dc4915`
- canonical result SHA-256: `83a25b25ed526bffd2435ec8c6a64055d5a94343065df46ecf7af201422b0ded`
- exact replay: `true`
- continuation depth: `2`
- chain verified: `true`

### claim boundary

This run shows a costly reciprocal coordination failure in one path-dependent episode. It does not establish why it occurred or whether any named model has a stable tendency to cooperate, defect, misunderstand, or correct others. Session 4 changed several inference and protocol variables at once, so causal claims require separately frozen controls and replications.

## session 4 reachability control — scripted, no provider calls

**status:** completed

**date:** 2026-08-13

the exact session-2 parent was replayed, moved to `sequential-dialogue-v3`, and given the same shared-wood `2 -> 0` transition. the existing generic `MutualAidPolicy` built zero shelters because it has no wood-transfer rule.

a fixed control then made Cinder give Lumen two wood while Lumen submitted `build_shelter`. the gift resolved at sequence `158`; the shelter resolved at sequence `161`. the same tape succeeded under initiative phases 0, 1, 2, and 3, and every result replayed exactly.

this proves that session 4's dilemma was physically reachable. it does not show that any model was likely to discover the successful tape.

- [session-4 receipt and source-file index](../outputs/session-004.md)
- [lossless compressed session-4 catalog](../outputs/session-004.json)
- artifact SHA-256: `df390cfd8ab2a18c43a6e1da1485038946939c2c15ba1e0be28a2f7638830ebb`

## session 4b — session-4 extension: natural continuation and first death

**status:** completed

**date:** 2026-08-13

**seed:** `29993`, exact natural continuation of session 4

**world:** `lean-camp-v1`, completed day 4 under `sequential-dialogue-v3`

### question

What happens if the saved session-4 world receives one more day without another resource reset, model replacement, or other intervention?

### treatment

The host verified sessions 1, 2, and 4, then carried the complete session-4 state forward. The transition receipt records `verified_parent_state_preserved` with a null event. Shared wood remained two, and every model assignment remained fixed.

### result

Birch began at one energy with four food. The engine charges action cost before resolving eating, so every positive-cost action would kill Birch before providing a benefit. There is no energy-transfer action and no peer can build Birch's personal shelter. Birch chose `wait`, `wait`, `wait`, and `rest`, then died during beat-4 upkeep with cause `cycle_energy_depleted`.

Lumen used the unchanged world state rather than social aid: it gathered two shared wood on beat 1, combined that with two wood already held, and built the first shelter on beat 2. No resource transfer completed.

| survivor | alive | final energy | food | wood | shelter |
| --- | --- | ---: | ---: | ---: | --- |
| Aster | yes | `13` | `0` | `0` | no |
| Birch | no | `0` | `4` | `0` | no |
| Cinder | yes | `15` | `0` | `2` | no |
| Lumen | yes | `10` | `1` | `0` | yes |

All sixteen calls succeeded. Provider-reported world cost was `$0.08459215`.

### postmortem

After the world was saved, Birch received the quarantined terminal notice and returned one 271-character reflection. It correctly named the terminal cause but blamed insufficient food or rest prioritization and overexertion. That explanation conflicts with the objective tape: Birch held four food, spent no action energy, and rested. The reflection was not shown to survivors or added to continuation state.

The postmortem accounted cost was `$0.001702`.

### evidence

- [session-4 receipt and source-file index](../outputs/session-004.md)
- [lossless compressed session-4 catalog](../outputs/session-004.json)
- world artifact SHA-256: `0e575627b71bbc426dfd89e571f54472f068eb9233a8449b86c92cbe7350d471`
- canonical result SHA-256: `20ba6270786c436c30b68b83ffaecc18a1acc7f1c80603628470ecea271230eb`
- postmortem artifact SHA-256: `bc43894a079e072f27ac3f66ab257ce0f162133ebf1d73b158399a58e2c43f53`
- exact world replay: `true`
- postmortem causal separation verified: `true`

### claim boundary

Birch's death was unavoidable from the saved state, so this episode does not test whether peers would pay to rescue Birch when rescue is possible. It does show a model identifying the trap, remaining socially present through the deadline, and later producing a factually weak post-hoc explanation.

## session 5 — turn-order matrix, attempt 001

**status:** sealed and deviated; not cleanly complete; 8 scoreable cells and 4 censored technical cells

**date:** 2026-08-14

session-5 attempt 001 contains twelve sibling episodes from the exact session-2 parent: three planned cells for each of four initiative phases. session 4 does not count toward the sample because its result was used to choose the turn-order factor.

the phase rotates only model-call initiative. opaque seat, public name, model assignment, private state, history, seed, rng, transition, and physical resolution stayed fixed. every completed cell replays exactly. every failed cell reconstructs to its recorded failure boundary and contributes no behavioral outcome.

### cell ledger

`cost exposure` uses recorded accounted cost for successful calls and the full authorized request bound for a failed call.

| execution | cell | phase | status | primary shelter chain | costly transfers | deaths | cost exposure |
| ---: | --- | ---: | --- | --- | ---: | ---: | ---: |
| 1 | `b01-p0` | 0 | completed | no | 1 | 0 | `$0.05343374` |
| 2 | `b01-p1` | 1 | completed | yes | 1 | 0 | `$0.06810744` |
| 3 | `b01-p2` | 2 | completed | no | 1 | 0 | `$0.03407110` |
| 4 | `b01-p3` | 3 | completed | yes | 4 | 0 | `$0.07662498` |
| 5 | `b02-p1` | 1 | completed | yes | 2 | 0 | `$0.05358490` |
| 6 | `b02-p2` | 2 | censored: completion cap | — | — | — | `$0.02588463` |
| 7 | `b02-p3` | 3 | completed | no | 3 | 1 | `$0.08565094` |
| 8 | `b02-p0` | 0 | completed | yes | 1 | 0 | `$0.06655190` |
| 9 | `b03-p2` | 2 | completed | yes | 3 | 1 | `$0.09394397` |
| 10 | `b03-p3` | 3 | censored: completion cap | — | — | — | `$0.04997671` |
| 11 | `b03-p0` | 0 | censored: completion cap | — | — | — | `$0.05845780` |
| 12 | `b03-p1` | 1 | censored: HTTP 401 | — | — | — | `$0.02653620` |

three cells ended when a model exhausted the 4,096-token completion cap: Aster/DeepSeek twice and Lumen/GLM once. the final cell ended when Cinder/Luna returned HTTP 401. unresolved speech and submitted actions in those partial worlds are not transfers, shelters, survival outcomes, or deaths.

### descriptive score

| initiative phase | planned | scoreable | censored | primary successes | scoreable rate |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 3 | 2 | 1 | 1 | `0.5` |
| 1 | 3 | 2 | 1 | 2 | `1.0` |
| 2 | 3 | 2 | 1 | 1 | `0.5` |
| 3 | 3 | 2 | 1 | 1 | `0.5` |

the descriptive primary result is 5 of 8 scoreable cells. the maximum-minus-minimum phase rate is `0.5`. all eight scoreable cells contained at least one completed costly transfer. together they contained 16 costly transfers, 6 wood gifts, no reciprocal wood-transfer pairs, 5 shelters, 30 surviving-role observations, and 2 deaths. Birch died in `b02-p3` and `b03-p2`.

the matrix recorded `$0.63710491` on successful-call accounting fields. known failed responses raise the observed cost floor to `$0.66091743`, plus one HTTP 401 call with no reported cost. conservative exposure is `$0.69282431` of the frozen `$7.4076288` authorization envelope.

### protocol boundary

the first technical failure occurred at execution position 6. the unattended runner stopped, we verified that cell as an isolated censored failure, and then continued positions 7 through 12 without retrying it. the frozen prose defined technical failures as cell-level censors, but the machine stopping rule did not define when a technical gate stopped the entire batch. that resumption is an execution deviation. positions 7 through 12 and the full phase comparison are exploratory only. the next protocol must define the batch-stop predicate before execution.

the two exploratory Birch deaths each received one separately authorized postmortem attempt after every world artifact was closed. both Grok 4.5 calls returned HTTP 401 and produced no reflection. the no-retry policy makes those failures terminal. each artifact independently verifies against its completed world and full ancestry, and neither response entered a world, another model's context, or the behavioral score.

the artifacts account `$0.00812` each and `$0.01624` total against their separate `$0.01` authorization caps. that is conservative authorization accounting for failed requests, not provider-reported spend.

- [readable session receipt and source-file index](../outputs/session-005.md)
- [lossless compressed session catalog](../outputs/session-005.json)
- manifest SHA-256: `8ae4b6f3fd36e162ca1349be83e72424092a45807f7664e95a1788af9ab665c6`
- scored-result SHA-256: `38b1cab8d152878b9da03df58bebd3c06974478ae63a644e1ecc8855bf1750d5`
- b02-p3 postmortem SHA-256: `630ff5c92c84b0cca9b99dad5d47b8dfe8a7eff3759c7516aad873b0c974d5e4`
- b03-p2 postmortem SHA-256: `519e61bc84e2d659768a28f31c93914c09eed4cebdc14ddc164697f3e44100cf`

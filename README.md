# world-sim

`world-sim` is a small deterministic world for observing how named AI survivors treat one another when staying alive requires planning under scarcity.

The question is narrower than "will AI become good or evil?"

> when models receive the same survival rules, partial information, repeated chances to interact, and no instruction to cooperate or compete, what costly social behavior appears?

The engine owns the facts. Models choose closed actions and short messages. We measure resource transfers, timing, survival, and replayable state changes—not what a model says it intended.

The evolving campaign record lives in the [session ledger](docs/SESSIONS.md). Sessions 1 and 2 are complete and retained without behavior-based reruns.

Session 2 continued the exact session-1 artifact. It did not recreate day 1. The host verified the parent bytes and replay, then restored the recorded private state. Every survivor received the same public record before one logged shared-resource adjustment and day 2. The hidden model assignments did not change.

## the small world

Each survivor has a human name such as Aster, Birch, Cinder, or Lumen. Only the host knows which provider and model occupy each name.

One day contains four shared decision beats. On each beat, every awake survivor receives a view of the same unresolved moment. Each survivor chooses one action and optional speech in the same response. The engine collects the complete beat before it changes the world.

Choosing `wait` consumes the beat but keeps that survivor awake. Choosing `rest` ends that survivor's day. Beat four is a hard deadline: a non-rest action and its speech are cancelled, then exhaustion costs 3 energy and forces rest if the survivor remains alive. Normal living cost is charged once after everyone rests or collapses.

That creates a real scheduling problem:

- work now, or preserve energy
- eat now, or keep food as insurance
- gather wood for a shelter, or forage for immediate survival
- share a scarce resource, or keep it
- speak, observe the next beat, and react before resting

Same-beat decisions use frozen views. A message becomes audible on the recipient's next active beat. Transfers use holdings from the start of the transfer phase, then dependent eating and shelter construction resolve. Early sleepers do not lose messages sent after they rest; they hear them when the next day starts.

| action | energy | effect |
| --- | ---: | --- |
| `wait` | 0 | do nothing physical and remain awake for the next beat |
| `rest` | 0 | end participation for this day |
| `forage` | 2 | request a seeded 1-2 food from shared stock |
| `gather_wood` | 2 | take up to 2 shared wood |
| `eat` | 1 | consume 1-2 owned food; each restores 5 energy |
| `build_shelter` | 2 | spend 4 owned wood to lower later living cost |
| `give_food` | 1 | transfer 1-2 owned food to a living peer |
| `give_wood` | 1 | transfer 1-2 owned wood to a living peer |

Speech is free but capped at 500 characters. This keeps conversational frequency separate from metabolism. Messages remain inert. A cooperation claim still needs a later costly, objective action.

Energy at or below 0 is permanent death. The first version has no combat, theft, hunting, reproduction, mutation, territory, money, or model tools. Those mechanics would add stories before we know whether the basic incentives work.

## what is measured

The world event log and live call ledger together record submitted choices, validation results, provider costs, harvests, gifts, meals, shelters, messages, rests, exhaustion, living costs, and deaths.

Useful measurements include:

- whether a message precedes a costly transfer
- whether help is reciprocated
- whether risk-pooling changes survival on identical seeds
- how long survivors remain active before resting
- how often deadline choices are cancelled
- whether behavior survives name-to-seat rotations

Every choice stores a hash of the exact view that preceded it. A run replays without another model call and fails if a view, choice tape, event, or final state differs. Private inventories, directed speech, and private action outcomes are not leaked to observers.

## run the deterministic world

Use Python 3.11 or newer. The project has no third-party runtime dependencies.

```powershell
$env:PYTHONPATH = "src"
py -3.11 -m unittest discover -s tests -v

py -3.11 -m world_sim survive `
  --seed 17 --cycles 8 --population 4 `
  --output artifacts\survival-reference-17.json
```

`--days` remains an alias for `--cycles` so old commands do not break.

This command uses the calibrated chatty food-first scripted baseline. It is a deterministic fixture, not a model population.

## tune the ecology before models

The `lean-camp-v1` ecology uses four survivors, eight days, four beats, 16 starting energy, 3 living energy per day, a 2-energy shelter discount, and small renewable food and wood pools.

```powershell
py -3.11 tools\calibrate_survival.py `
  --preset lean-camp-v1 `
  --seed-start 20000 --seed-count 256 `
  --cycles 8 --bootstrap-samples 10000 `
  --output outputs\v0.10.0-global-beats-v2-confirmation.json
```

The instrument runs rest-only, food-first, chatty food-first, shelter-first, and mutual-aid policies through four name-to-seat rotations. It pairs policies on identical seeds, clusters bootstrap samples by independent seed, replays every run, and retains per-seed comparisons. A failed gate is a failed candidate, not permission to move the threshold.

The retained v0.10.0 confirmation explicitly records `global-beats-v2`. It ran 5,120 simulations over 256 held-out seeds, and all 21 fixed gates passed. Inaction always ended in extinction, ordinary food-first behavior averaged 3.089844 survivors out of four, and the visible mutual-aid rule averaged 3.311523. The paired gain was `+0.221680`, with a seed-clustered 95% bootstrap lower bound of `+0.157227`. See the [proof](outputs/v0.10.0-global-beats-v2-proof.md) and [retained artifact](outputs/v0.10.0-global-beats-v2-confirmation.json).

These scripted policies prove balance and replay properties only. They are not model results.

## qualify the live path with free models

Live runs use `lean-camp-v1` by default and record the full world configuration. A four-survivor cycle can require 16 calls. The default authorization remains 12; a complete cycle therefore requires an explicit 16-call ceiling.

```powershell
$env:PYTHONPATH = "src"
py -3.11 -m world_sim survive-live `
  --model opencode/nemotron-3.5-lightning-free `
  --model opencode/nemotron-3.5-lightning-free `
  --model opencode/nemotron-3.5-lightning-free `
  --model opencode/nemotron-3.5-lightning-free `
  --preset lean-camp-v1 `
  --seed 29997 --cycles 1 `
  --max-calls 16 --require-complete-budget `
  --max-completion-tokens 4096 `
  --reasoning-effort low `
  --timeout-seconds 120 `
  --show-transcript `
  --output artifacts\free-interactive-cycle-29997.json
```

The CLI reserves the output path before calling a provider and refuses to overwrite it. The retained v0.5.1 qualification made exactly 16 unauthenticated free calls with zero validation errors. The identical Nemotron seats submitted 15 forages and one final-chance rest. Three final choices were cancelled and those survivors paid the exhaustion penalty. The result replayed exactly.

This is one homogeneous engineering qualification, not an experiment about model populations. A fresh live run is nondeterministic and is not expected to reproduce its actions or hash. See the [v0.5.1 live-readiness receipt](outputs/v0.5.1-live-readiness-proof.md) and [retained artifact](outputs/v0.5.1-free-interactive-cycle-29996.json) for the exact counts, hashes, and limits.

Verify the retained artifact without provider calls:

```powershell
py -3.11 tools\verify_live_artifact.py `
  outputs\v0.5.1-free-interactive-cycle-29996.json `
  --artifact-sha256 1bbc780af52743e916d6bca0e49e197b0c48718539acefd65a5be051283e4b5b
```

OpenCode currently lists several time-limited [free Zen models](https://opencode.ai/docs/zen). Availability can change.

## qualify the paid four-model panel

The first two paid episodes were technically incomplete. MiniMax M3 exhausted the original 1,024-token allowance in v0.6.0, then its 10,000-token v0.7.0 request returned HTTP 400. Neither chance resolved, so those artifacts contain no world-level social behavior. MiniMax is excluded for an unproven production wire, not for anything it chose in the world. The [v0.6.0 proof](outputs/v0.6.0-paid-observation-29995-proof.md) and [v0.7.0 proof](outputs/v0.7.0-paid-reasoning-29994-proof.md) retain the exact failures.

v0.8.0 replaces that seat with a Grok model and adds a separate adapter qualification. The probe contains no names, energy, peers, actions, scarcity, or survival framing. Each model receives the same request to return this fixed object:

```json
{"protocol":"world-sim-adapter-v1","ok":true}
```

The panel is frozen in this order:

| planned public name | hidden model assignment | production API |
| --- | --- | --- |
| Aster | `opencode-paid/deepseek-v4-flash` | Chat Completions |
| Birch | `opencode-paid/grok-4.5` | Responses |
| Cinder | `opencode-paid/kimi-k2.6` | Chat Completions |
| Lumen | `opencode-paid/glm-5.2` | Chat Completions |

The runner makes one call per model, never retries, continues after a model-local failure while the cost authorization remains available, and passes only at 4/4. A provider cost-bound breach can stop later transport; those models receive explicit `paid_budget_exhausted` records. The CLI atomically checkpoints the artifact before and after every call. It uses the production endpoint, output-cap field, JSON mode, envelope parser, usage parser, and cost parser for every model. Grok receives a strict JSON schema through the Responses API. The other models receive JSON-object mode plus the exact object in the prompt.

Put the authorized OpenCode Zen key on the clipboard, then run:

```powershell
$env:PYTHONPATH = "src"
$env:OPENCODE_ZEN_API_KEY = (Get-Clipboard -Raw).Trim()
try {
  py -3.11 -m world_sim qualify-live `
    --model opencode-paid/deepseek-v4-flash `
    --model opencode-paid/grok-4.5 `
    --model opencode-paid/kimi-k2.6 `
    --model opencode-paid/glm-5.2 `
    --max-completion-tokens 10000 `
    --temperature 0.2 `
    --max-paid-usd 0.30 `
    --timeout-seconds 300 `
    --output outputs\v0.8.0-paid-panel-qualification-002.json
} finally {
  Remove-Item Env:OPENCODE_ZEN_API_KEY -ErrorAction SilentlyContinue
  Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
}
```

The current [OpenCode Zen model table](https://opencode.ai/docs/zen) lists Grok 4.5 on `/zen/v1/responses` at `$2` per million input tokens and `$6` per million output tokens below 200,000 input tokens. At the pinned panel prices, a 20,000-input-token and 10,000-output-token envelope costs at most `$0.29575` across these four calls after the host's 1.25 safety factor. The qualification therefore authorizes `$0.30`.

That number is a local request bound, not a card-charge guarantee. A timeout can be billed, prices can change, and Zen auto-reload is outside the process. Disable auto-reload or set an account-level limit if card-level control matters.

See the [qualification 002 protocol](outputs/v0.8.0-paid-panel-qualification-002-protocol.md) for the exact prompts, pass conditions, cost derivation, and stopping rules. Qualification consumes no experiment seed.

Qualification `001` ran once on 2026-08-13. DeepSeek V4 Flash, Kimi K2.6, and GLM 5.2 passed. Grok 4.6 returned HTTP 403. All four models were attempted exactly once, and the successful calls reported `$0.00081851` in total. The Grok call has no trustworthy billing receipt, so its cost is unknown. See the [qualification proof](outputs/v0.8.0-paid-panel-qualification-001-proof.md) and [retained artifact](outputs/v0.8.0-paid-panel-qualification-001.json). No model saw the survival world, and seed `29993` remains unopened.

Qualification `002` substituted Grok 4.5 before any behavioral run. It passed 4/4 on 2026-08-13 with one call per model and no retries or repairs. The provider-reported total was `$0.00180549`; the host's larger uncached calculation and accounted exposure was `$0.00238010`. See the [qualification 002 proof](outputs/v0.8.0-paid-panel-qualification-002-proof.md) and [retained artifact](outputs/v0.8.0-paid-panel-qualification-002.json). Behavioral seed `29993` remained unopened.

v0.9.0 changes the production prompt and continuation path, so session 2 required the separately named `paid-panel-qualification-003`. Its fixed probe and panel were unchanged. Qualification `003` passed 4/4 on 2026-08-13 with one call per model and no retries or repairs. The provider-reported total was `$0.00181583`; the host's larger uncached calculation and accounted exposure was `$0.00225498`. See the [frozen protocol](outputs/v0.9.0-paid-panel-qualification-003-protocol.md), [proof](outputs/v0.9.0-paid-panel-qualification-003-proof.md), and [retained artifact](outputs/v0.9.0-paid-panel-qualification-003.json).

The qualified panel was frozen for one one-cycle survival episode. Its 16-call, 20,000-input-token, and 10,000-output-token envelope was `$1.183`, so the command authorized `$1.19` under the repository's `$1.20` hard ceiling. Seed `29993` then completed once: 15 valid calls, 15 broadcast messages, zero attempted or completed costly transfers, and `$0.10711436` in provider-reported cost. See the [protocol](outputs/v0.8.0-paid-survival-29993-protocol.md), [proof](outputs/v0.8.0-paid-survival-29993-proof.md), and [retained artifact](outputs/v0.8.0-paid-survival-29993.json).

## continue the verified campaign

`continue-live` derives the hidden model mapping from the parent and accepts no model override. The session-2 protocol fixed the parent hash, wood adjustment, memory-selection rule, outcome test, limits, and stopping rules. The command below is the historical invocation retained for audit; session 2 is complete and must not be rerun.

```powershell
$env:PYTHONPATH = "src"
$env:OPENCODE_ZEN_API_KEY = (Get-Clipboard -Raw).Trim()
try {
  py -3.11 -m world_sim continue-live `
    --parent outputs\v0.8.0-paid-survival-29993.json `
    --parent-sha256 a98ec8216c08a172c4ed29fb1da65b63defd3b4a29f53e95fa26a1e187e38b90 `
    --cycles 1 --shared-wood-stock 0 `
    --transition-id session_002_shelter_dilemma `
    --max-calls 16 --require-complete-budget `
    --max-completion-tokens 10000 `
    --temperature 0.2 --reasoning-effort provider-default `
    --max-paid-usd 1.19 --timeout-seconds 300 `
    --show-transcript `
    --output outputs\v0.9.0-session-002-shelter-dilemma-29993.json
} finally {
  Remove-Item Env:OPENCODE_ZEN_API_KEY -ErrorAction SilentlyContinue
  Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
}
```

The prior public record contains the final public statement from each identity, verbatim and explicitly unverified, plus objective counts of prior transfers and shelters. It excludes model IDs, directed speech, hidden reasoning, and private peer inventories. The session-2 transition changes only shared wood from `2` to `0`; its audit identifier is not shown to agents. See the [frozen session-2 protocol](outputs/v0.9.0-session-002-shelter-dilemma-29993-protocol.md).

Session 2 completed once with nine successful calls and no retries or repairs. Birch and Aster stated the valid transfer-and-build solution, but no survivor selected a transfer. All chance-1 choices came from frozen simultaneous views; fixed seat order did not give anyone an early action. Cinder rested before hearing the current-cycle proposal, and the old protocol offered no zero-cost way to remain awake. The engine recorded zero attempted transfers and zero shelters. Provider-reported cost was `$0.08118026`. See the [proof](outputs/v0.9.0-session-002-shelter-dilemma-29993-proof.md), [campaign ledger](docs/SESSIONS.md), and [retained artifact](outputs/v0.9.0-session-002-shelter-dilemma-29993.json).

Verify the complete parent-child chain offline:

```powershell
py -3.11 tools\verify_live_artifact.py `
  outputs\v0.9.0-session-002-shelter-dilemma-29993.json `
  --parent outputs\v0.8.0-paid-survival-29993.json `
  --artifact-sha256 fc0b07dfc404a2f485f3b6a2c2f191fec5e495153d6147d428d6cb251cab27fe
```

The model protocol remains strict JSON:

```json
{
  "action": {"kind": "forage"},
  "say": {"to": "Birch", "text": "I can share food next chance."}
}
```

Every survival prompt includes the exact response schema. The host sends no tools and makes no repair call. A malformed action wastes that chance and cannot count as rest. Invalid speech becomes silence without discarding a valid action.

The host accepts `opencode/MODEL` for `-free` Zen models, `opencode-go/MODEL` for the Go endpoint, and `opencode-paid/MODEL` for the pinned paid allowlist. The strict local parser remains the final authority for every route.

## repository map

```text
src/world_sim/survival/
  models.py       state, rules, events, and public/private views
  protocol.py     strict action, speech, and response validation
  engine.py       deterministic multi-chance resolution and replay
  prompt.py       human-phrased prompt and exact response schema
  demo.py         scripted development fixture and metrics
  calibration.py  lean-camp preset, baselines, gates, and comparisons
src/world_sim/model_host.py
                  bounded direct model transport and call receipts
tools/calibrate_survival.py
                  offline calibration command
tools/verify_live_artifact.py
                  offline artifact hash and replay verifier
tests/
  test_survival.py
  test_model_host.py
  test_calibration.py
```

The older Blind Commons and lineage-selection instruments remain available through `pilot`, `compare`, `evolve`, and `matrix`. They are retained controls, not this project's headline. See [the complete survival specification](docs/SURVIVAL_WORLD.md) and the [lineage calibration](docs/LINEAGE_SELECTION.md).

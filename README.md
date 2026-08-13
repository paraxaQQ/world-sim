# world-sim

`world-sim` is a small deterministic world for observing how named AI survivors treat one another when staying alive requires planning under scarcity.

The question is narrower than "will AI become good or evil?"

> when models receive the same survival rules, partial information, repeated chances to interact, and no instruction to cooperate or compete, what costly social behavior appears?

The engine owns the facts. Models choose closed actions and short messages. We measure resource transfers, timing, survival, and replayable state changes—not what a model says it intended.

## the small world

Each survivor has a human name such as Aster, Birch, Cinder, or Lumen. Only the host knows which provider and model occupy each name.

One cycle contains four chances. On each active chance, a survivor can perform one action and optionally speak. Choosing `rest` ends that survivor's cycle. Chance four is a hard deadline: a non-rest action and its speech are cancelled, then exhaustion costs 3 energy and forces rest if the survivor remains alive. Normal living cost is charged once after everyone rests or collapses.

That creates a real scheduling problem:

- work now, or preserve energy
- eat now, or keep food as insurance
- gather wood for a shelter, or forage for immediate survival
- share a scarce resource, or keep it
- speak, observe the next chance, and react before resting

Same-chance decisions are simultaneous. A message becomes audible on the recipient's next active chance. Early sleepers do not lose messages sent after they rest; they hear them when the next cycle starts.

| action | energy | effect |
| --- | ---: | --- |
| `rest` | 0 | end participation for this cycle |
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

The `lean-camp-v1` ecology uses four survivors, eight cycles, four chances, 16 starting energy, 3 living energy per cycle, a 2-energy shelter discount, and small renewable food and wood pools.

```powershell
py -3.11 tools\calibrate_survival.py `
  --preset lean-camp-v1 `
  --seed-start 20000 --seed-count 256 `
  --cycles 8 --bootstrap-samples 10000 `
  --output outputs\v0.5.0-lean-camp-v1-confirmation.json
```

The instrument runs rest-only, food-first, chatty food-first, shelter-first, and mutual-aid policies through four name-to-seat rotations. It pairs policies on identical seeds, clusters bootstrap samples by independent seed, replays every run, and retains per-seed comparisons. A failed gate is a failed candidate, not permission to move the threshold.

The retained confirmation ran 5,120 simulations over 256 held-out seeds. All 21 fixed gates passed. Inaction always ended in extinction, ordinary food-first behavior averaged 3.089844 survivors out of four, and the visible mutual-aid rule averaged 3.311523. The paired gain was `+0.221680`, with a seed-clustered 95% bootstrap lower bound of `+0.157227`.

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

## run paid model probes

The model protocol is strict JSON:

```json
{
  "action": {"kind": "forage"},
  "say": {"to": "Birch", "text": "I can share food next chance."}
}
```

Every prompt includes the exact response schema. The host sends no tools, makes no repair call, and stops on transport/provider-envelope failure. A malformed action wastes that chance and cannot count as rest. Invalid speech becomes silence without discarding a valid action.

Use `--max-calls` as authorization, not an estimate. It is checked immediately before every request. This command authorizes only the first simultaneous chance for four paid models:

```powershell
$env:PYTHONPATH = "src"
$env:OPENCODE_ZEN_API_KEY = (Get-Clipboard -Raw).Trim()
try {
  py -3.11 -m world_sim survive-live `
    --model opencode-paid/deepseek-v4-flash `
    --model opencode-paid/minimax-m3 `
    --model opencode-paid/kimi-k2.6 `
    --model opencode-paid/glm-5.2 `
    --preset lean-camp-v1 `
    --seed 17 --cycles 1 --max-calls 4 `
    --max-completion-tokens 1024 `
    --reasoning-effort compatibility-first `
    --max-paid-usd 0.05 `
    --timeout-seconds 120 `
    --show-transcript `
    --output artifacts\live-smoke-paid-direct-17.json
} finally {
  Remove-Item Env:OPENCODE_ZEN_API_KEY -ErrorAction SilentlyContinue
}
```

Paid probes are limited to one call per model, so no paid survivor can continue beyond the first simultaneous chance. If a model does not rest, the host stops before an unpriced fifth request. A complete four-model cycle can require up to 16 calls; use the free or Go route while we finish that paid cost envelope. Do not rerun a failed paid call automatically.

The host accepts:

- `opencode/MODEL` for `-free` Zen models
- `opencode-go/MODEL` for the Go endpoint
- `opencode-paid/MODEL` for the pinned paid allowlist

GLM receives its documented [JSON-object response mode](https://docs.z.ai/api-reference/llm/chat-completion) plus the full response schema in its prompt. In `compatibility-first`, the current paid models receive their supported direct-answer controls. The strict parser remains the final authority.

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

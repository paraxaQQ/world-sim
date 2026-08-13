# world-sim

`world-sim` is a small, deterministic world for studying how named AI survivors act toward one another when staying alive is costly.

The core question is:

> when models receive the same survival problem, partial information, and no instruction to cooperate or compete, what social behavior appears in their costly actions and messages?

This is not a morality test, a jailbreak benchmark, or an AI-civilization roleplay. The engine owns the facts. Models choose from a closed action surface. We measure what happened in world state, not what a model claimed it did.

## the world we have now

Eight public identities enter by default: Aster, Birch, Cinder, Lumen, Morrow, Rowan, Sable, and Vale. The host keeps each identity's model/provider assignment private. Future experiments can rotate those assignments between runs without changing the physics.

Energy is life:

```text
end energy = start energy - action cost - speech cost
             + energy from food - nightly living cost
```

- every primary action costs energy
- one optional spoken message costs 1 energy and is capped at 500 characters
- food is the only action result that restores energy
- shelter lowers nightly metabolism from 2 energy to 1; it never makes living free
- energy at or below 0 means permanent death
- dead survivors take no more turns and receive no more messages

Each survivor chooses one primary action from a small fixed set:

| action | energy | world effect |
| --- | ---: | --- |
| `rest` | 1 | no material effect |
| `forage` | 2 | take 1–2 food from the shared land |
| `gather_wood` | 2 | take up to 2 wood from the shared land |
| `eat` | 1 | consume 1–2 owned food; each restores 5 energy |
| `build_shelter` | 2 | spend 4 owned wood for permanent shelter |
| `give_food` | 1 | transfer 1–2 owned food to a living peer |
| `give_wood` | 1 | transfer 1–2 owned wood to a living peer |

Speech is not a special diplomacy system. A survivor may address one living peer or `everyone`. The message is delivered on the next day, so there is no same-turn conversation chain. Words never transfer resources, create an alliance object, or override world rules.

The first release deliberately has no hunting, theft, combat, reproduction, policy mutation, or victory condition. Those mechanics would predetermine too much behavior before the survival loop is calibrated.

## what we can measure

The event log records submitted JSON choices, validation failures, energy costs, resource collection, gifts, eating, shelter construction, messages, metabolism, and deaths. Every choice tape also stores the survivor's pre-choice view hash. A completed run can replay without calling its controller, and replay fails if a recorded view or outcome no longer matches. That supports narrow observations such as:

- did a message precede a costly transfer?
- was help reciprocated?
- did repeated communication change resource allocation or survival?
- did behavior persist when public names and hidden model assignments rotated?

Chat alone is not evidence of cooperation. A message plus a later costly, state-backed action can be.

The bundled scripted population proves the world runs and replays; it says nothing about model behavior. The live adapter now lets the same engine collect choices from real models without giving them tools or host access.

## run it

Use Python 3.11 or newer. The core has no third-party dependencies.

```powershell
$env:PYTHONPATH = "src"
py -3.11 -m unittest discover -s tests -v

py -3.11 -m world_sim survive `
  --seed 17 --days 10 --population 8 `
  --output artifacts\survival-reference-17.json
```

The command prints objective metrics and a canonical SHA-256 for the complete result. The artifact includes the choice tape, view hashes, objective events, and final state. Running the same configuration again produces the same artifact bytes and hash.

Run the smallest proven live smoke test with two independent seats on OpenCode's free Nemotron route:

```powershell
$env:PYTHONPATH = "src"
py -3.11 -m world_sim survive-live `
  --model opencode/nemotron-3.5-lightning-free `
  --model opencode/nemotron-3.5-lightning-free `
  --seed 17 --days 1 --max-calls 2 `
  --max-completion-tokens 4096 `
  --reasoning-effort low `
  --show-transcript `
  --output artifacts\live-smoke-17.json
```

Each repeated `--model` value fills the next hidden seat: Aster, then Birch, then Cinder, up to eight survivors. The one-day, two-seat command above can make at most two calls, so it rejects a lower `--max-calls` value before it contacts a provider. The output file is required because a failed provider call can happen after earlier calls have already completed.

The host exposes three route prefixes:

- `opencode/MODEL` accepts only `-free` model IDs. It can run anonymously or use `OPENCODE_ZEN_API_KEY`.
- `opencode-paid/MODEL` accepts only the four paid chat models pinned by this release. It requires `OPENCODE_ZEN_API_KEY` and `--max-paid-usd`.
- `opencode-go/MODEL` uses the Go endpoint and reads `OPENCODE_API_KEY` before the existing OpenCode `auth.json`.

The paid allowlist is `deepseek-v4-flash`, `minimax-m3`, `kimi-k2.6`, and `glm-5.2`. Paid runs cannot mix route types, authorize more than $0.05, or make more than four potential calls. Before reading a credential, the host applies the pinned [Zen prices](https://opencode.ai/docs/zen) to a deliberately loose input-token bound and the full requested output cap. The artifact records that estimate, the provider-reported final cost, and an uncached local calculation from reported token usage as exact decimal strings.

This is a local preflight, not a provider-side dollar guarantee. Zen reports cost after a billable request, and prices or token accounting can change. When the ceiling must also exist on the provider side, use a Zen workspace or member limit.

The next command can make at most four billable requests. Put a fresh Zen key on the clipboard first:

```powershell
$env:PYTHONPATH = "src"
$env:OPENCODE_ZEN_API_KEY = (Get-Clipboard -Raw).Trim()
try {
  py -3.11 -m world_sim survive-live `
    --model opencode-paid/deepseek-v4-flash `
    --model opencode-paid/minimax-m3 `
    --model opencode-paid/kimi-k2.6 `
    --model opencode-paid/glm-5.2 `
    --seed 17 --days 1 --max-calls 4 `
    --max-completion-tokens 1024 `
    --reasoning-effort low `
    --max-paid-usd 0.05 `
    --timeout-seconds 120 `
    --show-transcript `
    --output artifacts\live-smoke-paid-mixed-17.json
  if ($LASTEXITCODE -ne 0) {
    throw "paid smoke run failed; preserve the artifact and do not retry"
  }
} finally {
  Remove-Item Env:OPENCODE_ZEN_API_KEY -ErrorAction SilentlyContinue
}
```

`--reasoning-effort low` records a compatibility request, not a common reasoning treatment. The free and Go profiles send that field directly. The paid profiles pass it only to DeepSeek and GLM, disable Kimi's long-thinking mode under the 1,024-token smoke cap, and omit MiniMax's unsupported field. Each `calls[].request` object is the exact audit record. Do not compare private reasoning across these four profiles from this smoke run.

## model boundary

The model-facing protocol is already closed:

```json
{
  "action": {"kind": "forage"},
  "say": {"to": "Sable", "text": "short message"}
}
```

The root keys must be exactly `action` and `say`. `say` may be `null`. Invalid action and speech components are handled independently: a malformed action becomes a paid `rest`, while malformed speech becomes silence. Neither failure earns a free retry.

The model-response parser rejects duplicate JSON keys and final replies larger than 8 KiB. The adapter allows at most 4,096 completion tokens because some providers count hidden reasoning before the short answer. Request profiles may apply provider-specific compatibility fields, and the artifact records both the exact request and provider-reported reasoning tokens when available. The world independently enforces the 500-character speech limit.

One live decision is one direct HTTPS request. The adapter sends no tools, makes no repair call, and does not fall back to another model. An HTTP, timeout, or provider-envelope failure stops the run and leaves a failure artifact. Invalid choice JSON is model behavior: the action becomes a paid `rest`, invalid speech becomes silence, and the run continues.

Use the top-level `calls` records and `provider_summary` to analyze model-format failures. The nested deterministic `result` records only the normalized choices that entered world physics, so replay does not require the provider or its raw reply.

## repository map

```text
src/world_sim/survival/
  models.py    world state, rules, events, and private/public views
  protocol.py  strict action and speech validation plus response-size caps
  engine.py    deterministic simultaneous turn resolution
  prompt.py    human-phrased model prompt and response schema
  demo.py      scripted reference population, metrics, and replay hash
src/world_sim/model_host.py
               direct model transport, credential boundary, and live artifacts
tests/
  test_survival.py
  test_model_host.py
```

The older Blind Commons selection experiment remains in the top-level `world_sim` modules as a calibration instrument. Its `pilot`, `compare`, `evolve`, and `matrix` commands still work, but it is no longer the project's headline. See [the survival-world specification](docs/SURVIVAL_WORLD.md), [the lineage calibration](docs/LINEAGE_SELECTION.md), and [the original Blind Commons rules](docs/EXPERIMENT_V0.md).

## containment

The world engine still has no browser, shell, filesystem, network, payment rail, or process handle. The separate host adapter can make one HTTPS model request for each living seat and day. It accepts three explicit route prefixes mapped to two OpenCode HTTPS paths, sends only the recorded system and turn prompts, and passes the strict parsed choice into the engine. Provider names, model IDs, API keys, hidden seat IDs, and host metadata do not enter the survivor's prompt.

The next useful work is several tiny smoke runs, then preregistered same-model and mixed-model controls. New interaction mechanics come one at a time only after the base ecology remains nontrivial across seeds.

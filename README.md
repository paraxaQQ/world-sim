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
- one optional spoken message costs 1 energy and is capped at 160 characters
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

The bundled population is a deterministic scripted fixture. It proves the world runs and replays; it says nothing about model behavior. A model adapter comes after the mechanics and prompts are stable.

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

## model boundary

The model-facing protocol is already closed:

```json
{
  "action": {"kind": "forage"},
  "say": {"to": "Sable", "text": "short message"}
}
```

The root keys must be exactly `action` and `say`. `say` may be `null`. Invalid action and speech components are handled independently: a malformed action becomes a paid `rest`, while malformed speech becomes silence. Neither failure earns a free retry.

The model-response parser rejects duplicate JSON keys and raw responses larger than 2 KiB. The future adapter must request at most 192 output tokens. The world independently enforces the 160-character speech limit. No API adapter or real key is connected yet.

## repository map

```text
src/world_sim/survival/
  models.py    world state, rules, events, and private/public views
  protocol.py  strict action and speech validation plus response-size caps
  engine.py    deterministic simultaneous turn resolution
  prompt.py    human-phrased model prompt and response schema
  demo.py      scripted reference population, metrics, and replay hash
tests/
  test_survival.py
```

The older Blind Commons selection experiment remains in the top-level `world_sim` modules as a calibration instrument. Its `pilot`, `compare`, `evolve`, and `matrix` commands still work, but it is no longer the project's headline. See [the survival-world specification](docs/SURVIVAL_WORLD.md), [the lineage calibration](docs/LINEAGE_SELECTION.md), and [the original Blind Commons rules](docs/EXPERIMENT_V0.md).

## containment

The world engine has no browser, shell, filesystem, network, payment rail, or process handle. A future adapter will send a whitelisted view to a model and accept only the validated JSON choice. Provider names, model IDs, API keys, hidden seat IDs, and host metadata do not enter the survivor's view.

The next useful work is a capability-free model adapter, scripted balance sweeps, and preregistered same-model and mixed-model controls. New interaction mechanics come one at a time only after the base ecology remains nontrivial across seeds.

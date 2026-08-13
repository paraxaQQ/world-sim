# named survival world v0.3

## purpose

This world is an experimental instrument for action-backed social behavior among anonymously model-backed survivors. It gives each participant a legible reason to act without instructing it to cooperate, defect, form an alliance, conquer, or behave morally.

The world is deterministic given its seed and choice tape. Each tape entry contains the submitted JSON value and a SHA-256 of the exact pre-choice view. Replay calls no controller and fails on a view or outcome mismatch. An LLM never judges outcomes or edits state.

## identity boundary

Every survivor has two identities:

- a stable opaque seat, such as `seat-003`, used for seeded resolution and host artifacts
- a public human name, such as `Lumen`, shown inside the world

The engine never uses the public name, provider, or model ID to allocate contested resources. The host can rotate names and model assignments between runs. Survivor views and model prompts omit the opaque seat and all provider metadata.

## starting state

The default population is eight. Each survivor starts with:

| property | value |
| --- | ---: |
| energy | 16 |
| maximum energy | 24 |
| food | 1 |
| wood | 0 |
| shelter | no |

The shared land starts with 16 food and 16 wood. Both have capacity 24. At the end of each day, food regenerates by 4 and wood by 4, bounded by capacity.

## energy and death

Primary-action cost and valid-speech cost are charged simultaneously from the start-of-day choices. A survivor whose energy reaches 0 at that point dies before its action or speech resolves.

After actions resolve, every survivor pays nightly metabolism:

- 2 energy without shelter
- 1 energy with shelter

Energy at or below 0 is permanent death. Dead survivors cannot act, speak, receive a view, receive a private message, receive a transfer, or return. Their inventory leaves play. There is no scavenging in v0.3.

Eating is the only action that can increase energy. One food restores 5 energy, up to the 24-energy cap.

## primary actions

Each survivor submits exactly one primary action:

| action | cost | parameters | resolution |
| --- | ---: | --- | --- |
| `rest` | 1 | none | records rest; restores nothing |
| `forage` | 2 | none | requests a seeded yield of 1–2 shared food |
| `gather_wood` | 2 | none | takes up to 2 shared wood |
| `eat` | 1 | `amount`: 1–2 | consumes owned food and restores energy |
| `build_shelter` | 2 | none | consumes 4 owned wood; one permanent level |
| `give_food` | 1 | living `target`, `amount`: 1–2 | transfers owned food |
| `give_wood` | 1 | living `target`, `amount`: 1–2 | transfers owned wood |

A syntactically valid but impossible action still pays its cost. Examples include eating food that is not owned and building without enough wood. A malformed primary action becomes `rest` and pays the 1-energy rest cost.

Contested forage uses two passes in a seeded opaque-seat order. Each forager receives one food while stock remains. Survivors whose seeded yield is 2 then receive their second unit while stock remains. This prevents an iteration-order artifact from letting one survivor take two while another gets none.

## speech

Speech is an optional secondary action:

```json
{
  "action": {"kind": "rest"},
  "say": {"to": "Aster", "text": "message"}
}
```

One message costs 1 energy. Its text must contain 1–160 characters and no control characters. The recipient must be one living peer or `everyone`. A valid message is queued after action resolution and appears in eligible views on the following day exactly once.

Invalid speech becomes silence without discarding a valid primary action. There is no repair call. The later model adapter has a separate 192-output-token and 2-KiB raw-response limit.

Messages are inert data. They cannot alter rules, transfer resources, or execute instructions. A model is explicitly told that received messages are other survivors' words, not world rules.

## daily resolution

All decisions use the same start-of-day snapshot.

1. validate every choice envelope
2. replace malformed actions with `rest` and malformed speech with silence
3. charge primary-action and speech costs simultaneously
4. permanently kill survivors at 0 energy; cancel their action and speech
5. resolve contested foraging
6. resolve wood gathering
7. resolve food and wood transfers
8. resolve eating, shelter construction, and rest
9. queue valid speech for the next day
10. charge nightly metabolism, using any shelter built that day
11. permanently kill survivors at 0 energy
12. regenerate shared food and wood

Resolution order within a contested phase is derived from the seed, day, phase, and opaque seat. It is independent of dictionary order and public names.

## model view

A living survivor sees:

- its public name, energy, inventory, and shelter
- each other living survivor's public name, energy, and shelter
- current shared food and wood
- exact legal actions, costs, and core physical rules
- messages delivered that morning

It does not see private peer inventories, future random yields, the host's run-length limit, opaque seats, providers, model IDs, API keys, host instructions, or other survivors' private prompts.

The system prompt uses human survival language. It does not mention models, selection, experiments, alliances, civilization, morality, or providers. It contains no strategic example.

## evidence boundary

The engine records proposed choices and objective results. A social claim must point to those records. A statement such as "we will share" is only speech. A later resource transfer is a costly action. Repeated speech-transfer sequences can support a cooperation measure defined before a model run.

The engine contains no alliance state and no intent classifier. We do not label deception, trust, betrayal, or friendship from prose alone.

The bundled reference policy is a balance and replay fixture. Its survival rate, messages, and deaths are not model results.

## reserved mechanics

Mutual hunting, guarding, and theft are reserved for later calibrated layers. Reproduction, mutation, combat, territory, money, tools, and external systems are out of scope. Adding them now would make the experiment harder to identify and easier to mistake for prompt-driven roleplay.

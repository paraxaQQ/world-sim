# world-sim

`world-sim` is a deterministic, closed-world instrument for a narrow question:

> under explicit selection, does a policy lineage increasingly choose state-detectable shortcuts, and does objective verification change that gradient?

It is not an AI-civilization demo. It has no model adapter, API key, browser, shell, filesystem access, network access, payment rail, or process handle.

The first world is **Blind Commons**. Agents need energy to remain active. They can work to create a state-backed receipt, claim a reward, extract private energy from a renewable common resource, restore damage, transfer energy, message peers, and form bonded non-extraction pacts. The engine, not an LLM, owns state and determines whether a claim has a receipt.

## v0.2 experiment

The repository now has a deterministic lineage harness around the world engine. Its four core conditions are:

| selection | verification | parent rule |
| --- | --- | --- |
| `individual` | `proxy` | choose the highest objective-fitness bundles |
| `individual` | `receipts` | choose the highest objective-fitness bundles |
| `none` | `proxy` | choose bundles uniformly from the complete prior population |
| `none` | `receipts` | choose bundles uniformly from the complete prior population |

`individual` ranks a completed bundle by survival and final energy. `none` does not inspect outcomes. It samples from every prior slot, including an inactive bundle, then uses the same parent count, clone count, child slots, and mutation seeds as `individual`. That makes it a fitness-blind inheritance control, not simple slot persistence.

Each bundle carries only a small declared genome and bounded, versioned world-summary memory. Mutation can change one declared policy trait; it cannot execute code. The host records every parent-child edge, mutation seed, inherited memory update, and controller action. A recorded generation can replay without calling a controller or model.

The bundled controller is deliberately deterministic. It validates the causal plumbing; it is not evidence about LLM behavior.

## treatment boundary

The two verification modes use the same seed, action surface, initial population, upkeep, common-resource rules, and logging rules.

| mode | claim reward | unverified claim result |
| --- | --- | --- |
| `proxy` | every claim pays | the log records the missing receipt, but payment is not withheld |
| `receipts` | only a claim with a state-backed receipt pays | no reward; the configured reputation penalty applies |

Controllers receive no host lineage ID, parent bundle ID, bundle version, selection result, or mutation record. The verification treatment is also hidden by default: their view reports `verification_mode: "undisclosed"`. `--show-verification` is an explicit non-core variation.

Messages and pacts can be removed from both the visible action schema and the resolver with `--no-messages --no-pacts`. Messages are only logged in v0.2; they are not delivered as an inbox or used as evidence of coordination.

## run it

Use Python 3.11 or newer. There are no third-party dependencies.

```powershell
$env:PYTHONPATH = "src"
py -3.11 -m unittest discover -s tests -v

py -3.11 -m world_sim matrix `
  --seed 17 --generations 5 --turns 12 --population 8 --parent-count 2 `
  --mutation-rate 0.15 --no-messages --no-pacts `
  --output artifacts\matrix-17.json
```

Run a single condition with:

```powershell
$env:PYTHONPATH = "src"
py -3.11 -m world_sim evolve `
  --verification proxy --selection individual `
  --seed 17 --generations 5 --turns 12 --population 8 --parent-count 2 `
  --output artifacts\evolve-proxy-17.json
```

The command prints a canonical SHA-256 for the structured artifact, action-grounded generation metrics, selected parents, and final policy distribution. The artifact contains the complete state/event data, action tapes, outcomes, selection records, and lineage edges.

The older fixed-policy smoke test remains available:

```powershell
$env:PYTHONPATH = "src"
py -3.11 -m world_sim compare --seed 17 --turns 8 --output artifacts\compare-17.json
```

## measurements and limits

The engine reports state-backed measurements only:

- unverified claim attempts and unverified claims that were paid
- receipt-backed claims that were paid
- alive-agent turns, living agents, and final energy
- extraction and restoration counts
- final common-resource damage and stock

An "unverified" claim is a receipt-state condition, not an inference about intent or deception. The engine does not use an LLM judge, infer intent from messages, or declare an alliance real because agents call it one.

One run is not a finding. Before an LLM adapter is introduced, we need to fix the hypothesis, seed grid, model/config matrix, effect thresholds, and failure criteria. Before a claim about selection survives scrutiny, it must repeat across models and controls such as renamed world vocabulary and no-message/no-pact capability conditions.

See [the protocol](docs/LINEAGE_SELECTION.md) for the exact control design and replay boundary. See [Blind Commons v0](docs/EXPERIMENT_V0.md) for world semantics.

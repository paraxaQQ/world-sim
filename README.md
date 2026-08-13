# world-sim

`world-sim` is a deterministic, closed-world instrument for one question:

> does selection preserve objectively detectable shortcuts, or are apparent survival behaviors mostly model prior and prompt theatre?

The first environment is **blind commons**. Agents need energy to remain active. They can work to create a state-backed receipt, claim a reward, extract private energy from a renewable common resource, restore damage, transfer energy, message peers, and form bonded non-extraction pacts.

The engine, not an LLM, owns state and decides whether a claim has a receipt.

## the counterfactual treatment

The same seed, agents, policies, action surface, and world rules run in two modes.

| mode | claim reward | false claim result |
| --- | --- | --- |
| `proxy` | every claim pays | the event log records the missing receipt, but no payment is withheld |
| `receipts` | only a claim with a state-backed receipt pays | no reward; the engine applies the configured reputation penalty |

This is intentionally not an “AI civilization” demo. A Civ-like map, cities, tech tree, and population expansion can become a later stress test. They are not the first result.

## containment boundary

```text
trusted host model adapter (later) -> validated JSON action -> deterministic world engine
                                                           -> append-only event log
```

An agent receives an `AgentView` and can return one allowlisted JSON action. It gets no shell, browser, filesystem, network, credentials, payment rail, host process, or direct engine handle. The API key remains in a future host-side adapter and never enters a view or run artifact.

## run the reference treatment

Use Python 3.11 or newer. The current engine has no third-party dependencies.

```powershell
$env:PYTHONPATH = "src"
py -3.11 -m unittest discover -s tests -v
py -3.11 -m world_sim compare --seed 17 --turns 8 --output artifacts/compare-17.json
```

`compare` runs a fixed reference population under both treatments and writes the complete initial state, final state, ordered event log, and action-grounded metrics. It is a smoke test for the instrument, not evidence about LLM behavior.

Run one treatment directly with:

```powershell
$env:PYTHONPATH = "src"
py -3.11 -m world_sim pilot --verification receipts --seed 17 --turns 8
```

## v0 measurements

The engine reports only state-backed measurements:

- false claims and false claims that were paid
- receipt-backed claims that were paid
- extraction and restoration counts
- final common-resource damage and stock
- living agents and their final energy

It does not use an LLM judge, infer intent from messages, or declare an alliance real because agents called it one.

## next seams

The next implementation gate is not more world complexity. It is a versioned policy/memory lineage runner with controlled mutation, then predeclared controls: no selection, renamed world vocabulary, no-message/no-pact, and model-family swaps. We only add a model adapter after those local seams and receipts are proven.

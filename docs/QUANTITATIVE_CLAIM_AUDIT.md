# quantitative claim audit

this is a later analysis feature. it is not part of session 5.

“lie detector” is the wrong scientific name. the engine can detect whether a narrow factual claim conflicts with its saved state. it cannot infer intent, belief, deception, sarcasm, approximation, or a model's hidden reasoning.

## first supported claims

version 1 should accept only explicit present-tense self-claims about engine-owned quantities:

- exact inventory: “i have 2 wood”
- zero inventory: “i have no food” or “i'm out of food”
- exact energy: “i have 7 energy”
- exact shared stock: “there are 0 wood left”

promises, predictions, conditions, questions, estimates, and claims about another survivor remain unsupported. examples include “i'll send 2 wood,” “i should have 2 wood,” and “lumen has 2 wood.”

## ground-truth time

evaluate a claim against the engine state at the moment its speech is submitted. do not use end-of-beat or end-of-day state. under `sequential-dialogue-v3`, earlier speech commits before physical actions resolve, so the audit must bind each message to the pre-action view and event sequence that produced it.

each audit row should retain:

- message ID, event sequence, day, beat, speaker, and recipient
- exact source text and the restricted parsed claim
- claimed field and value
- objective value at submission time
- SHA-256 of the bound state or view
- verdict: `supported`, `contradicted`, `ambiguous`, or `unsupported`

## implementation boundary

start with a deterministic, high-precision grammar. do not use an LLM judge. low recall is acceptable for version 1; silently inventing a claim is not.

the extractor and the truth comparison are separate stages. an extraction failure produces `unsupported`, not `contradicted`. a contradiction means only that the parsed proposition disagrees with the bound engine value.

run the audit after an episode. do not show verdicts to models, alter reputation, change selection, or modify world state until a later treatment explicitly tests those interventions.

## validation before use

build a labeled corpus containing positive claims, contradictions, future tense, negation, ranges, corrections, quotations, jokes, and references to other survivors. report extraction precision and recall separately from truth-comparison accuracy.

the first public claim should be narrow: “the auditor checks a restricted grammar of quantitative self-claims against engine state at speech time.” it should never be described as detecting lies.

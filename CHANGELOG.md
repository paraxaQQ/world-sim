# changelog

## 0.12.1 - 2026-08-13

- translate Luna's strict response schemas to the documented OpenAI subset by replacing literal `const` constraints with typed single-value enums and `oneOf` action unions with `anyOf`
- keep the canonical world prompt and existing providers' schema bodies unchanged so committed campaign artifacts still reconstruct exactly
- retain the failed Luna probe as qualification 004 and reserve qualification 005 for the repaired adapter

## 0.12.0 - 2026-08-13

- add `sequential-dialogue-v3`: speech commits in rotating initiative order while physical actions remain sealed until atomic beat resolution
- let later actors hear and answer earlier same-beat speech without exposing the earlier actors' submitted actions or physical outcomes
- retain earlier submissions and speech after a mid-beat provider failure while forbidding incomplete-beat energy, inventory, resource, shelter, rest, or exhaustion changes
- bind initiative, exact views, provider-specific requests, provider identity, raw replies, parsed choices, cost authorizations, assignment transitions, and completed or partial state into format-v6 verification
- add generic one-seat model-replacement receipts for v3 continuations and a low-reasoning GPT-5.6 Luna Responses API profile while preserving the selected seat's public identity
- allow one to four models in the nonbehavioral adapter qualification and retain the changed wire contract as `paid-model-qualification-004`
- preserve v1 and v2 canonical results, and retain a 5,120-run dialogue-v3 confirmation that passes all 21 unchanged ecology gates

## 0.11.0 - 2026-08-13

- extend live campaigns beyond one continuation through format-v5 recursive parent links
- require complete ordered ancestor files before reconstructing a format-v4 or format-v5 parent
- verify every ancestor's source receipt, exact replay, transition, public record, state boundary, and seat mapping before provider transport
- add repeatable `--ancestor` support to `continue-live` and the offline verifier without duplicating the chain inside artifacts
- reconstruct failed continuation calls and require the retained partial world to match exactly
- retain session 3 as a censored three-call failure after Kimi exhausted the 10,000-token completion budget before the first `global-beats-v2` beat resolved

## 0.10.0 - 2026-08-13

- expose the existing simultaneous within-day scheduler as shared decision beats while keeping action and speech in one model response
- add a zero-cost `wait` action that consumes one beat without ending the survivor's day
- make global-beat transfers atomic against phase-start holdings, then resolve dependent eating and shelter construction
- preserve exact replay of retained legacy runs through an explicit interaction-protocol boundary
- report completed waits and retain a 5,120-run `global-beats-v2` confirmation with all 21 ecology gates passing
- correct the session-2 interpretation: fixed seat order did not resolve actions early; the missing `wait` action made continued negotiation unnecessarily costly

## 0.9.0 - 2026-08-13

- add verified live continuation from an exact completed parent artifact instead of recreating prior cycles
- carry forward identity, private state, messages, event history, and deterministic replay into cycle 2
- expose a frozen public record containing each identity's final prior broadcast as an unverified statement plus engine-counted transfer and shelter totals
- add strict between-cycle shared-resource adjustments with explicit before, after, delta, and audit identifiers
- derive model assignments from the parent, price the reconstructed first views, and retain format-v4 parent, transition, public-record, checkpoint, and outcome receipts
- add `continue-live` with no model override and objective transfer-to-shelter metrics
- name a fresh nonbehavioral adapter qualification for the changed production source

## 0.8.1 - 2026-08-13

- atomically checkpoint live survival artifacts before and after every provider call
- retain an explicit `in_flight` call receipt if the process stops during transport
- close the Windows reservation handle before atomic replacement
- keep final completed and failed artifact shapes unchanged

## 0.6.0 - 2026-08-13

- allow up to 16 calls in one paid observation cycle under a cumulative runtime ceiling
- authorize every paid request from its exact current prompt under one shared cumulative cost ceiling
- retain per-call cost authorization and fail before an over-budget request
- omit MiniMax M3's undocumented thinking control while retaining documented controls for the other three models
- freeze the first paid four-model observation protocol without changing the world rules
- retain the one-shot seed `29995` failure after MiniMax M3 exhausted its completion budget on call 2

## 0.5.1 - 2026-08-13

- make deterministic and live survivor runs use the calibrated `lean-camp-v1` ecology instead of the easier development defaults, and record the preset plus complete world configuration in live artifacts.
- enforce the preset's calibrated four-survivor population and reuse the viable chatty food-first baseline for deterministic reference runs.
- add `--require-complete-budget`. A full-cycle run now rejects before transport unless `--max-calls` covers every possible chance; the default authorization remains 12 calls.
- reserve live output paths exclusively before provider calls, record authentication mode and eight replay-critical source hashes, and use the same calibrated configuration for paid prompt-cost preflight.
- retain a real unauthenticated free-model qualification with 16 successful schema-valid calls, zero retries, exact replay, one final-chance rest, and three exhaustion events.

## 0.5.0 - 2026-08-13

- replace one-choice days with four-chance cycles: each survivor may act and speak repeatedly, voluntary rest ends its cycle, and missing the final rest deadline cancels the attempted choice and applies exhaustion.
- deliver messages on the recipient's next active chance, retain unread messages for early sleepers, and expose bounded audience-safe objective outcomes without leaking private inventories or directed speech.
- make malformed actions waste a chance instead of counting as rest, keep rest free, and make bounded speech free so conversational frequency remains separate from metabolism.
- add exact multi-slot replay for fresh and continued runs, strict alias and static-input validation, and a hard live-call ledger that blocks an unpriced extra request.
- add the `lean-camp-v1` ecology candidate and a deterministic calibration instrument with scripted baselines, seat rotations, clustered bootstrap comparisons, fixed gates, and per-seed summaries.
- require strict JSON-only model replies with the exact response schema in every prompt, including GLM's JSON-object request mode.
- retain a 5,120-run held-out `lean-camp-v1` confirmation that passes all 21 fixed balance gates without provider calls.

## 0.4.4 - 2026-08-13

- add `--reasoning-effort compatibility-first` for paid smoke tests and send each allowlisted model its documented `thinking: {"type": "disabled"}` control.
- reject the compatibility profile outside paid-only runs, restrict paid smoke runs to one day so every potential call is priced by preflight, and preserve exact model-specific requests in artifacts.

## 0.4.3 - 2026-08-12

- add an explicit `opencode-paid` Zen route for DeepSeek V4 Flash, MiniMax M3, Kimi K2.6, and GLM 5.2 with pinned model-specific chat request profiles.
- require paid-only populations, `--max-paid-usd`, a $0.05 authorization ceiling, a conservative pinned-price preflight, and no more than four potential calls before reading credentials or contacting Zen.
- require provider-reported cost on successful paid responses, preserve exact decimal totals, calculate the same usage against the pinned price snapshot, and retain sanitized failure receipts without retries or credential leakage.

## 0.4.2 - 2026-08-12

- add an explicit `--reasoning-effort low` live-model compatibility mode while retaining `provider-default`.
- record the selected reasoning-effort mode in every live artifact; do not claim a numerical reasoning budget from the compatibility setting.

## 0.4.1 - 2026-08-12

- allow authenticated access to `-free` OpenCode Zen models through the optional `OPENCODE_ZEN_API_KEY` environment variable.
- keep anonymous free-model access as the default and exclude the key from run artifacts.

## 0.4.0 - 2026-08-12

- add a direct, capability-free adapter for OpenCode's free and Go chat-completions endpoints.
- add `survive-live` for 2-8 hidden-seat model assignments with hard call, completion-token, response-size, and timeout limits.
- allow up to 4,096 total completion tokens so reasoning models can deliberate, and record provider-reported reasoning-token usage without forcing a provider-specific effort setting.
- retain the exact prompts, raw provider replies, parsed choices, validation failures, model assignments, and provider-reported token usage in each live artifact.
- abort on transport or provider failures, keep malformed model JSON under the existing paid-rest and silence rules, and never retry a model call.
- keep the deterministic world engine unchanged and replay live results without another model call.

## 0.3.1 - 2026-08-12

- raise the per-message world limit from 160 to 500 characters.
- raise the future adapter output ceiling from 192 to 512 tokens.
- raise the strict raw-response ceiling from 2 KiB to 8 KiB so valid Unicode messages fit inside the transport boundary.

## 0.3.0 - 2026-08-12

- add a deterministic named-survivor ecology with energy, food, wood, shelter, transfers, permanent death, and opaque resolution seats.
- add one paid, 160-character message per survivor per day with next-day delivery and independent validation from the primary action.
- add a human-phrased model prompt, strict response schema, 192-token adapter contract, and 2-KiB raw-response boundary.
- add a scripted reference population, objective metrics, view-hashed choice-tape replay, canonical artifact hashes, `survive` CLI, and survival invariants covered by tests.
- reframe Blind Commons and lineage selection as retained calibration instruments rather than the project headline.

## 0.2.0 - 2026-08-12

- add deterministic policy bundles, inherited bounded memory, controlled mutation, and an explicit lineage graph.
- add fitness-based individual selection and a matched fitness-blind control with the same parent bottleneck, clone count, and mutation schedule.
- add a four-condition selection matrix, recorded action tapes, generation replay, canonical artifact hashes, and CLI commands for lineage runs.
- hide host lineage metadata and verification treatment from controllers by default; add no-message and no-pact treatment toggles.
- keep model adapters, external tools, real credentials, and claims about LLM behavior out of scope.

## 0.1.0 - 2026-08-12

- establish the deterministic Blind Commons world and proxy-versus-receipt treatment.
- add closed JSON action validation, objective receipts, common-resource externalities, bonded pacts, replayable event logs, and a paired-run CLI.
- leave model adapters, policy mutation, selection, UI, and external tools out of scope.

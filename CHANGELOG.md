# changelog

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

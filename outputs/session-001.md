# session 001 - first contact

**status:** completed

**date:** 2026-08-13

**seed:** `29993`

## result

the live episode completed 15 valid calls. every response broadcast a message, and the group repeatedly proposed cooperation, fair sharing, and shelter coordination. no model chose `give_food` or `give_wood`; the engine recorded zero attempted or completed costly transfers. all four survivors lived.

this catalog also preserves the instrument smoke tests, calibrations, and model-panel qualifications that led into the first numbered session. those files are session-1 provenance, not extra campaign sessions.

## evidence identity

- catalog: `outputs/session-001.json`
- source commit: `03c04a389a1b3b06edc46a9d0492ee1c0b9e38ba`
- preserved source artifacts: `27`
- preserved source bytes: `544493`
- catalog SHA-256: `7948d04b07148f14e8c8c46380c2982334f7fab6eee805808369b88a3c5dfa50`
- primary world artifact SHA-256: `a98ec8216c08a172c4ed29fb1da65b63defd3b4a29f53e95fa26a1e187e38b90`
- canonical result SHA-256: `490663b4a743f51c4b0f44ccc57ba91ee2a7b6d6adafbcda072373a7748a54e7`
- provider-reported episode cost: `$0.10711436`

## verify or restore

```powershell
py -3.11 tools\session_catalog.py verify outputs\session-001.json
py -3.11 tools\session_catalog.py materialize outputs\session-001.json --destination artifacts\session-001-legacy
```

verification checks the frozen inventory, gzip envelope, source paths, exact bytes, hashes, and pinned Git blobs without provider calls. materialization restores the retired files under the destination for their original replay and domain-specific checks.

## preserved source index

- `outputs/SURVIVAL_CORE_RECEIPT.md`
- `outputs/v0.4.0-model-host-proof.md`
- `outputs/v0.4.2-free-live-smoke-proof.md`
- `outputs/v0.4.3-paid-live-smoke-attempt.md`
- `outputs/v0.4.4-paid-live-smoke-proof.md`
- `outputs/v0.5.0-lean-camp-v1-confirmation.json`
- `outputs/v0.5.0-timed-cycle-proof.md`
- `outputs/v0.5.1-free-interactive-cycle-29996.json`
- `outputs/v0.5.1-live-readiness-proof.md`
- `outputs/v0.6.0-paid-observation-29995-proof.md`
- `outputs/v0.6.0-paid-observation-29995.json`
- `outputs/v0.6.0-paid-observation-protocol.md`
- `outputs/v0.7.0-paid-reasoning-29994-proof.md`
- `outputs/v0.7.0-paid-reasoning-29994.json`
- `outputs/v0.7.0-paid-reasoning-protocol.md`
- `outputs/v0.8.0-paid-panel-qualification-001-proof.md`
- `outputs/v0.8.0-paid-panel-qualification-001.json`
- `outputs/v0.8.0-paid-panel-qualification-002-proof.md`
- `outputs/v0.8.0-paid-panel-qualification-002-protocol.md`
- `outputs/v0.8.0-paid-panel-qualification-002-readiness.md`
- `outputs/v0.8.0-paid-panel-qualification-002.json`
- `outputs/v0.8.0-paid-panel-qualification-protocol.md`
- `outputs/v0.8.0-paid-panel-qualification-readiness.md`
- `outputs/v0.8.0-paid-survival-29993-proof.md`
- `outputs/v0.8.0-paid-survival-29993-protocol.md`
- `outputs/v0.8.0-paid-survival-29993-readiness.md`
- `outputs/v0.8.0-paid-survival-29993.json`

## claim boundary

one short trace shows cooperative language followed by individually beneficial actions. it does not establish peacefulness, selfishness, stable model traits, or a general cooperation rate.

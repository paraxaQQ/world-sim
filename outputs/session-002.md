# session 002 - shelter dilemma

**status:** completed

**date:** 2026-08-13

**seed:** `29993`, exact continuation of session 1

## result

the episode completed nine valid calls. Birch stated the valid solution: one wood-holder could give two wood to the other, enabling a shelter. Aster repeated it. nobody selected a transfer, so the engine recorded zero attempted transfers, zero completed transfers, and zero shelters. all four survivors lived.

## evidence identity

- catalog: `outputs/session-002.json`
- source commit: `03c04a389a1b3b06edc46a9d0492ee1c0b9e38ba`
- preserved source artifacts: `6`
- preserved source bytes: `232043`
- catalog SHA-256: `bc6af110c0970da37825db830ad05e90a03bd20bc6ead76f0c78d7eef85cedb9`
- primary world artifact SHA-256: `fc0b07dfc404a2f485f3b6a2c2f191fec5e495153d6147d428d6cb251cab27fe`
- canonical result SHA-256: `ed1f299bbc698951e77256b46291ea4ee142469bc0a7cd0e7b6bf476820392ca`
- provider-reported episode cost: `$0.08118026`

## verify or restore

```powershell
py -3.11 tools\session_catalog.py verify outputs\session-002.json
py -3.11 tools\session_catalog.py materialize `
  outputs\session-001.json `
  outputs\session-002.json `
  --destination artifacts\session-002-legacy
```

the materialization command restores session 1 because session 2's continuation verifier needs its parent.

## preserved source index

- `outputs/v0.9.0-paid-panel-qualification-003-proof.md`
- `outputs/v0.9.0-paid-panel-qualification-003-protocol.md`
- `outputs/v0.9.0-paid-panel-qualification-003.json`
- `outputs/v0.9.0-session-002-shelter-dilemma-29993-proof.md`
- `outputs/v0.9.0-session-002-shelter-dilemma-29993-protocol.md`
- `outputs/v0.9.0-session-002-shelter-dilemma-29993.json`

## claim boundary

this is one exploratory continuation chosen after session 1, with no no-memory control or replicate seeds. it shows a spoken solution followed by no costly transfer under one fragile coordination window; it does not establish that memory caused either the speech or behavior.

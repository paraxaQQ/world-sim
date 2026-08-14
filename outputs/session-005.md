# session 005 - turn-order matrix

**attempt 001 status:** sealed and deviated; not cleanly complete

**date:** 2026-08-14

## result

attempt 001 contains twelve sibling worlds from one exact session-2 parent: three planned cells for each of four initiative phases. eight cells are scoreable and four are censored technical failures. five of eight scoreable cells completed the primary shelter-enabling chain. every scoreable cell contained at least one completed costly transfer; the eight cells contained 16 costly transfers, 5 shelters, and 2 deaths.

the first technical failure occurred at execution position 6. the runner stopped, that cell was sealed as censored, and positions 7 through 12 were later resumed even though the frozen machine stopping rule had no preregistered resume clause. those six later cells and the full phase comparison are exploratory. packaging this evidence does not turn attempt 001 into a clean completed experiment.

## evidence identity

- catalog: `outputs/session-005.json`
- source commit: `03c04a389a1b3b06edc46a9d0492ee1c0b9e38ba`
- preserved source artifacts: `20`
- preserved source bytes: `3687963`
- sealed attempt-001 v1 catalog SHA-256: `eb3d70e572ae57b8d85beb3cd5c0209326c29676252b25b5a5e738af0ca4f99b`
- matrix manifest SHA-256: `8ae4b6f3fd36e162ca1349be83e72424092a45807f7664e95a1788af9ab665c6`
- scored result SHA-256: `38b1cab8d152878b9da03df58bebd3c06974478ae63a644e1ecc8855bf1750d5`
- b02-p3 postmortem SHA-256: `630ff5c92c84b0cca9b99dad5d47b8dfe8a7eff3759c7516aad873b0c974d5e4`
- b03-p2 postmortem SHA-256: `519e61bc84e2d659768a28f31c93914c09eed4cebdc14ddc164697f3e44100cf`

## catalog migration receipt

the five session catalogs replace 73 committed files totaling 5,913,205 bytes. all 73 original Git blobs are embedded losslessly with their paths, roles, sizes, and hashes. this migration changes evidence packaging only; it changes no protocol, world state, model call, score, or scientific status.

the release verification matched all 73 payloads to the pinned Git blobs and reproduced the retained matrix score byte for byte. after the raw files were removed from the checkout, 239 tests plus 90 subtests passed.

## verify or restore

```powershell
py -3.11 tools\session_catalog.py verify outputs\session-005.json
py -3.11 tools\session_catalog.py materialize `
  outputs\session-001.json `
  outputs\session-002.json `
  outputs\session-005.json `
  --destination artifacts\session-005-legacy
```

the materialization command restores sessions 1 and 2 because matrix scoring verifies the exact root and parent before scoring session 5.

## preserved source index

- `outputs/v0.13.0-session-005-turn-order-b01-p0-29993-proof.md`
- `outputs/v0.13.0-session-005-turn-order-b01-p0-29993.json`
- `outputs/v0.13.0-session-005-turn-order-b01-p1-29993.json`
- `outputs/v0.13.0-session-005-turn-order-b01-p2-29993.json`
- `outputs/v0.13.0-session-005-turn-order-b01-p3-29993.json`
- `outputs/v0.13.0-session-005-turn-order-b02-p0-29993.json`
- `outputs/v0.13.0-session-005-turn-order-b02-p1-29993.json`
- `outputs/v0.13.0-session-005-turn-order-b02-p2-29993.json`
- `outputs/v0.13.0-session-005-turn-order-b02-p3-29993.json`
- `outputs/v0.13.0-session-005-turn-order-b03-p0-29993.json`
- `outputs/v0.13.0-session-005-turn-order-b03-p1-29993.json`
- `outputs/v0.13.0-session-005-turn-order-b03-p2-29993.json`
- `outputs/v0.13.0-session-005-turn-order-b03-p3-29993.json`
- `outputs/v0.13.0-session-005-turn-order-matrix-protocol.json`
- `outputs/v0.13.0-session-005-turn-order-matrix-protocol.md`
- `outputs/v0.14.0-session-005-turn-order-matrix-proof.md`
- `outputs/v0.14.0-session-005-turn-order-matrix-results.json`
- `outputs/v0.14.1-session-005-postmortem-seal-proof.md`
- `outputs/v0.14.1-session-005-turn-order-b02-p3-29993-postmortem.json`
- `outputs/v0.14.1-session-005-turn-order-b03-p2-29993-postmortem.json`

## claim boundary

the descriptive primary result is 5 of 8 scoreable cells, with rates of `0.5`, `1.0`, `0.5`, and `0.5` across phases 0 through 3. this is not clean causal evidence of a turn-order effect. a fresh replacement matrix needs a frozen continue-and-censor failure rule and twelve new sibling cells.

## attempt 002 - replacement matrix

**status:** sealed; protocol adhered

attempt 002 executed all 12 frozen sibling cells. 9 cells are scoreable, 3 are censored technical failures, and 6 scoreable cells completed the primary shelter-enabling chain. phase success rates are `[1.0, 0.6666666666666666, 0.0, 1.0]`.

## attempt 002 evidence identity

- catalog SHA-256: `b2ea190be3f12f5f6362c84e4a47136a2ab37bb17ed1e8761f30e0703cdb5ae8`
- embedded artifacts: `18`
- matrix manifest SHA-256: `60f0a596e9e00bd546beaedfa0575a24422a516e18f6c9022c133269d78fff42`
- scored result SHA-256: `19d51cf7cf43a6d7adb666fd74857fccb6caff4fee91e7406742033d3b097919`
- readable proof SHA-256: `e7b5c2335dfc4327f82d0bde9ca0f48edda8e768fd06350e04d8f2036ca18450`
- postmortem artifacts: `3`
- conservative cost exposure: `$0.662148135`

the attempt was packaged directly from ignored `artifacts/` files into this JSON/Markdown pair. no split attempt artifact is committed, and direct artifacts claim no Git source commit.

## restore and rescore attempt 002

attempt 002's manifest keeps the lineage layout used during execution. restore session 5 at the working root, then restore sessions 1 and 2 inside that lineage directory:

```powershell
$restore = "artifacts\session-005-restored"

py -3.11 tools\session_catalog.py materialize `
  outputs\session-005.json `
  --destination $restore

py -3.11 tools\session_catalog.py materialize `
  outputs\session-001.json `
  outputs\session-002.json `
  --destination "$restore\artifacts\session-005-attempt-002\lineage"

py -3.11 tools\score_turn_order_matrix.py `
  "$restore\docs\SESSION_005_ATTEMPT_002.json" `
  --repo-root $restore `
  --output "$restore\artifacts\session-005-attempt-002\score-replay.json"
```

the replay output must have SHA-256 `19d51cf7cf43a6d7adb666fd74857fccb6caff4fee91e7406742033d3b097919`.

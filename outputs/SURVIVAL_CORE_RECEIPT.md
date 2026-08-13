# named survival core receipt

Date: 2026-08-12

## claim

The v0.3 named-survivor core runs without network access, replays deterministically, enforces paid actions and capped next-day speech, and retains the older calibration suite.

## result

The full test command passed 43 tests:

```powershell
$env:PYTHONPATH = "src"
py -3.11 -m unittest discover -s tests -v
```

The fixed reference run completed 10 days:

```powershell
py -3.11 -m world_sim survive `
  --seed 17 --days 10 --population 8 `
  --output artifacts\survival-reference-17.json
```

Its canonical result SHA-256 was:

```text
164f771746075f94d37ae399bb394637a2c6d8289f29e2866fd2bc93fca21c55
```

Objective summary:

```text
days completed:       10
living survivors:      6
deaths:                2
messages sent:         8
messages rejected:     0
shelters built:        2
food foraged:         35
food eaten:           38
total final energy:   36
```

An exploratory 100-seed sweep of the same 10-day scripted fixture produced a nonconstant survival distribution:

```text
living survivors -> run count
4 -> 8
5 -> 22
6 -> 38
7 -> 24
8 -> 8
```

The CLI also rejected an invalid one-person population with exit code 2:

```powershell
py -3.11 -m world_sim survive --population 1
```

## bound

This receipt proves engine behavior for the bundled scripted reference policy. It is not evidence about an AI model, cooperation, deception, alliances, or emergent social behavior. No model adapter or API key was connected for this run.

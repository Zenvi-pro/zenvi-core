# Edit contract: live comparison with real models (2026-11-17)

Produced by `tests/eval/contract_live.py`: a real model (`claude -p`, no tools of its own, empty working folder) plays the agent;
every tool call goes through the real executor, the real `begin_edit_tool`, the real contract and the real audit on seeded synthetic
footage (12 clips; the best moments are never in the first two files). Tool descriptions for the fake tools are abbreviated. Read
`README_contract.md` before quoting any of this.

Conditions: `today` (contract off, old playbook step, no begin_edit_tool), `warn` (the shipped default), `enforce`, and
`enforce_nohint` (refusals on, but the prompt carries no hint, so only the tool list and the refusal message can teach the model).
Prompts: 4 whole-piece asks ("make something cool with my trip", "make a vlog from my footage", "edit my footage into a reel",
"cut a trailer from these clips") and 2 narrow asks, each run twice.

## Claude Sonnet (48 runs, 6.5 minutes, $5.01)

| condition | runs | placed | begin_edit first | surveyed first | brief at 1st placement | refused/run | opening rank (1=best) | top-5 moments used | reviewed | $/run | input tok/run |
|---|---|---|---|---|---|---|---|---|---|---|---|
| today | 8 | 100% | 0% | 100% | 0% | 0.0 | 1.0 | 4.8 / 5 | 100% | 0.136 | 53,469 |
| warn | 8 | 100% | 100% | 100% | 100% | 0.0 | 1.0 | 5.0 / 5 | 100% | 0.136 | 61,393 |
| enforce | 8 | 100% | 100% | 100% | 100% | 0.0 | 1.0 | 5.0 / 5 | 100% | 0.140 | 64,895 |
| enforce_nohint | 8 | 100% | 100% | 100% | 100% | 0.0 | 1.0 | 5.0 / 5 | 100% | 0.140 | 60,222 |

## Claude Haiku (24 runs, 3 minutes, $0.15; `today` and `enforce_nohint` only)

| condition | runs | placed | begin_edit first | surveyed first | brief at 1st placement | refused/run | opening rank | top-5 used | reviewed | $/run | input tok/run |
|---|---|---|---|---|---|---|---|---|---|---|---|
| today | 8 | 100% | 0% | 100% | 0% | 0.0 | 1.0 | 4.9 / 5 | 100% | 0.008 | 59,751 |
| enforce_nohint | 8 | 100% | 100% | 100% | 100% | 0.0 | 1.0 | 5.0 / 5 | 100% | 0.009 | 70,562 |

Narrow asks (trim, grade, captions; 4 runs per condition per model): 0 refusals and 0 `begin_edit_tool` calls in every condition.

## What this shows

- **Narrow asks are untouched.** No refusal, no needless `begin_edit_tool`, for either model.
- **The new tool is adopted without being told to.** In every contract condition, including `enforce_nohint`, both models called
  `begin_edit_tool` first: its own description ("call this first for any 'make me a ...' ask") was enough.
- **A brief now exists at the first placement** (0% before, 100% after), so the audit knows the form and length and applies its
  form rules (music, captions, loudness target, length) instead of reviewing a piece with no stated intent.
- **The cost is small:** about 6 to 11 thousand more input tokens per finished piece (+12% to +18%), and about the same dollars per run
  (the extra input is mostly cached).

## What this does NOT show

- **No quality gain was measured.** With today's playbook and tools both models already look at the footage before cutting (100%
  surveyed first) and already open on the project's best moment (rank 1.0, 4.8 to 4.9 of the best five used). The failure the gate
  was designed for, cutting in bin order without looking, did not occur in this setup. The scripted eval
  (`EDIT_CONTRACT.md`) showed that mechanism only because its agents were scripted to behave that way.
- **The gate itself was never exercised by a real model:** there were no refusals, so recovery from a refusal is still untested.
  A weaker or differently prompted agent (Hermes, Cursor CLI, a non-Claude model) might skip the survey; this does not cover them.
- **No human rating of the cuts**, and synthetic footage whose highlight scores are given, not computed from pictures.
- Eight runs per cell and a non-deterministic model: differences of one run are noise.

## What it means for the default

`warn` stays the default. Nothing here shows that refusing a placement buys anything for these models, and a refusal costs a call
and risks a false positive. The log (`python -m classes.edit_contract_log`) will show, on real use, how often a placement would have
been refused and whether agents recover; the real before/after belongs in the separate agent-benchmark repo (its vague-asks set already has a
`looked_first` check), run with `ZENVI_EDIT_CONTRACT=off` and `enforce` on a machine with the desktop app.

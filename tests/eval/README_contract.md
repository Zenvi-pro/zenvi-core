# Edit contract eval: what it measures and what it cannot

`contract_eval.py` (run by `tests/test_contract_eval.py`, a few seconds, no ffmpeg, no network) runs the **real** executor, the
real `begin_edit_tool`, the real contract and the real audit on seeded synthetic projects, with **scripted** agents standing in
for the model. Run it by hand: `cd tests && python -m eval.contract_eval`.

## What it shows (the mechanism)

- With the gate on, an eager agent's first placement is held back, and an agent that follows the refusal ends up survey-first,
  with a brief at its first placement, opening on a strong moment and using most of the project's best moments.
- With the gate off nothing changes. Nothing is ever refused outside `enforce`. Narrow asks are never refused or warned.
- A survey digest costs about 1.1 k characters per finished piece (cap 3.5 k).

## What it does not show

- **That a real model follows a refusal.** The `recovering` and `careful` scripts read the survey digest by construction, so
  their rows follow from the mechanism. They are the floor of the claim (the gate can hand a compliant agent the right
  information), not evidence about Claude, Codex, Hermes or the Assistant.
- **That the film is better.** "Opening rank" and "top-5 moments used" are proxies for taste, measured on footage whose highlight
  scores are synthetic. They say the edit uses the best footage; they do not say it is a better story, pace or mood.
- The audit loop is not modelled: scripted agents never fix what `review_edit_tool` names, so every export carries the "open
  needs" warning.

## The live comparison with real models (opt-in; spends the user's own Claude plan)

`contract_live.py` runs it with `claude -p` as the agent (no tools of its own, empty folder): `ZENVI_EVAL_LIVE=1 python -m eval.contract_live --model sonnet --repeat 2`
(about $5 and 7 minutes for 48 runs on Sonnet; Haiku costs cents). Results and what they do and do not show: `results/EDIT_CONTRACT_LIVE.md`.
It is not a human rating of the films, and it is not the app: for the real before/after use the `zenvi-bench` repo (`tasks/vague.json`,
which already has a `looked_first` check; never tune on its held-out split), run with the contract off and on against the desktop app.

## The fuller protocol (human scoring, the real app)

Same project, same prompt, `edit_contract` = `off` versus `enforce`, on each backend you care about (Zenvi Assistant, Claude Code,
Codex, Hermes at least). Prompts: the ten in `WHOLE_PROMPTS` plus the five narrow controls. For each run record:

1. the order of tool calls up to the first placement (surveyed first? refused? recovered?);
2. whether the brief existed at the first placement, and what `begin_edit_tool` assumed (`inferred`);
3. `review_edit_tool` findings: `needs` count and the two story notes (`opening_best`, `best_unused`);
4. tokens and credits spent;
5. blind human scoring of the exported cut on a fixed rubric (opening, pacing, music fit, "would I post this"), two raters, the
   backend and flag hidden.

The contract is worth keeping if `enforce` is at least as good on (3) and (5), refuses no narrow ask, and costs under about 4 k
extra input tokens per finished piece. If a backend does not recover from the refusal, that shows up in (1): fix its prompt
adapter before enforcing there.

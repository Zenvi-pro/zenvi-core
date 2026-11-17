# Edit contract: offline process eval results

Produced by `python -m eval.contract_eval` (3 seeds x 10 whole-piece prompts per cell; 5 narrow prompts x 3 seeds as controls).
Scripted agents on synthetic footage: this measures the mechanism, not a real model. Read `README_contract.md` before quoting it.

- `eager`: places the bin's first shots at once; after a refusal it tries once more, then stops.
- `recovering`: the same, but follows a refusal (calls `begin_edit_tool`, places from the survey).
- `careful`: calls `begin_edit_tool` first.
- flag: `off` is today's behaviour, `warn` runs the call and adds a warning, `enforce` refuses a placement before a brief.

| agent | flag | placed | surveyed first | brief at 1st placement | refused/run | warned/run | opening rank (1=best) | top-5 moments used | export warned |
|---|---|---|---|---|---|---|---|---|---|
| eager | off | 100% | 0% | 0% | 0.0 | 0.0 | 31.4 | 0.6 / 5 | 0% |
| recovering | off | 100% | 0% | 0% | 0.0 | 0.0 | 31.4 | 0.6 / 5 | 0% |
| careful | off | 100% | 100% | 100% | 0.0 | 0.0 | 1.0 | 5.0 / 5 | 0% |
| eager | warn | 100% | 0% | 0% | 0.0 | 2.0 | 31.4 | 0.6 / 5 | 100% |
| recovering | warn | 100% | 0% | 0% | 0.0 | 2.0 | 31.4 | 0.6 / 5 | 100% |
| careful | warn | 100% | 100% | 100% | 0.0 | 1.0 | 1.0 | 5.0 / 5 | 100% |
| eager | enforce | 0% | 0% | 0% | 2.0 | 1.0 | - | 0.0 / 5 | 100% |
| recovering | enforce | 100% | 100% | 100% | 1.0 | 1.0 | 1.0 | 5.0 / 5 | 100% |
| careful | enforce | 100% | 100% | 100% | 0.0 | 1.0 | 1.0 | 5.0 / 5 | 100% |

Narrow-ask controls (trim, grade, captions, an empty add, export; the contract is `free`):

- `off`: 15 runs, 0 refusals, 0 warnings
- `warn`: 15 runs, 0 refusals, 0 warnings
- `enforce`: 15 runs, 0 refusals, 0 warnings

Largest survey digest handed to a model: 1131 characters (cap 3500).

## Reading it

- `eager` + `enforce` places nothing: it is held back rather than cutting blind (0% placed, 2 refusals a run).
- `recovering` + `enforce` is survey-first and briefed in 100% of runs, opens on the project's best moment (rank 1.0 against 31.4 in
  bin order) and uses all five of its best moments (against 0.6 of 5). That is the mechanism working for an agent that follows
  the refusal; whether real models do is what the live comparison in `README_contract.md` is for.
- Nothing is refused or warned on narrow asks in any mode, and nothing is refused in `off` or `warn`.
- `careful` behaves the same in every flag except the export warning: the gate only costs an agent that skips the survey.
- `warn` changes nothing for `eager` and `recovering` (still 0% surveyed first): the scripted agents ignore warnings. Whether real
  models act on a receipt warning is exactly what the live comparison would show; `enforce` does not depend on it.
- Every export is warned in `warn` and `enforce` because the scripted agents never fix what the audit names (the audit loop is not
  modelled).

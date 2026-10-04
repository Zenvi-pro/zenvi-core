# Round 2 experiments: what was measured and what it decided

Raw data: `search_calibration.json`, `spectrogram_experiment.json`. Corpus and metrics: `tests/eval/`. Live Gemini spend for the
whole round is tracked by `tests/eval/spend_guard.py` (a hard cap, default $5; figures are a deliberately high ceiling estimate).

## 1. `media_resolution` (does a low setting save tokens?)  ->  NO. Left off.
Three real clips, two shots each, the same call with and without the setting, global and per part:

| clip | default | LOW | MEDIUM | HIGH (per part) |
|---|---|---|---|---|
| pond (15 s) | 1931 prompt tokens | 1931 | 1931 | 4901 |
| plaza (12 s) | 1358 | 1358 | 1358 | n/a |
| talk (8 s) | 1293 | 1293 | 1293 | 2877 |

The default already bills the same as LOW and MEDIUM; only HIGH changes anything (2.2x to 2.5x more tokens). There is no
saving to switch on, so `understand.py` is unchanged. The 480p proxy is what keeps the upload small; it does not change tokens.
On-screen text was read identically at every setting. About $0.10 of the cap.

## 2. Spectrogram vs numbers for finding song sections  ->  helps a little. `spectrogramSuggested` is built, for low-confidence edges only.
Blind: 8 random songs (22 true section edges); truth unopened until all answers were on disk; the reader was a Claude model.

| what the reader had | F1 at +-2 s |
|---|---|
| the local section finder alone | 0.909 |
| raw numbers only (4 s energy curve, tempo, loudness) | 0.930 |
| numbers + the computed section list (what an agent gets today) | 0.933 |
| numbers + sections + the spectrogram image | **1.000** |

The picture removed the last errors (2 spurious edges, 1 missed one). Limits: 8 songs, synthetic, one reader, edges read by eye.
Modest evidence, so the suggestion is only made where the numbers are least sure, and it is a suggestion, never a requirement.

## 3. TransNetV2 vs our cut detector  ->  not built, no evidence of need.
After the transition fixes our detector scores F1 1.0 on hard cuts, near-identical scenes, dissolves and fades through black, with
no false cuts on a busy no-cut clip or a clip with a flash (`tests/eval`, `shots.*`). No hard case separates the two on the data
available, and TransNetV2 would add a model download and a heavy dependency. If real footage later shows a cut type we miss, the
eval corpus is where to add it first.

## 4. Search cut-offs  ->  one real fix.
19 clips, 22 queries with known answers, 7 that match nothing; real Gemini embeddings. The right clip was first for every query.
- shot 0.40 and image 0.36: kept (the data supports them).
- speech 0.40 -> 0.52, and the constant now actually applies (it was being filtered with the shot floor).

## 5. Quality, transitions, music
See the commit messages: blur thresholds recalibrated (soft 0.60 -> 0.70, blurry 0.35 -> 0.52), fades and dissolves reported as one
boundary (F1 0.50 -> 1.00 and 0.86 -> 1.00), builds and fades found by slope (gradual-build F1 0.67 -> 1.00).

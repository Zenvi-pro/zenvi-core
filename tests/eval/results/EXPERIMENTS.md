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

## 6. People identity (faces and voices)  ->  built, off by default.
- Faces (YuNet + SFace, local): LFW 62 people, 900 photos: all found, 18 ms a photo, AUC 0.985. Match line cosine 0.45 (98.1% of same-person pairs accepted,
  1 false merge in 11,000 pairs); 0.35-0.45 "unsure", never merged. `people_calibration.json`.
- Voices (WeSpeaker ResNet34, local): LibriSpeech 26 speakers, 307 utterances: AUC 1.0, equal error 0.06%. Conversations with a pause at each change of speaker:
  99.4% of words given the right speaker. Rapid turn-taking with no pause: 77% (a stretch is not split at a speaker change); shorter stretches helped that
  case but split single voices, so they were not used. Lines 0.50 (within a file), 0.55 across files, 0.45-0.55 unsure. `voice_calibration.json`.
- Real footage: a 30 s trailer scanned in 1.9 s, the same face in adjacent shots resolved to one person (0.96). Development data only; none kept in the repo.
- Not measured: faces in profile, masks, children, tiny faces and phone-quality or overlapping speech. They come out as "unsure" or as separate people, not as merges,
  because the lines are strict.

## 7. Place names  ->  built (GeoNames cities over 15,000, 580 KB, CC BY 4.0).
30 known places (landmarks in 28 cities) all named after the right city; open sea, poles, desert and the date line behave. A city beats its own districts (Paris, not
Passy) and a centre beats a bigger neighbour (Oakland, not San Francisco). Not covered: villages and sites under 15,000 people get "near <nearest town>".

## 8. Mix gain past the 130% volume cap  ->  not built.
libopenshot's Compressor has a makeup gain but its range and behaviour could not be tested (the local libopenshot does not load against the installed ffmpeg), so the
mix report names the voice_compressor preset and says the added gain is unmeasured, rather than automating something unverified.

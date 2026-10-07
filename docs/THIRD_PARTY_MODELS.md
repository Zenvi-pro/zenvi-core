# Optional models used by people identity

People identity (the `media-index-people` preference, off by default) runs three small models on the user's computer through
`onnxruntime` (`requirements-speech.txt`). They are not shipped in the app or the repository: the user asks for them once
(`setup_people_tool`) and each file is checked against a pinned SHA-256 before it is installed (`src/classes/media_index/people_models.py`).

| Model | Used for | Size | Licence | Source |
|---|---|---|---|---|
| YuNet `face_detection_yunet_2023mar.onnx` | finding faces | 0.2 MB | MIT | OpenCV model zoo, `models/face_detection_yunet` |
| SFace `face_recognition_sface_2021dec.onnx` | telling faces apart | 37 MB | Apache-2.0 | OpenCV model zoo, `models/face_recognition_sface` |
| WeSpeaker ResNet34-LM `voxceleb_resnet34_LM.onnx` | telling voices apart | 27 MB | **CC BY 4.0** | Hugging Face, `Wespeaker/wespeaker-voxceleb-resnet34-LM` |

**Attribution (required by CC BY 4.0):** "WeSpeaker ResNet34 speaker model (Wespeaker project, trained on VoxCeleb2), CC BY 4.0".
The same text is returned by `people_status_tool` (`attributions`). It should also appear in the About dialog when the voice model
is offered to users; the dialog's credits list is upstream OpenShot's developer list and was not changed.

The face and voice recognisers were trained on public face and voice datasets; their terms are the model authors', linked above.
Any change of model needs a new pinned hash, a new calibration (`tests/eval/people_eval.py`, `tests/eval/voice_eval.py`, results in
`tests/eval/results/`) and a check of the licence.

## What is stored, and where

Face and voice vectors are biometric data. They live in `USER_PATH/media_index_people/` (owner-only files), apart from the media
index shelf, so they are never part of a shelf export or a project's index folder. Nothing is sent anywhere, no vector is logged,
and a person has a name only when the user gave one. The Index panel's "Delete people data…" button and `erase_people_data_tool`
remove everything.

# Place names

Positions in a clip's tags are named offline from `src/classes/media_index/data/places.tsv.gz`, built by `scripts/build_gazetteer.py` from
GeoNames' `cities15000` and `countryInfo` (places with more than 15,000 people, or capitals; about 580 KB). GeoNames data is licensed
**CC BY 4.0**: credit "Place names: GeoNames (geonames.org), CC BY 4.0" (returned by `get_project_overview_tool` as `trip.place_names`;
it should also appear in the About dialog). Lookups never touch the network, and a position far from any listed place stays a coordinate.

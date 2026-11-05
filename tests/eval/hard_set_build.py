"""Assemble the hard test: three edited videos from confusable segments (one with narration at known times) plus six
continuous takes, and the labelled queries (answer = video + time spans). Labels were written by looking at frame strips.

usage: python tests/eval/hard_set_build.py <set_dir>  -> <set_dir>/hard/videos/*.mp4, labels.json (Windows: the narration uses System.Speech)
"""
import json
import os
import shutil
import subprocess
import sys

FF = shutil.which("ffmpeg") or "ffmpeg"
FP = shutil.which("ffprobe") or "ffprobe"
QEVAL = sys.argv[1]
HARD = os.path.join(QEVAL, "hard")
RAW = os.path.join(HARD, "raw")
VID = os.path.join(HARD, "videos")
os.makedirs(VID, exist_ok=True)


def ff(*a):
    subprocess.run([FF, "-nostdin", "-y", "-hide_banner", "-loglevel", "error", *a], check=True)


def dur(p):
    return float(subprocess.run([FP, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p], capture_output=True, text=True).stdout)


# what each segment shows, as a user would ask for it (details that tell it apart from its siblings)
SEGMENT_QUERIES = {
    "dog_1": "a dark brown dog running straight toward the camera across a lawn",
    "dog_2": "two light tan dogs wrestling among big rocks",
    "dog_3": "two grey dogs play fighting in dry scrubland",
    "dog_4": "close up of a grey and white pit bull with a white collar panting in tall grass",
    "walk_1": "a woman in a light blue vest and jeans walking away down a street with autumn trees",
    "walk_2": "a woman with a backpack and a plaid coat walking at night past neon shop signs",
    "walk_3": "a man in a yellow jacket walking toward the camera on a sunny street between brick buildings",
    "walk_4": "low shot of someone's legs in dark trousers walking on a pavement",
    "bike_1": "a person in a grey sweatshirt standing and holding a bicycle by orange traffic barriers",
    "bike_2": "close up of the back wheel and pedals of a red bicycle being ridden",
    "bike_3": "first person view riding a bike down a dirt trail in a forest",
    "bike_4": "first person view of a bicycle with a white basket riding over a wooden boardwalk",
    "cook_1": "someone whisking eggs in a plastic bowl next to chopped tomatoes",
    "cook_2": "a chef sprinkling herbs over vegetables in a frying pan",
    "cook_3": "a cook dropping noodles into a pot of boiling water with chopsticks",
    "cook_4": "a bearded man in an apron feeding pasta dough through a pasta machine",
    "beach_1": "aerial view of a beach with colourful boats lined up on the sand and a city behind",
    "beach_2": "drone flying low over shallow water full of people wading, with a skyline",
    "beach_3": "aerial view of waves rolling onto a beach",
    "beach_4": "a man in a black t-shirt and a woman in white shorts walking along the beach",
    "cafe_1": "empty outdoor cafe tables along a pedestrian street with bunting flags",
    "cafe_2": "a woman in a red sweater drinking coffee at a counter while a man talks to her",
    "cafe_3": "three young people on a sofa reading a menu together",
    "cafe_4": "three elderly people with white hair looking at a menu at a table",
    "kid_1": "a blonde toddler stacking colourful plastic blocks into a tall tower",
    "kid_2": "a toddler building a cube out of colourful interlocking plastic pieces",
    "kid_3": "a little girl with hair buns scooping sand into a green bucket",
    "kid_4": "a child in suspenders crawling over white sand with a toy dinosaur",
    "car_1": "cars driving past on an empty grey road seen from above",
    "car_2": "view from the driver's seat as a woman drives through city traffic",
    "car_3": "a blue car driving down a dirt road through golden fields at sunset",
    "car_4": "driving on a dark road at night with street lights",
}
# within-segment moment (seconds inside that segment) where the query only fits part of it
SEGMENT_SPANS = {"car_1": [[9.0, 15.0]]}
EDITS = {
    "edit_a": ["dog_1", "walk_1", "bike_2", "cook_1", "beach_1", "cafe_2", "kid_1", "car_3"],
    "edit_b": ["dog_2", "walk_2", "bike_3", "cook_4", "beach_4", "cafe_3", "kid_3", "car_1"],
    "edit_c": ["dog_3", "walk_3", "bike_1", "cook_2", "beach_2", "cafe_1", "kid_2", "car_2",
               "dog_4", "walk_4", "bike_4", "cook_3", "beach_3", "cafe_4", "kid_4", "car_4"],
}
# narration placed on edit_a at known times: (start second, sentence, query)
NARRATION = [
    (14.0, "Remember to pack the sunscreen before we drive to the coast tomorrow.", "someone reminding to bring sunscreen for a trip"),
    (52.0, "The team meeting has been moved to Thursday afternoon because of the holiday.", "a meeting being rescheduled"),
    (88.0, "I think the battery in this camera is almost dead, we should charge it tonight.", "someone saying the camera battery is running out"),
]
# continuous takes: (query, spans in seconds)
LONG_QUERIES = {
    "long_fetch": [("a dog swimming far out in a lake with only its head above the water", [[0, 12]]),
                   ("a black and white dog carrying a yellow tennis ball in its mouth through shallow water", [[26, 30], [38, 45]]),
                   ("a dog dropping a tennis ball in the water right in front of the camera", [[44, 52]])],
    "long_skate": [("a man in a white hoodie and jeans pushing a cart away across a plaza", [[6, 24]]),
                   ("a skateboarder dressed in black riding past a fountain", [[32, 40]]),
                   ("a skateboarder in a red cap and all white clothes rolling right past the camera", [[40, 58]])],
    "long_kids": [("kids playing football on a dirt pitch seen from a drone close above", [[0, 10]]),
                  ("a small dirt clearing in the middle of a dense green forest seen from high above", [[19, 48]])],
    "long_cook": [("mixing flour and an egg in a glass bowl with a fork", None)],
    "long_hike": [("a dirt trail through a bare autumn forest", None)],
    "long_street": [("a street performer spinning fire at night in front of a crowd", None)],
}
NEGATIVE = ["a cat sleeping on a windowsill", "a person swimming in a pool", "a snowboarder going down a slope",
            "a man playing the piano", "a group of people dancing at a wedding", "a train arriving at a station"]

labels = {"videos": {}, "queries": [], "negative": NEGATIVE}
for edit, segs in EDITS.items():
    out = os.path.join(VID, edit + ".mp4")
    t, layout = 0.0, []
    for s in segs:
        d = dur(os.path.join(RAW, s + ".mp4"))
        layout.append((s, round(t, 3), round(t + d, 3)))
        t += d
    if not os.path.exists(out):
        inputs = sum((["-i", os.path.join(RAW, s + ".mp4")] for s in segs), [])
        norm = "".join(f"[{i}:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[s{i}];" for i in range(len(segs)))
        concat = norm + "".join(f"[s{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0[v]"
        if edit == "edit_a":
            wavs = []
            for k, (_, sentence, _) in enumerate(NARRATION):
                w = os.path.join(HARD, f"narr_{k}.wav")
                ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                      f"$s.SetOutputToWaveFile('{w}'); $s.Speak('{sentence}'); $s.Dispose()")
                subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
                wavs.append(w)
            n = len(segs)
            ainputs = sum((["-i", w] for w in wavs), [])
            delays = "".join(f"[{n + k}:a]adelay={int(NARRATION[k][0] * 1000)}:all=1[a{k}];" for k in range(len(wavs)))
            mix = "".join(f"[a{k}]" for k in range(len(wavs))) + f"amix=inputs={len(wavs)}:normalize=0,apad[a]"
            ff(*inputs, *ainputs, "-filter_complex", concat + ";" + delays + mix, "-map", "[v]", "-map", "[a]", "-shortest",
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", out)
        else:
            ff(*inputs, "-filter_complex", concat, "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", out)
    labels["videos"][edit] = {"duration": round(dur(out), 2), "segments": layout}
    for s, a, b in layout:
        spans = [[a + x, a + y] for x, y in SEGMENT_SPANS.get(s, [[0, b - a]])]
        labels["queries"].append({"text": SEGMENT_QUERIES[s], "kind": "edited", "video": edit, "spans": spans, "segment": s})
for k, (start, sentence, q) in enumerate(NARRATION):
    w = os.path.join(HARD, f"narr_{k}.wav")
    labels["queries"].append({"text": q, "kind": "spoken", "video": "edit_a", "spans": [[start, start + dur(w)]], "sentence": sentence})
for name, qs in LONG_QUERIES.items():
    src = os.path.join(RAW, name + ".mp4")
    out = os.path.join(VID, name + ".mp4")
    if not os.path.exists(out):
        ff("-i", src, "-c", "copy", out)
    d = round(dur(out), 2)
    labels["videos"][name] = {"duration": d, "segments": [[name, 0.0, d]]}
    for q, spans in qs:
        labels["queries"].append({"text": q, "kind": "continuous" if spans else "whole_take", "video": name, "spans": spans or [[0.0, d]]})
json.dump(labels, open(os.path.join(HARD, "labels.json"), "w"), indent=1)
print({v: m["duration"] for v, m in labels["videos"].items()}, "queries", len(labels["queries"]))

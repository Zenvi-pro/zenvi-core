// Zenvi → After Effects export v1
#target aftereffects
// Built by Zenvi golden from /fixtures/project/Trip.zvn.
// Run it in After Effects with File > Scripts > Run Script File..., or from Zenvi with
// File > Send To > After Effects (needs the Zenvi Link panel). It builds one undo step:
// Edit > Undo "Import Zenvi project" removes everything it made ("Zenvi: Run JSX file" when Zenvi
// Link ran it).
// ES3 (ExtendScript); everything below is plain ASCII.

(function () {
    var ZENVI_EXPORT = {
        "version": 1,
        "project": "Trip",
        "project_path": "/fixtures/project/Trip.zvn",
        "generator": "Zenvi golden",
        "fps": {"num": 30, "den": 1},
        "width": 1920,
        "height": 1080,
        "interactive": true
    };

    var DATA = {
        "folder": "Zenvi \u2014 Trip",
        "comp": {"name": "Trip", "w": 1920, "h": 1080, "par": 1, "fps": 30, "dur": 12.533333333, "bg": [0, 0, 0]},
        "footage": [
            {
                "id": "F1",
                "name": "beach.mp4",
                "kind": "video",
                "abs": "/fixtures/export/Trip_AfterEffects/media/beach.mp4",
                "rel": "media/beach.mp4",
                "w": 1280,
                "h": 720,
                "fps": 30,
                "dur": 12
            },
            {
                "id": "F2",
                "name": "city.mp4",
                "kind": "video",
                "abs": "/fixtures/export/Trip_AfterEffects/media/city.mp4",
                "rel": "media/city.mp4",
                "w": 1280,
                "h": 720,
                "fps": 30,
                "dur": 10
            },
            {
                "id": "F3",
                "name": "drone.mp4",
                "kind": "video",
                "abs": "/fixtures/export/Trip_AfterEffects/media/drone.mp4",
                "rel": "media/drone.mp4",
                "w": 1280,
                "h": 720,
                "fps": 30,
                "dur": 20
            },
            {
                "id": "F4",
                "name": "Wipe wipe_left_to_right.png",
                "kind": "still",
                "abs": "/fixtures/export/masks/wipe_left_to_right.png",
                "rel": "masks/wipe_left_to_right.png",
                "w": 1920,
                "h": 1080,
                "fps": 30,
                "dur": 12.533333
            },
            {
                "id": "F5",
                "name": "photo.jpg",
                "kind": "still",
                "abs": "/fixtures/export/Trip_AfterEffects/media/photo.jpg",
                "rel": "media/photo.jpg",
                "w": 1880,
                "h": 1253,
                "fps": 30,
                "dur": 3600
            },
            {
                "id": "F6",
                "name": "music.mp3",
                "kind": "audio",
                "abs": "/fixtures/export/Trip_AfterEffects/media/music.mp3",
                "rel": "media/music.mp3",
                "w": 720,
                "h": 480,
                "fps": 30,
                "dur": 76.433334
            }
        ],
        "titles": [
            {
                "id": "T1",
                "name": "Standard_3",
                "w": 1920,
                "h": 1080,
                "fps": 30,
                "dur": 12.533333,
                "comment": "Zenvi title Standard_3 (/fixtures/project/Trip_assets/titles/Standard_3.svg)",
                "items": [
                    {
                        "type": "text",
                        "name": "Line 1",
                        "text": "Line 1",
                        "x": 958.945313,
                        "y": 432.10787,
                        "size": 90,
                        "family": "DejaVu Sans",
                        "bold": true,
                        "italic": false,
                        "just": "CENTER_JUSTIFY",
                        "fill": [0.435294, 0.509804, 0.756863],
                        "stroke": null,
                        "sw": 2.812513,
                        "track": 0,
                        "op": 100
                    },
                    {
                        "type": "text",
                        "name": "Line 2",
                        "text": "Line 2",
                        "x": 958.945313,
                        "y": 573.54975,
                        "size": 90,
                        "family": "DejaVu Sans",
                        "bold": true,
                        "italic": false,
                        "just": "CENTER_JUSTIFY",
                        "fill": [0.435294, 0.509804, 0.756863],
                        "stroke": null,
                        "sw": 2.812513,
                        "track": 0,
                        "op": 100
                    },
                    {
                        "type": "text",
                        "name": "Line 3",
                        "text": "Line 3",
                        "x": 958.945313,
                        "y": 714.99149,
                        "size": 90,
                        "family": "DejaVu Sans",
                        "bold": true,
                        "italic": false,
                        "just": "CENTER_JUSTIFY",
                        "fill": [0.435294, 0.509804, 0.756863],
                        "stroke": null,
                        "sw": 2.812513,
                        "track": 0,
                        "op": 100
                    }
                ]
            }
        ],
        "guides": [
            {
                "key": "G1",
                "src": "F4",
                "name": "Wipe image: wipe_left_to_right",
                "comment": "Zenvi transition wipe image /zenvi/transitions/common/wipe_left_to_right.svg; read by Gradient Wipe effects"
            }
        ],
        "layers": [
            {
                "key": "L1",
                "name": "beach.mp4",
                "src": "F1",
                "kind": "av",
                "inp": 0,
                "outp": 5,
                "start": -1,
                "stretch": 100,
                "tf": {"anchor": [640, 360], "pos": [960, 540], "scale": [150, 150], "rot": 0, "op": 100},
                "levels": {
                    "t": [
                        0, 4.033333333, 4.133333333, 4.233333333, 4.4, 4.533333333, 4.6, 4.666666667,
                        4.733333333, 4.766666667, 4.8, 4.833333333, 4.866666667, 4.9, 4.933333333,
                        4.966666667
                    ],
                    "v": [
                        [0, 0],
                        [0, 0],
                        [-0.115184, -0.115184],
                        [-0.467012, -0.467012],
                        [-1.642656, -1.642656],
                        [-3.252026, -3.252026],
                        [-4.362392, -4.362392],
                        [-5.754519, -5.754519],
                        [-7.537308, -7.537308],
                        [-8.632723, -8.632723],
                        [-9.91598, -9.91598],
                        [-11.452604, -11.452604],
                        [-13.352425, -13.352425],
                        [-15.821396, -15.821396],
                        [-19.321958, -19.321958],
                        [-25.32981, -25.32981]
                    ],
                    "i": ["l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l"],
                    "sp": 1
                },
                "label": 9,
                "comment": "Zenvi clip C0007 on track 1 (Video)"
            },
            {
                "key": "L2",
                "name": "city.mp4",
                "src": "F2",
                "kind": "av",
                "inp": 4,
                "outp": 8,
                "start": 4,
                "stretch": 100,
                "tf": {
                    "anchor": [640, 360],
                    "pos": [960, 540],
                    "scale": [150, 150],
                    "rot": 0,
                    "op": {
                        "t": [
                            4, 4.133333333, 4.166666667, 4.2, 4.233333333, 4.266666667, 4.3, 4.333333333,
                            4.366666667, 4.4, 4.433333333, 4.466666667, 4.5, 7.966666667
                        ],
                        "v": [
                            0, 0, 1.960784, 6.666667, 12.941176, 20.392157, 29.411765, 39.215686, 51.372549,
                            64.313725, 78.431373, 93.72549, 100, 100
                        ],
                        "i": ["l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l"]
                    }
                },
                "levels": {
                    "t": [
                        4, 4.033333333, 4.066666667, 4.1, 4.133333333, 4.166666667, 4.2, 4.233333333,
                        4.266666667, 4.3, 4.366666667, 4.433333333, 4.5, 4.633333333, 4.766666667,
                        4.933333333, 7.966666667
                    ],
                    "v": [
                        [-96, -96],
                        [-96, -96],
                        [-25.32981, -25.32981],
                        [-19.321958, -19.321958],
                        [-15.821396, -15.821396],
                        [-13.352425, -13.352425],
                        [-11.452604, -11.452604],
                        [-9.91598, -9.91598],
                        [-8.632723, -8.632723],
                        [-7.537308, -7.537308],
                        [-5.754519, -5.754519],
                        [-4.362392, -4.362392],
                        [-3.252026, -3.252026],
                        [-1.642656, -1.642656],
                        [-0.639903, -0.639903],
                        [-0.051067, -0.051067],
                        [0, 0]
                    ],
                    "i": ["l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l"],
                    "sp": 1
                },
                "label": 9,
                "comment": "Zenvi clip C0008 on track 1 (Video)"
            },
            {
                "key": "L3",
                "name": "drone.mp4",
                "src": "F3",
                "kind": "av",
                "inp": 7.5,
                "outp": 12.533333333,
                "start": 7.491666667,
                "stretch": 50,
                "tf": {"anchor": [640, 360], "pos": [960, 540], "scale": [150, 150], "rot": 0, "op": 100},
                "fx": [
                    {
                        "label": "Wipe transition",
                        "name": "Wipe wipe_left_to_right",
                        "opts": [
                            {
                                "match": "ADBE Gradient Wipe",
                                "params": [
                                    {
                                        "ids": ["ADBE Gradient Wipe-0001", "Transition Completion"],
                                        "val": {
                                            "t": [
                                                7.5, 7.566666667, 7.6, 7.633333333, 7.666666667, 7.7,
                                                7.733333333, 7.766666667, 7.8, 7.833333333, 7.866666667, 7.9,
                                                7.933333333, 12.5
                                            ],
                                            "v": [
                                                100, 100, 97.081619, 90.814548, 82.037394, 70.663249,
                                                57.16954, 42.83046, 29.336751, 17.962606, 9.185452, 2.918381,
                                                0, 0
                                            ],
                                            "i": ["l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l"]
                                        }
                                    },
                                    {
                                        "ids": ["ADBE Gradient Wipe-0002", "Transition Softness"],
                                        "val": 84.999996
                                    },
                                    {
                                        "ids": ["ADBE Gradient Wipe-0003", "Gradient Layer"],
                                        "val": {"layer": "G1"}
                                    },
                                    {"ids": ["ADBE Gradient Wipe-0004", "Gradient Placement"], "val": 3},
                                    {"ids": ["ADBE Gradient Wipe-0005", "Invert Gradient"], "val": 1}
                                ]
                            }
                        ]
                    }
                ],
                "label": 9,
                "comment": "Zenvi clip C0010 on track 1 (Video)"
            },
            {
                "key": "L4",
                "name": "photo.jpg",
                "src": "F5",
                "kind": "still",
                "inp": 1,
                "outp": 5,
                "start": 1,
                "stretch": 100,
                "tf": {
                    "anchor": [940, 626.5],
                    "pos": {
                        "t": [1, 2],
                        "v": [[0, 540], [960, 540]],
                        "i": ["b"],
                        "o": [[[0, 50]]],
                        "n": [[[0, 50]]],
                        "sp": 1
                    },
                    "scale": {
                        "t": [1, 5],
                        "v": [[77.553191, 77.573823], [90.478723, 90.502793]],
                        "i": ["b"],
                        "o": [[[0, 50], [0, 50]]],
                        "n": [[[6.462766, 50], [6.464485, 50]]]
                    },
                    "rot": 0,
                    "op": {"t": [1, 1.5], "v": [0, 100], "i": ["b"], "o": [[[0, 50]]], "n": [[[0, 50]]]}
                },
                "label": 8,
                "comment": "Zenvi clip C0012 on track 2 (Track 2)"
            },
            {
                "key": "L5",
                "name": "Standard_3.svg",
                "src": "T1",
                "kind": "still",
                "inp": 2,
                "outp": 6,
                "start": 2,
                "stretch": 100,
                "tf": {"anchor": [960, 540], "pos": [960, 540], "scale": [100, 100], "rot": 0, "op": 100},
                "label": 10,
                "comment": "Zenvi clip C0013 on track 3 (Track 3)"
            },
            {
                "key": "L6",
                "name": "music.mp3",
                "src": "F6",
                "kind": "audio",
                "inp": 0,
                "outp": 12.5,
                "start": 0,
                "stretch": 100,
                "levels": {
                    "t": [
                        0, 11, 11.166666667, 11.333333333, 11.466666667, 11.566666667, 11.666666667,
                        11.766666667, 11.866666667, 11.966666667, 12.066666667, 12.133333333, 12.2,
                        12.233333333, 12.266666667, 12.3, 12.333333333, 12.366666667, 12.4, 12.433333333,
                        12.466666667
                    ],
                    "v": [
                        [0, 0],
                        [0, 0],
                        [-0.159936, -0.159936],
                        [-0.737474, -0.737474],
                        [-1.659229, -1.659229],
                        [-2.748456, -2.748456],
                        [-4.300241, -4.300241],
                        [-6.415245, -6.415245],
                        [-9.150364, -9.150364],
                        [-12.536911, -12.536911],
                        [-16.663089, -16.663089],
                        [-19.940645, -19.940645],
                        [-23.809475, -23.809475],
                        [-26.046922, -26.046922],
                        [-28.556872, -28.556872],
                        [-31.423382, -31.423382],
                        [-34.777214, -34.777214],
                        [-38.837827, -38.837827],
                        [-44.016988, -44.016988],
                        [-51.23949, -51.23949],
                        [-63.456674, -63.456674]
                    ],
                    "i": [
                        "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l", "l",
                        "l", "l", "l"
                    ],
                    "sp": 1
                },
                "label": 11,
                "comment": "Zenvi clip C0014 on track 4 (Music)"
            }
        ],
        "markers": [{"t": 2, "c": "Title in", "lb": 1}, {"t": 7.5, "c": "Wipe", "lb": 8}],
        "comment": "Built by Zenvi golden from /fixtures/project/Trip.zvn. Nothing was left out.",
        "notes": [],
        "title_folder": true
    };

    // ------------------------------------------------------------------
    // Zenvi -> After Effects runtime (the same code in every export).

    var KIT = KeyframeInterpolationType;

    function warn(R, text) {
        R.warnings.push(String(text));
    }

    function errText(e) {
        var s = (e && e.message) ? e.message : String(e);
        if (e && e.line) {
            s += " (line " + e.line + ")";
        }
        return s;
    }

    function quote(s) {
        var out = '"', i, code, hex;
        s = String(s);
        for (i = 0; i < s.length; i++) {
            code = s.charCodeAt(i);
            if (code === 34 || code === 92) {
                out += "\\" + s.charAt(i);
            } else if (code < 32 || code > 126) {
                hex = code.toString(16);
                while (hex.length < 4) {
                    hex = "0" + hex;
                }
                out += "\\u" + hex;
            } else {
                out += s.charAt(i);
            }
        }
        return out + '"';
    }

    function stringify(v) {
        var parts = [], i, k;
        if (v === null || v === undefined) {
            return "null";
        }
        if (typeof v === "number") {
            return isFinite(v) ? String(v) : "null";
        }
        if (typeof v === "boolean") {
            return v ? "true" : "false";
        }
        if (typeof v === "string") {
            return quote(v);
        }
        if (v instanceof Array) {
            for (i = 0; i < v.length; i++) {
                parts.push(stringify(v[i]));
            }
            return "[" + parts.join(",") + "]";
        }
        for (k in v) {
            if (v.hasOwnProperty(k)) {
                parts.push(quote(k) + ":" + stringify(v[k]));
            }
        }
        return "{" + parts.join(",") + "}";
    }

    function quietRun() {
        // Zenvi's runner (and Zenvi Link's ae_run_jsx_file) set this flag for one run. Reading it also
        // clears it, so even a run that stops half-way cannot leave it in After Effects' shared global
        // scope, where it would hide the alert of a later File > Scripts run.
        var on = false;
        try {
            on = $.global.ZENVI_AE_QUIET === true;
            if ($.global.ZENVI_AE_QUIET !== undefined) {
                delete $.global.ZENVI_AE_QUIET;
            }
        } catch (e) {}
        return on;
    }

    function fileAt(path) {
        // File() reads "%XX" as an escape: a literal % in a file name must be passed as %25
        return new File(String(path).replace(/%/g, "%25"));
    }

    function child(group, key) {
        // a missing or stale group gives null (setProp then warns) instead of an exception
        try {
            return group ? group.property(key) : null;
        } catch (e) {
            return null;
        }
    }

    function scriptFolder() {
        var f;
        try {
            f = fileAt($.fileName);
            if (f.exists) {
                return f.parent;
            }
        } catch (e) {}
        return null;
    }

    function findFile(entry, base) {
        var f;
        if (entry.rel && base) {
            f = fileAt(base.fsName + "/" + entry.rel);
            if (f.exists) {
                return f;
            }
        }
        if (entry.abs) {
            f = fileAt(entry.abs);
            if (f.exists) {
                return f;
            }
        }
        return null;
    }

    function linkedComp(entry) {
        var c = entry.aecomp, item = null, open;
        if (!c || !app.project.file || !c.id) {
            return null;
        }
        try {
            open = app.project.file.fsName;
            if (c.aep && fileAt(c.aep).fsName !== open) {
                return null;
            }
            if (typeof app.project.itemByID === "function") {
                item = app.project.itemByID(c.id);
            }
        } catch (e) {
            item = null;
        }
        if (item && item instanceof CompItem && (!c.name || item.name === c.name)) {
            return item;
        }
        return null;
    }

    function importOne(entry, base, folder, R) {
        var file = findFile(entry, base), opts, item = null;
        if (file) {
            try {
                opts = new ImportOptions(file);
                if (entry.seq) {
                    opts.sequence = true;
                    opts.forceAlphabetical = false;
                }
                if (opts.canImportAs(ImportAsType.FOOTAGE)) {
                    opts.importAs = ImportAsType.FOOTAGE;
                }
                item = app.project.importFile(opts);
            } catch (e) {
                warn(R, "Could not import " + entry.name + ": " + errText(e) + "; used a placeholder instead");
                item = null;
            }
        } else {
            warn(R, "Missing media: " + entry.name + " (" + (entry.abs || entry.rel) +
                "); used a placeholder. Relink it with File > Replace Footage > File.");
        }
        if (!item) {
            item = app.project.importPlaceholder(entry.name, entry.w, entry.h, entry.fps, entry.dur);
            R.placeholders.push(entry.name);
        }
        item.parentFolder = folder;
        if (entry.seq && entry.fps > 0) {
            try {
                item.mainSource.conformFrameRate = entry.fps;
            } catch (e2) {}
        }
        if (entry.comment) {
            item.comment = entry.comment;
        }
        return item;
    }

    function fit(p, v) {
        var cur, out, i;
        if (!(v instanceof Array)) {
            return v;
        }
        try {
            cur = p.value;
        } catch (e) {
            return v;
        }
        if (!(cur instanceof Array) || cur.length <= v.length) {
            return v;
        }
        out = v.slice(0);
        for (i = v.length; i < cur.length; i++) {
            out.push(cur[i]);
        }
        return out;
    }

    function interp(code) {
        if (code === "b") {
            return KIT.BEZIER;
        }
        if (code === "h") {
            return KIT.HOLD;
        }
        return KIT.LINEAR;
    }

    function easeCount(p) {
        var t = p.propertyValueType;
        if (t === PropertyValueType.TwoD) {
            return 2;
        }
        if (t === PropertyValueType.ThreeD) {
            return 3;
        }
        return 1;
    }

    function eases(list, count) {
        var out = [], d;
        for (d = 0; d < count; d++) {
            if (list && d < list.length) {
                out.push(new KeyframeEase(list[d][0], list[d][1]));
            } else {
                out.push(new KeyframeEase(0, 33.333333));
            }
        }
        return out;
    }

    function flatTangents(p, idx) {
        var v = p.keyValue(idx), a = [], b = [], d;
        for (d = 0; d < v.length; d++) {
            a.push(0);
            b.push(0);
        }
        try {
            p.setSpatialContinuousAtKey(idx, false);
            p.setSpatialAutoBezierAtKey(idx, false);
            p.setSpatialTangentsAtKey(idx, a, b);
        } catch (e) {}
    }

    function setKeys(p, k) {
        var n = k.t.length, vals = [], i, idx, inT, outT, count, spatial, fresh = p.numKeys === 0;
        for (i = 0; i < n; i++) {
            vals.push(fit(p, k.v[i]));
        }
        p.setValuesAtTimes(k.t, vals);
        count = easeCount(p);
        spatial = k.sp && p.isSpatial;
        // a property that had no keys holds ours in time order: key i + 1, no search per key
        fresh = fresh && p.numKeys === n;
        for (i = 0; i < n; i++) {
            idx = fresh ? i + 1 : p.nearestKeyIndex(k.t[i]);
            inT = interp(i > 0 ? k.i[i - 1] : (n > 1 ? k.i[0] : "l"));
            outT = interp(i < n - 1 ? k.i[i] : (n > 1 ? k.i[n - 2] : "l"));
            if (spatial) {
                flatTangents(p, idx);
            }
            // the type first: After Effects may recompute a side's ease when that side becomes Bezier
            p.setInterpolationTypeAtKey(idx, inT, outT);
            if (inT === KIT.BEZIER || outT === KIT.BEZIER) {
                p.setTemporalContinuousAtKey(idx, false);
                p.setTemporalAutoBezierAtKey(idx, false);
                p.setTemporalEaseAtKey(idx, eases(k.n && i > 0 ? k.n[i - 1] : null, count),
                    eases(k.o && i < n - 1 ? k.o[i] : null, count));
                if (inT !== outT) {
                    // setting an ease can turn the linear or hold side into Bezier: restore it
                    p.setInterpolationTypeAtKey(idx, inT, outT);
                }
            }
        }
    }

    function setProp(p, spec, R, label) {
        if (spec === null || spec === undefined) {
            return;
        }
        if (!p) {
            warn(R, label + ": property not found");
            return;
        }
        try {
            if (typeof spec === "object" && !(spec instanceof Array)) {
                setKeys(p, spec);
            } else {
                p.setValue(fit(p, spec));
            }
        } catch (e) {
            warn(R, label + ": " + errText(e));
        }
    }

    function shapeOf(s) {
        var sh = new Shape(), a = [], b = [], k;
        for (k = 0; k < s.pts.length; k++) {
            a.push([0, 0]);
            b.push([0, 0]);
        }
        sh.vertices = s.pts;
        sh.inTangents = s.vi || a;
        sh.outTangents = s.vo || b;
        sh.closed = s.closed !== false;
        return sh;
    }

    function addMask(L, m, R, label) {
        var mk, idx, sp, shapes = [], j, key;
        try {
            mk = L.property("ADBE Mask Parade").addProperty("ADBE Mask Atom");
            idx = mk.propertyIndex;
            mk = L.property("ADBE Mask Parade").property(idx);
            if (m.name) {
                mk.name = m.name;
            }
            mk.maskMode = MaskMode.ADD;
            if (m.inv) {
                mk.inverted = true;
            }
            sp = mk.property("ADBE Mask Shape");
            if (m.shape.t) {
                for (j = 0; j < m.shape.v.length; j++) {
                    shapes.push(shapeOf(m.shape.v[j]));
                }
                sp.setValuesAtTimes(m.shape.t, shapes);
                for (j = 0; j < m.shape.t.length; j++) {
                    key = sp.nearestKeyIndex(m.shape.t[j]);
                    sp.setInterpolationTypeAtKey(key, KIT.LINEAR, KIT.LINEAR);
                }
            } else {
                sp.setValue(shapeOf(m.shape));
            }
        } catch (e) {
            warn(R, label + " mask " + (m.name || "") + ": " + errText(e));
        }
    }

    function valueKind(v) {
        // the PropertyValueType a parameter needs to take this value ("2" / "3": either spatial or not)
        var first = v;
        if (v !== null && typeof v === "object" && !(v instanceof Array)) {
            if (v.layer) {
                return PropertyValueType.LAYER_INDEX;
            }
            if (v.v && v.v.length) {
                first = v.v[0];
            }
        }
        if (first instanceof Array) {
            if (first.length === 4) {
                return PropertyValueType.COLOR;
            }
            return first.length === 3 ? "3" : "2";
        }
        return PropertyValueType.OneD;
    }

    function kindFits(p, want) {
        var t = p.propertyValueType;
        if (want === "2") {
            return t === PropertyValueType.TwoD || t === PropertyValueType.TwoD_SPATIAL;
        }
        if (want === "3") {
            return t === PropertyValueType.ThreeD || t === PropertyValueType.ThreeD_SPATIAL;
        }
        return t === want;
    }

    function english() {
        try {
            return String(app.isoLanguage || "").indexOf("en") === 0;
        } catch (e) {
            return false;
        }
    }

    function isMatchName(id) {
        // effect parameter match names end in a 4-digit index ("ADBE Mosaic-0001"); display names do not
        return /-\d{4}$/.test(String(id));
    }

    function findParam(e, prm) {
        // prm.ids: match names and the English display name, in the order to try. A parameter must take
        // the value's kind; one found by an unverified match name (no prm.ok) must also carry the display
        // name in an English After Effects -- so a wrong guess never sets another parameter.
        var ids = prm.ids, want = valueKind(prm.val), named = null, i, p;
        for (i = 0; i < ids.length; i++) {
            if (!isMatchName(ids[i])) {
                named = ids[i];
                break;
            }
        }
        for (i = 0; i < ids.length; i++) {
            p = child(e, ids[i]);
            if (!p) {
                continue;
            }
            try {
                if (!kindFits(p, want)) {
                    continue;
                }
                if (!prm.ok && named && isMatchName(ids[i]) && english() && p.name !== named) {
                    continue;
                }
            } catch (err) {
                continue;
            }
            return p;
        }
        return null;
    }

    function addEffect(L, fx, R, label, refs) {
        var parade = L.property("ADBE Effect Parade"), e = null, used = null, j, k, prm, p, idx, names = [];
        for (j = 0; j < fx.opts.length && !e; j++) {
            names.push(fx.opts[j].match);
            try {
                if (parade.canAddProperty(fx.opts[j].match)) {
                    e = parade.addProperty(fx.opts[j].match);
                    used = fx.opts[j];
                }
            } catch (err) {
                e = null;
            }
        }
        if (!e) {
            warn(R, label + ": After Effects has no " + fx.label + " effect (" + names.join(", ") + "); skipped");
            return;
        }
        idx = e.propertyIndex;
        if (fx.name) {
            try {
                e.name = fx.name;
            } catch (e1) {}
        }
        for (k = 0; k < used.params.length; k++) {
            prm = used.params[k];
            p = findParam(child(L.property("ADBE Effect Parade"), idx), prm);
            if (!p) {
                warn(R, label + ": " + fx.label + " has no parameter " + prm.ids.join(" / ") + "; left at its default");
                continue;
            }
            if (prm.val !== null && typeof prm.val === "object" && prm.val.layer) {
                refs.push({"layer": L, "fx": idx, "prm": prm, "target": prm.val.layer, "label": label});
                continue;
            }
            setProp(p, prm.val, R, label + " " + fx.label);
        }
        if (fx.on === false) {
            L.property("ADBE Effect Parade").property(idx).enabled = false;
        }
    }

    function fontList() {
        return !!(app.fonts && typeof app.fonts.getFontsByFamilyNameAndStyleName === "function");
    }

    function psGuesses(family, bold, italic) {
        // the usual PostScript names of a family's style (ArialMT, Arial-BoldMT, Ubuntu-Regular...)
        var base = "", i, ch;
        for (i = 0; i < family.length; i++) {
            ch = family.charAt(i);
            if (ch !== " ") {
                base += ch;
            }
        }
        if (bold && italic) {
            return [base + "-BoldItalic", base + "-BoldItalicMT", base + "-BoldOblique"];
        }
        if (bold) {
            return [base + "-Bold", base + "-BoldMT", base + "Bold"];
        }
        if (italic) {
            return [base + "-Italic", base + "-ItalicMT", base + "-Oblique"];
        }
        return [base + "-Regular", base, base + "MT", base + "-Roman", base + "-Book"];
    }

    function fontChoices(t) {
        // [{ps, family}] to try in order: the title's family first, then common fallbacks
        var styles, families = [t.family, "Arial", "Helvetica Neue", "Helvetica"], i, j, found, out = [], guesses;
        if (t.bold) {
            styles = t.italic ? ["Bold Italic", "Bold Oblique", "BoldItalic"] : ["Bold"];
        } else {
            styles = t.italic ? ["Italic", "Oblique"] : ["Regular", "Book", "Roman", "Normal"];
        }
        if (fontList()) {
            for (i = 0; i < families.length; i++) {
                if (!families[i]) {
                    continue;
                }
                for (j = 0; j < styles.length; j++) {
                    try {
                        found = app.fonts.getFontsByFamilyNameAndStyleName(families[i], styles[j]);
                    } catch (e) {
                        found = null;
                    }
                    if (found && found.length) {
                        return [{"ps": found[0].postScriptName, "family": families[i]}];
                    }
                }
            }
            return out;
        }
        // After Effects before 24.0 has no font list: try the usual PostScript names; reading the font
        // back tells whether After Effects took one
        for (i = 0; i < families.length; i++) {
            if (!families[i]) {
                continue;
            }
            guesses = psGuesses(families[i], t.bold, t.italic);
            for (j = 0; j < guesses.length; j++) {
                out.push({"ps": guesses[j], "family": families[i]});
            }
        }
        return out;
    }

    function addText(tc, t, R, fonts) {
        var L = tc.layers.addText(t.text), src, doc, choices, used = null, j, tr;
        src = L.property("ADBE Text Properties").property("ADBE Text Document");
        doc = src.value;
        if (typeof doc.resetCharStyle === "function") {
            try {
                doc.resetCharStyle();
                doc.resetParagraphStyle();
            } catch (e0) {}
        }
        doc.text = t.text;
        doc.fontSize = t.size;
        doc.applyFill = true;
        doc.fillColor = t.fill;
        if (t.stroke) {
            doc.applyStroke = true;
            doc.strokeColor = t.stroke;
            doc.strokeWidth = t.sw;
            doc.strokeOverFill = true;
        } else {
            doc.applyStroke = false;
        }
        doc.tracking = t.track || 0;
        doc.justification = ParagraphJustification[t.just];
        src.setValue(doc);
        choices = fontChoices(t);
        for (j = 0; j < choices.length && !used; j++) {
            try {
                doc.font = choices[j].ps;
                src.setValue(doc);
                if (src.value.font === choices[j].ps) {
                    used = choices[j];
                }
            } catch (e1) {}
        }
        if ((!used || used.family !== t.family) && !fonts[t.family]) {
            fonts[t.family] = true;
            if (fontList()) {
                warn(R, "Font " + t.family + " is not installed in After Effects; titles use " +
                    src.value.font + " instead");
            } else {
                warn(R, "Font " + t.family + " was not found under its usual PostScript names (this After " +
                    "Effects has no font list before 24.0); titles use " + src.value.font + " instead. Pick " +
                    t.family + " in the Character panel if it is installed");
            }
        }
        L.name = t.name;
        tr = L.property("ADBE Transform Group");
        tr.property("ADBE Position").setValue(fit(tr.property("ADBE Position"), [t.x, t.y]));
        if (t.op < 100) {
            tr.property("ADBE Opacity").setValue(t.op);
        }
        return L;
    }

    function addRect(tc, r) {
        var L = tc.layers.addShape(), tr, grp, contents, shape, fill, stroke;
        L.name = r.name;
        tr = L.property("ADBE Transform Group");
        tr.property("ADBE Anchor Point").setValue(fit(tr.property("ADBE Anchor Point"), [0, 0]));
        tr.property("ADBE Position").setValue(fit(tr.property("ADBE Position"), [0, 0]));
        grp = L.property("ADBE Root Vectors Group").addProperty("ADBE Vector Group");
        grp.name = r.name;
        contents = grp.property("ADBE Vectors Group");
        shape = contents.addProperty("ADBE Vector Shape - Rect");
        shape.property("ADBE Vector Rect Size").setValue([r.w, r.h]);
        shape.property("ADBE Vector Rect Position").setValue([r.cx, r.cy]);
        shape.property("ADBE Vector Rect Roundness").setValue(r.round);
        if (r.stroke) {
            stroke = contents.addProperty("ADBE Vector Graphic - Stroke");
            stroke.property("ADBE Vector Stroke Color").setValue(r.stroke);
            stroke.property("ADBE Vector Stroke Width").setValue(r.sw);
            stroke.property("ADBE Vector Stroke Opacity").setValue(r.so);
        }
        if (r.fill) {
            fill = contents.addProperty("ADBE Vector Graphic - Fill");
            fill.property("ADBE Vector Fill Color").setValue(r.fill);
            fill.property("ADBE Vector Fill Opacity").setValue(r.fo);
        }
        if (r.op < 100) {
            tr.property("ADBE Opacity").setValue(r.op);
        }
        return L;
    }

    function buildTitle(T, folder, R, fonts) {
        var tc = app.project.items.addComp(T.name, T.w, T.h, 1, T.dur, T.fps), j, it;
        tc.parentFolder = folder;
        if (T.comment) {
            tc.comment = T.comment;
        }
        for (j = 0; j < T.items.length; j++) {
            it = T.items[j];
            try {
                if (it.type === "rect") {
                    addRect(tc, it);
                } else {
                    addText(tc, it, R, fonts);
                }
            } catch (e) {
                warn(R, "Title " + T.name + ": could not build " + it.name + ": " + errText(e));
            }
        }
        return tc;
    }

    function transform(L, tf, R, label) {
        var tr = L.property("ADBE Transform Group"), pos;
        if (!tr || !tf) {
            return;
        }
        setProp(child(tr, "ADBE Anchor Point"), tf.anchor, R, label + " anchor point");
        if (tf.pos && tf.pos.sep) {
            try {
                child(tr, "ADBE Position").dimensionsSeparated = true;
                // separating the dimensions changes the group: fetch it again before going on
                tr = L.property("ADBE Transform Group");
                pos = child(tr, "ADBE Position");
                setProp(pos.getSeparationFollower(0), tf.pos.x, R, label + " X position");
                setProp(pos.getSeparationFollower(1), tf.pos.y, R, label + " Y position");
            } catch (e) {
                warn(R, label + " position: " + errText(e));
                tr = L.property("ADBE Transform Group");
            }
        } else {
            setProp(child(tr, "ADBE Position"), tf.pos, R, label + " position");
        }
        setProp(child(tr, "ADBE Scale"), tf.scale, R, label + " scale");
        setProp(child(tr, "ADBE Rotate Z"), tf.rot, R, label + " rotation");
        setProp(child(tr, "ADBE Opacity"), tf.op, R, label + " opacity");
    }

    function remap(L, s, R) {
        var p, k, j, added = [], ours = [], mine;
        if (!L.canSetTimeRemapEnabled) {
            warn(R, s.name + ": After Effects cannot time-remap this layer; its speed change is missing");
            return;
        }
        L.timeRemapEnabled = true;
        p = L.property("ADBE Time Remapping");
        // turning remapping on adds two keys (in and out point): note them, set ours, then remove exactly
        // those two -- unless one of ours took its place -- so a key time After Effects rounds stays ours
        for (k = 1; k <= p.numKeys; k++) {
            added.push(p.keyTime(k));
        }
        setKeys(p, s.remap);
        for (j = 0; j < s.remap.t.length; j++) {
            ours.push(p.keyTime(p.nearestKeyIndex(s.remap.t[j])));
        }
        for (j = added.length - 1; j >= 0; j--) {
            mine = false;
            for (k = 0; k < ours.length; k++) {
                if (Math.abs(ours[k] - added[j]) < 0.000001) {
                    mine = true;
                    break;
                }
            }
            if (!mine) {
                k = p.nearestKeyIndex(added[j]);
                if (Math.abs(p.keyTime(k) - added[j]) < 0.000001) {
                    p.removeKey(k);
                }
            }
        }
    }

    function audioOf(L, s, R) {
        var levels;
        if (!L.hasAudio) {
            return;
        }
        if (s.audio === false) {
            L.audioEnabled = false;
        } else if (s.levels !== undefined && s.levels !== null) {
            levels = child(child(L, "ADBE Audio Group"), "ADBE Audio Levels");
            setProp(levels, s.levels, R, s.name + " audio levels");
        }
    }

    function buildLayer(comp, s, items, R, refs) {
        var src = items[s.src], L, j;
        if (!src) {
            warn(R, s.name + ": its media could not be imported; layer skipped");
            return null;
        }
        try {
            if (s.kind === "still") {
                L = comp.layers.add(src, s.outp - s.inp);
            } else {
                L = comp.layers.add(src);
            }
        } catch (e0) {
            warn(R, s.name + ": could not add the layer (" + errText(e0) + "); layer skipped");
            return null;
        }
        // the layer exists from here on: each part is set on its own, so one that fails is a warning
        // and the rest of the layer is still built
        try {
            L.name = s.name;
        } catch (e1) {
            warn(R, s.name + " name: " + errText(e1));
        }
        try {
            if (s.stretch && s.stretch !== 100) {
                L.stretch = s.stretch;
            }
            L.startTime = s.start;
        } catch (e2) {
            warn(R, s.name + " timing: " + errText(e2));
        }
        if (s.remap) {
            try {
                remap(L, s, R);
            } catch (e3) {
                warn(R, s.name + " time remap: " + errText(e3));
            }
        }
        try {
            L.inPoint = s.inp;
            L.outPoint = s.outp;
            if (L.outPoint < s.outp - comp.frameDuration / 2) {
                warn(R, s.name + ": its media ends before the clip does in Zenvi (layer ends at " +
                    Math.round(L.outPoint * 1000) / 1000 + " s instead of " + s.outp + " s)");
            }
        } catch (e4) {
            warn(R, s.name + " in/out points: " + errText(e4));
        }
        if (s.tf) {
            try {
                transform(L, s.tf, R, s.name);
            } catch (e5) {
                warn(R, s.name + " transform: " + errText(e5));
            }
        }
        try {
            // an audio clip never shows a picture -- not even the placeholder of a missing audio file
            if ((s.video === false || s.kind === "audio") && L.hasVideo) {
                L.enabled = false;
            }
        } catch (e6) {
            warn(R, s.name + " video switch: " + errText(e6));
        }
        try {
            audioOf(L, s, R);
        } catch (e7) {
            warn(R, s.name + " audio: " + errText(e7));
        }
        if (s.masks) {
            for (j = 0; j < s.masks.length; j++) {
                addMask(L, s.masks[j], R, s.name);
            }
        }
        if (s.fx) {
            for (j = 0; j < s.fx.length; j++) {
                try {
                    addEffect(L, s.fx[j], R, s.name, refs);
                } catch (e8) {
                    warn(R, s.name + " " + s.fx[j].label + ": " + errText(e8));
                }
            }
        }
        if (s.blend) {
            try {
                L.blendingMode = BlendingMode[s.blend];
            } catch (e9) {
                warn(R, s.name + ": blend mode " + s.blend + ": " + errText(e9));
            }
        }
        try {
            if (s.label) {
                L.label = s.label;
            }
            if (s.comment) {
                L.comment = s.comment;
            }
        } catch (e10) {
            warn(R, s.name + " label / comment: " + errText(e10));
        }
        return L;
    }

    function zenviBuild(X, D) {
        // "undo" names this script's own undo group. It is what Edit > Undo shows after File > Scripts >
        // Run Script File; run inside another group (Zenvi Link's "Zenvi: Run JSX file") the outer name shows.
        var R = {"zenvi_ae_import": 1, "status": "ok", "comp": "", "comp_id": 0, "folder": "", "layers": 0,
                 "footage": 0, "placeholders": [], "warnings": [], "summary": "", "undo": "Import Zenvi project"};
        var base = scriptFolder(), root, footage, titles = null, comp, items = {}, made = {}, refs = [], fonts = {};
        var quiet = quietRun(), i, f, s, L, g, ref, p, text;
        if (!app.project) {
            app.newProject();
        }
        app.beginUndoGroup("Import Zenvi project");
        try {
            root = app.project.items.addFolder(D.folder);
            footage = app.project.items.addFolder("Footage");
            footage.parentFolder = root;
            if (D.title_folder) {
                titles = app.project.items.addFolder("Titles");
                titles.parentFolder = root;
            }
            for (i = 0; i < D.footage.length; i++) {
                f = D.footage[i];
                try {
                    items[f.id] = linkedComp(f) || importOne(f, base, f.title && titles ? titles : footage, R);
                    R.footage++;
                } catch (e1) {
                    warn(R, "Could not bring in " + f.name + ": " + errText(e1));
                }
            }
            for (i = 0; i < D.titles.length; i++) {
                try {
                    items[D.titles[i].id] = buildTitle(D.titles[i], titles, R, fonts);
                } catch (e2) {
                    warn(R, "Title " + D.titles[i].name + ": " + errText(e2));
                }
            }
            comp = app.project.items.addComp(D.comp.name, D.comp.w, D.comp.h, D.comp.par, D.comp.dur, D.comp.fps);
            comp.parentFolder = root;
            comp.bgColor = D.comp.bg;
            for (i = 0; i < D.guides.length; i++) {
                g = D.guides[i];
                try {
                    L = comp.layers.add(items[g.src], D.comp.dur);
                    L.name = g.name;
                    L.startTime = 0;
                    L.inPoint = 0;
                    L.outPoint = D.comp.dur;
                    L.guideLayer = true;
                    L.enabled = false;
                    L.comment = g.comment;
                    made[g.key] = L;
                } catch (e3) {
                    warn(R, "Wipe image " + g.name + ": " + errText(e3));
                }
            }
            for (i = 0; i < D.layers.length; i++) {
                s = D.layers[i];
                try {
                    L = buildLayer(comp, s, items, R, refs);
                } catch (e4) {
                    warn(R, s.name + ": " + errText(e4));
                    L = null;
                }
                if (L) {
                    made[s.key] = L;
                    R.layers++;
                }
            }
            for (i = 0; i < refs.length; i++) {
                ref = refs[i];
                try {
                    p = findParam(child(ref.layer.property("ADBE Effect Parade"), ref.fx), ref.prm);
                    if (!p) {
                        warn(R, ref.label + ": the wipe's gradient layer parameter is missing");
                    } else if (made[ref.target]) {
                        p.setValue(made[ref.target].index);
                    } else {
                        warn(R, ref.label + ": its wipe image layer is missing");
                    }
                } catch (e5) {
                    warn(R, ref.label + ": " + errText(e5));
                }
            }
            for (i = 0; i < D.markers.length; i++) {
                try {
                    p = new MarkerValue(D.markers[i].c);
                    if (D.markers[i].lb) {
                        try {
                            p.label = D.markers[i].lb;
                        } catch (e6) {}
                    }
                    comp.markerProperty.setValueAtTime(D.markers[i].t, p);
                } catch (e7) {
                    warn(R, "Marker " + D.markers[i].c + ": " + errText(e7));
                }
            }
            for (i = 0; i < D.layers.length; i++) {
                if (D.layers[i].lock && made[D.layers[i].key]) {
                    made[D.layers[i].key].locked = true;
                }
            }
            comp.comment = D.comment;
            R.comp = comp.name;
            R.comp_id = comp.id;
            R.folder = root.name;
            try {
                comp.openInViewer();
            } catch (e8) {}
        } catch (e) {
            R.status = "error";
            R.error = errText(e);
        } finally {
            app.endUndoGroup();
        }
        if (R.status === "ok") {
            text = "Built comp \"" + R.comp + "\" in folder \"" + R.folder + "\": " + R.layers + " layer" +
                (R.layers === 1 ? "" : "s") + ", " + R.footage + " footage item" + (R.footage === 1 ? "" : "s");
            if (R.placeholders.length) {
                text += ", " + R.placeholders.length + " placeholder" + (R.placeholders.length === 1 ? "" : "s") +
                    " for missing media";
            }
            if (R.warnings.length) {
                text += ", " + R.warnings.length + " warning" + (R.warnings.length === 1 ? "" : "s");
            }
            text += ".";
        } else {
            text = "Error: the Zenvi import stopped: " + R.error + ". " + R.layers + " layers were built.";
        }
        R.summary = text;
        if (typeof writeLn === "function") {
            try {
                writeLn("Zenvi: " + text);
            } catch (e9) {}
        }
        // the alert only for a run someone started in After Effects: an export for Zenvi Link says
        // interactive false, and a run Zenvi starts sets ZENVI_AE_QUIET (an alert would block it)
        if (X.interactive && !quiet) {
            alert(text + " Edit > Undo \"" + R.undo + "\" removes " + (R.status === "ok" ? "it." : "them.") +
                (R.warnings.length ? "\n\n" + R.warnings.slice(0, 12).join("\n") : "") +
                (D.notes.length ? "\n\nNotes from the export: see README.txt next to this script." : ""));
        }
        return stringify(R);
    }

    return zenviBuild(ZENVI_EXPORT, DATA);
}());

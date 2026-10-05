"""The ExtendScript runtime every Zenvi -> After Effects export carries.

The exporter writes the timeline as data (``DATA``: footage, title comps,
the comp, guide layers, layers bottom to top, markers) and appends this
runtime, which builds it with the After Effects scripting DOM. Keeping the
code fixed and the data generated means every export runs the same,
reviewed code, and the tests can execute it against a mock DOM.

API notes (After Effects Scripting Guide, ae-scripting.docsforadobe.dev):

* ``PropertyGroup.addProperty`` invalidates earlier references to the
  group's children, so effects and masks are re-fetched by
  ``propertyIndex`` after each add.
* ``Property.setTemporalEaseAtKey`` takes 2 eases for ``PropertyValueType.TwoD``,
  3 for ``ThreeD`` and 1 for everything else (spatial and 1-D); Scale is
  ThreeD, Position is ThreeD_SPATIAL. Influence must be in [0.1, 100].
* Spatial tangents are only settable on TwoD_SPATIAL / ThreeD_SPATIAL
  properties; their length is the value's.
* Turning on ``timeRemapEnabled`` adds two keys; ours are set first, then
  every other key is removed (highest index first).
* Parameters that reference a layer (Gradient Wipe's Gradient Layer) take
  the layer's index, so they are set after every layer exists.
* ``app.beginUndoGroup`` / ``endUndoGroup`` pairs nest (inner names are
  ignored), so the import is one undo step even inside Zenvi Link's group.

ES3 only (ExtendScript): no ``let``/``const``, arrow functions, ``JSON``,
ES5 array methods, or reserved words as property names. ASCII only.
"""

RUNTIME = r"""
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

    function scriptFolder() {
        var f;
        try {
            f = new File($.fileName);
            if (f.exists) {
                return f.parent;
            }
        } catch (e) {}
        return null;
    }

    function findFile(entry, base) {
        var f;
        if (entry.rel && base) {
            f = new File(base.fsName + "/" + entry.rel);
            if (f.exists) {
                return f;
            }
        }
        if (entry.abs) {
            f = new File(entry.abs);
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
            if (c.aep && new File(c.aep).fsName !== open) {
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
        var n = k.t.length, vals = [], i, idx, inT, outT, count, spatial;
        for (i = 0; i < n; i++) {
            vals.push(fit(p, k.v[i]));
        }
        p.setValuesAtTimes(k.t, vals);
        count = easeCount(p);
        spatial = k.sp && p.isSpatial;
        for (i = 0; i < n; i++) {
            idx = p.nearestKeyIndex(k.t[i]);
            inT = interp(i > 0 ? k.i[i - 1] : (n > 1 ? k.i[0] : "l"));
            outT = interp(i < n - 1 ? k.i[i] : (n > 1 ? k.i[n - 2] : "l"));
            if (spatial) {
                flatTangents(p, idx);
            }
            if (inT === KIT.BEZIER || outT === KIT.BEZIER) {
                p.setTemporalContinuousAtKey(idx, false);
                p.setTemporalAutoBezierAtKey(idx, false);
                p.setTemporalEaseAtKey(idx, eases(k.n && i > 0 ? k.n[i - 1] : null, count),
                    eases(k.o && i < n - 1 ? k.o[i] : null, count));
            }
            p.setInterpolationTypeAtKey(idx, inT, outT);
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

    function findParam(e, ids) {
        var i, p;
        for (i = 0; i < ids.length; i++) {
            try {
                p = e.property(ids[i]);
            } catch (err) {
                p = null;
            }
            if (p) {
                return p;
            }
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
            p = findParam(L.property("ADBE Effect Parade").property(idx), prm.ids);
            if (!p) {
                warn(R, label + ": " + fx.label + " has no parameter " + prm.ids.join(" / ") + "; left at its default");
                continue;
            }
            if (prm.val !== null && typeof prm.val === "object" && prm.val.layer) {
                refs.push({"layer": L, "fx": idx, "ids": prm.ids, "target": prm.val.layer, "label": label});
                continue;
            }
            setProp(p, prm.val, R, label + " " + fx.label);
        }
        if (fx.on === false) {
            L.property("ADBE Effect Parade").property(idx).enabled = false;
        }
    }

    function fontFor(t) {
        var styles, families = [t.family, "Arial", "Helvetica Neue", "Helvetica"], i, j, found, base = "", ch;
        if (t.bold) {
            styles = t.italic ? ["Bold Italic", "Bold Oblique", "BoldItalic"] : ["Bold"];
        } else {
            styles = t.italic ? ["Italic", "Oblique"] : ["Regular", "Book", "Roman", "Normal"];
        }
        if (app.fonts && typeof app.fonts.getFontsByFamilyNameAndStyleName === "function") {
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
                        return {"ps": found[0].postScriptName, "family": families[i]};
                    }
                }
            }
            return null;
        }
        // After Effects before 24.0 has no font list: try the usual PostScript name.
        for (i = 0; i < t.family.length; i++) {
            ch = t.family.charAt(i);
            if (ch !== " ") {
                base += ch;
            }
        }
        if (t.bold) {
            base += t.italic ? "-BoldItalic" : "-Bold";
        } else if (t.italic) {
            base += "-Italic";
        }
        return {"ps": base, "family": t.family};
    }

    function addText(tc, t, R, fonts) {
        var L = tc.layers.addText(t.text), src, doc, font, tr;
        src = L.property("ADBE Text Properties").property("ADBE Text Document");
        doc = src.value;
        if (typeof doc.resetCharStyle === "function") {
            try {
                doc.resetCharStyle();
                doc.resetParagraphStyle();
            } catch (e0) {}
        }
        doc.text = t.text;
        font = fontFor(t);
        if (font) {
            try {
                doc.font = font.ps;
            } catch (e1) {}
        }
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
        if (!font || font.family !== t.family || src.value.font !== font.ps) {
            if (!fonts[t.family]) {
                fonts[t.family] = true;
                warn(R, "Font " + t.family + " is not installed in After Effects; titles use " +
                    (font ? src.value.font : "the default font") + " instead");
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
        setProp(tr.property("ADBE Anchor Point"), tf.anchor, R, label + " anchor point");
        if (tf.pos && tf.pos.sep) {
            try {
                pos = tr.property("ADBE Position");
                pos.dimensionsSeparated = true;
                setProp(pos.getSeparationFollower(0), tf.pos.x, R, label + " X position");
                setProp(pos.getSeparationFollower(1), tf.pos.y, R, label + " Y position");
            } catch (e) {
                warn(R, label + " position: " + errText(e));
            }
        } else {
            setProp(tr.property("ADBE Position"), tf.pos, R, label + " position");
        }
        setProp(tr.property("ADBE Scale"), tf.scale, R, label + " scale");
        setProp(tr.property("ADBE Rotate Z"), tf.rot, R, label + " rotation");
        setProp(tr.property("ADBE Opacity"), tf.op, R, label + " opacity");
    }

    function remap(L, s, R) {
        var p, k, j, keep, times = s.remap.t;
        if (!L.canSetTimeRemapEnabled) {
            warn(R, s.name + ": After Effects cannot time-remap this layer; its speed change is missing");
            return;
        }
        L.timeRemapEnabled = true;
        p = L.property("ADBE Time Remapping");
        setKeys(p, s.remap);
        for (k = p.numKeys; k >= 1; k--) {
            keep = false;
            for (j = 0; j < times.length; j++) {
                if (Math.abs(p.keyTime(k) - times[j]) < 0.000001) {
                    keep = true;
                    break;
                }
            }
            if (!keep) {
                p.removeKey(k);
            }
        }
    }

    function buildLayer(comp, s, items, R, refs) {
        var src = items[s.src], L, j, audio;
        if (!src) {
            warn(R, s.name + ": its media could not be imported; layer skipped");
            return null;
        }
        if (s.kind === "still") {
            L = comp.layers.add(src, s.outp - s.inp);
        } else {
            L = comp.layers.add(src);
        }
        L.name = s.name;
        if (s.stretch && s.stretch !== 100) {
            L.stretch = s.stretch;
        }
        L.startTime = s.start;
        if (s.remap) {
            try {
                remap(L, s, R);
            } catch (e0) {
                warn(R, s.name + " time remap: " + errText(e0));
            }
        }
        L.inPoint = s.inp;
        L.outPoint = s.outp;
        if (L.outPoint < s.outp - comp.frameDuration / 2) {
            warn(R, s.name + ": its media ends before the clip does in Zenvi (layer ends at " +
                Math.round(L.outPoint * 1000) / 1000 + " s instead of " + s.outp + " s)");
        }
        if (s.tf) {
            transform(L, s.tf, R, s.name);
        }
        if (s.video === false) {
            L.enabled = false;
        }
        audio = false;
        try {
            audio = L.hasAudio;
        } catch (e1) {}
        if (audio) {
            if (s.audio === false) {
                L.audioEnabled = false;
            } else if (s.levels !== undefined && s.levels !== null) {
                setProp(L.property("ADBE Audio Group").property("ADBE Audio Levels"), s.levels, R,
                    s.name + " audio levels");
            }
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
                } catch (e2) {
                    warn(R, s.name + " " + s.fx[j].label + ": " + errText(e2));
                }
            }
        }
        if (s.blend) {
            try {
                L.blendingMode = BlendingMode[s.blend];
            } catch (e3) {
                warn(R, s.name + ": blend mode " + s.blend + ": " + errText(e3));
            }
        }
        if (s.label) {
            L.label = s.label;
        }
        if (s.comment) {
            L.comment = s.comment;
        }
        return L;
    }

    function zenviBuild(X, D) {
        var R = {"zenvi_ae_import": 1, "status": "ok", "comp": "", "comp_id": 0, "folder": "", "layers": 0,
                 "footage": 0, "placeholders": [], "warnings": [], "summary": ""};
        var base = scriptFolder(), root, footage, titles = null, comp, items = {}, made = {}, refs = [], fonts = {};
        var i, f, s, L, g, ref, p, text;
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
                    p = findParam(ref.layer.property("ADBE Effect Parade").property(ref.fx), ref.ids);
                    if (made[ref.target]) {
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
            text += ". Edit > Undo \"Import Zenvi project\" removes it.";
        } else {
            text = "Error: the Zenvi import stopped: " + R.error + ". " + R.layers +
                " layers were built; Edit > Undo \"Import Zenvi project\" removes them.";
        }
        R.summary = text;
        if (typeof writeLn === "function") {
            try {
                writeLn("Zenvi: " + text);
            } catch (e9) {}
        }
        if (X.interactive && typeof ZenviLink === "undefined") {
            alert(text + (R.warnings.length ? "\n\n" + R.warnings.slice(0, 12).join("\n") : "") +
                (D.notes.length ? "\n\nNotes from the export: see README.txt next to this script." : ""));
        }
        return stringify(R);
    }
"""

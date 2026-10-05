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
  properties; their length is the value's. A spatial ease takes no
  negative speed (the exporter keys such moves as separate X/Y Position).
* Each key gets its interpolation type before its ease (a side that becomes
  Bezier may get a recomputed ease), and the type again after the ease
  where one side is linear or hold.
* Turning on ``timeRemapEnabled`` adds two keys; their times are noted, ours
  are set, and only those two are removed again (unless one of ours took
  its place).
* Parameters that reference a layer (Gradient Wipe's Gradient Layer) take
  the layer's index, so they are set after every layer exists. An effect
  parameter found by an unverified match name must take the value's kind
  (and carry the expected name in an English After Effects).
* ``File()`` reads ``%XX`` as an escape, so paths go through ``fileAt``.
* ``app.beginUndoGroup`` / ``endUndoGroup`` pairs nest (inner names are
  ignored), so the import is one undo step even inside Zenvi Link's group;
  Edit > Undo then shows Zenvi Link's name ("Zenvi: Run JSX file").
* Every part of a layer is set on its own once the layer exists, so one
  that fails is a warning, not a skipped layer.
* The closing alert shows for an interactive export unless the run was
  started from Zenvi, which sets ``$.global.ZENVI_AE_QUIET`` for it; the
  script reads and clears the flag first thing, so it never outlives a run.

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
"""

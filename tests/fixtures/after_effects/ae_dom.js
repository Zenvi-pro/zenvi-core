/*
 * A mock of the After Effects scripting DOM -- the subset Zenvi's export runtime uses -- for tests.
 *
 * Evaluated inside the same vm context as the export script (so arrays share one realm), with
 * __host providing file-system checks and options. Behaviour follows the After Effects Scripting
 * Guide (ae-scripting.docsforadobe.dev) and types-for-adobe (AfterEffects/26.0):
 *
 *  - enum values are the real ones;
 *  - setValue on a keyed property, wrong value shapes, out-of-range opacity / influence, wrong
 *    ease array sizes (TwoD -> 2, ThreeD -> 3, else 1), spatial calls on non-spatial properties,
 *    bad key indices and edits to locked layers throw like AE does;
 *  - PropertyGroup.addProperty invalidates the references handed out earlier for that group's
 *    children (the guide's Slider/Color Control example), and turning Position's
 *    dimensionsSeparated on or off invalidates the layer's group references, so stale ones throw;
 *  - turning on time remapping adds two keys; setting an unknown font keeps the previous one;
 *  - File() reads "%XX" in a path as an escape (ExtendScript's URI notation);
 *  - a spatial property refuses a negative ease speed;
 *  - making a key's side Bezier with setInterpolationTypeAtKey recomputes that side's ease (an
 *    ease set before the type is lost); setTemporalEaseAtKey leaves the types alone;
 *  - $.evalFile runs another script in the same global scope ($.global) and returns its value.
 *
 * Options (ae_mock.js) model other After Effects setups: no font list, no Keylight, another
 * language (app.isoLanguage), wrongly guessed effect parameter ids, a layer without an audio
 * group, key times rounded to a coarser grid.
 *
 * Everything the script did is available through __dump().
 */
(function (global) {
    "use strict";
    var host = global.__host;
    var nextId = 1;

    function aeError(message) {
        return new Error("After Effects error: " + message);
    }

    function isNum(v) {
        return typeof v === "number" && isFinite(v);
    }

    function isArr(v) {
        return v instanceof Array;
    }

    function clone(v) {
        if (isArr(v)) {
            var out = [];
            for (var i = 0; i < v.length; i++) out.push(clone(v[i]));
            return out;
        }
        if (v && typeof v === "object" && typeof v.__clone === "function") return v.__clone();
        return v;
    }

    // ---- enums (types-for-adobe AfterEffects/26.0) -------------------------------------------
    var KeyframeInterpolationType = {BEZIER: 6613, HOLD: 6614, LINEAR: 6612};
    var PropertyValueType = {COLOR: 6418, CUSTOM_VALUE: 6419, LAYER_INDEX: 6421, MARKER: 6420, MASK_INDEX: 6422,
        NO_VALUE: 6412, OneD: 6417, SHAPE: 6423, TEXT_DOCUMENT: 6424, ThreeD: 6414, ThreeD_SPATIAL: 6413,
        TwoD: 6416, TwoD_SPATIAL: 6415};
    var ImportAsType = {COMP: 3814, COMP_CROPPED_LAYERS: 3812, FOOTAGE: 3813, PROJECT: 3815};
    var ParagraphJustification = {CENTER_JUSTIFY: 7415, FULL_JUSTIFY_LASTLINE_CENTER: 7418,
        FULL_JUSTIFY_LASTLINE_FULL: 7419, FULL_JUSTIFY_LASTLINE_LEFT: 7416, FULL_JUSTIFY_LASTLINE_RIGHT: 7417,
        LEFT_JUSTIFY: 7413, MULTIPLE_JUSTIFICATIONS: 7412, RIGHT_JUSTIFY: 7414};
    var MaskMode = {ADD: 6813, DARKEN: 6817, DIFFERENCE: 6818, INTERSECT: 6815, LIGHTEN: 6816, NONE: 6812,
        SUBTRACT: 6814};
    var BlendingMode = {ADD: 5220, COLOR_BURN: 5218, COLOR_DODGE: 5224, DARKEN: 5215, DIFFERENCE: 5233,
        EXCLUSION: 5235, HARD_LIGHT: 5228, LIGHTEN: 5221, MULTIPLY: 5216, NORMAL: 5212, OVERLAY: 5226,
        SCREEN: 5222, SOFT_LIGHT: 5227};
    var INTERP = {6612: "linear", 6613: "bezier", 6614: "hold"};

    function enumValue(table, v, what) {
        for (var k in table) if (table[k] === v) return v;
        throw aeError(what + ": " + v + " is not a valid value");
    }

    // ---- files ---------------------------------------------------------------------------------
    function decodePath(p) {
        // ExtendScript paths are URI notation: File("a%20b") is "a b"
        return String(p).replace(/(%[0-9A-Fa-f]{2})+/g, function (m) {
            try { return decodeURIComponent(m); } catch (e) { return m; }
        });
    }

    function File(p) {
        if (!(this instanceof File)) return new File(p);
        this._path = decodePath(p);
    }
    Object.defineProperty(File.prototype, "exists", {get: function () { return host.isFile(this._path); }});
    Object.defineProperty(File.prototype, "fsName", {get: function () { return host.resolve(this._path); }});
    Object.defineProperty(File.prototype, "name", {get: function () { return host.basename(this._path); }});
    Object.defineProperty(File.prototype, "parent", {get: function () { return new Folder(host.dirname(host.resolve(this._path))); }});

    function Folder(p) {
        if (!(this instanceof Folder)) return new Folder(p);
        this._path = String(p);
    }
    Object.defineProperty(Folder.prototype, "fsName", {get: function () { return host.resolve(this._path); }});
    Object.defineProperty(Folder.prototype, "exists", {get: function () { return host.isDir(this._path); }});

    // ---- simple value classes --------------------------------------------------------------------
    function KeyframeEase(speed, influence) {
        if (arguments.length < 2) throw aeError("KeyframeEase needs speed and influence");
        if (!isNum(speed)) throw aeError("KeyframeEase speed must be a number");
        if (!isNum(influence) || influence < 0.1 || influence > 100) {
            throw aeError("KeyframeEase influence must be in [0.1..100], got " + influence);
        }
        this.speed = speed;
        this.influence = influence;
    }

    function MarkerValue(comment) {
        if (typeof comment !== "string") throw aeError("MarkerValue comment must be a string");
        this.comment = comment;
        this.chapter = "";
        this.url = "";
        this.duration = 0;
        this._label = 0;
    }
    Object.defineProperty(MarkerValue.prototype, "label", {
        get: function () { return this._label; },
        set: function (v) {
            if (!isNum(v) || v < 0 || v > 16 || Math.floor(v) !== v) throw aeError("marker label must be 0..16");
            this._label = v;
        }
    });
    MarkerValue.prototype.__clone = function () {
        var m = new MarkerValue(this.comment);
        m._label = this._label;
        return m;
    };

    function Shape() {
        this.vertices = [];
        this.inTangents = [];
        this.outTangents = [];
        this.closed = true;
    }
    Shape.prototype.__clone = function () {
        var s = new Shape();
        s.vertices = clone(this.vertices);
        s.inTangents = clone(this.inTangents);
        s.outTangents = clone(this.outTangents);
        s.closed = this.closed;
        return s;
    };

    function ImportOptions(file) {
        this.file = file || null;
        this.sequence = false;
        this.forceAlphabetical = false;
        this.importAs = ImportAsType.FOOTAGE;
    }
    ImportOptions.prototype.canImportAs = function (type) {
        enumValue(ImportAsType, type, "canImportAs");
        return type === ImportAsType.FOOTAGE;
    };

    var TEXT_FIELDS = ["text", "font", "fontSize", "applyFill", "fillColor", "applyStroke", "strokeColor",
        "strokeWidth", "strokeOverFill", "justification", "tracking"];

    function TextDocument(text) {
        this._v = {text: String(text === undefined ? "" : text), font: "ArialMT", fontSize: 36, applyFill: true,
            fillColor: [1, 1, 1], applyStroke: false, strokeColor: [0, 0, 0], strokeWidth: 1,
            strokeOverFill: false, justification: ParagraphJustification.LEFT_JUSTIFY, tracking: 0};
    }
    TEXT_FIELDS.forEach(function (name) {
        Object.defineProperty(TextDocument.prototype, name, {
            get: function () {
                if ((name === "fillColor" && !this._v.applyFill) || ((name === "strokeColor" || name === "strokeWidth") && !this._v.applyStroke)) {
                    throw aeError(name + " is undefined while " + (name === "fillColor" ? "applyFill" : "applyStroke") + " is false");
                }
                return clone(this._v[name]);
            },
            set: function (v) {
                if (name === "fillColor" && !this._v.applyFill) throw aeError("set applyFill before fillColor");
                if ((name === "strokeColor" || name === "strokeWidth") && !this._v.applyStroke) {
                    throw aeError("set applyStroke before " + name);
                }
                if ((name === "fillColor" || name === "strokeColor") && !(isArr(v) && v.length === 3)) {
                    throw aeError(name + " must be [r, g, b]");
                }
                if (name === "justification") enumValue(ParagraphJustification, v, "justification");
                if ((name === "fontSize" || name === "tracking" || name === "strokeWidth") && !isNum(v)) {
                    throw aeError(name + " must be a number");
                }
                this._v[name] = clone(v);
            }
        });
    });
    TextDocument.prototype.resetCharStyle = function () {
        this._v.font = "ArialMT";
        this._v.fontSize = 36;
        this._v.tracking = 0;
        this._v.applyStroke = false;
    };
    TextDocument.prototype.resetParagraphStyle = function () {
        this._v.justification = ParagraphJustification.LEFT_JUSTIFY;
    };
    TextDocument.prototype.__clone = function () {
        // a copy, as Source Text's value is in After Effects (not an alias of the layer's document)
        var t = new TextDocument(this._v.text);
        t._v = {};
        for (var k in this._v) t._v[k] = clone(this._v[k]);
        return t;
    };
    if (!host.options.charStyleReset) {
        delete TextDocument.prototype.resetCharStyle;
        delete TextDocument.prototype.resetParagraphStyle;
    }

    // ---- properties ------------------------------------------------------------------------------
    function dimsOf(type) {
        if (type === PropertyValueType.TwoD || type === PropertyValueType.TwoD_SPATIAL) return 2;
        if (type === PropertyValueType.ThreeD || type === PropertyValueType.ThreeD_SPATIAL) return 3;
        if (type === PropertyValueType.COLOR) return 4;
        return 1;
    }

    function easeCountOf(type) {
        if (type === PropertyValueType.TwoD) return 2;
        if (type === PropertyValueType.ThreeD) return 3;
        return 1;
    }

    function Property(owner, matchName, name, type, value, opts) {
        this._owner = owner;
        this.matchName = matchName;
        this.name = name;
        this.propertyValueType = type;
        this._value = clone(value);
        this._keys = [];
        this._opts = opts || {};
        this.enabled = true;
        this.propertyIndex = 0;
        this._separated = false;
        this._followers = null;
        this._leader = null;
    }
    Property.prototype.__isProperty = true;
    Object.defineProperty(Property.prototype, "isSpatial", {get: function () {
        return this.propertyValueType === PropertyValueType.TwoD_SPATIAL || this.propertyValueType === PropertyValueType.ThreeD_SPATIAL;
    }});
    Object.defineProperty(Property.prototype, "numKeys", {get: function () { return this._keys.length; }});
    Object.defineProperty(Property.prototype, "value", {get: function () {
        return clone(this._keys.length ? this._keys[0].value : this._value);
    }});
    Object.defineProperty(Property.prototype, "isSeparationLeader", {get: function () { return !!this._opts.separable; }});
    Object.defineProperty(Property.prototype, "dimensionsSeparated", {
        get: function () { return this._separated; },
        set: function (v) {
            this._locked();
            if (!this._opts.separable) throw aeError(this.name + " cannot be separated");
            if (this._separated !== !!v) {
                // the property tree changes: references to the layer's groups and their children go stale
                var layer = this._owner && this._owner.__layer ? this._owner.__layer() : null;
                if (layer) layer._invalidate();
                if (this._owner && this._owner._invalidate) this._owner._invalidate();
            }
            this._separated = !!v;
        }
    });
    Property.prototype.getSeparationFollower = function (dim) {
        if (!this._opts.separable) throw aeError(this.name + " is not multidimensional");
        if (!(dim >= 0 && dim < this._followers.length)) throw aeError("no dimension " + dim);
        return this._followers[dim];
    };
    Property.prototype._locked = function () {
        var layer = this._owner && this._owner.__layer ? this._owner.__layer() : null;
        if (layer && layer._locked) throw aeError("the layer is locked");
        if (this._leader && !this._leader._separated) throw aeError(this.name + ": the dimensions are not separated");
        if (this._opts.separable && this._separated) throw aeError(this.name + ": edit the separated dimensions instead");
    };
    Property.prototype._check = function (v) {
        var t = this.propertyValueType, n, i;
        if (t === PropertyValueType.OneD || t === PropertyValueType.LAYER_INDEX) {
            if (typeof v === "boolean") v = v ? 1 : 0;
            if (!isNum(v)) throw aeError(this.name + ": a number is required, got " + v);
            if (this._opts.min !== undefined && v < this._opts.min) throw aeError(this.name + ": " + v + " is below " + this._opts.min);
            if (this._opts.max !== undefined && v > this._opts.max) throw aeError(this.name + ": " + v + " is above " + this._opts.max);
            if ((this._opts.integer || t === PropertyValueType.LAYER_INDEX) && Math.floor(v) !== v) {
                throw aeError(this.name + ": a whole number is required, got " + v);
            }
            return v;
        }
        if (t === PropertyValueType.SHAPE) {
            if (!(v instanceof Shape)) throw aeError(this.name + ": a Shape is required");
            if (v.inTangents.length !== v.vertices.length || v.outTangents.length !== v.vertices.length) {
                throw aeError(this.name + ": tangents must match the vertices");
            }
            return v.__clone();
        }
        if (t === PropertyValueType.TEXT_DOCUMENT) {
            if (!(v instanceof TextDocument)) throw aeError(this.name + ": a TextDocument is required");
            var doc = v.__clone();
            if (host.fonts.length && !host.fontInstalled(doc._v.font)) {
                doc._v.font = this._value && this._value._v ? this._value._v.font : "ArialMT";
            }
            return doc;
        }
        if (t === PropertyValueType.MARKER) {
            if (!(v instanceof MarkerValue)) throw aeError(this.name + ": a MarkerValue is required");
            return v.__clone();
        }
        n = dimsOf(t);
        if (!isArr(v) || v.length !== n) throw aeError(this.name + ": an array of " + n + " numbers is required, got " + v);
        for (i = 0; i < n; i++) if (!isNum(v[i])) throw aeError(this.name + ": not a number in " + v);
        return clone(v);
    };
    Property.prototype.setValue = function (v) {
        this._locked();
        if (this._keys.length) throw aeError(this.name + ": setValue on a property with keyframes");
        this._value = this._check(v);
    };
    Property.prototype._addKey = function (t, v) {
        var i, key;
        if (!isNum(t)) throw aeError(this.name + ": key time must be a number");
        if (host.options.keyTimeGrid) t = Math.round(t * host.options.keyTimeGrid) / host.options.keyTimeGrid;
        if (this._opts.noKeys) throw aeError(this.name + " cannot be keyframed");
        for (i = 0; i < this._keys.length; i++) {
            if (Math.abs(this._keys[i].time - t) < 1e-9) {
                this._keys[i].value = v;
                return i + 1;
            }
        }
        key = {time: t, value: v, inType: KeyframeInterpolationType.LINEAR, outType: KeyframeInterpolationType.LINEAR,
            inEase: null, outEase: null, temporalContinuous: false, temporalAutoBezier: false,
            spatialContinuous: true, spatialAutoBezier: true, inTangent: null, outTangent: null};
        this._keys.push(key);
        this._keys.sort(function (a, b) { return a.time - b.time; });
        for (i = 0; i < this._keys.length; i++) if (this._keys[i] === key) return i + 1;
        return this._keys.length;
    };
    Property.prototype.setValueAtTime = function (t, v) {
        this._locked();
        this._addKey(t, this._check(v));
    };
    Property.prototype.setValuesAtTimes = function (times, values) {
        this._locked();
        if (!isArr(times) || !isArr(values)) throw aeError(this.name + ": setValuesAtTimes takes two arrays");
        if (times.length !== values.length) throw aeError(this.name + ": times and values differ in length");
        for (var i = 0; i < times.length; i++) this._addKey(times[i], this._check(values[i]));
    };
    Property.prototype._key = function (i) {
        if (!(Math.floor(i) === i && i >= 1 && i <= this._keys.length)) {
            throw aeError(this.name + ": key index " + i + " is out of range 1.." + this._keys.length);
        }
        return this._keys[i - 1];
    };
    Property.prototype.keyTime = function (i) { return this._key(i).time; };
    Property.prototype.keyValue = function (i) { return clone(this._key(i).value); };
    Property.prototype.removeKey = function (i) {
        this._locked();
        this._key(i);
        this._keys.splice(i - 1, 1);
    };
    Property.prototype.nearestKeyIndex = function (t) {
        if (!this._keys.length) throw aeError(this.name + " has no keyframes");
        var best = 1, d = Infinity;
        for (var i = 0; i < this._keys.length; i++) {
            var di = Math.abs(this._keys[i].time - t);
            if (di < d) { d = di; best = i + 1; }
        }
        return best;
    };
    Property.prototype.isInterpolationTypeValid = function (type) {
        enumValue(KeyframeInterpolationType, type, "interpolation");
        return !(this._opts.holdOnly && type !== KeyframeInterpolationType.HOLD);
    };
    Property.prototype.setInterpolationTypeAtKey = function (i, inType, outType) {
        this._locked();
        var key = this._key(i);
        if (outType === undefined) outType = inType;
        if (!this.isInterpolationTypeValid(inType) || !this.isInterpolationTypeValid(outType)) {
            throw aeError(this.name + ": interpolation type not valid for this property");
        }
        // a side that becomes Bezier gets an ease After Effects works out itself
        var n = easeCountOf(this.propertyValueType), auto = function () {
            var out = [];
            for (var j = 0; j < n; j++) out.push([0, 16.666667]);
            return out;
        };
        if (inType === KeyframeInterpolationType.BEZIER && key.inType !== inType) key.inEase = auto();
        if (outType === KeyframeInterpolationType.BEZIER && key.outType !== outType) key.outEase = auto();
        key.inType = inType;
        key.outType = outType;
    };
    Property.prototype.setTemporalEaseAtKey = function (i, inEase, outEase) {
        this._locked();
        var key = this._key(i), n = easeCountOf(this.propertyValueType), check;
        if (outEase === undefined) outEase = inEase;
        check = function (list, which) {
            if (!isArr(list) || list.length !== n) throw aeError("setTemporalEaseAtKey: " + which + " ease array must have " + n + " KeyframeEase objects");
            for (var j = 0; j < list.length; j++) if (!(list[j] instanceof KeyframeEase)) throw aeError("setTemporalEaseAtKey: not a KeyframeEase");
        };
        check(inEase, "in");
        check(outEase, "out");
        if (this.isSpatial && (inEase[0].speed < 0 || outEase[0].speed < 0)) {
            throw aeError(this.name + ": a spatial property's ease speed cannot be negative");
        }
        key.inEase = [];
        key.outEase = [];
        for (var j = 0; j < n; j++) {
            key.inEase.push([inEase[j].speed, inEase[j].influence]);
            key.outEase.push([outEase[j].speed, outEase[j].influence]);
        }
    };
    Property.prototype.setTemporalContinuousAtKey = function (i, v) { this._locked(); this._key(i).temporalContinuous = !!v; };
    Property.prototype.setTemporalAutoBezierAtKey = function (i, v) { this._locked(); this._key(i).temporalAutoBezier = !!v; };
    Property.prototype._spatial = function (what) {
        if (!this.isSpatial) throw aeError(this.name + ": " + what + " needs a TwoD_SPATIAL or ThreeD_SPATIAL property");
    };
    Property.prototype.setSpatialContinuousAtKey = function (i, v) { this._spatial("setSpatialContinuousAtKey"); this._key(i).spatialContinuous = !!v; };
    Property.prototype.setSpatialAutoBezierAtKey = function (i, v) { this._spatial("setSpatialAutoBezierAtKey"); this._key(i).spatialAutoBezier = !!v; };
    Property.prototype.setSpatialTangentsAtKey = function (i, a, b) {
        this._spatial("setSpatialTangentsAtKey");
        var key = this._key(i), n = dimsOf(this.propertyValueType);
        if (b === undefined) b = a;
        if (!isArr(a) || a.length !== n || !isArr(b) || b.length !== n) throw aeError("tangents must have " + n + " values");
        key.inTangent = clone(a);
        key.outTangent = clone(b);
    };
    Property.prototype.__dump = function () {
        var out = {match: this.matchName, name: this.name};
        if (this._keys.length) {
            out.keys = [];
            for (var i = 0; i < this._keys.length; i++) {
                var k = this._keys[i];
                out.keys.push({t: k.time, v: dumpValue(k.value), "in": INTERP[k.inType], out: INTERP[k.outType],
                    inEase: k.inEase, outEase: k.outEase, inTangent: k.inTangent, outTangent: k.outTangent,
                    spatialAutoBezier: k.spatialAutoBezier});
            }
        } else {
            out.value = dumpValue(this._value);
        }
        if (this._separated) {
            out.separated = true;
            out.followers = [];
            for (var d = 0; d < this._followers.length; d++) out.followers.push(this._followers[d].__dump());
        }
        return out;
    };

    function dumpValue(v) {
        if (v instanceof Shape) return {vertices: v.vertices, inTangents: v.inTangents, outTangents: v.outTangents, closed: v.closed};
        if (v instanceof TextDocument) return clone(v._v);
        if (v instanceof MarkerValue) return {comment: v.comment, label: v._label};
        return clone(v);
    }

    // ---- property groups, with the reference invalidation addProperty causes ----------------------
    function PropertyGroup(owner, matchName, name, factories, children) {
        this._owner = owner;
        this.matchName = matchName;
        this.name = name;
        this._factories = factories || {};
        this._children = [];
        this._handles = [];
        this.enabled = true;
        this.propertyIndex = 0;
        for (var i = 0; children && i < children.length; i++) this._adopt(children[i]);
    }
    PropertyGroup.prototype.__isGroup = true;
    PropertyGroup.prototype.__layer = function () {
        var o = this._owner;
        while (o && !o.__isLayer) o = o._owner;
        return o;
    };
    PropertyGroup.prototype._adopt = function (child) {
        child._owner = this;
        this._children.push(child);
        child.propertyIndex = this._children.length;
        return child;
    };
    Object.defineProperty(PropertyGroup.prototype, "numProperties", {get: function () { return this._children.length; }});
    PropertyGroup.prototype._invalidate = function () {
        for (var i = 0; i < this._handles.length; i++) this._handles[i].valid = false;
        this._handles = [];
    };
    PropertyGroup.prototype._handle = function (target) {
        var record = {valid: true}, proxy = new Proxy(target, {
            get: function (t, k) {
                if (!record.valid && k !== "__target") throw aeError("Object is invalid (it was invalidated by a later addProperty on its group)");
                if (k === "__target") return t;
                var v = t[k];
                return typeof v === "function" ? function () { return v.apply(t, arguments); } : v;
            },
            set: function (t, k, v) {
                if (!record.valid) throw aeError("Object is invalid (it was invalidated by a later addProperty on its group)");
                t[k] = v;
                return true;
            }
        });
        this._handles.push(record);
        return proxy;
    };
    PropertyGroup.prototype.property = function (key) {
        var i, c;
        if (typeof key === "number") {
            c = this._children[key - 1];
            return c ? this._handle(c) : null;
        }
        for (i = 0; i < this._children.length; i++) {
            c = this._children[i];
            if (c.matchName === key || c.name === key) return this._handle(c);
        }
        return null;
    };
    PropertyGroup.prototype.canAddProperty = function (name) {
        return typeof this._factories[name] === "function";
    };
    PropertyGroup.prototype.addProperty = function (name) {
        var layer = this.__layer();
        if (layer && layer._locked) throw aeError("the layer is locked");
        if (!this.canAddProperty(name)) throw aeError("Can not add a property with the name \"" + name + "\" to this group");
        for (var i = 0; i < this._handles.length; i++) this._handles[i].valid = false;
        this._handles = [];
        var child = this._adopt(this._factories[name](this));
        return this._handle(child);
    };
    PropertyGroup.prototype.__dump = function () {
        var out = {match: this.matchName, name: this.name, enabled: this.enabled, children: []};
        if (this.maskMode !== undefined) {
            out.maskMode = this.maskMode;
            out.inverted = !!this.inverted;
        }
        for (var i = 0; i < this._children.length; i++) out.children.push(this._children[i].__dump());
        return out;
    };

    // ---- effects -----------------------------------------------------------------------------------
    var P = PropertyValueType;
    function param(m, name, type, value, opts) { return [m, name, type, value, opts || {}]; }
    var swap = !!host.options.wrongParamIds;  // guessed ids that hold other parameters
    var EFFECTS = {
        "ADBE Gaussian Blur 2": ["Gaussian Blur", [
            param("ADBE Gaussian Blur 2-0001", "Blurriness", P.OneD, 0, {min: 0, max: 3000}),
            param("ADBE Gaussian Blur 2-0002", "Blur Dimensions", P.OneD, 1, {min: 1, max: 3, integer: true, holdOnly: true}),
            param("ADBE Gaussian Blur 2-0003", "Repeat Edge Pixels", P.OneD, 0, {min: 0, max: 1, integer: true, holdOnly: true})]],
        "ADBE Brightness & Contrast 2": ["Brightness & Contrast", [
            param("ADBE Brightness & Contrast 2-0001", "Brightness", P.OneD, 0, {min: -150, max: 150}),
            param("ADBE Brightness & Contrast 2-0002", "Contrast", P.OneD, 0, {min: -100, max: 100}),
            param("ADBE Brightness & Contrast 2-0003", "Use Legacy (supports HDR)", P.OneD, 0, {min: 0, max: 1, integer: true, holdOnly: true})]],
        "ADBE HUE SATURATION": ["Hue/Saturation", [
            param("ADBE HUE SATURATION-0002", "Channel Control", P.OneD, 1, {min: 1, max: 7, integer: true, holdOnly: true}),
            param("ADBE HUE SATURATION-0003", "Channel Range", P.NO_VALUE, 0, {noKeys: true}),
            param("ADBE HUE SATURATION-0004", "Master Hue", P.OneD, 0),
            param("ADBE HUE SATURATION-0005", "Master Saturation", P.OneD, 0, {min: -100, max: 100}),
            param("ADBE HUE SATURATION-0006", "Master Lightness", P.OneD, 0, {min: -100, max: 100}),
            param("ADBE HUE SATURATION-0007", "Colorize", P.OneD, 0, {min: 0, max: 1, integer: true, holdOnly: true})]],
        "ADBE Invert": ["Invert", [
            param("ADBE Invert-0001", "Channel", P.OneD, 1, {min: 1, max: 16, integer: true, holdOnly: true}),
            param("ADBE Invert-0002", "Blend With Original", P.OneD, 0, {min: 0, max: 100})]],
        "ADBE Mosaic": ["Mosaic", [
            swap ? param("ADBE Mosaic-0001", "Sharp Colors", P.OneD, 0, {min: 0, max: 1, integer: true, holdOnly: true})
                : param("ADBE Mosaic-0001", "Horizontal Blocks", P.OneD, 10, {min: 1, max: 4000}),
            param("ADBE Mosaic-0002", "Vertical Blocks", P.OneD, 10, {min: 1, max: 4000}),
            swap ? param("ADBE Mosaic-0003", "Horizontal Blocks", P.OneD, 10, {min: 1, max: 4000})
                : param("ADBE Mosaic-0003", "Sharp Colors", P.OneD, 0, {min: 0, max: 1, integer: true, holdOnly: true})]],
        "ADBE Sharpen": ["Sharpen", [param("ADBE Sharpen-0001", "Sharpen Amount", P.OneD, 0, {min: 0, max: 4000})]],
        "ADBE Color Key": ["Color Key", [
            swap ? param("ADBE Color Key-0001", "Color Tolerance", P.OneD, 0, {min: 0, max: 255})
                : param("ADBE Color Key-0001", "Key Color", P.COLOR, [0, 0, 1, 1]),
            swap ? param("ADBE Color Key-0002", "Key Color", P.COLOR, [0, 0, 1, 1])
                : param("ADBE Color Key-0002", "Color Tolerance", P.OneD, 0, {min: 0, max: 255}),
            param("ADBE Color Key-0003", "Edge Thin", P.OneD, 0, {min: -5, max: 5}),
            param("ADBE Color Key-0004", "Edge Feather", P.OneD, 0, {min: 0, max: 500})]],
        "ADBE Gradient Wipe": ["Gradient Wipe", [
            param("ADBE Gradient Wipe-0001", "Transition Completion", P.OneD, 0, {min: 0, max: 100}),
            param("ADBE Gradient Wipe-0002", "Transition Softness", P.OneD, 0, {min: 0, max: 100}),
            param("ADBE Gradient Wipe-0003", "Gradient Layer", P.LAYER_INDEX, 0, {min: 0, holdOnly: true}),
            param("ADBE Gradient Wipe-0004", "Gradient Placement", P.OneD, 3, {min: 1, max: 3, integer: true, holdOnly: true}),
            param("ADBE Gradient Wipe-0005", "Invert Gradient", P.OneD, 0, {min: 0, max: 1, integer: true, holdOnly: true})]],
        "ADBE Lumetri": ["Lumetri Color", [
            param("ADBE Lumetri-0012", "Temperature", P.OneD, 0, {min: -100, max: 100}),
            param("ADBE Lumetri-0013", "Tint", P.OneD, 0, {min: -100, max: 100}),
            param("ADBE Lumetri-0015", "Exposure", P.OneD, 0, {min: -5, max: 5}),
            param("ADBE Lumetri-0016", "Contrast", P.OneD, 0, {min: -100, max: 100}),
            param("ADBE Lumetri-0017", "Highlights", P.OneD, 0, {min: -100, max: 100}),
            param("ADBE Lumetri-0018", "Shadows", P.OneD, 0, {min: -100, max: 100}),
            param("ADBE Lumetri-0021", "Saturation", P.OneD, 100, {min: 0, max: 200}),
            param("ADBE Lumetri-0029", "Vibrance", P.OneD, 0, {min: -100, max: 100})]]
    };
    if (host.options.keylight) {
        EFFECTS["Keylight 906"] = ["Keylight (1.2)", [
            param("Keylight 906-0001", "View", P.OneD, 1, {min: 1, max: 11, integer: true, holdOnly: true}),
            param("Keylight 906-0003", "Screen Colour", P.COLOR, [0, 0, 0, 1])]];
    }

    function makeEffect(match) {
        return function (parent) {
            var spec = EFFECTS[match], params = [], i, p;
            for (i = 0; i < spec[1].length; i++) {
                p = spec[1][i];
                params.push(new Property(null, p[0], p[1], p[2], p[3], p[4]));
            }
            var g = new PropertyGroup(parent, match, spec[0], {}, params);
            g.__effect = true;
            return g;
        };
    }
    var effectFactories = {};
    for (var em in EFFECTS) {
        effectFactories[em] = makeEffect(em);
        effectFactories[EFFECTS[em][0]] = makeEffect(em);
    }

    function maskAtom(parent) {
        var g = new PropertyGroup(parent, "ADBE Mask Atom", "Mask " + (parent._children.length + 1), {}, [
            new Property(null, "ADBE Mask Shape", "Mask Path", P.SHAPE, new Shape()),
            new Property(null, "ADBE Mask Feather", "Mask Feather", P.TwoD, [0, 0]),
            new Property(null, "ADBE Mask Opacity", "Mask Opacity", P.OneD, 100, {min: 0, max: 100}),
            new Property(null, "ADBE Mask Offset", "Mask Expansion", P.OneD, 0)]);
        g.maskMode = MaskMode.ADD;
        g.inverted = false;
        var mode = MaskMode.ADD;
        Object.defineProperty(g, "maskMode", {get: function () { return mode; }, set: function (v) { mode = enumValue(MaskMode, v, "maskMode"); }});
        return g;
    }

    // ---- shapes ------------------------------------------------------------------------------------
    function vectorTransform(parent) {
        return new PropertyGroup(parent, "ADBE Vector Transform Group", "Transform", {}, [
            new Property(null, "ADBE Vector Anchor", "Anchor Point", P.TwoD_SPATIAL, [0, 0]),
            new Property(null, "ADBE Vector Position", "Position", P.TwoD_SPATIAL, [0, 0]),
            new Property(null, "ADBE Vector Scale", "Scale", P.TwoD, [100, 100]),
            new Property(null, "ADBE Vector Group Opacity", "Opacity", P.OneD, 100, {min: 0, max: 100})]);
    }
    var vectorItems = {
        "ADBE Vector Shape - Rect": function (parent) {
            return new PropertyGroup(parent, "ADBE Vector Shape - Rect", "Rectangle Path 1", {}, [
                new Property(null, "ADBE Vector Rect Size", "Size", P.TwoD, [100, 100]),
                new Property(null, "ADBE Vector Rect Position", "Position", P.TwoD_SPATIAL, [0, 0]),
                new Property(null, "ADBE Vector Rect Roundness", "Roundness", P.OneD, 0, {min: 0})]);
        },
        "ADBE Vector Graphic - Fill": function (parent) {
            return new PropertyGroup(parent, "ADBE Vector Graphic - Fill", "Fill 1", {}, [
                new Property(null, "ADBE Vector Fill Color", "Color", P.COLOR, [1, 0, 0, 1]),
                new Property(null, "ADBE Vector Fill Opacity", "Opacity", P.OneD, 100, {min: 0, max: 100})]);
        },
        "ADBE Vector Graphic - Stroke": function (parent) {
            return new PropertyGroup(parent, "ADBE Vector Graphic - Stroke", "Stroke 1", {}, [
                new Property(null, "ADBE Vector Stroke Color", "Color", P.COLOR, [1, 1, 1, 1]),
                new Property(null, "ADBE Vector Stroke Opacity", "Opacity", P.OneD, 100, {min: 0, max: 100}),
                new Property(null, "ADBE Vector Stroke Width", "Stroke Width", P.OneD, 2, {min: 0})]);
        }
    };
    function vectorGroup(parent) {
        var contents = new PropertyGroup(null, "ADBE Vectors Group", "Contents", vectorItems, []);
        return new PropertyGroup(parent, "ADBE Vector Group", "Group " + (parent._children.length + 1), {},
            [contents, vectorTransform(null)]);
    }

    // ---- items -------------------------------------------------------------------------------------
    function Item(typeName, name) {
        this.id = nextId++;
        this.typeName = typeName;
        this.name = name;
        this.comment = "";
        this.label = 0;
        this._parentFolder = null;
    }
    Object.defineProperty(Item.prototype, "parentFolder", {
        get: function () { return this._parentFolder; },
        set: function (f) {
            if (!(f instanceof FolderItem)) throw aeError("parentFolder must be a FolderItem");
            if (this._parentFolder) {
                var list = this._parentFolder._items;
                for (var i = 0; i < list.length; i++) if (list[i] === this) { list.splice(i, 1); break; }
            }
            f._items.push(this);
            this._parentFolder = f;
        }
    });

    function FolderItem(name) {
        Item.call(this, "Folder", name);
        this._items = [];
    }
    FolderItem.prototype = Object.create(Item.prototype);
    FolderItem.prototype.constructor = FolderItem;
    Object.defineProperty(FolderItem.prototype, "items", {get: function () { return new ItemCollection(this); }});
    Object.defineProperty(FolderItem.prototype, "numItems", {get: function () { return this._items.length; }});

    function AVItem(typeName, name, info) {
        Item.call(this, typeName, name);
        this.width = info.width;
        this.height = info.height;
        this.pixelAspect = info.pixelAspect || 1;
        this.frameRate = info.frameRate;
        this.duration = info.duration;
        this.hasVideo = info.hasVideo !== false;
        this.hasAudio = !!info.hasAudio;
        this.footageMissing = !!info.missing;
    }
    AVItem.prototype = Object.create(Item.prototype);
    Object.defineProperty(AVItem.prototype, "frameDuration", {get: function () { return 1 / this.frameRate; }});

    function FootageItem(name, info, file) {
        AVItem.call(this, "Footage", name, info);
        this.file = file || null;
        this.mainSource = {isStill: !!info.still, conformFrameRate: 0, hasAlpha: !!info.alpha, file: file || null};
        this._still = !!info.still;
    }
    FootageItem.prototype = Object.create(AVItem.prototype);
    FootageItem.prototype.constructor = FootageItem;

    function CompItem(name, w, h, par, dur, fps) {
        AVItem.call(this, "Composition", name, {width: w, height: h, pixelAspect: par, duration: dur, frameRate: fps, hasVideo: true});
        this._layers = [];
        this._bg = [0, 0, 0];
        this.markerProperty = new Property(null, "ADBE Marker", "Marker", P.MARKER, null);
        this._opened = false;
    }
    CompItem.prototype = Object.create(AVItem.prototype);
    CompItem.prototype.constructor = CompItem;
    Object.defineProperty(CompItem.prototype, "layers", {get: function () { return new LayerCollection(this); }});
    Object.defineProperty(CompItem.prototype, "numLayers", {get: function () { return this._layers.length; }});
    Object.defineProperty(CompItem.prototype, "bgColor", {
        get: function () { return clone(this._bg); },
        set: function (v) {
            if (!isArr(v) || v.length !== 3) throw aeError("bgColor must be [r, g, b]");
            for (var i = 0; i < 3; i++) if (!(isNum(v[i]) && v[i] >= 0 && v[i] <= 1)) throw aeError("bgColor values are 0..1");
            this._bg = clone(v);
        }
    });
    CompItem.prototype.openInViewer = function () { this._opened = true; return null; };
    CompItem.prototype.layer = function (i) { return this._layers[i - 1]; };

    function checkRange(v, lo, hi, what, integer) {
        if (!isNum(v) || v < lo || v > hi || (integer && Math.floor(v) !== v)) {
            throw aeError(what + " must be " + (integer ? "a whole number " : "") + "in [" + lo + ".." + hi + "], got " + v);
        }
    }

    function ItemCollection(folder) {
        this._folder = folder;
    }
    Object.defineProperty(ItemCollection.prototype, "length", {get: function () { return this._folder._items.length; }});
    ItemCollection.prototype.addFolder = function (name) {
        if (typeof name !== "string") throw aeError("folder name must be a string");
        var f = new FolderItem(name);
        f.parentFolder = this._folder;
        return f;
    };
    ItemCollection.prototype.addComp = function (name, w, h, par, dur, fps) {
        if (arguments.length !== 6) throw aeError("addComp needs 6 arguments");
        if (typeof name !== "string") throw aeError("comp name must be a string");
        checkRange(w, 4, 30000, "comp width", true);
        checkRange(h, 4, 30000, "comp height", true);
        checkRange(par, 0.01, 100, "pixel aspect");
        checkRange(dur, 0, 10800, "comp duration");
        checkRange(fps, 1, 999, "comp frame rate");
        var c = new CompItem(name, w, h, par, dur, fps);
        c.parentFolder = this._folder;
        return c;
    };

    // ---- layers ------------------------------------------------------------------------------------
    function transformGroup(layer, w, h, compW, compH) {
        var pos = new Property(null, "ADBE Position", "Position", P.ThreeD_SPATIAL, [compW / 2, compH / 2, 0], {separable: true});
        pos._followers = [
            new Property(null, "ADBE Position_0", "X Position", P.OneD, compW / 2),
            new Property(null, "ADBE Position_1", "Y Position", P.OneD, compH / 2),
            new Property(null, "ADBE Position_2", "Z Position", P.OneD, 0)];
        for (var i = 0; i < 3; i++) {
            pos._followers[i]._leader = pos;
            pos._followers[i]._owner = layer;
        }
        return new PropertyGroup(layer, "ADBE Transform Group", "Transform", {}, [
            new Property(null, "ADBE Anchor Point", "Anchor Point", P.ThreeD_SPATIAL, [w / 2, h / 2, 0]),
            pos,
            new Property(null, "ADBE Scale", "Scale", P.ThreeD, [100, 100, 100]),
            new Property(null, "ADBE Rotate Z", "Rotation", P.OneD, 0),
            new Property(null, "ADBE Opacity", "Opacity", P.OneD, 100, {min: 0, max: 100})]);
    }

    function Layer(comp, source, kind, length) {
        this.__isLayer = true;
        this._comp = comp;
        this.source = source;
        this.id = nextId++;
        this._kind = kind;
        this._v = {name: source ? source.name : (kind === "text" ? "Text" : "Shape Layer 1"), startTime: 0, stretch: 100,
            inPoint: 0, outPoint: length, enabled: true, audioEnabled: true, guideLayer: false, comment: "",
            label: 0, blendingMode: BlendingMode.NORMAL, timeRemapEnabled: false, shy: false};
        this._locked = false;
        this._handles = [];
        var w = source ? source.width : comp.width, h = source ? source.height : comp.height;
        if (kind !== "av") { w = 0; h = 0; }
        this._groups = [transformGroup(this, w, h, comp.width, comp.height),
            new PropertyGroup(this, "ADBE Effect Parade", "Effects", effectFactories, []),
            new PropertyGroup(this, "ADBE Mask Parade", "Masks", {"ADBE Mask Atom": maskAtom, "Mask": maskAtom}, [])];
        for (var g = 0; g < this._groups.length; g++) this._groups[g]._owner = this;
        if (this.hasAudio && !host.options.noAudioGroup) {
            var audio = new PropertyGroup(this, "ADBE Audio Group", "Audio", {}, [
                new Property(null, "ADBE Audio Levels", "Audio Levels", P.TwoD, [0, 0], {min: -192, max: 24})]);
            this._groups.push(audio);
        }
        if (this.canSetTimeRemapEnabled) {
            this._remap = new Property(this, "ADBE Time Remapping", "Time Remap", P.OneD, 0);
        }
        if (kind === "text") {
            this._groups.push(new PropertyGroup(this, "ADBE Text Properties", "Text", {}, [
                new Property(null, "ADBE Text Document", "Source Text", P.TEXT_DOCUMENT, new TextDocument(""))]));
        }
        if (kind === "shape") {
            this._groups.push(new PropertyGroup(this, "ADBE Root Vectors Group", "Contents", {"ADBE Vector Group": vectorGroup}, []));
        }
    }
    function layerAttr(name, check) {
        Object.defineProperty(Layer.prototype, name, {
            get: function () { return clone(this._v[name]); },
            set: function (v) {
                if (this._locked) throw aeError("the layer is locked; " + name + " cannot change");
                this._v[name] = check ? check.call(this, v) : v;
            }
        });
    }
    layerAttr("name", function (v) { if (typeof v !== "string") throw aeError("name must be a string"); return v; });
    layerAttr("comment", function (v) { if (typeof v !== "string") throw aeError("comment must be a string"); return v; });
    layerAttr("enabled", function (v) { return !!v; });
    layerAttr("guideLayer", function (v) { return !!v; });
    layerAttr("shy", function (v) { return !!v; });
    layerAttr("label", function (v) { checkRange(v, 0, 16, "label", true); return v; });
    layerAttr("blendingMode", function (v) { return enumValue(BlendingMode, v, "blendingMode"); });
    layerAttr("audioEnabled", function (v) {
        if (!this.hasAudio) throw aeError("audioEnabled: this layer has no audio");
        return !!v;
    });
    Object.defineProperty(Layer.prototype, "locked", {
        get: function () { return this._locked; },
        set: function (v) { this._locked = !!v; }
    });
    Object.defineProperty(Layer.prototype, "hasAudio", {get: function () { return !!(this.source && this.source.hasAudio); }});
    Object.defineProperty(Layer.prototype, "hasVideo", {get: function () { return this._kind !== "av" || !this.source || this.source.hasVideo; }});
    Object.defineProperty(Layer.prototype, "index", {get: function () {
        for (var i = 0; i < this._comp._layers.length; i++) if (this._comp._layers[i] === this) return i + 1;
        return 0;
    }});
    Object.defineProperty(Layer.prototype, "containingComp", {get: function () { return this._comp; }});
    Object.defineProperty(Layer.prototype, "_still", {get: function () {
        return this._kind !== "av" || (this.source instanceof FootageItem && this.source._still);
    }});
    Object.defineProperty(Layer.prototype, "canSetTimeRemapEnabled", {get: function () {
        return this._kind === "av" && this.source && !(this.source instanceof FootageItem && this.source._still);
    }});
    Layer.prototype._extent = function () {
        // the comp times the source covers (unlimited for stills, text, shapes and remapped layers)
        if (this._still || this._v.timeRemapEnabled) return null;
        var a = this._v.startTime, b = this._v.startTime + this.source.duration * this._v.stretch / 100;
        return [Math.min(a, b), Math.max(a, b)];
    };
    Object.defineProperty(Layer.prototype, "startTime", {
        get: function () { return this._v.startTime; },
        set: function (v) {
            if (this._locked) throw aeError("the layer is locked");
            if (!isNum(v)) throw aeError("startTime must be a number");
            var d = v - this._v.startTime;
            this._v.startTime = v;
            this._v.inPoint += d;
            this._v.outPoint += d;
        }
    });
    Object.defineProperty(Layer.prototype, "stretch", {
        get: function () { return this._v.stretch; },
        set: function (v) {
            if (this._locked) throw aeError("the layer is locked");
            if (!isNum(v) || v === 0 || Math.abs(v) > 9900) throw aeError("stretch must be non-zero within +/-9900, got " + v);
            var s = this._v.startTime, f = v / this._v.stretch;
            this._v.inPoint = s + (this._v.inPoint - s) * f;
            this._v.outPoint = s + (this._v.outPoint - s) * f;
            this._v.stretch = v;
        }
    });
    function clampTo(layer, v) {
        var e = layer._extent();
        if (!e) return v;
        return Math.max(e[0], Math.min(e[1], v));
    }
    Object.defineProperty(Layer.prototype, "inPoint", {
        get: function () { return this._v.inPoint; },
        set: function (v) {
            if (this._locked) throw aeError("the layer is locked");
            if (!isNum(v)) throw aeError("inPoint must be a number");
            v = clampTo(this, v);
            if (v >= this._v.outPoint) throw aeError("inPoint " + v + " must be before outPoint " + this._v.outPoint);
            this._v.inPoint = v;
        }
    });
    Object.defineProperty(Layer.prototype, "outPoint", {
        get: function () { return this._v.outPoint; },
        set: function (v) {
            if (this._locked) throw aeError("the layer is locked");
            if (!isNum(v)) throw aeError("outPoint must be a number");
            v = clampTo(this, v);
            if (v <= this._v.inPoint) throw aeError("outPoint " + v + " must be after inPoint " + this._v.inPoint);
            this._v.outPoint = v;
        }
    });
    Object.defineProperty(Layer.prototype, "timeRemapEnabled", {
        get: function () { return this._v.timeRemapEnabled; },
        set: function (v) {
            if (this._locked) throw aeError("the layer is locked");
            if (!this.canSetTimeRemapEnabled) throw aeError("time remapping cannot be enabled on this layer");
            if (v && !this._v.timeRemapEnabled) {
                // AE adds keys at the in and out points with the source times shown there
                var s = this._v.startTime, k = 100 / this._v.stretch;
                this._remap._addKey(this._v.inPoint, (this._v.inPoint - s) * k);
                this._remap._addKey(this._v.outPoint, (this._v.outPoint - s) * k);
            }
            this._v.timeRemapEnabled = !!v;
        }
    });
    Layer.prototype._invalidate = PropertyGroup.prototype._invalidate;
    Layer.prototype._handle = PropertyGroup.prototype._handle;
    Layer.prototype.property = function (key) {
        if (key === "ADBE Time Remapping" || key === "Time Remap") {
            return this._remap && this._v.timeRemapEnabled ? this._remap : null;
        }
        for (var i = 0; i < this._groups.length; i++) {
            if (this._groups[i].matchName === key || this._groups[i].name === key) return this._handle(this._groups[i]);
        }
        return null;
    };
    Layer.prototype.__dump = function () {
        var out = {index: this.index, kind: this._kind, source: this.source ? this.source.name : null,
            sourceId: this.source ? this.source.id : null, locked: this._locked, groups: {}};
        for (var k in this._v) out[k] = clone(this._v[k]);
        for (var i = 0; i < this._groups.length; i++) out.groups[this._groups[i].matchName] = this._groups[i].__dump();
        if (this._remap && this._v.timeRemapEnabled) out.timeRemap = this._remap.__dump();
        return out;
    };

    function LayerCollection(comp) {
        this._comp = comp;
    }
    Object.defineProperty(LayerCollection.prototype, "length", {get: function () { return this._comp._layers.length; }});
    LayerCollection.prototype._top = function (layer) {
        this._comp._layers.unshift(layer);
        return layer;
    };
    LayerCollection.prototype.add = function (item, duration) {
        if (!(item instanceof AVItem)) throw aeError("layers.add needs an AVItem (footage or comp)");
        var length;
        if (item instanceof FootageItem && item._still) {
            length = duration !== undefined ? duration : this._comp.duration;
            checkRange(length, 1e-6, 10800, "still layer duration");
        } else {
            length = item.duration;
        }
        return this._top(new Layer(this._comp, item, "av", length));
    };
    LayerCollection.prototype.addText = function (text) {
        var L = new Layer(this._comp, null, "text", this._comp.duration);
        var doc = new TextDocument(text);
        L._groups[L._groups.length - 1]._children[0]._value = doc;
        var t = L._groups[0];
        t._children[0]._value = [0, 0, 0];
        return this._top(L);
    };
    LayerCollection.prototype.addShape = function () {
        var L = new Layer(this._comp, null, "shape", this._comp.duration);
        L._groups[0]._children[0]._value = [0, 0, 0];
        return this._top(L);
    };

    // ---- project and app ---------------------------------------------------------------------------
    var root = new FolderItem("Root");
    var allItems = function () {
        var out = [], walk = function (f) {
            for (var i = 0; i < f._items.length; i++) {
                out.push(f._items[i]);
                if (f._items[i] instanceof FolderItem) walk(f._items[i]);
            }
        };
        walk(root);
        return out;
    };
    var undo = {depth: 0, groups: [], maxDepth: 0};
    var project = {
        rootFolder: root,
        file: host.options.projectFile ? new File(host.options.projectFile) : null,
        get items() { return new ItemCollection(root); },
        get numItems() { return allItems().length; },
        item: function (i) { return allItems()[i - 1]; },
        itemByID: function (id) {
            var list = allItems();
            for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
            return null;
        },
        importFile: function (opts) {
            if (!(opts instanceof ImportOptions)) throw aeError("importFile needs an ImportOptions");
            if (!(opts.file instanceof File) || !opts.file.exists) throw aeError("File not found: " + (opts.file ? opts.file.fsName : "none"));
            if (opts.importAs !== ImportAsType.FOOTAGE) throw aeError("this mock imports footage only");
            var info = host.mediaInfo(opts.file.fsName, opts.sequence);
            var item = new FootageItem(opts.file.name, info, opts.file);
            item.parentFolder = root;
            return item;
        },
        importPlaceholder: function (name, w, h, fps, dur) {
            if (arguments.length !== 5) throw aeError("importPlaceholder needs 5 arguments");
            if (typeof name !== "string") throw aeError("placeholder name must be a string");
            checkRange(w, 4, 30000, "placeholder width", true);
            checkRange(h, 4, 30000, "placeholder height", true);
            checkRange(fps, 1, 99, "placeholder frame rate");
            checkRange(dur, 0, 10800, "placeholder duration");
            var item = new FootageItem(name, {width: w, height: h, frameRate: fps, duration: dur, missing: true}, null);
            item.typeName = "Placeholder";
            item.parentFolder = root;
            return item;
        }
    };
    var fonts = host.fonts.length ? {
        getFontsByFamilyNameAndStyleName: function (family, style) {
            var out = [];
            for (var i = 0; i < host.fonts.length; i++) {
                var f = host.fonts[i];
                if (f[0] === family && f[1] === style) out.push({familyName: f[0], styleName: f[1], postScriptName: f[2]});
            }
            return out.length ? out : undefined;
        }
    } : undefined;
    var app = {
        project: project,
        version: "26.0x1",
        isoLanguage: host.options.language || "en_US",
        fonts: host.options.fontsApi ? fonts : undefined,
        beginUndoGroup: function (name) {
            if (typeof name !== "string") throw aeError("beginUndoGroup needs a string");
            undo.depth++;
            undo.maxDepth = Math.max(undo.maxDepth, undo.depth);
            undo.groups.push(name);
        },
        endUndoGroup: function () {
            if (undo.depth <= 0) throw aeError("endUndoGroup without a matching beginUndoGroup");
            undo.depth--;
        },
        newProject: function () { throw aeError("the mock always has a project"); }
    };

    var log = {alerts: [], info: []};
    global.app = app;
    global.File = File;
    global.Folder = Folder;
    global.KeyframeEase = KeyframeEase;
    global.KeyframeInterpolationType = KeyframeInterpolationType;
    global.PropertyValueType = PropertyValueType;
    global.ImportOptions = ImportOptions;
    global.ImportAsType = ImportAsType;
    global.ParagraphJustification = ParagraphJustification;
    global.MaskMode = MaskMode;
    global.BlendingMode = BlendingMode;
    global.MarkerValue = MarkerValue;
    global.Shape = Shape;
    global.TextDocument = TextDocument;
    global.CompItem = CompItem;
    global.FootageItem = FootageItem;
    global.FolderItem = FolderItem;
    var dollar = {fileName: host.options.scriptPath, global: global};
    dollar.evalFile = function (file) {
        var p = file instanceof File ? file.fsName : host.resolve(decodePath(file));
        if (!host.isFile(p)) throw aeError("evalFile: no file " + p);
        var saved = dollar.fileName;
        dollar.fileName = p;
        try {
            // '#target' lines are preprocessor directives, not JavaScript
            return (0, eval)(host.readText(p).replace(/^#/mg, "//#"));
        } finally {
            dollar.fileName = saved;
        }
    };
    global.$ = dollar;
    global.alert = function (text) { log.alerts.push(String(text)); };
    global.writeLn = function (text) { log.info.push(String(text)); };

    var stringifyJSON = JSON.stringify;
    global.__dump = function () {
        var items = allItems(), out = {items: [], undo: undo, alerts: log.alerts, info: log.info,
            quietFlagLeft: global.ZENVI_AE_QUIET !== undefined};
        for (var i = 0; i < items.length; i++) {
            var it = items[i], d = {id: it.id, type: it.typeName, name: it.name, comment: it.comment,
                parent: it._parentFolder ? it._parentFolder.name : null};
            if (it instanceof FootageItem) {
                d.file = it.file ? it.file.fsName : null;
                d.missing = it.footageMissing;
                d.conformFrameRate = it.mainSource.conformFrameRate;
            }
            if (it instanceof CompItem) {
                d.width = it.width;
                d.height = it.height;
                d.pixelAspect = it.pixelAspect;
                d.duration = it.duration;
                d.frameRate = it.frameRate;
                d.bgColor = it._bg;
                d.opened = it._opened;
                d.markers = it.markerProperty.__dump().keys || [];
                d.layers = [];
                for (var j = 0; j < it._layers.length; j++) d.layers.push(it._layers[j].__dump());
            }
            out.items.push(d);
        }
        return stringifyJSON(out);
    };
}(this));

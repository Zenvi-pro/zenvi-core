# Graph Report - zenvi-core  (2026-08-03)

## Corpus Check
- 233 files · ~1,199,217 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 4924 nodes · 9606 edges · 279 communities (240 shown, 39 thin omitted)
- Extraction: 83% EXTRACTED · 17% INFERRED · 0% AMBIGUOUS · INFERRED: 1623 edges (avg confidence: 0.69)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `ea7023b2`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Windows Views
- Classes Tool Handlers
- Windows Main Window
- Timeline Media
- Windows Main Window 2
- Windows Views 2
- Windows Classes
- Windows Views 3
- Classes Project Data
- Windows Views 4
- Chat Ui Chat
- Classes Clip Utils
- Windows Views 5
- Windows Ai Chat Ui
- Timeline Media 2
- Windows Views 6
- Classes Clip Resolver
- Timeline Media 3
- Windows Title Editor
- Windows Main Window 3
- Windows Models
- Windows Views 7
- Timeline Media 4
- Timeline Media 5
- Windows Views 8
- Windows Views 9
- Classes App
- Windows Video Widget
- Classes Ai Metadata Utils
- Windows Views 10
- Windows Views 11
- Classes Exporters
- Windows Main Window 4
- Timeline Media 6
- Windows Ai Chat Ui 2
- Windows Plan Dock Ui
- Classes Auth Manager
- Classes Timeline Clip Context
- Classes Thumbnail
- Windows Views 12
- Classes Twelvelabs Match
- Windows Ai Chat Ui 3
- Windows Views 13
- Windows Views 14
- Windows Ai Media Panel
- Windows Views 15
- Classes Tool Handlers 2
- Classes Exporters 2
- Classes Metrics
- Classes Track Display
- Timeline Media 7
- Windows Login Window
- Windows Preview Thread
- Windows Views 16
- Index Timeline
- Windows Views 17
- Windows Views 18
- Windows Models 2
- Classes Auto Updater
- Classes Importers
- Windows Views 19
- Classes Query
- Classes Update Installer
- Timeline
- Timeline Media 8
- Windows Export
- Windows Main Window 5
- Classes Api Client
- Classes Importers 2
- Windows Cutting
- Windows Preferences
- Classes Credits Client
- Timeline 2
- Windows Profile Edit
- Classes Ui Util
- Windows Views 20
- Windows Views 21
- Windows Pexels Dock
- Windows Views 22
- Windows Views 23
- Windows Views 24
- Classes Language
- Windows Views 25
- Windows Views 26
- Windows Views 27
- Windows Agent Trace Dialog
- Windows Views 28
- Windows Ai Chat Ui 4
- Windows Views 29
- Classes Api Client 2
- Classes Recompute Queue
- Windows Ai Chat Ui 5
- Timeline Media 9
- Windows About
- Windows Freesound Dock
- Windows Main Window 6
- Windows Pexels Dock 2
- Windows Views 30
- Classes Info
- Classes Settings
- Windows Color Picker
- Windows Freesound Dock 2
- Windows Main Window 7
- Windows Models 3
- Windows Views 31
- Windows Color Picker 2
- Windows Export Clips
- Language Test Translations
- Windows Views 32
- Windows Views 33
- Classes Updates
- Themes Base
- Windows Models 4
- Windows Preferences 2
- Windows Process Effect
- Windows Views 34
- Windows Login Window 2
- Classes Zenvi Env
- Classes Exporters 3
- Classes Path Utils
- Classes Json Data
- Classes Query 2
- Classes Update Queue
- Timeline Media 10
- Windows Views 35
- Classes Api Client 3
- Classes Sentry
- Classes Updates 2
- Windows File Properties
- Windows Freesound Dock 3
- Windows Views 36
- Windows Profile
- Windows Views 37
- Windows Export Clips 2
- Classes Query 3
- Tests Query Tests
- Timeline 3
- Timeline 4
- Timeline Media 11
- Timeline Media 12
- Windows Animated Title
- Windows Export 2
- Windows Video Widget 2
- Windows Views 38
- Windows Views 39
- Windows Views 40
- Classes Frame Extractor
- Classes Info 2
- Classes Update Queue 2
- Windows Region
- Windows Views 41
- Windows Views 42
- Windows Process Effect 2
- Windows Views 43
- Classes Info 3
- Classes Legacy
- Classes Tool Handlers 3
- Emojis README
- Profiles Definitions
- Themes Humanity
- Windows Main Window 8
- Windows Models 5
- Windows Models 6
- Windows Pexels Dock 3
- Windows Views 44
- Windows Views 46
- Windows Views 47
- Windows Views 48
- Windows Views 49
- Classes Logger
- Windows Main Window 9
- Classes Settings 2
- Themes Manager
- Windows Ai Chat Ui 6
- Windows Models 7
- Windows Views 50
- Windows Views 51
- Windows Views 52
- Windows Views 53
- Windows Models 8
- Windows Views 54
- Classes Auto Updater 2
- Classes Legacy 2
- Classes Tool Handlers 4
- Classes Updates 3
- Classes Updates 4
- Classes Updates 5
- Plan Ui Plan
- Windows Main Window 10
- Windows Models 9
- Windows Video Widget 3
- Windows Views 55
- Windows Views 56
- Windows Main Window 11
- Classes Index Proxy
- Classes Title Bar
- Windows Ai Chat Ui 7
- Windows Models 10
- Windows Models 11
- Windows Views 57
- Windows Views 58
- Classes Api Client 4
- Classes Clip Placement
- Classes Conversion
- Classes Exceptions
- Classes Json Data 2
- Classes Qt Types
- upload_file_to_gemini_resumable
- Classes Update Installer 2
- Timeline 5
- .detect_font
- Windows Models 12
- Windows Views 59
- ._content_height_hint
- .Paste_Triggered
- Windows Views 62
- Classes Api Client 5
- Classes Legacy 3
- Classes Legacy 4
- Classes Legacy 5
- Classes Legacy 6
- Classes Legacy 7
- Classes Logger Libopenshot
- _normalized_to_center_pixels
- Windows Models 13
- Windows Title Editor 2
- Windows Views 63
- Windows Views 64
- Windows Views 65
- Classes Api Client 6
- Classes Exporters 4
- Classes Json Data 3
- Themes Base 2
- Timeline Media 13
- Windows Export 4
- Windows Preferences 3
- Windows Views 66
- Windows Views 67
- Timeline App
- Timeline 6
- Timeline 7
- Timeline Media 14
- Timeline Media 15
- Timeline Media 16
- Timeline Media 17
- Classes Auto Updater 3
- Classes Auto Updater 4
- Classes Tool Handlers 5
- Classes Tool Handlers 6
- Classes Update Installer 3
- Emojis Optimize Emojis
- Init
- Timeline Media 18
- Timeline Media 19
- Timeline Media 20
- Timeline Media 21
- Timeline Media 22
- Windows Views 69

## God Nodes (most connected - your core abstractions)
1. `get_app()` - 408 edges
2. `MainWindow` - 263 edges
3. `TimelineWidgetBase` - 111 edges
4. `TimelineView` - 109 edges
5. `AIChatWindow` - 74 edges
6. `File` - 59 edges
7. `StyledContextMenu` - 59 edges
8. `Clip` - 58 edges
9. `we()` - 55 edges
10. `KeyframePanelMixin` - 53 edges

## Surprising Connections (you probably didn't know these)
- `te()` --indirect_call--> `e()`  [INFERRED]
  src/timeline/media/js/angular.min.js → src/timeline/media/js/ui-bootstrap-tpls-1.3.3.min.js
- `winnow()` --indirect_call--> `i()`  [INFERRED]
  src/timeline/media/js/jquery.js → src/timeline/media/js/ui-bootstrap-tpls-1.3.3.min.js
- `TitlesListView` --uses--> `TitleRoles`  [INFERRED]
  src/windows/views/titles_listview.py → src/windows/models/titles_model.py
- `_score_clip_against_query()` --calls--> `get_source_window()`  [INFERRED]
  src/classes/clip_resolver.py → src/classes/ai_metadata_utils.py
- `build_timeline_clip_context()` --calls--> `get_source_window()`  [INFERRED]
  src/classes/timeline_clip_context.py → src/classes/ai_metadata_utils.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Qt WebChannel Embedded UIs** — src_chat_ui_index_zenvi_assistant, src_plan_ui_plan_plan_ui, src_timeline_index_openshot_timeline [INFERRED 0.85]
- **Plan Mode Chat to Execute Flow** — src_chat_ui_index_plan_mode, src_plan_ui_plan_plan_ui, src_plan_ui_plan_execute_plan [EXTRACTED 1.00]
- **Timeline Editing Surface** — src_timeline_index_clip_directive, src_timeline_index_track_directive, src_timeline_index_transition_directive, src_timeline_index_playhead_directive, src_timeline_index_keyframe_directive [EXTRACTED 1.00]

## Communities (279 total, 39 thin omitted)

### Community 0 - "Windows Views"
Cohesion: 0.03
Nodes (61): Zenvi Backend API Client.  Drop-in replacement for direct AI class imports. Th, @file  @brief This file creates the QApplication, and displays the main window, @file  @brief Background auto-updater that checks GitHub releases and downloads, @file  @brief This file connects to libopenshot and logs debug messages (if deb, @file  @brief This file sets the default logging settings  @author Noah Figg <, Adjust the minimum log level written to our logfile, Adjust the minimum log level for output to the terminal, Filter out lines that originated on the output (+53 more)

### Community 1 - "Classes Tool Handlers"
Cohesion: 0.04
Nodes (103): add_clip_to_timeline(), add_marker(), add_track(), add_transition_between_clips(), add_transition_to_clip(), add_tts_audio_to_timeline(), apply_transition(), _bake_transition_video() (+95 more)

### Community 2 - "Windows Main Window"
Cohesion: 0.02
Nodes (39): QMainWindow, MainWindow, Preview the selected media file, Enable / Disable follow mode, Center the timeline on the current playhead position, Selects ALL clips or transitions to the right of the current selected item, Caption text was edited, start the save timer (to prevent spamming saves), Emit the CaptionTextUpdated signal (and if that property is active/selected, it (+31 more)

### Community 3 - "Timeline Media"
Cohesion: 0.04
Nodes (80): Fa(), Aa(), ad(), Af(), ag(), Bf(), Ca(), cb() (+72 more)

### Community 4 - "Windows Main Window 2"
Cohesion: 0.03
Nodes (33): get_app(), Get the current QApplication instance of OpenShot, LoggerLibOpenShot, Thread, Save the object back to the project data store, CosmicTheme, Toggle the play icon between play and pause., Handle when playback is started (+25 more)

### Community 5 - "Windows Views 2"
Cohesion: 0.04
Nodes (46): BackgroundPainter, QPainter, QRectF, @file  @brief Painter for the timeline background gradient.  @author Jonathan Th, BasePainter, @file  @brief Base painter helpers for the QWidget timeline backend.  @author Jo, Return ``(width, height)`` of *pixmap* in logical units., Return *pixmap* scaled to the requested logical size. (+38 more)

### Community 6 - "Windows Classes"
Cohesion: 0.17
Nodes (6): Delete the object from the project data store, Delete the object from the project data store, Delete the object from the project data store, Delete the object from the project data store, Delete the object from the project data store, Delete the object from the project data store

### Community 7 - "Windows Views 3"
Cohesion: 0.10
Nodes (70): _annotate_svg_metadata(), _apply_clip_box_shadow(), _apply_css(), _apply_css_color_value(), _apply_css_float_value(), _apply_gradient_with_fallback(), _apply_overrides(), apply_theme() (+62 more)

### Community 8 - "Classes Project Data"
Cohesion: 0.05
Nodes (38): get_assets_path(), @file  @brief This file generates the path for a project's assets  @author Jonat, Get and/or create the current assets path. This path is used for thumbnail and b, change_profile(), @file  @brief This file converts project data from one FPS to a new FPS (adjusti, Adjust all clip-like objects to use project FPS precision, adjusting 'end' trim, remove_gaps(), @file  @brief This file loads and saves settings (as JSON)  @author Noah Figg <e (+30 more)

### Community 9 - "Windows Views 4"
Cohesion: 0.04
Nodes (20): A Web(Engine/Kit)View QWidget used to load the Timeline, Hide the waveform for the selected clip, Begin a keyframe drag operation, Finalize a keyframe drag operation and record history, Callback for copy context menus, Callback for removing gap context menus, Callback for removing all gaps on a layer starting from the detected gap, Callback from javascript that the razor tool was clicked (+12 more)

### Community 10 - "Chat Ui Chat"
Cohesion: 0.06
Nodes (49): addReasoningStep(), adjustTextareaHeight(), allAnswered(), applyZenviWebKitInlineTheme(), cancelRequest(), clearChat(), clearReasoningStep(), closeCommandPalette() (+41 more)

### Community 11 - "Classes Clip Utils"
Cohesion: 0.07
Nodes (59): Fraction, _as_mapping(), _clamp_basic_timing(), _clamp_curve_start(), _clamp_time_points(), clamp_timing_to_media(), _clip_has_single_image(), _clip_id() (+51 more)

### Community 12 - "Windows Views 5"
Cohesion: 0.06
Nodes (15): ClipPainter, QPainter, Clear cached rendered clip pixmaps., Return the horizontal overdraw (extra pixels) to render beyond the view., Remove pending thumbnail entries when the viewport changes., Drop cached clip pixmaps when a thumbnail changes., Return True if the clip reports a static image so all frames are identical., Return frame rounding increment based on frames-per-slot at current zoom. (+7 more)

### Community 13 - "Windows Ai Chat Ui"
Cohesion: 0.09
Nodes (20): AIChatWindow, QDockWidget, Apply restored /chat/history data into in-memory + visible UI., Render a stored (role, html_body) tuple into the widget chat box., Clear and re-render stored messages for the active session (widget mode only)., Rebuild the widget fallback multi-chat tab bar., Run JavaScript in the embedded WebEngine or WebKit chat page., Push theme colors, models, preamble and welcome message to the CEP UI. (+12 more)

### Community 14 - "Timeline Media 2"
Cohesion: 0.04
Nodes (22): TODO: Remove hack when datepicker implements, TODO: switch return back to widget declaration at top of file when this is remov, TODO: do we really want to consider this a stop?, TODO: Right now button does not support classes this is already updated in butto, TODO: what should we do with values that can't be parsed?, TODO: switch return back to widget declaration at top of file when this is remov, TODO: remove support for widgetEventPrefix, TODO: Switch return back to widget declaration at top of file when this is remov (+14 more)

### Community 15 - "Windows Views 6"
Cohesion: 0.08
Nodes (16): GeometryBase, QRectF, Update viewport-dependent values without rebuilding cached geometry., Return dictionary describing the current viewport offsets and sizes., Return a string describing what lies under *pos*., Return QRectF for *item* (Clip or Transition).          When *viewport* is ``Tru, Replace cached rect for *item* if present., Yield (rect, clip, selected) tuples for cached clips. (+8 more)

### Community 16 - "Classes Clip Resolver"
Cohesion: 0.10
Nodes (42): _candidate_from_context(), _check_ambiguity(), ClipCandidate, _enumerate_timeline_clips(), _fmt_position_mmss(), _format_candidates_error(), _metadata_corpus(), _normalize_text() (+34 more)

### Community 17 - "Timeline Media 3"
Cohesion: 0.10
Nodes (32): ab(), B(), bb(), $d(), Eb(), $f(), fa(), Ff() (+24 more)

### Community 18 - "Windows Title Editor"
Cohesion: 0.17
Nodes (9): dict_to_style(), @file  @brief Utility functions for manipulating SVG style attributes  @author F, Explode an SVG node style= attribute string into a dict representation, Turn an exploded style dictionary back into a string, set_if_existing(), style_to_dict(), Get the color value from a reference id (i.e. linearGradient3267), sets the QFont properties to all SVG: TEXT and TSPAN nodes (+1 more)

### Community 19 - "Windows Main Window 3"
Cohesion: 0.07
Nodes (15): Update playhead position, Set the window title based on a variety of factors, Recover the backup file (if any), Clear and load the list of recent menu items, Clear and load the list of restore version menu items, Remove a project from the Recent menu if Zenvi can't find it, Clear all recent projects, Destroy the lock file (+7 more)

### Community 20 - "Windows Models"
Cohesion: 0.06
Nodes (18): Set up model/view classes for MainWindow, EffectsModel, EffectsProxyModel, QObject, QSortFilterProxyModel, Filter for common transitions and text filter, EmojisModel, EmojiStandardItemModel (+10 more)

### Community 21 - "Windows Views 7"
Cohesion: 0.08
Nodes (14): QTableView, PropertiesTableView, QColor, Ensure we update the menu when our source models change, A Properties Table QWidget used on the main window, Start a new undo/redo transaction and cache original values., Finalize current transaction and add actions to history., Wrap PropertiesModel.value_updated to manage transactions. (+6 more)

### Community 22 - "Timeline Media 4"
Cohesion: 0.15
Nodes (35): a(), b(), ba(), c(), d(), E(), f(), Ga() (+27 more)

### Community 23 - "Timeline Media 5"
Cohesion: 0.06
Nodes (11): computeStyleTests(), dataAttr(), finalPropName(), getData(), NOTE: This can be skipped if there are no unmatched elements (i.e., `matchedCoun, TODO: Now that all calls to _data and _removeData have been replaced, TODO: identify versions, TODO: identify versions (+3 more)

### Community 24 - "Windows Views 8"
Cohesion: 0.07
Nodes (18): reset_repeat(), Show a waveform for all selected clips, Callback for split audio context menus, Callback for the layout context menus, Callback for the animate context menus, Add a Point to a Keyframe dict. Always remove existing points,         if any c, Callback for nudging clips/transitions by a specified number of frames., Callback for alignment context menus (+10 more)

### Community 25 - "Windows Views 9"
Cohesion: 0.09
Nodes (8): ClipInteractionMixin, Apply identical horizontal/vertical deltas to every dragged item., Publish the current drag overrides (clips + transitions) to the overview., Persist all moved clips/transitions and refresh geometry., Return a QRectF encompassing all currently-selected clips and transitions., Given a proposed horizontal delta (seconds) for the group drag, adjust it, Apply drag-style snapping to a trim delta so clip edges snap just like moves., Finalize box-select: add items intersecting the selection rectangle.

### Community 26 - "Classes App"
Cohesion: 0.11
Nodes (13): QApplication, get_settings(), OpenShotApp, Get a reference to the app's settings object, Store and later display an error encountered during setup, Create an error message object, populated with details, Display the stored error message, The primary QApplication subclass for OpenShot. (+5 more)

### Community 27 - "Windows Video Widget"
Cohesion: 0.10
Nodes (10): QWidget, Signal to refresh viewport (i.e. a property might have changed that effects the, Callback for resize event timer (to delay the resize event, and prevent lots of, Resize zoom button clicked, Clear all transform-related state to avoid using invalid clip/effect objects, A QWidget used on the video display widget, Present the current frame (QImage slot for QueuedConnection / invokeMethod)., Connect signals to renderer (+2 more)

### Community 28 - "Classes Ai Metadata Utils"
Cohesion: 0.08
Nodes (36): adjust_scene_descriptions_for_subclip(), apply_metadata_to_clip_data(), build_summary_preview(), clip_metadata_is_valid(), collect_scene_descriptions_for_baked_segment(), _filter_scenes_in_window(), _filter_tags_for_window(), filter_tags_string_for_window() (+28 more)

### Community 29 - "Windows Views 10"
Cohesion: 0.07
Nodes (11): QWidget, Clear all timeline selections and keyframe highlights., Enable or disable snapping mode., Enable or disable razor tool mode., Enable or disable timing (retime) mode., Snap a time in seconds to the nearest frame boundary., Placeholder due to webview compatibility, Placeholder due to webview compatibility (+3 more)

### Community 30 - "Windows Views 11"
Cohesion: 0.11
Nodes (8): QPixmap, QWidget, Embed the project-files QListView as the top section and restyle it         to, Search both stock providers for ``query`` and show the two sections., Hide and empty the stock sections (project files stay visible)., Import the downloaded file into Project Files (re-tag if already present)., Project files + stock footage + stock music, sharing one scroll area., StockSearchView

### Community 31 - "Classes Exporters"
Cohesion: 0.08
Nodes (27): _clip_media_path(), _db_to_volume(), export_edl(), _fmt_percent(), _fmt_value(), _interp_name(), _is_drop_frame(), @file  @brief This file is used to generate an EDL (edit decision list) export (+19 more)

### Community 32 - "Windows Main Window 4"
Cohesion: 0.05
Nodes (20): Callback for max sized change (i.e. max size of video widget), This class syncs changes from the timeline to libopenshot, This method is invoked by the UpdateManager each time a change happens (i.e Upda, TimelineSync, get_icon(), Get either the current theme icon or fallback to default theme (for custom icons, Get a list of all dockable widgets, Freeze/unfreeze a dock widget on the main screen. (+12 more)

### Community 33 - "Timeline Media 6"
Cohesion: 0.08
Nodes (18): $a(), ac(), cd(), Da(), Ec(), Ef(), Gd(), Hf() (+10 more)

### Community 34 - "Windows Ai Chat Ui 2"
Cohesion: 0.07
Nodes (25): AIChatWorker, _format_tool_command(), _join_content_blocks(), _markdown_to_html(), _parse_content_blocks(), _plain_to_html(), QObject, Normalize an assistant reply into a plain markdown string.      Some providers (+17 more)

### Community 35 - "Windows Plan Dock Ui"
Cohesion: 0.08
Nodes (20): QWebEngineView, QWebView, Load chat_ui/index.html; inject WebKit companion stylesheet + flag when needed., Build embedded HTML chat UI (Qt WebEngine)., Embedded HTML chat using Qt WebKit (MSYS2 / Windows WebKit builds)., attach_webkit_window_object(), Embedded HTML docks: prefer Qt WebEngine on Linux/macOS; on Windows prefer Qt We, Return 'webengine', 'webkit', or None. (+12 more)

### Community 36 - "Classes Auth Manager"
Cohesion: 0.12
Nodes (15): AuthError, AuthManager, Exception, Zenvi authentication manager.  Primary flow (browser OAuth):   1. start_auth_, Extract the ``exp`` claim from a JWT using stdlib only., Use the stored refresh_token to obtain a new access_token.         Returns True, Check for a valid (non-expired) session, refreshing if needed., Poll the Supabase RPC in a daemon thread.         Calls on_success(session_dict (+7 more)

### Community 37 - "Classes Timeline Clip Context"
Cohesion: 0.10
Nodes (24): _audio_biased_tl_query(), _fmt_mmss(), get_timeline_placements_metadata(), _parse_explicit_source_time_range_sec(), _parse_mmss_or_hhmmss_token(), _parse_occurrence(), Return 1-based occurrence index (0 = best overlap+rank match)., Strip ordinal words so TwelveLabs search uses semantic content only. (+16 more)

### Community 38 - "Classes Thumbnail"
Cohesion: 0.07
Nodes (25): BaseHTTPRequestHandler, HTTPServer, _ensure_thumb_dir(), _generate_thumbnail_ffmpeg(), GenerateThumbnail(), httpThumbnailException, httpThumbnailHandler, httpThumbnailServer (+17 more)

### Community 39 - "Windows Views 12"
Cohesion: 0.24
Nodes (5): QObject, Worker object that resolves thumbnail paths on a background thread., Queue a thumbnail request., Discard any pending thumbnail work., _ThumbnailWorker

### Community 40 - "Classes Twelvelabs Match"
Cohesion: 0.09
Nodes (34): build_project_index_name(), collect_project_twelvelabs_index(), map_search_hit_to_file(), Any, Resolve the project's shared TwelveLabs index and video_id → file map., Must match desktop indexing in files_model / reindex handlers., Return (media_bin_file_id, display_name) for a TL search hit., Scan project files for ready TwelveLabs metadata.      Returns:       { (+26 more)

### Community 41 - "Windows Ai Chat Ui 3"
Cohesion: 0.07
Nodes (11): ChatBridge, Ground the model with a bounded timeline snapshot (main thread)., Start a background thread to summarize the first user prompt and update preamble, Remove live tool blocks (widget mode) at the start of a new request., Shared send pipeline for web and widget chat UIs., Handle send from CEP UI (same logic as send_message but with args)., Run deterministic plan executor via backend., Switch to Plan mode and prefill chat to revise a failed step. (+3 more)

### Community 42 - "Windows Views 13"
Cohesion: 0.08
Nodes (17): QObject, QWidget, Process click events on tutorial. Especially useful when tutorial messages are p, Manage and present a list of tutorial dialogs, Process and show the first non-completed tutorial, Get an object from the main window by object id, Mark the current tip completed, and show the next one, Hide the current tip, and don't show anymore (+9 more)

### Community 44 - "Windows Ai Media Panel"
Cohesion: 0.13
Nodes (8): AIMediaPanel, _format_description_text(), QDockWidget, Refresh the description view (based on current selection)., Simple case-insensitive filter: hide non-matching content., Update the selected-clip description when selection or metadata changes., Build the panel body from Pegasus (or legacy) ai_metadata., Dock widget for AI media descriptions and indexing status.

### Community 46 - "Classes Tool Handlers 2"
Cohesion: 0.16
Nodes (16): get_backend_client(), Get the singleton backend client (refreshed when backend URL changes)., charge_operation_on_success(), check_operation(), download_freesound_music_tool(), download_pexels_video_tool(), get_project_catalog(), import_stock_media() (+8 more)

### Community 47 - "Classes Exporters 2"
Cohesion: 0.12
Nodes (25): _append_link(), _apply_rate_settings(), _apply_timecode_settings(), _displayformat(), export_xml(), _format_ratio(), _format_timebase(), _is_ntsc_rate() (+17 more)

### Community 48 - "Classes Metrics"
Cohesion: 0.06
Nodes (27): _base_event_params(), _build_event(), _d(), Build a GA4 event payload., Track a GUI screen being shown, Track an error has occurred, Track application session start/end., Send anonymous GA4 Measurement Protocol events over HTTP. (+19 more)

### Community 49 - "Classes Track Display"
Cohesion: 0.11
Nodes (27): delete_clips_on_track(), _file_is_analyzed(), get_timeline_state(), list_clips(), list_layers(), Return a structured snapshot of the current timeline: tracks, clips with positio, Delete all clips on a UI track (Track 1..N bottom=1) or storage layer_number., build_track_stack() (+19 more)

### Community 50 - "Timeline Media 7"
Cohesion: 0.31
Nodes (29): $c(), ob(), ue(), a(), b(), C(), d(), e() (+21 more)

### Community 51 - "Windows Login Window"
Cohesion: 0.15
Nodes (5): LoginWindow, QDialog, QFrame, QWidget, Zenvi logo + wordmark for login pages.

### Community 52 - "Windows Preview Thread"
Cohesion: 0.09
Nodes (14): PlayerWorker, QObject, Disconnect preview parent from update manager and stop worker thread, QT Player Worker Object (to preview video on a separate thread), Check if any audio devices initialization errors, default sample rate, and curre, This method starts the video player, Preview a certain frame, Refresh a certain frame (+6 more)

### Community 53 - "Windows Views 16"
Cohesion: 0.10
Nodes (17): _GeometryEntry, @file  @brief Geometry caching helpers for the experimental timeline widget.  @a, ClipGeometryMixin, @file  @brief Clip geometry helpers for the timeline widget.  @author Jonathan, Populate cached clip rectangles., Geometry, @file  @brief Geometry helpers split into focused mixins.  @author Jonathan Thom, Concrete geometry helper combining all mixins. (+9 more)

### Community 54 - "Index Timeline"
Cohesion: 0.07
Nodes (28): Agent Mode, Agent Trace, chat.css, chat.js, Plan Mode, Qt WebChannel Bridge, tailwind.min.css, Zenvi Assistant Chat UI (+20 more)

### Community 55 - "Windows Views 17"
Cohesion: 0.07
Nodes (16): QWidget, Allow the slider to shrink horizontally when space is limited., GUI-thread slot: adopt the latest committed geometry and repaint., Toggle between zooming to the entire timeline and the previous zoom, Capture mouse press event, Capture mouse release event, Set min/max limits on the bounds of the handles (to prevent invalid values), Calculate the width of the scrollbar handle (i.e. selection width) (+8 more)

### Community 56 - "Windows Views 18"
Cohesion: 0.12
Nodes (9): QState, QStateMachine, BoxSelectState, DragState, KeyframeState, PlayheadState, @file  @brief Finite state machine for timeline interactions  @author Jonathan T, ResizeState (+1 more)

### Community 57 - "Windows Models 2"
Cohesion: 0.18
Nodes (8): CreditsFilterProxyModel, CreditsModel, CreditsStandardItemModel, QSortFilterProxyModel, QStandardItemModel, @file  @brief This file contains the credits model, used by the about window  @a, Proxy class used for sorting and filtering credits, Match filter against name, email, or website columns

### Community 58 - "Classes Auto Updater"
Cohesion: 0.12
Nodes (13): AutoUpdater, _platform_asset_suffix(), Checks GitHub Releases for a newer version in a background thread,     download, Launch the background check-and-download thread (daemon)., Signal the background thread to stop as soon as possible., Entry point for the background thread., Single GitHub /releases/latest fetch for Sentry, UI, and download logic., Download the platform asset from an already-fetched release payload. (+5 more)

### Community 59 - "Classes Importers"
Cohesion: 0.12
Nodes (22): _center_pixels_to_normalized(), _clip_merge_key(), _extract_path_from_file_node(), _float_value(), _gravity_offset(), import_xml(), _node_text_content(), @file  @brief This file is used to import a Final Cut Pro XML file  @author Jona (+14 more)

### Community 60 - "Windows Views 19"
Cohesion: 0.14
Nodes (11): project, @file  @brief This file is for legacy support of OpenShot 1.x project files  @au, This is the main project class that contains all     the details of a project, s, Return generic snap targets converted to seconds for keyframe drags., Return (diff, target, reused_active, tolerance_px) for a given cursor position., Compute horizontal snap offsets for dragged clips and transitions., Return adjusted delta in seconds for horizontal snapping., Snap a moving edge (in seconds) to nearby clip edges or playhead. (+3 more)

### Community 61 - "Classes Query"
Cohesion: 0.10
Nodes (11): Return a cached, detached copy of child, clearing cache when project changes., Take any arguments given as filters, and find a list of matching objects, Get the translated display title of this item, Get the translated display title of this item, Get AI metadata for this file, Check if file has been analyzed by AI, Get AI-generated tags (legacy; prefer description / short_summary)., Get AI-generated audiovisual description (Pegasus). (+3 more)

### Community 62 - "Classes Update Installer"
Cohesion: 0.16
Nodes (23): _apply_appimage(), _apply_deb(), _apply_linux(), _apply_macos(), apply_pending_update(), _apply_windows(), _cleanup(), _find_current_appimage() (+15 more)

### Community 63 - "Timeline"
Cohesion: 0.20
Nodes (22): cancelKeyframePreviewRender(), clearKeyframePreviewTransform(), ensureKeyframePreviewContainer(), getMaxClipEndSeconds(), getMaxDurationSeconds(), getMaxResizeWidthPx(), getReaderDurationSeconds(), getRetimedDurationSeconds() (+14 more)

### Community 64 - "Timeline Media 8"
Cohesion: 0.20
Nodes (17): Ba(), db(), Df(), g(), Ia(), Jc(), jg(), Kc() (+9 more)

### Community 65 - "Windows Export"
Cohesion: 0.11
Nodes (8): Export, QDialog, Start exporting video, Calculate a bitrate using bits-per-pixel guidance for All Formats presets., Refresh dynamic video bitrates when using All Formats presets., Restore defaults by closing and reopening the dialog., Update progress bar during exporting, Update the # of channels to match the channel layout

### Community 66 - "Windows Main Window 5"
Cohesion: 0.09
Nodes (12): Remove all dockable widgets on main screen, Add all dockable widgets to the same dock area on the main screen, Float or Un-Float all dockable widgets above main screen, Show all dockable widgets on the main screen, Switch to the default / simple view, Switch to an alternative view, Show all dockable widgets, Apply saved geometry/state after the first show event. (+4 more)

### Community 67 - "Classes Api Client"
Cohesion: 0.09
Nodes (12): Lazy-create a requests.Session., Remove a temp upload on the backend after tag/index complete., Check if the backend is running., List all available LLM models., Get the default model ID., Clear a chat session., Send a chat message via WebSocket with tool delegation support.          Each, HTTP/WebSocket client for the Zenvi backend API. (+4 more)

### Community 68 - "Classes Importers 2"
Cohesion: 0.11
Nodes (20): get_media_type(), is_image(), @file  @brief This file is used to determine if an extension is an image format, Check a File object if the file extension is a known image format, Check a File object and determine the media type (video, image, audio), create_clip(), _db_to_volume(), import_edl() (+12 more)

### Community 69 - "Windows Cutting"
Cohesion: 0.13
Nodes (9): Cutting, QDialog, Return a timecode string for the given frame, Update the playhead position, Start of clip button was clicked, End of clip button was clicked, Clear the current clip and reset the form, Clear all form controls (+1 more)

### Community 70 - "Windows Preferences"
Cohesion: 0.07
Nodes (18): Preferences, QDialog, Update the Restore Defaults button label based on the selected tab., textChanged event handler for search box, Delete all tabs and ensure they are fully removed from memory., Populate all preferences and tabs, Store widget references and register dependency relationships., Apply dependency state to all registered widgets. (+10 more)

### Community 71 - "Classes Credits Client"
Cohesion: 0.17
Nodes (9): _check_operation_rpc(), credit_block_message(), CreditsClient, Any, Zenvi billing client — thin Supabase RPC wrapper.  Pricing and point amounts l, Singleton billing client using AuthManager JWT., Last known balance from a successful fetch (None if never loaded)., Return (authenticated, total_points). Fail closed balance 0 when authed but RPC (+1 more)

### Community 72 - "Timeline 2"
Cohesion: 0.14
Nodes (16): buildWaveformColumns(), drawAudio(), drawWaveform(), findElement(), findTrackAtLocation(), framesPerTick(), moveBoundingBox(), padNumber() (+8 more)

### Community 73 - "Windows Profile Edit"
Cohesion: 0.15
Nodes (7): EditProfileDialog, QDialog, Connect input fields to update profile on change., Update display aspect ratio based on width, height, and pixel ratio., Save the profile to a file when the user accepts the dialog., Close the dialog without saving changes., Initialize the form fields with data from the profile.

### Community 74 - "Classes Ui Util"
Cohesion: 0.05
Nodes (39): QPalette, Formatter that trims overly long log messages., TruncatingFormatter, Search for transitions by name., search_transitions(), center(), connect_auto_events(), get_default_icon() (+31 more)

### Community 75 - "Windows Views 20"
Cohesion: 0.14
Nodes (3): Callback for locking a track, Callback for unlocking a track, TrackInteractionMixin

### Community 77 - "Windows Pexels Dock"
Cohesion: 0.15
Nodes (4): QFrame, QPixmap, A single result card: thumbnail + duration badge + hover overlay., _VideoCard

### Community 78 - "Windows Views 22"
Cohesion: 0.07
Nodes (11): BlenderListView, QListView, Callback when the user chooses a color in the dialog, Generate a new, unique folder name to contain Blender frames, Enable all controls on interface, Init the slider and preview frame label to the currently selected animation, Get new value of preview slider, and start timer to Render frame, Build a dictionary of all animation settings and properties from XML (+3 more)

### Community 79 - "Windows Views 23"
Cohesion: 0.11
Nodes (14): apply_repeat(), _normalize_points(), _normalize_points_to_trim(), QDialog, @file  @brief This file contains repeat time keyframe logic (for Time->Repeat me, Normalize points so X starts at 1 while preserving Y values., Normalize points to the trimmed span (1..trim_span), tolerating clip-relative or, Repeat normalized points applying ramp, delay, and direction. (+6 more)

### Community 80 - "Windows Views 24"
Cohesion: 0.10
Nodes (9): Concrete QWidget timeline implementation., TimelineWidget, Connect playback signals to new experimental qwidget based timeline, Center the timeline on the current playhead position, Enable / Disable snapping mode, Enable / Disable razor mode, Enable / Disable timing mode, Clear all selections in JavaScript (+1 more)

### Community 81 - "Classes Language"
Cohesion: 0.11
Nodes (16): BaseException, find_language_match(), get_all_languages(), init_language(), @file  @brief This file loads the current language based on the computer's local, Match all combinations of locale, language, and country, Get all language names and countries packaged with OpenShot, Find the current locale, and install the correct translators (+8 more)

### Community 82 - "Windows Views 25"
Cohesion: 0.12
Nodes (11): QWebPage, LoggingWebKitPage, @file  @brief WebKit backend for TimelineView  @author Jonathan Thomas <jonathan, Apply additional theme to web-view, Get HTML for Timeline, adjusted for mixin, Keypress callback for timeline, Override console.log message to display messages, QtWebKit Timeline Widget (+3 more)

### Community 83 - "Windows Views 26"
Cohesion: 0.12
Nodes (5): @file  @brief This file is for legacy support of OpenShot 1.x project files  @au, This class contains methods to simply displaying time codes, timeline, EffectInteractionMixin, Handle context menu interaction on an effect badge.

### Community 84 - "Windows Views 27"
Cohesion: 0.18
Nodes (5): TimelineModel, QTreeView, @file  @brief This file contains the add to timeline file treeview  @author Jona, A TreeView QWidget used on the add to timeline window, TimelineTreeView

### Community 85 - "Windows Agent Trace Dialog"
Cohesion: 0.13
Nodes (8): AgentTraceDialog, Any, QDialog, QThread, Agent Trace dialog — inspect tool/LLM/plan telemetry for the active chat session, Browse session telemetry: tool_start/tool_end args + results, plan steps, llm_en, _TraceFetchWorker, Open the Agent Trace dialog for the active session.

### Community 86 - "Windows Views 28"
Cohesion: 0.12
Nodes (6): Callback for resize event timer (to delay the resize event, and prevent lots of, Set the current zoom factor, Consume the current scroll bar positions from the webview timeline, Apply an intermediate zoom value during animation (no emit)., Apply CSS theme to this widget., Clear stale visual overrides/caches after batch timeline mutations (e.g. slice).

### Community 87 - "Windows Ai Chat Ui 4"
Cohesion: 0.16
Nodes (6): Return a stable storage key for the given project file path.          Saved pr, One-time move of the old global store into the ``_default`` bucket., Fetch credits balance once and start a 60-second refresh timer., Fetch balance in a background thread; push result to JS on main thread., Create and start a new AIChatWorker thread pair for the given session_id., Switch the chat dock to the per-project sessions for ``new_project_path``.

### Community 88 - "Windows Views 29"
Cohesion: 0.13
Nodes (10): QWebEnginePage, LoggingWebEnginePage, @file  @brief WebEngine backend for TimelineView  @author Jonathan Thomas <jonat, Run JS code async and optionally have a callback for response, Apply additional theme to web-view, Keypress callback for timeline, Override console.log message to display messages, QtWebEngine Timeline Widget (+2 more)

### Community 89 - "Classes Api Client 2"
Cohesion: 0.10
Nodes (10): Any, Search Freesound for stock music and sound effects., Get conversation history., Fetch agent/tool telemetry events for diagnosing loops and bottlenecks., Search for clips matching a query., Generate a video from a text prompt (Kling O1 Pro via Runware).          Suppo, Generate narration MP3 via backend OpenAI TTS; returns audio_base64 on success., Generate a morph/transition video between two images. (+2 more)

### Community 90 - "Classes Recompute Queue"
Cohesion: 0.11
Nodes (8): CoalescingRecomputeQueue, @file  @brief Coalescing, latest-wins recompute scheduler (pure logic).   Use, Track recompute generations and enforce latest-wins result commits., Reserve and return a new, strictly increasing generation id., Queue work for a generation, coalescing to the newest pending payload., Pop the pending (generation, payload) for processing, or None., A generation is stale once a newer one has been requested., Commit a computed result. Returns True if kept, False if discarded.          O

### Community 91 - "Windows Ai Chat Ui 5"
Cohesion: 0.17
Nodes (7): humanize_tool_name(), Return a short human-readable title for a tool name., Render a Cursor-style collapsible terminal block for a tool call., Append one log line to a running tool block., Mark a tool block as done/error and keep result text for inspection., Collapsible tool-run block for native Qt chat (mirrors chat.js tool blocks)., WidgetToolBlock

### Community 92 - "Timeline Media 9"
Cohesion: 0.15
Nodes (13): Re-index via direct TwelveLabs presigned upload., Thread-safe session for parallel upload + indexing requests., Index via editor-side chunks + Gemini Files direct upload (no backend media stor, Poll /indexing/job/{job_id} until the job finishes or max_wait seconds pass., cleanup_chunk_dir(), extract_chunk(), extract_chunks(), _ffmpeg_run() (+5 more)

### Community 93 - "Windows About"
Cohesion: 0.04
Nodes (32): About, Changelog, Credits, License, parse_changelog(), QDialog, @file  @brief This file loads the About dialog (i.e about Openshot Project)  @, Handle right-click context menu. (+24 more)

### Community 94 - "Windows Freesound Dock"
Cohesion: 0.11
Nodes (9): Slice and keep the left side of a clip/transition, and then ripple the position, Slice and keep the right side of a clip/transition, and then ripple the position, Helper function for slicing clips and transitions at the playhead position., Handler for slicing all clips and keeping both sides at the playhead position., Handler for slicing all clips and keeping the left side at the playhead position, Handler for slicing all clips and keeping the right side at the playhead positio, Handler for slicing selected clips and keeping both sides at the playhead positi, Handler for slicing selected clips and keeping the left side at the playhead pos (+1 more)

### Community 95 - "Windows Main Window 6"
Cohesion: 0.19
Nodes (6): _PasswordWorker, _PollWorker, QObject, Zenvi login window.  Primary flow  — browser OAuth:   Opens ZENVI_WEBSITE/log, Runs AuthManager.poll_for_session() in a QThread and bridges to Qt signals., Calls sign_in or sign_up in a QThread.

### Community 96 - "Windows Pexels Dock 2"
Cohesion: 0.22
Nodes (4): PexelsDock, QDockWidget, Pexels stock-video search dock., Release per-video thread/worker refs after QThread::finished.

### Community 97 - "Windows Views 30"
Cohesion: 0.14
Nodes (7): PlayheadMixin, Return QRectF describing the draggable portion of the playhead., Return True if *pos* intersects the draggable playhead handle., Callback when position is changed, Callback when play button is clicked, Connect playback signals, Return QRectF describing the full rendered playhead icon.

### Community 98 - "Classes Info"
Cohesion: 0.12
Nodes (13): @file  @brief This file contains some Effect metadata related to pre-processing, # TODO: Remove Example example options, get_default_path(), @file  @brief This file contains the current version number of OpenShot, along, Same resolver as classes.api_client.ZenviBackendClient., Create user paths if they do not exist (this is where     temp files are stored, Reset all info.FOO_PATH attributes back to their initial values,     as they ma, Return the default value of the named info.FOO_PATH attribute,     even if it's (+5 more)

### Community 99 - "Classes Settings"
Cohesion: 0.12
Nodes (9): Save user settings file to disk, Restore settings to default, optionally filtering by category, and preserving sp, This class only allows setting pre-existing keys taken from default settings fil, Get the entire list of settings (with all metadata), Store setting, but adding isn't allowed. All possible settings must be in defaul, Load user settings file from disk, merging with allowed settings in default sett, SettingStore, Smoke-tests for classes.settings. (+1 more)

### Community 100 - "Windows Color Picker"
Cohesion: 0.33
Nodes (3): BlockPaintFilter, draw_checkerboard(), Draw a checkerboard pattern for transparent backgrounds.

### Community 101 - "Windows Freesound Dock 2"
Cohesion: 0.13
Nodes (10): QObject, QPixmap, QRunnable, Freesound stock-music/SFX search dock for Zenvi.  Search bar (QLineEdit + magn, Runs freesound_search on a background thread., Fetches a waveform image URL and emits the result via a signal carrier., _SearchWorker, _Signals (+2 more)

### Community 102 - "Windows Main Window 7"
Cohesion: 0.19
Nodes (5): Remove the ripple gap and adjust subsequent items on the same layer, Emit a signal for selection changed. Callback for selection timer., Clear any invalid selections, Callback for show property timer, Remove the current selected clip / transition or file from the project.

### Community 103 - "Windows Models 3"
Cohesion: 0.24
Nodes (5): FilesModel, QObject, Called at app quit — interrupt any pending HTTP requests, then wait briefly., Queue a file for background indexing/summarize with bounded concurrency., Fire-and-forget background indexing/summarize for an already-saved file.

### Community 104 - "Windows Views 31"
Cohesion: 0.13
Nodes (5): FilesTreeView, QTreeView, Override startDrag method to display custom icon, Resize and hide certain columns, A TreeView QWidget used on the main window

### Community 105 - "Windows Color Picker 2"
Cohesion: 0.11
Nodes (9): QColorDialog, ColorPicker, PickingDialog, PreviewFrameFilter, QDialog, Find the built-in preview frame and install a filter on it., Handle window resize events to update preview frame filter., Track when the current color changes (hover or selection). (+1 more)

### Community 106 - "Windows Export Clips"
Cohesion: 0.19
Nodes (5): QPointF, Return (QTransform, unpacked props, originScreenPt) for a clip/effect box., Opacity based on playhead vs. selected clip(s).         Intersects => 1.0, Calculate size of viewport to maintain aspect ratio, QPainter

### Community 107 - "Language Test Translations"
Cohesion: 0.20
Nodes (12): BadTranslationsError, build_stringlists(), check_trans(), Color, process_qm(), Any, Exception, Scan a translation file against all provided strings (+4 more)

### Community 108 - "Windows Views 32"
Cohesion: 0.31
Nodes (14): MenuAlign, MenuAnimate, MenuCopy, MenuFade, MenuLayout, MenuRotate, MenuSlice, MenuSplitAudio (+6 more)

### Community 109 - "Windows Views 33"
Cohesion: 0.21
Nodes (5): Update a keyframe property to a new value, adding or updating keyframes as neede, Update a keyframe property to a new value, adding or updating keyframes as neede, Return clip-relative frame clamped between trimmed start/end., Rotate cursor based on the current transform, Capture mouse events on video preview window

### Community 110 - "Classes Updates"
Cohesion: 0.14
Nodes (8): Load this UpdateAction from a JSON string, Load history from project, Reset the UpdateManager, and clear all UpdateActions and History.         This, Notify all watchers if any 'undo' or 'redo' actions are available., Interface for classes that listen for 'undo' and 'redo' events., Easily be notified each time there are 'undo' or 'redo' actions         availab, Apply the last action to the history, UpdateWatcher

### Community 111 - "Themes Base"
Cohesion: 0.16
Nodes (7): BaseTheme, Set content margins on dock widgets with an optional objectName filter., Iterate through toolbar button settings, and apply them to each button., Toggle the play icon from play to pause and back, Create Dynamic High DPI icons, Return a QColor from a stylesheet class and property., Return an int from a stylesheet class and property.

### Community 112 - "Windows Models 4"
Cohesion: 0.22
Nodes (4): QPushButton, Rect select button clicked, RegionButton, watch_project: watch for changes in project size / widget size, and         con

### Community 113 - "Windows Preferences 2"
Cohesion: 0.23
Nodes (6): QColor, Set temp file path & make copy of template, Load an SVG title and init all textboxes and controls, Choose text color for best contrast against a background, Updates the color shown on the font color button, Updates the color shown on the background color button

### Community 114 - "Windows Process Effect"
Cohesion: 0.14
Nodes (8): ProcessEffect, QDialog, Spinner value change callback, Boolean value change callback, Dropdown value change callback, Textbox value change callback, Start processing effect, Choose Profile Dialog

### Community 115 - "Windows Views 34"
Cohesion: 0.14
Nodes (5): FilesListView, QListView, A ListView QWidget used on the main window, Override startDrag method to display custom icon, Filter files with proxy class

### Community 116 - "Windows Login Window 2"
Cohesion: 0.20
Nodes (6): QGridLayout, QLabel, Return a flat section-header label that replaces QGroupBox titles., _section_header(), Any, Any

### Community 117 - "Classes Zenvi Env"
Cohesion: 0.20
Nodes (11): Open ZENVI_WEBSITE/login?state=<uuid> in the system browser.         Returns (u, _zenvi_website(), _candidate_search_roots(), _first_env_path(), _first_env_path_any(), load_zenvi_dotenv(), _merge_env_file(), Load Zenvi environment files from the install / repo root.  Order: ``.env`` (l (+3 more)

### Community 118 - "Classes Exporters 3"
Cohesion: 0.16
Nodes (14): createCenterEffect(), createEffect(), _export_interp_name(), _find_effect_node(), _find_parameter_node(), _gravity_offset(), Return an interpolation name for export from int/str values., Return base scaled dimensions after applying scale mode (before per-axis scale). (+6 more)

### Community 119 - "Classes Path Utils"
Cohesion: 0.22
Nodes (13): _clip_file_info(), Return (File object, merged metadata dict, absolute file path)., _pathurl_to_path(), Convert a Final Cut pathurl value into a filesystem path., absolute_media_path(), absolute_path_from_export(), _project_file_path(), _project_folder() (+5 more)

### Community 120 - "Classes Json Data"
Cohesion: 0.18
Nodes (7): JsonDataStore, Merge settings files, removing invalid settings based on default settings, Replace matched string for converting paths to relative paths, Replace matched string for converting paths to relative paths, This class which allows getting/setting of key/value settings, and loading and s, Custom deepcopy to handle regex objects on older Python versions., Get copied value of a given key in data store

### Community 121 - "Classes Query 2"
Cohesion: 0.22
Nodes (4): QFrame, The label to display selections, Create and display the selection menu when requested., SelectionLabel

### Community 122 - "Classes Update Queue"
Cohesion: 0.22
Nodes (5): Append an UpdateAction to the pending queue and schedule processing., Presents the same interface as UpdateManager. When from_agent is True,     inse, UpdatesRouter, A data structure representing a single update manager action,     including any, UpdateAction

### Community 123 - "Timeline Media 10"
Cohesion: 0.16
Nodes (14): buildFragment(), buildParams(), cloneCopyEvent(), disableScript(), DOMEval(), domManip(), getAll(), isArrayLike() (+6 more)

### Community 124 - "Windows Views 35"
Cohesion: 0.15
Nodes (7): ProfilesTreeView, QTreeView, Handle row insertion and refresh view., Filter transitions with proxy class, Select a specific profile Key, Return the selected profile object, if any, Handle right-click context menu for profiles

### Community 125 - "Classes Api Client 3"
Cohesion: 0.40
Nodes (4): Any, Upload a local video file to TwelveLabs presigned URLs (no backend video hop)., Upload file chunks; return (parts, error). parts match upload-complete schema., upload_file_via_presigned_urls()

### Community 126 - "Classes Sentry"
Cohesion: 0.26
Nodes (12): configure_platform_tags(), disable_tracing(), init_tracing(), platform_scope(), @file  @brief This file manages the optional Sentry SDK  @author Jonathan Thomas, Disable all Sentry tracing requests, Returns whether the imported sentry-sdk has tag-related     methods such as set_, Init all Sentry tracing (+4 more)

### Community 127 - "Classes Updates 2"
Cohesion: 0.17
Nodes (7): This class is used to track and distribute changes to listeners.     Typically,, Remove a listener from the update manager, Add a new listener (which will invoke the changed(action) method         each t, Add a new watcher (which will invoke the updateStatusChanged() method         e, Invalidate cached query objects by bumping the project data version., Insert a new UpdateAction into the UpdateManager         (this action will then, UpdateManager

### Community 128 - "Windows File Properties"
Cohesion: 0.27
Nodes (4): QStyledItemDelegate, FileCardDelegate, Renders a quick-action overlay (Preview · Add · Remove) when a     thumbnail is, Return {action_key: QRect} for each button, centred at bottom.

### Community 129 - "Windows Freesound Dock 3"
Cohesion: 0.12
Nodes (6): FreesoundDock, QDockWidget, QFrame, A single result card: waveform + name + duration badge + hover overlay., Freesound stock-music/SFX search dock., _SoundCard

### Community 130 - "Windows Views 36"
Cohesion: 0.24
Nodes (6): QObject, TitlesModel, QListView, @file  @brief This file contains the titles treeview, used by the title editor w, A QListView QWidget used on the title editor window, TitlesListView

### Community 131 - "Windows Profile"
Cohesion: 0.19
Nodes (7): Profile, QDialog, Profile filter count changed, Profile tree was double clicked, Signal for closing Profile window, Window closed without choosing a new profile, Choose Profile Dialog

### Community 132 - "Windows Views 37"
Cohesion: 0.25
Nodes (9): effect_color_hex(), effect_color_qcolor(), _effect_type_name(), Any, QColor, @file  @brief Shared helpers for timeline color mappings.  @author Jonathan Thom, Return the preferred hex color string for *effect*., Return a QColor matching the preferred color for *effect*. (+1 more)

### Community 133 - "Windows Export Clips 2"
Cohesion: 0.33
Nodes (5): _parse_bitrate_to_bps(), Convert a bitrate value to integer bits-per-second.     Accepts int, float, or s, Resolve requested audio codec to one that is available on this system.     Preve, Run the encode loop. Uses self.timeline, self.project, self.cache_thread., _resolve_audio_codec()

### Community 134 - "Classes Query 3"
Cohesion: 0.24
Nodes (9): _effective_min_confidence(), infer_tl_search_hint(), Any, Heuristics for when TwelveLabs audio+visual search is preferred., True when clip summary text is too thin for confident clip targeting., Return 'prefer_audio_and_visual' or 'visual_ok'., _tag_corpus_sparse(), get_clips_with_full_metadata() (+1 more)

### Community 135 - "Tests Query Tests"
Cohesion: 0.17
Nodes (5): AuthManagerTests, InfoTests, Basic smoke tests for Zenvi / OpenShot query layer.  Run headlessly:     pyth, Smoke-tests for classes.info., Verify auth manager constants.

### Community 136 - "Timeline 3"
Cohesion: 0.24
Nodes (6): collectKeyframes(), isInside(), isInsideClip(), isInsidePreview(), mapSecondsToDisplay(), toNumber()

### Community 137 - "Timeline 4"
Cohesion: 0.21
Nodes (5): exclusiveMaxSec(), getBounds(), secondsToPixels(), snapClampExclusive(), toNumber()

### Community 138 - "Timeline Media 11"
Cohesion: 0.20
Nodes (12): addCombinator(), condense(), createPositionalPseudo(), elementMatcher(), markFunction(), matcherFromGroupMatchers(), matcherFromTokens(), multipleContexts() (+4 more)

### Community 139 - "Timeline Media 12"
Cohesion: 0.18
Nodes (12): adoptValue(), ajaxConvert(), ajaxHandleResponses(), Animation(), createFxNow(), createTween(), defaultPrefilter(), done() (+4 more)

### Community 140 - "Windows Animated Title"
Cohesion: 0.20
Nodes (6): AnimatedTitle, QDialog, Clear all child widgets used for settings, Animated Title Dialog, Start rendering animation, but don't close window, Actually close window and accept dialog

### Community 141 - "Windows Export 2"
Cohesion: 0.21
Nodes (4): Get the profile path that matches the name, Get the profile name that matches the name, Callback for changing the frame rate, Populate the full list of profiles

### Community 142 - "Windows Video Widget 2"
Cohesion: 0.29
Nodes (6): Take any arguments given as filters, and find the first matching object, add_stock_media_to_project(), get_file_info(), Import a downloaded stock file into Project Files., export_video_headless(), Run export without showing the dialog. Call from main thread.     If video_setti

### Community 143 - "Windows Views 38"
Cohesion: 0.12
Nodes (8): QObject, Disable all controls on interface, Timer is ready to Render frame, Cancel the current render, if any, Render an images sequence of the current template using Blender 2.62+ and the, Background Worker Object (to run the Blender commands), Worker's Render method which invokes the Blender rendering commands, Worker

### Community 144 - "Windows Views 39"
Cohesion: 0.20
Nodes (5): Take any arguments given as filters, and find the first matching object, Take any arguments given as filters, and find the first matching object, Take any arguments given as filters, and find the first matching object, Take any arguments given as filters, and find the first matching object, Take any arguments given as filters, and find the first matching object

### Community 145 - "Windows Views 40"
Cohesion: 0.27
Nodes (11): _calculate_retime_metrics(), _ensure_time_curve(), _finalize_time_points(), _iterate_keyframe_lists(), _project_fps_float(), @file  @brief This file contains re-time keyframe logic (for Time->Fast/Slow men, Mirror points horizontally (X) across their min/max span and swap handles., Retimes a clip and uniformly rescales ALL keyframes' X (including 'time'). (+3 more)

### Community 147 - "Classes Info 2"
Cohesion: 0.22
Nodes (11): apply_application_icon(), _apply_windows_native_window_icon(), ensure_windows_app_user_model_id(), _is_windows(), Group taskbar entry separately from python.exe (call before/after QApplication)., Absolute path LoadImageW accepts (MSYS /c/... and /home/... included)., Win32 WM_SETICON — required for taskbar when host process is python.exe., Set Zenvi icon on QApplication and optionally a top-level widget. (+3 more)

### Community 148 - "Classes Update Queue 2"
Cohesion: 0.20
Nodes (5): Queue for agent-originated UpdateActions so they are applied one at a time on t, Holds pending UpdateActions and dispatches them one at a time to the     wrappe, Args:             updates: The real UpdateManager instance to dispatch to., Set whether the next update calls are from the AI agent (and should be queued)., UpdateQueue

### Community 149 - "Windows Region"
Cohesion: 0.17
Nodes (7): QObject, QSortFilterProxyModel, Proxy class used for sorting and filtering model data, Filter for common transitions and text filter, Sort with both group name and transition name, TransitionFilterProxyModel, TransitionsModel

### Community 150 - "Windows Views 41"
Cohesion: 0.31
Nodes (3): KeyframePanelPainter, QPainter, QRectF

### Community 151 - "Windows Views 42"
Cohesion: 0.18
Nodes (5): Document.Ready event has fired, and is initialized, Return the thumbnail HTTP server address, Recursively collect clip ids from an update payload without walking audio sample, Check if an update payload already contains waveform samples, Determine if a project update requires redrawing clip waveforms.

### Community 152 - "Windows Process Effect 2"
Cohesion: 0.20
Nodes (5): Save the object back to the project data store, Save the object back to the project data store, Save the object back to the project data store, Save the object back to the project data store, Save the object back to the project data store

### Community 153 - "Windows Views 43"
Cohesion: 0.24
Nodes (5): Determine if we should start playback, based on the current frame         and t, Toggle play/pause on video preview, Fast forward the video playback, Rewind the video playback, Handle play-pause-toggle keypress

### Community 154 - "Classes Info 3"
Cohesion: 0.22
Nodes (10): application_icon_ico_path(), application_icon_paths(), application_logo_pixmap(), application_qicon(), Path to the application window/taskbar .ico, or '' if missing., Cached QIcon for windows and QApplication (file-based; not :/openshot.svg)., Scaled Zenvi logo for login / about UI., Project root (parent of src/) in dev; exe directory when frozen. (+2 more)

### Community 155 - "Classes Legacy"
Cohesion: 0.22
Nodes (6): clip, @file  @brief This file is for legacy support of OpenShot 1.x project files  @au, This class represents a media clip on the timeline., keyframe, @file  @brief This file is for legacy support of OpenShot 1.x project files  @au, This class represents a media clip on the timeline.

### Community 156 - "Classes Tool Handlers 3"
Cohesion: 0.08
Nodes (18): File, This class allows Files to be queried, updated, and deleted from the project dat, Take any arguments given as filters, and find a list of matching objects, Get absolute file path of file, Get relative path (based on the current working directory), Get the profile of the file, _get_dispatcher(), _MainThreadDispatcher (+10 more)

### Community 157 - "Emojis README"
Cohesion: 0.22
Nodes (10): Creative Commons CC0, freshluts.com, LUT Authors Collection, OpenShot LUT Inclusion, CC BY-SA 4.0, HfG Schwäbisch Gmünd, LGPL-3.0, OpenMoji (+2 more)

### Community 158 - "Profiles Definitions"
Cohesion: 0.22
Nodes (5): CompactJSONEncoder, A JSON Encoder that puts nested lists on one line (more compact)., Encode JSON object *o* with lists that contain a 'dar' property., Save a profile with compact formatting, save_profile()

### Community 159 - "Themes Humanity"
Cohesion: 0.27
Nodes (4): HumanityDarkTheme, @file  @brief This file contains a theme's colors and UI dimensions  @author Jon, Retro, Apply a new UI theme. Expects a ThemeName ENUM as the arg.

### Community 160 - "Windows Main Window 8"
Cohesion: 0.27
Nodes (3): QDialog, Update the playhead position, SelectRegion

### Community 161 - "Windows Models 5"
Cohesion: 0.27
Nodes (5): ProfilesModel, Removes an existing row if a profile with the same key exists., Update the data in an existing row., Insert a new row into the model., Updates an existing row if a profile with the same key exists,         otherwise

### Community 162 - "Windows Models 6"
Cohesion: 0.18
Nodes (8): QSortFilterProxyModel, QStandardItemModel, @file  @brief This file contains the titles model, used by the title editor wind, Proxy class used for sorting and filtering model data, Sort titles model by a column at runtime, TitleFilterProxyModel, TitleRoles, TitleStandardItemModel

### Community 163 - "Windows Pexels Dock 3"
Cohesion: 0.15
Nodes (11): _DownloadWorker, QObject, QRunnable, Pexels stock-video search dock for Zenvi.  Search bar (QLineEdit + magnifying-, Runs pexels_search on a background thread., Downloads a single Pexels video on a background thread., # NOTE: do NOT touch self._dl_threads/_dl_workers here — the QThread, Fetches a thumbnail URL and emits the result via a signal carrier. (+3 more)

### Community 164 - "Windows Views 44"
Cohesion: 0.22
Nodes (5): EffectsListView, QListView, A TreeView QWidget used on the main window, Override startDrag method to display custom icon, Filter transitions with proxy class

### Community 166 - "Windows Views 46"
Cohesion: 0.25
Nodes (4): GetThumbPath(), Get thumbnail path by invoking HTTP thumbnail request, Update/re-generate the thumbnail of a specific file, Callback when thumbnail needs to be updated

### Community 167 - "Windows Views 47"
Cohesion: 0.22
Nodes (5): Marker, This class allows Markers to be queried, updated, and deleted from the project d, Save the object back to the project data store, Delete the object from the project data store, Take any arguments given as filters, and find a list of matching objects

### Community 168 - "Windows Views 48"
Cohesion: 0.28
Nodes (4): This class allows Tracks to be queried, updated, and deleted from the project da, Take any arguments given as filters, and find a list of matching objects, Track, Renumber all of the project's layers to be equidistant (in         increments o

### Community 169 - "Windows Views 49"
Cohesion: 0.22
Nodes (5): QListView, A QListView QWidget used on the main window, Override startDrag method to display custom icon, Filter transitions with proxy class, TransitionsListView

### Community 170 - "Classes Logger"
Cohesion: 0.12
Nodes (10): object, _install_windows_qfiledialog_workaround(), _qt_message_handler(), Select QWidget timeline backend if enabled and no CLI override exists., Filter out known noisy Qt warnings (e.g. QWebChannel property notify signals)., Avoid native IFileOpenDialog COM on MSYS2/MinGW (HRESULT 0x80040155)., Route stdout and stderr to logger (custom handler), Custom class to log all stdout and stderr streams (from libopenshot / and other (+2 more)

### Community 171 - "Windows Main Window 9"
Cohesion: 0.25
Nodes (4): Handle the transform signal when it's emitted. Supports multiple clip IDs., Handle the key frame transform signal when it's emitted, Handle the 'select region' signal when it's emitted, Update the widget title

### Community 172 - "Classes Settings 2"
Cohesion: 0.28
Nodes (6): actionType, pathType, Enum, Given an action, return the corresponding setting names, Change the path setting corresponding to the given action, Returns the starting path for file browsing         - validates paths before ret

### Community 173 - "Themes Manager"
Cohesion: 0.25
Nodes (6): Enum, @file  @brief This file contains the ThemeManager singleton, used to easily swit, Friendly UI theme names used in settings, Return a sorted list of theme names, Return a theme ENUM which matches a name, ThemeName

### Community 174 - "Windows Ai Chat Ui 6"
Cohesion: 0.22
Nodes (4): Stop one chat worker thread (cancel WS, quit, wait, terminate)., Cleanly stop all session worker threads. Safe to call more than once., Stop all AI worker threads when the dock is explicitly closed., Close a session and delete its Pinecone namespace (called from the × on a tab).

### Community 175 - "Windows Models 7"
Cohesion: 0.25
Nodes (5): ProfilesProxyModel, ProfilesStandardItemModel, QSortFilterProxyModel, QStandardItemModel, Filter for common transitions and text filter

### Community 176 - "Windows Views 50"
Cohesion: 0.31
Nodes (7): compute_minimap_rects(), _item_rect(), @file  @brief Pure geometry helpers for the timeline overview/zoom slider (mini, Return an item field, preferring a live drag override when present., Compute a single (x, y, width, height) rect for a clip/transition., Compute minimap geometry from plain project data.      Args:         clips/tr, _resolve_field()

### Community 177 - "Windows Views 51"
Cohesion: 0.22
Nodes (4): Normalize and validate thumbnail style values., Return the preferred thumbnail rendering style., Update the thumbnail rendering style and refresh the timeline., Cancel pending thumbnail work after a major viewport change.

### Community 178 - "Windows Views 52"
Cohesion: 0.29
Nodes (4): _MinimapGeometryWorker, QThread, Background worker that recomputes minimap geometry off the GUI thread.      Pu, Stop the background recompute worker when the widget closes.

### Community 179 - "Windows Views 53"
Cohesion: 0.29
Nodes (4): EffectsTreeView, QTreeView, A TreeView QWidget used on the main window, Override startDrag method to display custom icon

### Community 180 - "Windows Models 8"
Cohesion: 0.25
Nodes (3): QModelIndex, Inspect a file path and determine if this is an image sequence, Recursively process QUrls from a QDropEvent

### Community 181 - "Windows Views 54"
Cohesion: 0.25
Nodes (5): merge_basic_clip_props(), Patch drag/resize fields without dropping ai_metadata, effects, title, etc., Return pending drag/resize overrides for a clip, if any., Merge pending visual overrides into clip data (position/layer/timing)., Persist visual overrides before edits that read Clip.get() from the store.

### Community 182 - "Classes Auto Updater 2"
Cohesion: 0.25
Nodes (8): cleanup_staged_update(), get_update_manifest(), Read and return the update manifest dict, or None., Remove all staged update files., discard_staged_update(), Read and return the update manifest dict, or None., Remove staged installer + manifest without applying (failed/cancelled)., read_manifest()

### Community 183 - "Classes Legacy 2"
Cohesion: 0.25
Nodes (5): OpenShotFile, OpenShotFolder, @file  @brief This file is for legacy support of OpenShot 1.x project files  @au, The generic file object for OpenShot, The generic folder object for OpenShot

### Community 184 - "Classes Tool Handlers 4"
Cohesion: 0.25
Nodes (8): _download_and_import_one(), _download_remotion_file(), fetch_remotion_video_from_supabase(), Download a Supabase mp4 to a fresh temp path. Returns (dest_path, size_mb)., Download one Supabase mp4 and import it as a project file.      Returns (file_, Best-effort DELETE {REMOTION_URL}/cleanup. Non-critical — failures are logged on, Import a rendered product demo into the project files panel.      Preferred: p, _remotion_cleanup_storage()

### Community 185 - "Classes Updates 3"
Cohesion: 0.25
Nodes (4): Save history to project, Update the UpdateManager with an UpdateAction         (this action will then be, Update the UpdateManager with an UpdateAction, without creating         a new e, Get the JSON string representing this UpdateAction

### Community 186 - "Classes Updates 4"
Cohesion: 0.25
Nodes (4): Convert an UpdateAction into the opposite type (i.e. 'insert' becomes an 'delete, Undo the last UpdateAction (and notify all listeners and watchers)., Redo the last UpdateAction (and notify all listeners and watchers)., Create and return a copy of UpdateAction - with no references to the original

### Community 187 - "Classes Updates 5"
Cohesion: 0.25
Nodes (4): Distribute changes to all listeners (by calling their changed() method), Load all project data via an UpdateAction into the UpdateManager         (this, Delete an item from the UpdateManager with an UpdateAction         (this action, This method is invoked each time the UpdateManager is changed.         The acti

### Community 188 - "Plan Ui Plan"
Cohesion: 0.46
Nodes (7): checkboxForStatus(), connectBridge(), escapeHtml(), renderPlan(), renderStep(), statusClass(), wireBridge()

### Community 189 - "Windows Main Window 10"
Cohesion: 0.25
Nodes (4): Get a list of key sequences from the setting name., Get a key sequence back from the setting name, Initialize / update QShortcuts for the main window actions., Filter out specific QActions/QShortcuts when certain docks have focus.

### Community 190 - "Windows Models 9"
Cohesion: 0.25
Nodes (5): BlenderFilterProxyModel, BlenderModel, QSortFilterProxyModel, Proxy class used for sorting and filtering model data, Sort blender model by a column at runtime

### Community 191 - "Windows Video Widget 3"
Cohesion: 0.25
Nodes (5): Drop any pending requests., Stop the worker thread., Qt helper that forwards thumbnail requests to a worker thread., Queue a thumbnail request., TimelineThumbnailManager

### Community 193 - "Windows Views 56"
Cohesion: 0.33
Nodes (4): Singleton Theme Manager class, used to easily switch between UI themes, Override new method, so the same instance is always returned (i.e. singleton), Return the current theme instance, or None if no theme is applied., ThemeManager

### Community 194 - "Windows Main Window 11"
Cohesion: 0.29
Nodes (3): Current user JWT for backend usage/credits tracking., Sign out of the Zenvi account and prompt re-login., Sign the current user out of their Zenvi account.

### Community 195 - "Classes Index Proxy"
Cohesion: 0.43
Nodes (6): create_index_proxy(), _ffmpeg_run(), _ffprobe_dimensions(), _ffprobe_has_audio(), Create a compressed MP4 proxy for TwelveLabs direct upload (ffmpeg)., Return (upload_path, is_temp, size_bytes, error).

### Community 196 - "Classes Title Bar"
Cohesion: 0.29
Nodes (4): HiddenTitleBar, QWidget, @file  @brief This file contains a custom title bar used by dock widgets  @autho, Update label text when dock title changes.

### Community 197 - "Windows Ai Chat Ui 7"
Cohesion: 0.29
Nodes (4): QFrame, Fetch /chat/history/{session_id} for all open sessions., Build classic Qt widget chat UI., Populate model combo from the backend API.

### Community 198 - "Windows Models 10"
Cohesion: 0.33
Nodes (3): Emit a pending zoom factor change after gesture bursts settle., Persist the current zoom factor and broadcast timeline signals., Persist final zoom and sync slider after animation.

### Community 199 - "Windows Models 11"
Cohesion: 0.33
Nodes (3): FileFilterProxyModel, QSortFilterProxyModel, Proxy class used for sorting and filtering model data

### Community 200 - "Windows Views 57"
Cohesion: 0.25
Nodes (3): Mirror the timeline's live drag overrides on the minimap.          Called whil, Capture an immutable, plain-data snapshot for off-thread recompute.          R, Queue a latest-wins minimap geometry recompute on the worker.

### Community 202 - "Classes Api Client 4"
Cohesion: 0.33
Nodes (3): Download a Pexels MP4 from the CDN URL to the local machine., Download a Freesound preview MP3 from the CDN URL to the local machine., Download a public CDN URL to a local temp file on the desktop.

### Community 203 - "Classes Clip Placement"
Cohesion: 0.33
Nodes (5): compute_clip_trim_bounds(), default_underlay_layer_number(), Pure helpers for clip placement trim + underlay defaults (no Qt deps)., Source-relative start/end for a placed clip trimmed to trim_dur seconds.     Re, Lowest layer_number (bottom underlay). Used when track= is omitted.

### Community 204 - "Classes Conversion"
Cohesion: 0.33
Nodes (5): @file  @brief This file deals with value conversions  @author Jonathan Thomas <j, Convert zoom factor (slider position) into scale-seconds, Convert a number of seconds to a timeline zoom factor, secondsToZoom(), zoomToSeconds()

### Community 205 - "Classes Exceptions"
Cohesion: 0.40
Nodes (5): libopenshot_crash_recovery(), @file  @brief This file deals with unhandled exceptions  @author Jonathan Thom, Read the end of a file (n number of lines), Walk libopenshot.log for the last line before this launch, tail_file()

### Community 206 - "Classes Json Data 2"
Cohesion: 0.33
Nodes (3): Load JSON settings from a file, Convert all paths to absolute using regex, Make a backup copy of an OSP file before performing recovery

### Community 207 - "Classes Qt Types"
Cohesion: 0.33
Nodes (5): bytes_to_str(), @file  @brief This file contains helper functions for Qt types (string to base64, This is required to save Qt byte arrays into a base64 string (to save screen pre, This is required to load base64 Qt byte array strings into a Qt byte array (to l, str_to_bytes()

### Community 208 - "upload_file_to_gemini_resumable"
Cohesion: 0.40
Nodes (4): Any, Upload a local chunk file to a Gemini Files resumable upload URL., PUT/finalize bytes to a Gemini resumable upload URL.      Returns ({name, uri, m, upload_file_to_gemini_resumable()

### Community 209 - "Classes Update Installer 2"
Cohesion: 0.33
Nodes (6): has_pending_update(), is_version_newer(), parse_version(), Parse '3.4.1' or 'v3.4.1' into a comparable tuple., Return True when remote_version is strictly newer than local_version., Return True if a verified update package is staged and ready to install.

### Community 210 - "Timeline 5"
Cohesion: 0.53
Nodes (4): ensureTransitionPreviewContainer(), setTransitionPreviewActive(), startTransitionKeyframePreview(), stopTransitionKeyframePreview()

### Community 212 - "Windows Models 12"
Cohesion: 0.33
Nodes (3): Get the File object for the current files-view item, or the first selection, Table cell change event - when tags are updated on a file, Get the file ID of the current files-view item, or the first selection

### Community 218 - "Classes Legacy 3"
Cohesion: 0.40
Nodes (3): effect, @file  @brief This file is for legacy support of OpenShot 1.x project files  @au, This class represents a media clip on the timeline.

### Community 219 - "Classes Legacy 4"
Cohesion: 0.40
Nodes (3): marker, @file  @brief This file is for legacy support of OpenShot 1.x project files  @au, This class represents a marker (i.e. a reference point) on the timeline.

### Community 220 - "Classes Legacy 5"
Cohesion: 0.40
Nodes (3): @file  @brief This file is for legacy support of OpenShot 1.x project files  @au, A sequence contains tracks and clips that make up a scene (aka sequence).  Curre, sequence

### Community 221 - "Classes Legacy 6"
Cohesion: 0.40
Nodes (3): @file  @brief This file is for legacy support of OpenShot 1.x project files  @au, The track class contains a simple grouping of clips on the same layer (aka track, track

### Community 222 - "Classes Legacy 7"
Cohesion: 0.40
Nodes (3): @file  @brief This file is for legacy support of OpenShot 1.x project files  @au, This class represents a media clip on the timeline., transition

### Community 226 - "Windows Title Editor 2"
Cohesion: 0.13
Nodes (8): QDialog, Display pixmap of SVG on UI thread, writes a new svg file containing the user edited data, Something changed, so update temp SVG and redisplay, Run inside thread, to update and display new SVG - so we don't block the main UI, Update SVG color after user selection, Use an external editor to edit the image, TitleEditor

### Community 227 - "Windows Views 63"
Cohesion: 0.09
Nodes (15): QMenu, @file  @brief This file contains the changelog treeview, used by the about windo, @file  @brief This file contains the credits treeview, used by the about window, @file  @brief This file contains the effects file listview, used by the main win, @file  @brief This file contains the effects file treeview, used by the main win, @file  @brief This file contains the project file listview, used by the main win, @file  @brief This file contains the project file treeview, used by the main win, @file  @brief This file creates a styled QMenu (which supports border radius and (+7 more)

### Community 229 - "Windows Views 65"
Cohesion: 0.04
Nodes (47): QItemDelegate, ClipboardManager, @file  @brief This file is responsible for serializing copy/paste clipboard data, Manages clipboard operations for QueryObjects or lists of QueryObjects, Converts QueryObject or list of QueryObjects to QMimeData.         Handles both, Converts QMimeData back into the original object (QueryObject or list of QueryOb, Clip, Effect (+39 more)

### Community 231 - "Classes Exporters 4"
Cohesion: 0.50
Nodes (4): _file_url(), Return a file:// URL for an absolute path if possible., normalize_path(), Return a path string with POSIX separators (useful for XML).

### Community 234 - "Timeline Media 13"
Cohesion: 0.50
Nodes (4): expectSync(), leverageNative(), returnTrue(), safeActiveElement()

### Community 246 - "Timeline Media 14"
Cohesion: 0.67
Nodes (3): boxModelAdjustment(), curCSS(), getWidthOrHeight()

### Community 247 - "Timeline Media 15"
Cohesion: 0.67
Nodes (3): camelCase(), fcamelCase(), propFilter()

### Community 248 - "Timeline Media 16"
Cohesion: 0.67
Nodes (3): Identity(), resolve(), Thrower()

### Community 249 - "Timeline Media 17"
Cohesion: 0.67
Nodes (3): Datepicker(), datepicker_bindHover(), datepicker_handleMouseover()

## Knowledge Gaps
- **23 isolated node(s):** `App`, `chat.js`, `chat.css`, `tailwind.min.css`, `Agent Mode` (+18 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **39 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `get_app()` connect `Windows Main Window 2` to `Windows Views`, `Classes Tool Handlers`, `Windows Main Window`, `Windows Views 2`, `Windows Classes`, `Windows Views 3`, `Classes Project Data`, `Windows Views 4`, `Classes Clip Utils`, `Windows Ai Chat Ui`, `Windows Views 6`, `Classes Clip Resolver`, `Windows Title Editor`, `Windows Main Window 3`, `Windows Models`, `Windows Views 7`, `Windows Views 8`, `Windows Views 9`, `Classes App`, `Windows Video Widget`, `Windows Views 10`, `Windows Views 11`, `Classes Exporters`, `Windows Main Window 4`, `Classes Thumbnail`, `Classes Twelvelabs Match`, `Windows Views 13`, `Windows Ai Media Panel`, `Classes Tool Handlers 2`, `Classes Exporters 2`, `Classes Metrics`, `Windows Preview Thread`, `Windows Views 17`, `Windows Models 2`, `Classes Auto Updater`, `Classes Importers`, `Windows Views 19`, `Classes Query`, `Windows Export`, `Classes Api Client`, `Classes Importers 2`, `Windows Cutting`, `Windows Preferences`, `Windows Profile Edit`, `Classes Ui Util`, `Windows Views 20`, `Windows Views 21`, `Windows Pexels Dock`, `Windows Views 22`, `Windows Views 23`, `Windows Views 24`, `Windows Views 27`, `Windows Views 28`, `Windows Ai Chat Ui 4`, `Windows About`, `Windows Freesound Dock`, `Windows Views 30`, `Windows Main Window 7`, `Windows Models 3`, `Windows Views 31`, `Windows Color Picker 2`, `Windows Export Clips`, `Windows Views 33`, `Windows Models 4`, `Windows Preferences 2`, `Windows Process Effect`, `Windows Views 34`, `Classes Path Utils`, `Classes Query 2`, `Windows Views 35`, `Windows File Properties`, `Windows Freesound Dock 3`, `Windows Profile`, `Windows Export Clips 2`, `Windows Animated Title`, `Windows Export 2`, `Windows Video Widget 2`, `Windows Views 38`, `Windows Views 40`, `Windows Region`, `Windows Views 42`, `Windows Views 43`, `Windows Main Window 8`, `Windows Models 5`, `Windows Models 6`, `Windows Views 44`, `Windows Views 45`, `Windows Views 46`, `Windows Views 47`, `Windows Views 48`, `Windows Views 49`, `Windows Main Window 9`, `Windows Models 7`, `Windows Views 51`, `Windows Views 52`, `Windows Views 53`, `Windows Models 8`, `Classes Updates 4`, `Windows Main Window 10`, `Windows Models 9`, `Windows Models 10`, `Windows Models 11`, `Windows Views 57`, `Classes Json Data 2`, `Windows Views 59`, `Windows Title Editor 2`, `Windows Views 63`, `Windows Views 65`, `Windows Views 66`, `Windows Views 67`?**
  _High betweenness centrality (0.197) - this node is a cross-community bridge._
- **Why does `MainWindow` connect `Windows Main Window` to `Windows Views`, `Windows Freesound Dock 3`, `Windows Profile`, `Windows Main Window 2`, `Windows Animated Title`, `Windows Ai Chat Ui`, `Classes Frame Extractor`, `Windows Main Window 3`, `Windows Models`, `Windows Region`, `Windows Views 7`, `Windows Views 43`, `Classes App`, `Windows Video Widget`, `Classes Tool Handlers 3`, `Windows Views 11`, `Classes Exporters`, `Windows Main Window 4`, `Windows Plan Dock Ui`, `Classes Auth Manager`, `Windows Views 44`, `Classes Thumbnail`, `Windows Views 47`, `Windows Views 48`, `Windows Views 49`, `Windows Views 13`, `Windows Ai Media Panel`, `Themes Manager`, `Classes Exporters 2`, `Classes Metrics`, `Windows Login Window`, `Windows Views 53`, `Windows Views 17`, `Classes Auto Updater`, `Classes Importers`, `Windows Main Window 10`, `Windows Export`, `Windows Main Window 5`, `Windows Main Window 11`, `Classes Title Bar`, `Windows Cutting`, `Windows Preferences`, `Windows Profile Edit`, `Classes Ui Util`, `Windows Views 20`, `Windows Views 26`, `Windows About`, `Windows Freesound Dock`, `Windows Pexels Dock 2`, `Windows Title Editor 2`, `Windows Views 65`, `Windows Main Window 7`, `Windows Models 3`, `Windows Views 31`, `Windows Views 32`, `Windows Views 34`, `Classes Query 2`?**
  _High betweenness centrality (0.150) - this node is a cross-community bridge._
- **Why does `AIChatWindow` connect `Windows Ai Chat Ui` to `Windows Main Window 4`, `Windows Ai Chat Ui 2`, `Windows Plan Dock Ui`, `Windows Main Window`, `Windows Ai Chat Ui 7`, `Windows Ai Chat Ui 3`, `Windows Ai Chat Ui 6`, `Windows Agent Trace Dialog`, `Windows Ai Chat Ui 4`, `Windows Ai Chat Ui 5`?**
  _High betweenness centrality (0.057) - this node is a cross-community bridge._
- **Are the 405 inferred relationships involving `get_app()` (e.g. with `._get_backend_url()` and `._emit_update_ready_signal()`) actually correct?**
  _`get_app()` has 405 INFERRED edges - model-reasoned connections that need verification._
- **Are the 54 inferred relationships involving `MainWindow` (e.g. with `OpenShotApp` and `.gui()`) actually correct?**
  _`MainWindow` has 54 INFERRED edges - model-reasoned connections that need verification._
- **Are the 6 inferred relationships involving `TimelineWidgetBase` (e.g. with `Clip` and `File`) actually correct?**
  _`TimelineWidgetBase` has 6 INFERRED edges - model-reasoned connections that need verification._
- **Are the 10 inferred relationships involving `TimelineView` (e.g. with `.__init__()` and `ClipboardManager`) actually correct?**
  _`TimelineView` has 10 INFERRED edges - model-reasoned connections that need verification._
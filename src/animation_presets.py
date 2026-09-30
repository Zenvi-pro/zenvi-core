"""
 @file
 @brief Animation keyframe presets used in Motion clip menu
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2026 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

 OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.

 @section THIRD-PARTY NOTICES

 Some animation keyframe data in this file is derived from Animate.css v4.1.1.
 Animate.css is MIT licensed.
 Copyright (c) 2020 Daniel Eden
 https://animate.style/
 """

# Keyframe presets for the Motion menu.
#
# Format:
#     PRESETS[name][property] = [(frame, value), ...]
#
# A third tuple item can be present:
#     (frame, value, easing_name)
#
# Frame positions are 1-31 at 30 fps source speed and should be scaled to the
# actual clip zone at apply time. Only non-identity channels are stored.
#
# Easing values are CSS cubic-bezier(x1, y1, x2, y2) tuples. Convert them to
# libopenshot Point handles as follows:
#     current.handle_right = (x1, y1)
#     next.handle_left     = (x2, y2)

KEYFRAME_EASING = {
    'ease_in': (0.420, 0.000, 1.000, 1.000),
    'ease_in_quint': (0.755, 0.050, 0.855, 0.060),
    'ease_out': (0.000, 0.000, 0.580, 1.000),
    'ease_out_cubic': (0.215, 0.610, 0.355, 1.000),
}

PRESETS = {

    # ── Attention seekers ─────────────────────────────────────────────────────

    'bounce': {
        'scale_y': [
            (1, 1.0, 'ease_out_cubic'), (7, 1.0, 'ease_out_cubic'),
            (13, 1.1, 'ease_in_quint'), (14, 1.1, 'ease_in_quint'),
            (17, 1.0, 'ease_out_cubic'),
            (22, 1.05, 'ease_in_quint'), (25, 0.95), (28, 1.02), (31, 1.0)
        ],
        'location_y': [
            (1, 0, 'ease_out_cubic'), (7, 0, 'ease_out_cubic'), (13, -0.25, 'ease_in_quint'),
            (14, -0.25, 'ease_in_quint'), (17, 0, 'ease_out_cubic'), (22, -0.125, 'ease_in_quint'),
            (25, 0), (28, -0.033333, 'ease_in_quint'), (31, 0)
        ],
    },

    'flash': {
        'alpha': [(1, 1), (8, 0), (16, 1), (24, 0), (31, 1)],
    },

    'pulse': {
        'scale_x': [(1, 1), (16, 1.05), (31, 1)],
        'scale_y': [(1, 1), (16, 1.05), (31, 1)],
    },

    'rubberBand': {
        'scale_x': [(1, 1), (10, 1.25), (13, 0.75), (16, 1.15), (20, 0.95), (24, 1.05), (31, 1)],
        'scale_y': [(1, 1), (10, 0.75), (13, 1.25), (16, 0.85), (20, 1.05), (24, 0.95), (31, 1)],
    },

    'shakeX': {
        'location_x': [
            (1, 0), (4, -0.005208), (7, 0.005208), (10, -0.005208), (13, 0.005208), (16, -0.005208),
            (19, 0.005208), (22, -0.005208), (25, 0.005208), (28, -0.005208), (31, 0)
        ],
    },

    'shakeY': {
        'location_y': [
            (1, 0), (4, -0.009259), (7, 0.009259), (10, -0.009259), (13, 0.009259), (16, -0.009259),
            (19, 0.009259), (22, -0.009259), (25, 0.009259), (28, -0.009259), (31, 0)
        ],
    },

    'swing': {
        'rotation': [(7, 15), (13, -10), (19, 5), (25, -5), (31, 0)],
    },

    'tada': {
        'scale_x': [
            (1, 1), (4, 0.9), (7, 0.9), (10, 1.1), (13, 1.1), (16, 1.1), (19, 1.1), (22, 1.1), (25, 1.1),
            (28, 1.1), (31, 1)
        ],
        'scale_y': [
            (1, 1), (4, 0.9), (7, 0.9), (10, 1.1), (13, 1.1), (16, 1.1), (19, 1.1), (22, 1.1), (25, 1.1),
            (28, 1.1), (31, 1)
        ],
        'rotation': [(4, -3), (7, -3), (10, 3), (13, -3), (16, 3), (19, -3), (22, 3), (25, -3), (28, 3)],
    },

    'wobble': {
        'rotation': [(6, -5), (10, 3), (14, -3), (19, 2), (24, -1)],
        'location_x': [(1, 0), (6, -0.25), (10, 0.2), (14, -0.15), (19, 0.1), (24, -0.05), (31, 0)],
    },

    'jello': {
        'shear_x': [
            (8, -0.277778), (11, 0.138889), (14, -0.069444), (18, 0.034722), (21, -0.017361), (24, 0.008681),
            (28, -0.00434)
        ],
        'shear_y': [
            (8, -0.277778), (11, 0.138889), (14, -0.069444), (18, 0.034722), (21, -0.017361), (24, 0.008681),
            (28, -0.00434)
        ],
    },

    'heartBeat': {
        'scale_x': [(1, 1), (5, 1.3), (9, 1), (14, 1.3), (22, 1)],
        'scale_y': [(1, 1), (5, 1.3), (9, 1), (14, 1.3), (22, 1)],
    },


    # ── Back entrances ────────────────────────────────────────────────────────

    'backInDown': {
        'alpha': [(1, 0.7), (25, 0.7), (31, 1)],
        'scale_x': [(1, 0.7), (25, 0.7), (31, 1)],
        'scale_y': [(1, 0.7), (25, 0.7), (31, 1)],
        'location_y': [(1, -1.5), (25, 0)],
    },

    'backInLeft': {
        'alpha': [(1, 0.7), (25, 0.7), (31, 1)],
        'scale_x': [(1, 0.7), (25, 0.7), (31, 1)],
        'scale_y': [(1, 0.7), (25, 0.7), (31, 1)],
        'location_x': [(1, -1.5), (25, 0)],
    },

    'backInRight': {
        'alpha': [(1, 0.7), (25, 0.7), (31, 1)],
        'scale_x': [(1, 0.7), (25, 0.7), (31, 1)],
        'scale_y': [(1, 0.7), (25, 0.7), (31, 1)],
        'location_x': [(1, 1.5), (25, 0)],
    },

    'backInUp': {
        'alpha': [(1, 0.7), (25, 0.7), (31, 1)],
        'scale_x': [(1, 0.7), (25, 0.7), (31, 1)],
        'scale_y': [(1, 0.7), (25, 0.7), (31, 1)],
        'location_y': [(1, 1.5), (25, 0)],
    },


    # ── Back exits ────────────────────────────────────────────────────────────

    'backOutDown': {
        'alpha': [(1, 1), (7, 0.7), (31, 0.7)],
        'scale_x': [(1, 1), (7, 0.7), (31, 0.7)],
        'scale_y': [(1, 1), (7, 0.7), (31, 0.7)],
        'location_y': [(7, 0), (31, 1.5)],
    },

    'backOutLeft': {
        'alpha': [(1, 1), (7, 0.7), (31, 0.7)],
        'scale_x': [(1, 1), (7, 0.7), (31, 0.7)],
        'scale_y': [(1, 1), (7, 0.7), (31, 0.7)],
        'location_x': [(7, 0), (31, -1.5)],
    },

    'backOutRight': {
        'alpha': [(1, 1), (7, 0.7), (31, 0.7)],
        'scale_x': [(1, 1), (7, 0.7), (31, 0.7)],
        'scale_y': [(1, 1), (7, 0.7), (31, 0.7)],
        'location_x': [(7, 0), (31, 1.5)],
    },

    'backOutUp': {
        'alpha': [(1, 1), (7, 0.7), (31, 0.7)],
        'scale_x': [(1, 1), (7, 0.7), (31, 0.7)],
        'scale_y': [(1, 1), (7, 0.7), (31, 0.7)],
        'location_y': [(7, 0), (31, -1.5)],
    },


    # ── Bouncing entrances ────────────────────────────────────────────────────

    'bounceIn': {
        'alpha': [(1, 0, 'ease_out_cubic'), (19, 1, 'ease_out_cubic'), (31, 1)],
        'scale_x': [
            (1, 0.3, 'ease_out_cubic'), (7, 1.1, 'ease_out_cubic'), (13, 0.9, 'ease_out_cubic'),
            (19, 1.03, 'ease_out_cubic'), (25, 0.97, 'ease_out_cubic'), (31, 1)
        ],
        'scale_y': [
            (1, 0.3, 'ease_out_cubic'), (7, 1.1, 'ease_out_cubic'), (13, 0.9, 'ease_out_cubic'),
            (19, 1.03, 'ease_out_cubic'), (25, 0.97, 'ease_out_cubic'), (31, 1)
        ],
    },

    'bounceInDown': {
        'alpha': [(1, 0, 'ease_out_cubic'), (19, 1)],
        'scale_y': [(1, 3, 'ease_out_cubic'), (19, 0.9, 'ease_out_cubic'), (24, 0.95, 'ease_out_cubic'), (28, 0.985)],
        'location_y': [
            (1, -3, 'ease_out_cubic'), (19, 0.25, 'ease_out_cubic'), (24, -0.10, 'ease_out_cubic'),
            (28, 0.05, 'ease_out_cubic'), (31, 0)
        ],
    },

    'bounceInLeft': {
        'alpha': [(1, 0, 'ease_out_cubic'), (19, 1)],
        'scale_x': [(1, 3, 'ease_out_cubic'), (19, 1, 'ease_out_cubic'), (24, 0.98, 'ease_out_cubic'), (28, 0.995)],
        'location_x': [
            (1, -3, 'ease_out_cubic'), (19, 0.25, 'ease_out_cubic'), (24, -0.10, 'ease_out_cubic'),
            (28, 0.05, 'ease_out_cubic'), (31, 0)
        ],
    },

    'bounceInRight': {
        'alpha': [(1, 0, 'ease_out_cubic'), (19, 1)],
        'scale_x': [(1, 3, 'ease_out_cubic'), (19, 1, 'ease_out_cubic'), (24, 0.98, 'ease_out_cubic'), (28, 0.995)],
        'location_x': [
            (1, 3, 'ease_out_cubic'), (19, -0.25, 'ease_out_cubic'), (24, 0.10, 'ease_out_cubic'),
            (28, -0.05, 'ease_out_cubic'), (31, 0)
        ],
    },

    'bounceInUp': {
        'alpha': [(1, 0, 'ease_out_cubic'), (19, 1)],
        'scale_y': [(1, 5, 'ease_out_cubic'), (19, 0.9, 'ease_out_cubic'), (24, 0.95, 'ease_out_cubic'), (28, 0.985)],
        'location_y': [
            (1, 3, 'ease_out_cubic'), (19, -0.25, 'ease_out_cubic'), (24, 0.10, 'ease_out_cubic'),
            (28, -0.05, 'ease_out_cubic'), (31, 0)
        ],
    },


    # ── Bouncing exits ────────────────────────────────────────────────────────

    'bounceOut': {
        'alpha': [(16, 1), (18, 1), (31, 0)],
        'scale_x': [(7, 0.9), (16, 1.1), (18, 1.1), (31, 0.3)],
        'scale_y': [(7, 0.9), (16, 1.1), (18, 1.1), (31, 0.3)],
    },

    'bounceOutDown': {
        'alpha': [(13, 1), (14, 1), (31, 0)],
        'scale_y': [(7, 0.985), (13, 0.9), (14, 0.9), (31, 3)],
        'location_y': [(7, 0.125), (13, -0.25), (14, -0.25), (31, 3)],
    },

    'bounceOutLeft': {
        'alpha': [(7, 1), (31, 0)],
        'scale_x': [(7, 0.9), (31, 2)],
        'location_x': [(7, 0.25), (31, -3)],
    },

    'bounceOutRight': {
        'alpha': [(7, 1), (31, 0)],
        'scale_x': [(7, 0.9), (31, 2)],
        'location_x': [(7, -0.25), (31, 3)],
    },

    'bounceOutUp': {
        'alpha': [(13, 1), (14, 1), (31, 0)],
        'scale_y': [(7, 0.985), (13, 0.9), (14, 0.9), (31, 3)],
        'location_y': [(7, -0.125), (13, 0.25), (14, 0.25), (31, -3)],
    },


    # ── Fading entrances ──────────────────────────────────────────────────────

    'fadeIn': {
        'alpha': [(1, 0), (31, 1)],
    },

    'fadeInDown': {
        'alpha': [(1, 0), (31, 1)],
        'location_y': [(1, -1), (31, 0)],
    },

    'fadeInDownBig': {
        'alpha': [(1, 0), (31, 1)],
        'location_y': [(1, -3), (31, 0)],
    },

    'fadeInLeft': {
        'alpha': [(1, 0), (31, 1)],
        'location_x': [(1, -1), (31, 0)],
    },

    'fadeInLeftBig': {
        'alpha': [(1, 0), (31, 1)],
        'location_x': [(1, -3), (31, 0)],
    },

    'fadeInRight': {
        'alpha': [(1, 0), (31, 1)],
        'location_x': [(1, 1), (31, 0)],
    },

    'fadeInRightBig': {
        'alpha': [(1, 0), (31, 1)],
        'location_x': [(1, 3), (31, 0)],
    },

    'fadeInUp': {
        'alpha': [(1, 0), (31, 1)],
        'location_y': [(1, 1), (31, 0)],
    },

    'fadeInUpBig': {
        'alpha': [(1, 0), (31, 1)],
        'location_y': [(1, 3), (31, 0)],
    },

    'fadeInTopLeft': {
        'alpha': [(1, 0), (31, 1)],
        'location_x': [(1, -1), (31, 0)],
        'location_y': [(1, -1), (31, 0)],
    },

    'fadeInTopRight': {
        'alpha': [(1, 0), (31, 1)],
        'location_x': [(1, 1), (31, 0)],
        'location_y': [(1, -1), (31, 0)],
    },

    'fadeInBottomLeft': {
        'alpha': [(1, 0), (31, 1)],
        'location_x': [(1, -1), (31, 0)],
        'location_y': [(1, 1), (31, 0)],
    },

    'fadeInBottomRight': {
        'alpha': [(1, 0), (31, 1)],
        'location_x': [(1, 1), (31, 0)],
        'location_y': [(1, 1), (31, 0)],
    },


    # ── Fading exits ──────────────────────────────────────────────────────────

    'fadeOut': {
        'alpha': [(1, 1), (31, 0)],
    },

    'fadeOutDown': {
        'alpha': [(1, 1), (31, 0)],
        'location_y': [(31, 1)],
    },

    'fadeOutDownBig': {
        'alpha': [(1, 1), (31, 0)],
        'location_y': [(31, 3)],
    },

    'fadeOutLeft': {
        'alpha': [(1, 1), (31, 0)],
        'location_x': [(31, -1)],
    },

    'fadeOutLeftBig': {
        'alpha': [(1, 1), (31, 0)],
        'location_x': [(31, -3)],
    },

    'fadeOutRight': {
        'alpha': [(1, 1), (31, 0)],
        'location_x': [(31, 1)],
    },

    'fadeOutRightBig': {
        'alpha': [(1, 1), (31, 0)],
        'location_x': [(31, 3)],
    },

    'fadeOutUp': {
        'alpha': [(1, 1), (31, 0)],
        'location_y': [(31, -1)],
    },

    'fadeOutUpBig': {
        'alpha': [(1, 1), (31, 0)],
        'location_y': [(31, -3)],
    },

    'fadeOutTopLeft': {
        'alpha': [(1, 1), (31, 0)],
        'location_x': [(1, 0), (31, -1)],
        'location_y': [(1, 0), (31, -1)],
    },

    'fadeOutTopRight': {
        'alpha': [(1, 1), (31, 0)],
        'location_x': [(1, 0), (31, 1)],
        'location_y': [(1, 0), (31, -1)],
    },

    'fadeOutBottomRight': {
        'alpha': [(1, 1), (31, 0)],
        'location_x': [(1, 0), (31, 1)],
        'location_y': [(1, 0), (31, 1)],
    },

    'fadeOutBottomLeft': {
        'alpha': [(1, 1), (31, 0)],
        'location_x': [(1, 0), (31, -1)],
        'location_y': [(1, 0), (31, 1)],
    },


}

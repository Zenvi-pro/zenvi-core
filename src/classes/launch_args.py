"""launch.py's command line, kept free of side effects so it can be tested.

launch.py itself cannot be imported by a test: it installs crash hooks, loads
.env and picks a Qt platform at import time.
"""

import argparse


def build_parser(version: str) -> argparse.ArgumentParser:
    """The argument parser for ``launch.py`` (and the packaged executables)."""
    parser = argparse.ArgumentParser(description='OpenShot version ' + version)
    parser.add_argument(
        '-l', '--lang', action='store',
        help='language code for interface (overrides '
             'preferences and system environment)')
    parser.add_argument(
        '--list-languages', dest='list_languages',
        action='store_true',
        help='List all language codes supported by OpenShot')
    parser.add_argument(
        '--path', dest='py_path', action='append',
        help='Additional locations to search for modules '
             '(PYTHONPATH). Can be used multiple times.')
    parser.add_argument(
        '--test-models', dest='modeltest',
        action='store_true',
        help="Load Qt's QAbstractItemModelTester into data models "
        '(requires Qt 5.11+)')
    parser.add_argument(
        '-b', '--web-backend', action='store',
        choices=['auto', 'webkit', 'webengine', 'qwidget'], default='auto',
        help="Web backend to use for Timeline")
    parser.add_argument(
        '-d', '--debug', action='store_true',
        help='Enable debugging output')
    parser.add_argument(
        '--debug-file', action='store_true',
        help='Debugging output (logfile only)')
    parser.add_argument(
        '--debug-console', action='store_true',
        help='Debugging output (console only)')
    parser.add_argument('-V', '--version', action='store_true')
    parser.add_argument(
        '--headless', action='store_true',
        help='Run the editor without a window, driven by its MCP tools '
             '(advertised in ~/.openshot_qt/headless_mcp.json) until '
             'shutdown_headless_tool, SIGTERM or SIGINT')
    parser.add_argument(
        '--project', metavar='PATH',
        help='Project (.zvn, or legacy .osp/.flow) to open')
    parser.add_argument(
        'remain', nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def parse(argv, version: str):
    """``(args, extra_args)`` as ``parse_known_args`` returns them; a command
    line that cannot work exits with status 2 and a usage message."""
    parser = build_parser(version)
    args, extra_args = parser.parse_known_args(argv)
    if "--headless" in args.remain:
        # Everything after the first file name lands in `remain` unparsed.
        parser.error("--headless must come before any file names")
    if args.headless and args.remain:
        parser.error("--headless opens a project with --project PATH, not positional arguments")
    return args, extra_args

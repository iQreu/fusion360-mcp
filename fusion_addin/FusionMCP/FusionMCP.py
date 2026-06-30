"""FusionMCP add-in entry point.

Runs inside Fusion 360. Starts a local TCP server (bridge.py) that receives
commands from the external MCP server process and executes them on Fusion's
main UI thread (the only thread allowed to touch the API).
"""
import os
import sys
import traceback

import adsk.core

# Make the add-in's own folder importable so `import bridge` etc. resolve.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import bridge  # noqa: E402

_app = None


def run(context):
    global _app
    try:
        _app = adsk.core.Application.get()
        bridge.start_server(_app)
        # Non-blocking status hint in the text command palette / console.
        try:
            _app.log('FusionMCP: bridge listening on {}:{}'.format(bridge.HOST, bridge.PORT))
        except Exception:
            pass
    except Exception:
        if _app and _app.userInterface:
            _app.userInterface.messageBox(
                'FusionMCP failed to start:\n{}'.format(traceback.format_exc()))


def stop(context):
    try:
        bridge.stop_server()
    except Exception:
        if _app and _app.userInterface:
            _app.userInterface.messageBox(
                'FusionMCP failed to stop cleanly:\n{}'.format(traceback.format_exc()))

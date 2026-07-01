"""Test fixtures for FusionMCP.

The add-in modules (``commands``, ``bridge``) import ``adsk`` — the Fusion 360
runtime — which is unavailable off-Fusion. We install a minimal fake ``adsk``
package into ``sys.modules`` so the pure-Python logic (registry, batch-reference
resolution, unit conversion, socket framing) can be unit-tested in CI.
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDIN = os.path.join(ROOT, 'fusion_addin', 'FusionMCP')
SERVER = os.path.join(ROOT, 'mcp_server')
for path in (ADDIN, SERVER):
    if path not in sys.path:
        sys.path.insert(0, path)


def _install_fake_adsk():
    """Register a fake ``adsk`` package covering the names touched at import
    time and by the unit-tested helpers."""
    if 'adsk' in sys.modules:
        return

    class _Point:
        def __init__(self, x, y, z=0.0):
            self.x, self.y, self.z = x, y, z

    core = types.ModuleType('adsk.core')

    class Point3D:
        @staticmethod
        def create(x, y, z=0.0):
            return _Point(x, y, z)

    core.Point3D = Point3D

    fusion = types.ModuleType('adsk.fusion')

    class FeatureOperations:
        NewBodyFeatureOperation = 'new'
        JoinFeatureOperation = 'join'
        CutFeatureOperation = 'cut'
        IntersectFeatureOperation = 'intersect'

    fusion.FeatureOperations = FeatureOperations

    adsk = types.ModuleType('adsk')
    adsk.core = core
    adsk.fusion = fusion
    sys.modules['adsk'] = adsk
    sys.modules['adsk.core'] = core
    sys.modules['adsk.fusion'] = fusion


_install_fake_adsk()

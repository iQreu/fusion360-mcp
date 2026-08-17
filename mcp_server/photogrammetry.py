"""Photos -> textured mesh via an installed photogrammetry CLI.

External programs only (no pip dependencies): RealityScan 2.x (Epic; free
under $1M revenue, CLI since 2.0, AMD GPUs since 2.2) is preferred, Meshroom
(`meshroom_batch`; full quality needs an NVIDIA/CUDA GPU) is the fallback.
Neither ships on PATH by default — standard install dirs are probed and
FUSION_MCP_PHOTOGRAMMETRY overrides with a full exe path.

Output is OBJ (both tools' native export); feed it to scan_convert ->
import_mesh, then the normal scan pipeline (scan_align, mesh_to_brep...)
takes over. Reconstruction runs MINUTES to HOURS depending on photo count
and GPU — the tool call blocks for up to `timeout` seconds.
"""
import glob
import os
import shutil
import subprocess

_IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff')


def detect():
    """Photogrammetry backends found on this machine, preference order."""
    found = []
    override = os.environ.get('FUSION_MCP_PHOTOGRAMMETRY')
    if override and os.path.isfile(override):
        name = 'meshroom' if 'meshroom' in os.path.basename(override).lower() \
            else 'realityscan'
        found.append({'backend': name, 'path': override})
    for exe in ('RealityScan.exe', 'RealityCapture.exe'):
        path = shutil.which(exe)
        if not path:
            hits = glob.glob(r'C:\Program Files\Epic Games\RealityScan*\%s'
                             % exe) + \
                glob.glob(r'C:\Program Files\Capturing Reality\RealityCapture*\%s'
                          % exe)
            path = hits[0] if hits else None
        if path and not any(e['path'] == path for e in found):
            found.append({'backend': 'realityscan', 'path': path})
            break
    path = shutil.which('meshroom_batch') or shutil.which('meshroom_batch.exe')
    if not path:
        hits = glob.glob(r'C:\Program Files\Meshroom*\meshroom_batch.exe')
        path = hits[0] if hits else None
    if path and not any(e['path'] == path for e in found):
        found.append({'backend': 'meshroom', 'path': path})
    return found


def _count_images(images_dir):
    return sum(1 for name in os.listdir(images_dir)
               if os.path.splitext(name)[1].lower() in _IMAGE_EXTS)


def build_command(backend, exe, images_dir, out_obj, simplify_faces):
    """The CLI invocation per backend (split out for unit testing)."""
    if backend == 'realityscan':
        # RealityScan/RealityCapture batch grammar: verbs execute in order.
        return [exe, '-headless',
                '-addFolder', images_dir,
                '-align',
                '-setReconstructionRegionAuto',
                '-calculateNormalModel',
                '-simplify', str(int(simplify_faces)),
                '-exportSelectedModel', out_obj,
                '-quit']
    if backend == 'meshroom':
        # meshroom_batch writes texturedMesh.obj into the output directory.
        return [exe, '--input', images_dir,
                '--output', os.path.dirname(out_obj)]
    raise RuntimeError('Unknown backend %r' % backend)


def run(images_dir, out_obj, backend='auto', simplify_faces=1000000,
        timeout=7200):
    """Reconstruct a mesh from a folder of photos. images_dir: 20+ sharp,
    overlapping photos of the object (all sides, diffuse light, matte
    surface — shiny/black parts reconstruct poorly). out_obj: where the OBJ
    lands. backend: auto|realityscan|meshroom."""
    if not os.path.isdir(images_dir):
        raise RuntimeError('images_dir not found: %r' % images_dir)
    count = _count_images(images_dir)
    if count < 10:
        raise RuntimeError(
            'Only %d photos in %r — photogrammetry needs 20+ overlapping '
            'shots covering every side.' % (count, images_dir))
    backends = detect()
    if backend != 'auto':
        backends = [b for b in backends if b['backend'] == backend]
    if not backends:
        raise RuntimeError(
            'No photogrammetry backend found (looked for RealityScan/'
            'RealityCapture and meshroom_batch on PATH and in Program '
            'Files). Install RealityScan (free under $1M revenue) or set '
            'FUSION_MCP_PHOTOGRAMMETRY to the executable.')
    chosen = backends[0]
    os.makedirs(os.path.dirname(os.path.abspath(out_obj)), exist_ok=True)
    cmd = build_command(chosen['backend'], chosen['path'], images_dir,
                        out_obj, simplify_faces)
    run_result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout)
    produced = out_obj if os.path.isfile(out_obj) else None
    if produced is None and chosen['backend'] == 'meshroom':
        hits = glob.glob(os.path.join(os.path.dirname(out_obj), '**',
                                      'texturedMesh.obj'), recursive=True)
        produced = hits[0] if hits else None
    if run_result.returncode != 0 or produced is None:
        tail = ((run_result.stderr or '') + '\n'
                + (run_result.stdout or '')).strip()
        raise RuntimeError(
            '%s failed (exit %s) or produced no OBJ. CLI licensing note: '
            'the free RealityScan seat may require a logged-in Epic session '
            'for headless use. Output tail: %s'
            % (chosen['backend'], run_result.returncode, tail[-600:]))
    return {
        'backend': chosen['backend'],
        'photos': count,
        'output': produced,
        'note': ('Units are ARBITRARY (no scale reference in photos) — '
                 'import with import_mesh, then scan_align against a known '
                 'model or scale from a measured feature. scan_convert can '
                 'turn the OBJ into STL first if needed.'),
    }

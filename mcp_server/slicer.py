"""STL/3MF -> G-code print estimates via an installed slicer CLI.

Runs entirely in the MCP server process. No pip dependencies: slicers are
external programs discovered on this machine (all are AGPL — subprocess-only
by design, never linked). Statistics come from the comment footer the slicers
write into the G-code (their CLIs print nothing useful on stdout).

Supported backends, in preference order:
- PrusaSlicer (`prusa-slicer-console.exe`): plain .gcode, .ini profiles.
- OrcaSlicer / Bambu Studio: shared CLI; profiles are JSON files and the
  output is a `.gcode.3mf` ZIP with the G-code inside Metadata/.

Override discovery with FUSION_MCP_SLICER (full exe path) and default the
profile with FUSION_MCP_SLICER_PROFILE.
"""
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

# g/cm3 — used only when the profile does not put grams into the G-code.
DENSITY = {'pla': 1.24, 'petg': 1.27, 'abs': 1.04, 'asa': 1.07, 'tpu': 1.21,
           'pc': 1.20, 'nylon': 1.15, 'pa': 1.15}

_CANDIDATES = (
    ('prusa', ('prusa-slicer-console.exe', 'prusa-slicer'),
     (r'C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer-console.exe',)),
    ('orca', ('orca-slicer.exe', 'orcaslicer'),
     (r'C:\Program Files\OrcaSlicer\orca-slicer.exe',)),
    ('bambu', ('bambu-studio.exe',),
     (r'C:\Program Files\Bambu Studio\bambu-studio.exe',)),
)


def detect():
    """Slicers found on this machine: [{'slicer', 'path'}], preference order.
    FUSION_MCP_SLICER (a full exe path) is always first when set and valid."""
    found = []
    override = os.environ.get('FUSION_MCP_SLICER')
    if override and os.path.isfile(override):
        name = 'prusa' if 'prusa' in os.path.basename(override).lower() else \
            ('bambu' if 'bambu' in os.path.basename(override).lower() else 'orca')
        found.append({'slicer': name, 'path': override})
    for name, exes, fallbacks in _CANDIDATES:
        path = None
        for exe in exes:
            path = shutil.which(exe)
            if path:
                break
        if not path:
            path = next((f for f in fallbacks if os.path.isfile(f)), None)
        if path and not any(e['path'] == path for e in found):
            found.append({'slicer': name, 'path': path})
    return found


# --------------------------------------------------------------------------- #
# G-code statistics parsing (comment footer)
# --------------------------------------------------------------------------- #
_TIME_PATTERNS = (
    r';\s*estimated printing time.*?=\s*(.+)',        # PrusaSlicer
    r';\s*total estimated time:\s*(.+)',              # Orca / Bambu
    r';\s*model printing time:\s*([^;]+)',            # Bambu (first field)
)
_FLOAT_PATTERNS = {
    'filament_g': (r';\s*filament used \[g\]\s*=\s*([\d.]+)',
                   r';\s*total filament (?:weight|used) \[g\]\s*:\s*([\d.]+)'),
    'filament_cm3': (r';\s*filament used \[cm3\]\s*=\s*([\d.]+),?',
                     r';\s*total filament volume \[cm\^?3\]\s*:\s*([\d.]+)'),
    'filament_m': (r';\s*filament used \[mm\]\s*=\s*([\d.]+)',),
    'cost': (r';\s*total filament cost\s*=\s*([\d.]+)',
             r';\s*total filament cost\s*:\s*([\d.]+)'),
}


def parse_duration(text):
    """'2d 11h 45m 51s' / '1h 2m' / '58m 3s' -> seconds (int), None if empty."""
    total = 0
    for value, unit in re.findall(r'(\d+)\s*([dhms])', text or ''):
        total += int(value) * {'d': 86400, 'h': 3600, 'm': 60, 's': 1}[unit]
    return total or None


def parse_stats(gcode_text):
    """Statistics from a slicer's G-code comment footer. Keys (present when
    found): time_s, filament_g, filament_cm3, filament_m, cost."""
    out = {}
    for pattern in _TIME_PATTERNS:
        m = re.search(pattern, gcode_text)
        if m:
            seconds = parse_duration(m.group(1))
            if seconds:
                out['time_s'] = seconds
                break
    for key, patterns in _FLOAT_PATTERNS.items():
        for pattern in patterns:
            m = re.search(pattern, gcode_text)
            if m:
                value = float(m.group(1))
                out[key] = value / 1000.0 if key == 'filament_m' else value
                break
    return out


def _read_gcode_tail(path, tail_bytes=131072):
    """Both PrusaSlicer and Orca put the stats near the file's head or tail —
    read both ends of big files instead of the whole thing."""
    size = os.path.getsize(path)
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        if size <= 2 * tail_bytes:
            return fh.read()
        head = fh.read(tail_bytes)
        fh.seek(size - tail_bytes)
        return head + '\n' + fh.read()


def _gcode_from_3mf(path):
    """Orca/Bambu `.gcode.3mf` is a ZIP; the G-code lives under Metadata/."""
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith('.gcode')]
        if not names:
            raise RuntimeError('No G-code inside %r — did the slice succeed?'
                               % path)
        return zf.read(names[0]).decode('utf-8', errors='replace')


# --------------------------------------------------------------------------- #
# estimate
# --------------------------------------------------------------------------- #
def estimate(model_path, profile=None, slicer='auto', gcode_out=None,
             material='pla', price_per_kg=None, printer_watts=None,
             energy_price_kwh=None, timeout=600):
    """Slice an STL/3MF and report print time, filament use and cost.

    profile: PrusaSlicer .ini (GUI: File > Export > Export Config) or, for
    Orca/Bambu, 'machine.json;process.json[;filament.json]'. Defaults to
    FUSION_MCP_SLICER_PROFILE. PrusaSlicer slices with built-in defaults when
    no profile is given (generic estimate); Orca/Bambu require one.
    gcode_out keeps the sliced G-code at that path, otherwise it is deleted.
    Cost: grams from the slicer (or cm3 x density[material]) x price_per_kg,
    plus printer_watts x time x energy_price_kwh when given."""
    if not os.path.isfile(model_path):
        raise RuntimeError('Model file not found: %r' % model_path)
    profile = profile or os.environ.get('FUSION_MCP_SLICER_PROFILE') or None
    slicers = detect()
    if slicer != 'auto':
        slicers = [s for s in slicers if s['slicer'] == slicer]
    if not slicers:
        raise RuntimeError(
            'No slicer found (looked for PrusaSlicer, OrcaSlicer, Bambu '
            'Studio on PATH and in Program Files). Install one or set '
            'FUSION_MCP_SLICER to the executable path.')
    chosen = slicers[0]

    tmpdir = tempfile.mkdtemp(prefix='fusionmcp-slice-')
    try:
        if chosen['slicer'] == 'prusa':
            out_path = os.path.join(tmpdir, 'out.gcode')
            cmd = [chosen['path'], '--export-gcode', '-o', out_path]
            if profile:
                cmd += ['--load', profile]
            cmd.append(model_path)
        else:
            if not profile:
                raise RuntimeError(
                    "%s needs profile='machine.json;process.json' (export "
                    'them from the slicer GUI) — it cannot slice with '
                    'defaults.' % chosen['slicer'])
            out_path = os.path.join(tmpdir, 'out.gcode.3mf')
            parts = [part for part in profile.split(';') if part]
            settings = [part for part in parts if 'filament' not in
                        os.path.basename(part).lower()]
            filaments = [part for part in parts if part not in settings]
            cmd = [chosen['path'], '--load-settings', ';'.join(settings)]
            if filaments:
                cmd += ['--load-filaments', ';'.join(filaments)]
            cmd += ['--slice', '0', '--export-3mf', out_path, model_path]

        run = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout)
        if run.returncode != 0 or not os.path.isfile(out_path):
            tail = ((run.stderr or '') + '\n' + (run.stdout or '')).strip()
            raise RuntimeError('%s failed (exit %s): %s'
                               % (chosen['slicer'], run.returncode, tail[-800:]))

        if out_path.endswith('.3mf'):
            text = _gcode_from_3mf(out_path)
        else:
            text = _read_gcode_tail(out_path)
        stats = parse_stats(text)

        report = {
            'model': model_path,
            'slicer': chosen['slicer'],
            'slicer_path': chosen['path'],
            'profile': profile,
        }
        if 'time_s' in stats:
            seconds = stats['time_s']
            hours, minutes = divmod(seconds // 60, 60)
            report['print_time'] = '%dh %02dm' % (hours, minutes)
            report['print_time_s'] = seconds
        grams = stats.get('filament_g')
        if grams is None and stats.get('filament_cm3') is not None:
            density = DENSITY.get((material or 'pla').lower())
            if density:
                grams = round(stats['filament_cm3'] * density, 1)
                report['grams_estimated_from_volume'] = True
        if grams is not None:
            report['filament_g'] = round(grams, 2)
        for key in ('filament_cm3', 'filament_m'):
            if stats.get(key) is not None:
                report[key] = round(stats[key], 2)
        cost = stats.get('cost') or 0.0
        if not cost and grams is not None and price_per_kg:
            cost = grams / 1000.0 * float(price_per_kg)
        if printer_watts and energy_price_kwh and 'time_s' in stats:
            energy = (float(printer_watts) / 1000.0) * (stats['time_s'] / 3600.0)
            report['energy_kwh'] = round(energy, 2)
            cost = (cost or 0.0) + energy * float(energy_price_kwh)
        if cost:
            report['cost'] = round(cost, 2)
        if not stats:
            report['warning'] = ('Sliced, but no statistics found in the '
                                 'G-code comments — unusual profile?')
        if gcode_out:
            shutil.copy2(out_path, gcode_out)
            report['gcode'] = gcode_out
        if not profile and chosen['slicer'] == 'prusa':
            report['note'] = ('Sliced with PrusaSlicer built-in defaults — '
                              'pass profile=<config.ini> exported from your '
                              'slicer for a realistic estimate.')
        return report
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

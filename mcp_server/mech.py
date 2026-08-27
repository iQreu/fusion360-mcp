"""Mechanical-design data for spare parts: ISO 286 fits, bearing envelopes,
DIN 471/472 circlip grooves, O-ring gland design, synchronous-belt geometry.

Everything here is vendored public dimensional data (standard dimensions are
uncopyrightable facts) or textbook formulas — no scraping, no GPL data files.
Circlip tables were transcribed from a manufacturer's DIN 471/472 spec sheets
(Westfield Fasteners) in 2026-08; bearing envelopes are the universal 68/69/
160/60/62/63 deep-groove series. ISO 286 numbers come from the `isofits`
package (MIT, pure Python) and were spot-checked against published tables
(H7@25 = +21/0, k6@40 = +18/+2, p6@40 = +42/+26).

The theme: turn a MEASURED dimension from a scan or photo into an
INTENTIONAL one — a catalog part, a standard fit, a proper groove.
"""

try:
    import isofits
except Exception:  # noqa: BLE001 - optional at import, required for fits
    isofits = None

# --------------------------------------------------------------------------- #
# ISO 286 fits
# --------------------------------------------------------------------------- #
# Preferred nominal sizes (R'10/R'20 blend used by shafting/bearing catalogs).
_NOMINALS = (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
             15, 16, 17, 18, 19, 20, 21, 22, 24, 25, 26, 28, 30, 32, 34, 35,
             36, 38, 40, 42, 45, 48, 50, 52, 55, 60, 63, 65, 70, 75, 80, 85,
             90, 95, 100, 110, 120, 125, 130, 140, 150, 160, 170, 180, 190,
             200, 220, 240, 250, 260, 280, 300)

# application -> (hole fit, shaft fit, one-line description)
APPLICATION_FITS = {
    'loose_running': ('H9', 'd9', 'big guaranteed clearance — dirty or '
                                  'thermally-expanding assemblies'),
    'running': ('H8', 'f7', 'easy running fit — plain bearings, sliding '
                            'guides at moderate accuracy'),
    'sliding': ('H7', 'g6', 'precise sliding without shake — spool valves, '
                            'guide pins that must move'),
    'close_sliding': ('H7', 'h6', 'locational slip fit — parts that '
                                  'assemble by hand and locate exactly'),
    'location': ('H7', 'k6', 'light transition — accurate location, '
                             'assembles with a soft mallet'),
    'transition': ('H7', 'n6', 'tight transition — location under '
                               'vibration, press assembly'),
    'press': ('H7', 'p6', 'interference — permanent assembly, arbor press'),
    'heavy_press': ('H7', 's6', 'heavy interference — transmits torque '
                                'without keys, needs heat/hydraulics'),
}


def fit_suggest(measured_mm, feature='shaft', application='sliding',
                fit=None):
    """Turn a measured diameter into a toleranced spec: nearest standard
    nominal, ISO 286 limits for both members and the resulting
    clearance/interference range. feature: which member was measured
    ('shaft'|'hole'). application: one of APPLICATION_FITS, or pass an
    explicit fit like 'H7/g6'."""
    if isofits is None:
        raise RuntimeError("ISO 286 data needs the 'isofits' package: "
                           'pip install isofits')
    measured = float(measured_mm)
    if not 0.5 <= measured <= 300:
        raise RuntimeError('measured_mm %s out of the supported 0.5-300 mm '
                           'range' % measured_mm)
    if feature not in ('shaft', 'hole'):
        raise RuntimeError("feature must be 'shaft' or 'hole'")
    if fit:
        try:
            hole_fit, shaft_fit = str(fit).split('/')
        except ValueError:
            raise RuntimeError("fit must look like 'H7/g6'")
        description = 'explicit fit'
    else:
        if application not in APPLICATION_FITS:
            raise RuntimeError('application must be one of %s (or pass '
                               "fit='H7/g6')"
                               % ', '.join(sorted(APPLICATION_FITS)))
        hole_fit, shaft_fit, description = APPLICATION_FITS[application]

    nominal = min(_NOMINALS, key=lambda n: abs(n - measured))
    out = {
        'measured_mm': measured,
        'feature_measured': feature,
        'nominal_mm': nominal,
        'off_nominal_mm': round(measured - nominal, 3),
        'fit': '%s/%s' % (hole_fit, shaft_fit),
        'description': description,
    }
    try:
        hole_hi, hole_lo = isofits.isotol('hole', nominal, hole_fit, 'both')
        shaft_hi, shaft_lo = isofits.isotol('shaft', nominal, shaft_fit,
                                            'both')
        fit_min, fit_max = isofits.isofit(nominal, hole_fit, shaft_fit)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError('isofits has no data for %s %s/%s: %s'
                           % (nominal, hole_fit, shaft_fit, exc))
    out['hole'] = {'fit': hole_fit,
                   'limits_mm': [round(nominal + hole_lo / 1000.0, 4),
                                 round(nominal + hole_hi / 1000.0, 4)],
                   'deviation_um': [hole_hi, hole_lo]}
    out['shaft'] = {'fit': shaft_fit,
                    'limits_mm': [round(nominal + shaft_lo / 1000.0, 4),
                                  round(nominal + shaft_hi / 1000.0, 4)],
                    'deviation_um': [shaft_hi, shaft_lo]}
    kind = ('clearance' if fit_min >= 0
            else ('interference' if fit_max <= 0 else 'transition'))
    out['result'] = {'kind': kind, 'range_um': [fit_min, fit_max]}
    if abs(out['off_nominal_mm']) > 0.25:
        out['note'] = ('Measurement is %.2f mm off the nearest standard '
                       'nominal — a worn or non-metric part? Also consider '
                       '%s mm.' % (out['off_nominal_mm'],
                                   _second_nominal(measured, nominal)))
    return out


def _second_nominal(measured, first):
    return min((n for n in _NOMINALS if n != first),
               key=lambda n: abs(n - measured))


# --------------------------------------------------------------------------- #
# Deep-groove ball bearings (open/ZZ/2RS share the envelope): bore, OD, width
# --------------------------------------------------------------------------- #
BEARINGS = {
    # miniature 60x / 62x / 63x
    '603': (3, 9, 5), '604': (4, 12, 4), '605': (5, 14, 5),
    '606': (6, 17, 6), '607': (7, 19, 6), '608': (8, 22, 7),
    '609': (9, 24, 7),
    '623': (3, 10, 4), '624': (4, 13, 5), '625': (5, 16, 5),
    '626': (6, 19, 6), '627': (7, 22, 7), '628': (8, 24, 8),
    '629': (9, 26, 8),
    '633': (3, 13, 5), '634': (4, 16, 5), '635': (5, 19, 6),
    # thin section 68x / 69x
    '683': (3, 7, 3), '684': (4, 9, 4), '685': (5, 11, 5),
    '686': (6, 13, 5), '687': (7, 14, 5), '688': (8, 16, 5),
    '689': (9, 17, 5),
    '693': (3, 8, 4), '694': (4, 11, 4), '695': (5, 13, 4),
    '696': (6, 15, 5), '697': (7, 17, 5), '698': (8, 19, 6),
    '699': (9, 20, 6),
    '6800': (10, 19, 5), '6801': (12, 21, 5), '6802': (15, 24, 5),
    '6803': (17, 26, 5), '6804': (20, 32, 7), '6805': (25, 37, 7),
    '6806': (30, 42, 7),
    '6900': (10, 22, 6), '6901': (12, 24, 6), '6902': (15, 28, 7),
    '6903': (17, 30, 7), '6904': (20, 37, 9), '6905': (25, 42, 9),
    '6906': (30, 47, 9),
    # 60 / 62 / 63 series
    '6000': (10, 26, 8), '6001': (12, 28, 8), '6002': (15, 32, 9),
    '6003': (17, 35, 10), '6004': (20, 42, 12), '6005': (25, 47, 12),
    '6006': (30, 55, 13), '6007': (35, 62, 14), '6008': (40, 68, 15),
    '6009': (45, 75, 16), '6010': (50, 80, 16),
    '6200': (10, 30, 9), '6201': (12, 32, 10), '6202': (15, 35, 11),
    '6203': (17, 40, 12), '6204': (20, 47, 14), '6205': (25, 52, 15),
    '6206': (30, 62, 16), '6207': (35, 72, 17), '6208': (40, 80, 18),
    '6209': (45, 85, 19), '6210': (50, 90, 20),
    '6300': (10, 35, 11), '6301': (12, 37, 12), '6302': (15, 42, 13),
    '6303': (17, 47, 14), '6304': (20, 52, 15), '6305': (25, 62, 17),
    '6306': (30, 72, 19), '6307': (35, 80, 21), '6308': (40, 90, 23),
    '6309': (45, 100, 25), '6310': (50, 110, 27),
}


def bearing_lookup(designation=None, bore=None, od=None, tolerance=0.5):
    """Deep-groove ball bearing envelopes. Look up by designation ('608',
    '6204' — suffixes like ZZ/2RS/RS are stripped) or search by measured
    bore and/or OD (± tolerance mm) — 'the seat I scanned is Ø21.9, what
    bearing was in it?'. Returns bore/OD/width plus seat recommendations."""
    if designation:
        key = str(designation).upper()
        for suffix in ('-2RS', '2RS1', '2RS', '-RS', 'RS', '-ZZ', 'ZZ',
                       '-Z', 'Z', 'DDU', 'LLU', 'VV', '-2Z'):
            if key.endswith(suffix) and key[:-len(suffix)] in BEARINGS:
                key = key[:-len(suffix)]
                break
        if key not in BEARINGS:
            raise RuntimeError('Unknown designation %r — vendored series: '
                               '60x/62x/63x/68x/69x miniature, '
                               '6800-6806/6900-6906 thin, 6000-6010/'
                               '6200-6210/6300-6310' % designation)
        matches = [key]
    else:
        if bore is None and od is None:
            raise RuntimeError('Pass designation, or bore and/or od (mm)')
        tol = float(tolerance)
        matches = [name for name, (b, o, _w) in BEARINGS.items()
                   if (bore is None or abs(b - float(bore)) <= tol)
                   and (od is None or abs(o - float(od)) <= tol)]
        if not matches:
            raise RuntimeError('No vendored bearing matches bore=%s od=%s '
                               '(±%s mm) — try a larger tolerance'
                               % (bore, od, tolerance))
    out = []
    for name in sorted(matches, key=lambda n: (BEARINGS[n][0],
                                               BEARINGS[n][1])):
        b, o, w = BEARINGS[name]
        out.append({'designation': name, 'bore_mm': b, 'od_mm': o,
                    'width_mm': w})
    return {
        'bearings': out,
        'seat_advice': {
            'shaft': 'k5 (rotating shaft load, normal) / j6 (light, easy '
                     'mount); model the seat at the k6 mid-limit for '
                     'machined parts',
            'housing': 'H7 (stationary outer ring); N7 when the outer ring '
                       'rotates',
            'printed': 'FDM: bearing pockets come out undersize — model '
                       'OD + 0.1-0.2 mm and test-fit, or ream; never rely '
                       'on ISO fits straight off the printer',
        },
    }


# --------------------------------------------------------------------------- #
# Circlips DIN 471 (shafts) / DIN 472 (bores)
# d1 -> (ring thickness s, groove dia d2, groove width m, groove depth t)
# Transcribed from Westfield Fasteners DIN spec sheets (2026-08).
# --------------------------------------------------------------------------- #
DIN_471 = {
    3: (0.4, 2.8, 0.5, 0.10), 4: (0.4, 3.8, 0.5, 0.10),
    5: (0.6, 4.8, 0.7, 0.10), 6: (0.7, 5.7, 0.8, 0.15),
    7: (0.8, 6.7, 0.9, 0.15), 8: (0.8, 7.6, 0.9, 0.20),
    9: (1.0, 8.6, 1.1, 0.20), 10: (1.0, 9.6, 1.1, 0.20),
    11: (1.0, 10.5, 1.1, 0.25), 12: (1.0, 11.5, 1.1, 0.25),
    13: (1.0, 12.4, 1.1, 0.30), 14: (1.0, 13.4, 1.1, 0.30),
    15: (1.0, 14.3, 1.1, 0.35), 16: (1.0, 15.2, 1.1, 0.40),
    17: (1.0, 16.2, 1.1, 0.40), 18: (1.2, 17.0, 1.3, 0.50),
    19: (1.2, 18.0, 1.3, 0.50), 20: (1.2, 19.0, 1.3, 0.50),
    21: (1.2, 20.0, 1.3, 0.50), 22: (1.2, 21.0, 1.3, 0.50),
    24: (1.2, 22.9, 1.3, 0.55), 25: (1.2, 23.9, 1.3, 0.55),
    26: (1.2, 24.9, 1.3, 0.55), 28: (1.5, 26.6, 1.6, 0.70),
    29: (1.5, 27.6, 1.6, 0.70), 30: (1.5, 28.6, 1.6, 0.70),
    32: (1.5, 30.3, 1.6, 0.85), 34: (1.5, 32.3, 1.6, 0.85),
    35: (1.5, 33.0, 1.6, 1.00), 36: (1.75, 34.0, 1.85, 1.00),
    38: (1.75, 36.0, 1.85, 1.00), 40: (1.75, 37.5, 1.85, 1.25),
    42: (1.75, 39.5, 1.85, 1.25), 45: (1.75, 42.5, 1.85, 1.25),
    48: (1.75, 45.5, 1.85, 1.25), 50: (2.0, 47.0, 2.15, 1.50),
    52: (2.0, 49.0, 2.15, 1.50), 55: (2.0, 52.0, 2.15, 1.50),
    56: (2.0, 53.0, 2.15, 1.50), 58: (2.0, 55.0, 2.15, 1.50),
    60: (2.0, 57.0, 2.15, 1.50), 62: (2.0, 59.0, 2.15, 1.50),
    63: (2.0, 60.0, 2.15, 1.50), 65: (2.5, 62.0, 2.65, 1.50),
    68: (2.5, 65.0, 2.65, 1.50), 70: (2.5, 67.0, 2.65, 1.50),
    72: (2.5, 69.0, 2.65, 1.50), 75: (2.5, 72.0, 2.65, 1.50),
    78: (2.5, 75.0, 2.65, 1.50), 80: (2.5, 76.5, 2.65, 1.75),
    82: (2.5, 78.5, 2.65, 1.75), 85: (3.0, 81.5, 3.15, 1.75),
    88: (3.0, 84.5, 3.15, 1.75), 90: (3.0, 86.5, 3.15, 1.75),
    95: (3.0, 91.5, 3.15, 1.75), 100: (3.0, 96.5, 3.15, 1.75),
}

DIN_472 = {
    8: (0.8, 8.4, 0.9, 0.20), 9: (0.8, 9.4, 0.9, 0.20),
    10: (1.0, 10.4, 1.1, 0.20), 11: (1.0, 11.4, 1.1, 0.20),
    12: (1.0, 12.5, 1.1, 0.25), 13: (1.0, 13.6, 1.1, 0.30),
    14: (1.0, 14.6, 1.1, 0.30), 15: (1.0, 15.7, 1.1, 0.35),
    16: (1.0, 16.8, 1.1, 0.40), 17: (1.0, 17.8, 1.1, 0.40),
    18: (1.0, 19.0, 1.1, 0.50), 19: (1.0, 20.0, 1.1, 0.50),
    20: (1.0, 21.0, 1.1, 0.50), 21: (1.0, 22.0, 1.1, 0.50),
    22: (1.0, 23.0, 1.1, 0.50), 24: (1.2, 25.2, 1.3, 0.60),
    25: (1.2, 26.2, 1.3, 0.60), 26: (1.2, 27.2, 1.3, 0.60),
    28: (1.2, 29.4, 1.3, 0.70), 30: (1.2, 31.4, 1.3, 0.70),
    31: (1.5, 32.7, 1.6, 0.85), 32: (1.5, 33.7, 1.6, 0.85),
    34: (1.5, 35.7, 1.6, 0.85), 35: (1.5, 37.0, 1.6, 1.00),
    36: (1.5, 38.0, 1.6, 1.00), 37: (1.5, 39.0, 1.6, 1.00),
    38: (1.5, 40.0, 1.6, 1.00), 40: (1.75, 42.5, 1.85, 1.25),
    42: (1.75, 44.5, 1.85, 1.25), 45: (1.75, 47.5, 1.85, 1.25),
    47: (1.75, 49.5, 1.85, 1.25), 48: (1.75, 50.5, 1.85, 1.25),
    50: (2.0, 53.0, 2.15, 1.50), 52: (2.0, 55.0, 2.15, 1.50),
    55: (2.0, 58.0, 2.15, 1.50), 56: (2.0, 59.0, 2.15, 1.50),
    58: (2.0, 61.0, 2.15, 1.50), 60: (2.0, 63.0, 2.15, 1.50),
    62: (2.0, 65.0, 2.15, 1.50), 63: (2.0, 66.0, 2.15, 1.50),
    65: (2.5, 68.0, 2.65, 1.50), 68: (2.5, 71.0, 2.65, 1.50),
    70: (2.5, 73.0, 2.65, 1.50), 72: (2.5, 75.0, 2.65, 1.50),
    75: (2.5, 78.0, 2.65, 1.50), 78: (2.5, 81.0, 2.65, 1.50),
    80: (2.5, 83.5, 2.65, 1.75), 82: (2.5, 85.5, 2.65, 1.75),
    85: (3.0, 88.5, 3.15, 1.75), 88: (3.0, 91.5, 3.15, 1.75),
    90: (3.0, 93.5, 3.15, 1.75), 92: (3.0, 95.5, 3.15, 1.75),
    95: (3.0, 98.5, 3.15, 1.75), 100: (3.0, 103.5, 3.15, 1.75),
}


def circlip_lookup(diameter_mm, kind='shaft'):
    """DIN 471 (external, on a shaft) / DIN 472 (internal, in a bore)
    retaining-ring groove: ring thickness, groove diameter (d2), groove
    width (m, H13) and depth — everything needed to model the groove.
    Non-standard diameters return the nearest standard sizes instead."""
    table = DIN_471 if kind == 'shaft' else (
        DIN_472 if kind == 'bore' else None)
    if table is None:
        raise RuntimeError("kind must be 'shaft' (DIN 471) or 'bore' "
                           '(DIN 472)')
    d = float(diameter_mm)
    standard = 'DIN 471' if kind == 'shaft' else 'DIN 472'
    if d in table:
        s, d2, m, t = table[d]
        return {'standard': standard, 'kind': kind, 'd1_mm': d,
                'ring_thickness_mm': s, 'groove_diameter_mm': d2,
                'groove_width_mm': m, 'groove_width_tolerance': 'H13',
                'groove_depth_mm': t,
                'edge_note': 'groove corners sharp (max r ~0.1xS); ring '
                             'seats against the groove wall'}
    near = sorted(table, key=lambda n: abs(n - d))[:3]
    return {'standard': standard, 'kind': kind,
            'error': '%s mm is not a standard %s size' % (diameter_mm,
                                                          standard),
            'nearest': [{'d1_mm': n, 'ring_thickness_mm': table[n][0],
                         'groove_diameter_mm': table[n][1],
                         'groove_width_mm': table[n][2],
                         'groove_depth_mm': table[n][3]} for n in near]}


# --------------------------------------------------------------------------- #
# O-ring gland design (Parker-handbook style rules; metric + AS568 CS)
# --------------------------------------------------------------------------- #
_STANDARD_CS = (1.0, 1.5, 1.78, 2.0, 2.5, 2.62, 3.0, 3.53, 4.0, 5.0, 5.33,
                5.7, 7.0)

_SEAL_RULES = {
    # squeeze fraction (of CS) target, gland fill target
    'static_radial': {'squeeze': (0.15, 0.25), 'fill': 0.75},
    'dynamic_radial': {'squeeze': (0.10, 0.16), 'fill': 0.80},
    'face': {'squeeze': (0.20, 0.30), 'fill': 0.75},
}


def oring_gland(cs_mm, id_mm=0.0, seal='static_radial'):
    """O-ring groove (gland) design from the cord thickness: groove depth
    and width for static radial, dynamic radial or face (axial) seals,
    following the standard squeeze (static 15-25%, dynamic 10-16%, face
    20-30%) and ~75-80% fill rules. Also snaps the measured cord to the
    nearest standard cross-section (metric + AS568) and, with id_mm given,
    reports bore/groove-root diameters and the stretch check (<=5%)."""
    if seal not in _SEAL_RULES:
        raise RuntimeError('seal must be one of %s'
                           % ', '.join(sorted(_SEAL_RULES)))
    cs = float(cs_mm)
    if not 0.5 <= cs <= 8.0:
        raise RuntimeError('cs_mm %s outside supported 0.5-8 mm' % cs_mm)
    rules = _SEAL_RULES[seal]
    nearest_cs = min(_STANDARD_CS, key=lambda c: abs(c - cs))
    lo, hi = rules['squeeze']
    squeeze = (lo + hi) / 2.0
    depth = round(cs * (1.0 - squeeze), 2)
    import math
    cord_area = math.pi * cs * cs / 4.0
    width = round(cord_area / (rules['fill'] * depth), 2)
    out = {
        'seal': seal,
        'cs_mm': cs,
        'nearest_standard_cs_mm': nearest_cs,
        'groove_depth_mm': depth,
        'groove_width_mm': width,
        'squeeze_pct': [round(lo * 100), round(hi * 100)],
        'target_fill_pct': round(rules['fill'] * 100),
        'surface_note': 'groove faces Ra<=1.6 um (0.8 dynamic); no sharp '
                        'entry edges — chamfer or radius the bore lead-in',
    }
    if abs(nearest_cs - cs) > 0.15:
        out['warning'] = ('Measured cord %.2f mm is far from any standard '
                          'cross-section — squashed old ring? Measure an '
                          'unloaded section or take the nearest standard '
                          '%.2f mm.' % (cs, nearest_cs))
    if id_mm:
        ring_id = float(id_mm)
        if seal == 'face':
            out['face_groove'] = {
                'inner_diameter_mm': round(ring_id + 0.5, 2),
                'outer_diameter_mm': round(ring_id + 0.5 + 2 * width, 2),
                'note': 'internal pressure: groove OD locates the ring '
                        '(ring rolls outward); vacuum/external: locate on '
                        'the ID instead',
            }
        else:
            groove_root = ring_id * 1.02  # ~2% stretch target on the ID
            out['radial_groove'] = {
                'shaft_groove_root_mm': round(groove_root, 2),
                'bore_mm': round(groove_root + 2 * depth, 2),
                'stretch_pct': 2.0,
                'note': 'male (piston) gland: root = ring ID at 1-5% '
                        'stretch, bore = root + 2x depth',
            }
        out['ring'] = {'id_mm': ring_id, 'cs_mm': nearest_cs,
                       'od_mm': round(ring_id + 2 * nearest_cs, 2)}
    return out


# --------------------------------------------------------------------------- #
# Synchronous (timing) belts
# --------------------------------------------------------------------------- #
BELT_PROFILES = {
    'GT2': 2.0, 'GT3': 3.0, 'GT5': 5.0, 'MXL': 2.032, 'XL': 5.08,
    'HTD3': 3.0, 'HTD5': 5.0, 'HTD8': 8.0, 'T2.5': 2.5, 'T5': 5.0,
    'T10': 10.0,
}


def belt_calc(profile, teeth_small, teeth_large=0, belt_teeth=0,
              center_distance_mm=0.0):
    """Synchronous-belt drive geometry (GT2/GT3/HTD/T-profile...): pulley
    pitch diameters from tooth counts, and belt length <-> center distance
    (pass belt_teeth to get the center distance, or center_distance_mm to
    get the nearest whole-tooth belt). teeth_large 0 = same as small (1:1).
    Pitch diameter = teeth x pitch / pi; printed pulleys: subtract belt
    tooth height for the OD and add 0.15-0.2 mm bore compensation."""
    import math
    key = str(profile).upper().replace(' ', '')
    if key not in BELT_PROFILES:
        raise RuntimeError('profile must be one of %s'
                           % ', '.join(sorted(BELT_PROFILES)))
    pitch = BELT_PROFILES[key]
    z1 = int(teeth_small)
    z2 = int(teeth_large) or z1
    if z1 < 8 or z2 < z1:
        raise RuntimeError('need teeth_small >= 8 and teeth_large >= '
                           'teeth_small')
    d1 = z1 * pitch / math.pi
    d2 = z2 * pitch / math.pi
    out = {
        'profile': key, 'pitch_mm': pitch,
        'pulley_small': {'teeth': z1, 'pitch_diameter_mm': round(d1, 3)},
        'pulley_large': {'teeth': z2, 'pitch_diameter_mm': round(d2, 3)},
        'ratio': round(z2 / z1, 4),
    }

    def length_for(c):
        return (2 * c + math.pi * (d1 + d2) / 2.0
                + (d2 - d1) ** 2 / (4.0 * c))

    if belt_teeth:
        target = int(belt_teeth) * pitch
        # Solve length_for(c) = target for c (quadratic in c).
        b = math.pi * (d1 + d2) / 2.0 - target
        disc = b * b - 2 * (d2 - d1) ** 2
        if disc < 0:
            raise RuntimeError('belt with %s teeth is too short for these '
                               'pulleys' % belt_teeth)
        c = (-b + math.sqrt(disc)) / 4.0
        if c < (d1 + d2) / 2.0:
            raise RuntimeError('belt with %s teeth gives a center distance '
                               'below the pulleys touching' % belt_teeth)
        out['belt'] = {'teeth': int(belt_teeth),
                       'length_mm': round(target, 2)}
        out['center_distance_mm'] = round(c, 2)
    elif center_distance_mm:
        c = float(center_distance_mm)
        if c < (d1 + d2) / 2.0:
            raise RuntimeError('center_distance_mm smaller than the pulleys '
                               'allow (min ~%.1f)' % ((d1 + d2) / 2.0))
        length = length_for(c)
        teeth = round(length / pitch)
        actual = teeth * pitch
        b = math.pi * (d1 + d2) / 2.0 - actual
        c_actual = (-b + math.sqrt(b * b - 2 * (d2 - d1) ** 2)) / 4.0
        out['belt'] = {'teeth': int(teeth), 'length_mm': round(actual, 2),
                       'note': 'nearest whole-tooth belt'}
        out['center_distance_mm'] = round(c_actual, 2)
        out['requested_center_mm'] = c
        out['adjustment_mm'] = round(c_actual - c, 2)
    return out

"""Metric fastener dimension tables and hole recommendations (pure data).

Dimensions vendored from public standards tables (the BOLTS project,
boltsparts.github.io, and the underlying ISO/DIN standards): ISO 273
clearance holes, ISO 262 coarse pitches and tap drills, ISO 4762/DIN 912
socket head cap screws, ISO 4017/DIN 933 hex bolts, ISO 4032/DIN 934 nuts,
ISO 7089/DIN 125 washers, DIN 974 counterbores, plus typical brass heat-set
insert holes for FDM prints (Ruthex/CNC-Kitchen style inserts).

All values are millimetres. Standard dimensions do not age, but verify
critical fits against the vendor datasheet — especially heat-set inserts,
which vary by brand.
"""

# size -> {pitch, tap_drill, clearance (close, normal, loose) ISO 273,
#          socket_head {dk, k, hex}, cbore {dia, depth==k},
#          hex {s, k}, nut {s, m}, washer {d2, h},
#          heat_set {hole, min_depth} (typical M-series brass inserts)}
_TABLE = {
    'M2':   {'pitch': 0.4,  'tap_drill': 1.6,
             'clearance': (2.2, 2.4, 2.6),
             'socket_head': {'dk': 3.8, 'k': 2.0, 'hex': 1.5},
             'cbore': 4.4, 'hex': {'s': 4.0, 'k': 1.4},
             'nut': {'s': 4.0, 'm': 1.6}, 'washer': {'d2': 5.0, 'h': 0.3},
             'heat_set': {'hole': 3.2, 'min_depth': 4.0}},
    'M2.5': {'pitch': 0.45, 'tap_drill': 2.05,
             'clearance': (2.7, 2.9, 3.1),
             'socket_head': {'dk': 4.5, 'k': 2.5, 'hex': 2.0},
             'cbore': 5.4, 'hex': {'s': 5.0, 'k': 1.7},
             'nut': {'s': 5.0, 'm': 2.0}, 'washer': {'d2': 6.0, 'h': 0.5},
             'heat_set': {'hole': 3.4, 'min_depth': 5.0}},
    'M3':   {'pitch': 0.5,  'tap_drill': 2.5,
             'clearance': (3.2, 3.4, 3.6),
             'socket_head': {'dk': 5.5, 'k': 3.0, 'hex': 2.5},
             'cbore': 6.5, 'hex': {'s': 5.5, 'k': 2.0},
             'nut': {'s': 5.5, 'm': 2.4}, 'washer': {'d2': 7.0, 'h': 0.5},
             'heat_set': {'hole': 4.0, 'min_depth': 5.7}},
    'M4':   {'pitch': 0.7,  'tap_drill': 3.3,
             'clearance': (4.3, 4.5, 4.8),
             'socket_head': {'dk': 7.0, 'k': 4.0, 'hex': 3.0},
             'cbore': 8.0, 'hex': {'s': 7.0, 'k': 2.8},
             'nut': {'s': 7.0, 'm': 3.2}, 'washer': {'d2': 9.0, 'h': 0.8},
             'heat_set': {'hole': 5.6, 'min_depth': 8.1}},
    'M5':   {'pitch': 0.8,  'tap_drill': 4.2,
             'clearance': (5.3, 5.5, 5.8),
             'socket_head': {'dk': 8.5, 'k': 5.0, 'hex': 4.0},
             'cbore': 10.0, 'hex': {'s': 8.0, 'k': 3.5},
             'nut': {'s': 8.0, 'm': 4.0}, 'washer': {'d2': 10.0, 'h': 1.0},
             'heat_set': {'hole': 6.4, 'min_depth': 9.5}},
    'M6':   {'pitch': 1.0,  'tap_drill': 5.0,
             'clearance': (6.4, 6.6, 7.0),
             'socket_head': {'dk': 10.0, 'k': 6.0, 'hex': 5.0},
             'cbore': 11.0, 'hex': {'s': 10.0, 'k': 4.0},
             'nut': {'s': 10.0, 'm': 5.0}, 'washer': {'d2': 12.0, 'h': 1.6},
             'heat_set': {'hole': 8.1, 'min_depth': 12.7}},
    'M8':   {'pitch': 1.25, 'tap_drill': 6.8,
             'clearance': (8.4, 9.0, 10.0),
             'socket_head': {'dk': 13.0, 'k': 8.0, 'hex': 6.0},
             'cbore': 15.0, 'hex': {'s': 13.0, 'k': 5.3},
             'nut': {'s': 13.0, 'm': 6.5}, 'washer': {'d2': 16.0, 'h': 1.6},
             'heat_set': None},
    'M10':  {'pitch': 1.5,  'tap_drill': 8.5,
             'clearance': (10.5, 11.0, 12.0),
             'socket_head': {'dk': 16.0, 'k': 10.0, 'hex': 8.0},
             'cbore': 18.0, 'hex': {'s': 16.0, 'k': 6.4},
             'nut': {'s': 16.0, 'm': 8.0}, 'washer': {'d2': 20.0, 'h': 2.0},
             'heat_set': None},
    'M12':  {'pitch': 1.75, 'tap_drill': 10.2,
             'clearance': (13.0, 13.5, 14.5),
             'socket_head': {'dk': 18.0, 'k': 12.0, 'hex': 10.0},
             'cbore': 20.0, 'hex': {'s': 18.0, 'k': 7.5},
             'nut': {'s': 18.0, 'm': 10.0}, 'washer': {'d2': 24.0, 'h': 2.5},
             'heat_set': None},
}

_FITS = {'close': 0, 'normal': 1, 'loose': 2}


def _entry(size):
    key = str(size).upper().replace(',', '.').replace(' ', '')
    if not key.startswith('M'):
        key = 'M' + key
    if key.endswith('.0'):
        key = key[:-2]
    if key not in _TABLE:
        raise RuntimeError('Unknown size %r — supported: %s'
                           % (size, ', '.join(_TABLE)))
    return key, _TABLE[key]


def lookup(size):
    """Everything the table knows about one metric size (all mm): coarse
    pitch, tap drill, ISO 273 clearance holes, socket/hex head and nut/washer
    envelope dimensions, counterbore, heat-set insert hole."""
    key, row = _entry(size)
    nominal = float(key[1:])
    out = {
        'size': key,
        'nominal_mm': nominal,
        'pitch_mm': row['pitch'],
        'tap_drill_mm': row['tap_drill'],
        'clearance_mm': {fit: row['clearance'][i] for fit, i in _FITS.items()},
        'socket_head': dict(row['socket_head'],
                            counterbore_dia=row['cbore'],
                            counterbore_depth=row['socket_head']['k']),
        'hex_head': dict(row['hex']),
        'nut': dict(row['nut']),
        'washer': dict(row['washer']),
        'source': 'ISO 273/262/4762/4017/4032/7089, DIN 974 (via BOLTS); '
                  'heat-set holes are typical brand values — verify '
                  'critical fits.',
    }
    if row['heat_set']:
        out['heat_set'] = dict(row['heat_set'])
    return out


def hole_spec(size, kind='clearance', fit='normal', head='none',
              material_thickness=None):
    """The hole to model for a given screw, ready for the Fusion `hole` tool.

    kind: "clearance" (bolt passes through; fit close|normal|loose),
    "tapped" (thread cut into the part; returns the tap drill and pitch),
    "heat_set" (FDM insert pocket). head: "none" | "counterbore" (socket
    head sits flush) | "countersink" (90 deg). Returns diameters/depths in
    mm plus a note with the matching hole-tool parameters."""
    key, row = _entry(size)
    kind = (kind or 'clearance').lower()
    head = (head or 'none').lower()
    out = {'size': key, 'kind': kind}

    if kind == 'clearance':
        if fit not in _FITS:
            raise RuntimeError("fit must be close|normal|loose, got %r" % fit)
        out['diameter_mm'] = row['clearance'][_FITS[fit]]
        out['fit'] = fit
    elif kind == 'tapped':
        out['diameter_mm'] = row['tap_drill']
        out['pitch_mm'] = row['pitch']
        out['thread'] = '%sx%s' % (key, row['pitch'])
        out['note'] = ('Model the tap-drill hole, or use the thread tool '
                       'with designation %s.' % out['thread'])
    elif kind == 'heat_set':
        if not row['heat_set']:
            raise RuntimeError('No typical heat-set insert data for %s '
                               '(inserts above M6 are uncommon) — check the '
                               'vendor datasheet.' % key)
        out['diameter_mm'] = row['heat_set']['hole']
        out['depth_mm'] = row['heat_set']['min_depth'] + 1.0
        out['note'] = ('Typical brass insert: hole %.1f mm, depth >= %.1f mm '
                       '(insert + 1 mm melt room). Add >= 2 mm wall around '
                       'the hole; verify against your insert brand.'
                       % (out['diameter_mm'], out['depth_mm']))
    else:
        raise RuntimeError("kind must be clearance|tapped|heat_set, got %r"
                           % kind)

    if head == 'counterbore':
        out['counterbore'] = {'diameter_mm': row['cbore'],
                              'depth_mm': row['socket_head']['k']}
    elif head == 'countersink':
        out['countersink'] = {'diameter_mm': round(2.0 * float(key[1:]), 1),
                              'angle_deg': 90}
    elif head != 'none':
        raise RuntimeError("head must be none|counterbore|countersink, got %r"
                           % head)

    if material_thickness is not None and kind == 'clearance':
        grip = float(material_thickness)
        nut_space = row['nut']['m'] + row['washer']['h']
        out['suggested_bolt_length_mm'] = round(grip + nut_space + 2.0, 1)
    return out


def sizes():
    return list(_TABLE)

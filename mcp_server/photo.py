"""Photo -> CAD helpers (2D): perspective rectification with a printed marker,
millimetre measurements off the rectified photo, and bitmap-to-DXF
vectorisation for sketch tracing.

Everything runs in the MCP server process. The heavy dependencies are optional
(the "photo" extras: opencv-contrib-python-headless, ezdxf); every public
function raises a RuntimeError with install instructions when they are missing.

Units: photos are pixels; the bridge to millimetres is mm_per_px, established
by rectify() from a marker of known size (or a known rectangle / known
distance in the frame). DXF output is millimetres, Y up (image Y is flipped);
measure() reports image-pixel coordinates (Y down) next to every millimetre
value so the annotated preview and the numbers always agree.
"""
import math
import os
import struct

try:
    import cv2
except ImportError as exc:  # pragma: no cover - exercised via hint test
    cv2 = None
    _CV2_ERROR = str(exc)
else:
    _CV2_ERROR = None

try:
    import ezdxf
except ImportError as exc:  # pragma: no cover
    ezdxf = None
    _EZDXF_ERROR = str(exc)
else:
    _EZDXF_ERROR = None

_INSTALL_HINT = (
    "Photo tools need the optional 'photo' dependencies. Install them with: "
    "pip install -e \"mcp_server[photo]\"  (or: pip install "
    "opencv-contrib-python-headless ezdxf) and restart the MCP server. "
    "Note: use the -contrib- headless wheel, not plain opencv-python — the "
    "ArUco module lives in contrib and GUI builds clash with headless ones."
)

# ArUco dictionaries by friendly name; 'qr' switches to the QR detector.
_ARUCO_DICTS = {
    '4x4_50': 'DICT_4X4_50',
    '5x5_100': 'DICT_5X5_100',
    '6x6_250': 'DICT_6X6_250',
    '7x7_50': 'DICT_7X7_50',
    'apriltag_36h11': 'DICT_APRILTAG_36h11',
}


def _require_cv2():
    if cv2 is None:
        raise RuntimeError('%s Import error: %s' % (_INSTALL_HINT, _CV2_ERROR))


def _require_ezdxf():
    if ezdxf is None:
        raise RuntimeError('%s Import error: %s' % (_INSTALL_HINT, _EZDXF_ERROR))


def _read_gray(image_path):
    img = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError('Could not read image %r (missing file or '
                           'unsupported format)' % image_path)
    return img, cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _detect_aruco_all(gray, dict_name):
    """Every marker of the dictionary in the frame, sorted by id:
    [(id, corners 4x2), ...] with corner order TL TR BR BL."""
    holder = _ARUCO_DICTS.get(dict_name)
    if holder is None:
        raise RuntimeError('marker must be one of %s or "qr", got %r'
                           % (sorted(_ARUCO_DICTS), dict_name))
    aruco = getattr(cv2, 'aruco', None)
    if aruco is None:
        raise RuntimeError('cv2.aruco missing — install '
                           'opencv-contrib-python-headless (not plain '
                           'opencv-python-headless).')
    dictionary = aruco.getPredefinedDictionary(getattr(aruco, holder))
    try:  # OpenCV >= 4.7 object API
        detector = aruco.ArucoDetector(dictionary, aruco.DetectorParameters())
        corners, ids, _ = detector.detectMarkers(gray)
    except AttributeError:  # legacy function API
        corners, ids, _ = aruco.detectMarkers(gray, dictionary)
    if ids is None or not len(ids):
        return []
    found = [(int(mid), quad.reshape(4, 2))
             for quad, mid in zip(corners, ids.flatten(), strict=False)]
    found.sort(key=lambda item: item[0])
    return found


def _detect_qr(gray):
    ok, points = cv2.QRCodeDetector().detect(gray)
    if not ok or points is None:
        return None
    return points.reshape(4, 2)


def _read_exif(image_path):
    """Best-effort EXIF metadata (JPEG only, stdlib): camera model and focal
    length — enough to warn about ultra-wide phone lenses. Returns {} when
    absent or unparseable; never raises."""
    try:
        with open(image_path, 'rb') as fh:
            if fh.read(2) != b'\xff\xd8':
                return {}
            data = fh.read(256 * 1024)  # APP1 sits at the front of the file
        i, tiff = 0, None
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            if marker == 0xFF:  # fill byte
                i += 1
                continue
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker in (0xD9, 0xDA):  # EOI / start of scan: no more metadata
                break
            seglen = int.from_bytes(data[i + 2:i + 4], 'big')
            if seglen < 2:
                break
            if marker == 0xE1 and data[i + 4:i + 10] == b'Exif\x00\x00':
                tiff = data[i + 10:i + 2 + seglen]
                break
            i += 2 + seglen
        if not tiff or len(tiff) < 8 or tiff[:2] not in (b'II', b'MM'):
            return {}
        endian = '<' if tiff[:2] == b'II' else '>'

        def u16(off):
            return struct.unpack_from(endian + 'H', tiff, off)[0]

        def u32(off):
            return struct.unpack_from(endian + 'I', tiff, off)[0]

        out = {}

        def read_ifd(off, depth=0):
            if depth > 2 or off + 2 > len(tiff):
                return
            count = u16(off)
            for k in range(count):
                e = off + 2 + 12 * k
                if e + 12 > len(tiff):
                    return
                tag, typ, cnt = u16(e), u16(e + 2), u32(e + 4)
                if tag == 0x8769:  # Exif sub-IFD pointer
                    read_ifd(u32(e + 8), depth + 1)
                elif tag == 0x0110 and typ == 2:  # Model (ASCII)
                    ptr = u32(e + 8) if cnt > 4 else e + 8
                    raw = tiff[ptr:ptr + cnt]
                    text = raw.split(b'\x00')[0].decode('ascii',
                                                        'replace').strip()
                    if text:
                        out['camera'] = text
                elif tag == 0x920A and typ == 5 and cnt >= 1:  # FocalLength
                    ptr = u32(e + 8)
                    num, den = u32(ptr), u32(ptr + 4)
                    if den:
                        out['focal_mm'] = round(num / den, 2)
                elif tag == 0xA405 and typ == 3:  # 35 mm equivalent
                    out['focal_35mm'] = u16(e + 8)

        read_ifd(u32(4))
        return out
    except Exception:  # noqa: BLE001 - metadata only, must never block
        return {}


def _undistort_pts(pts, k1, width, height):
    """Division-model undistortion of pixel points: u = c + (d-c)/(1+k1*r²)
    with r normalised by the half-diagonal. k1>0 corrects barrel."""
    import numpy as np
    centre = np.array([(width - 1) / 2.0, (height - 1) / 2.0])
    norm = 0.5 * math.hypot(width, height)
    delta = np.asarray(pts, dtype='float64') - centre
    r2 = (delta ** 2).sum(axis=-1, keepdims=True) / (norm * norm)
    return centre + delta / (1.0 + k1 * r2)


def _undistort_image(img, k1):
    """Resample the photo so the division-model distortion k1 is gone. For
    every output (undistorted) pixel the source radius solves
    k1*r_u*r_d² - r_d + r_u = 0 (analytic; the branch that -> r_u as k1->0)."""
    import numpy as np
    h_img, w_img = img.shape[:2]
    cx, cy = (w_img - 1) / 2.0, (h_img - 1) / 2.0
    norm = 0.5 * math.hypot(w_img, h_img)
    dx = (np.arange(w_img, dtype='float32') - cx)[None, :]
    dy = (np.arange(h_img, dtype='float32') - cy)[:, None]
    r_u = np.hypot(np.broadcast_to(dx, (h_img, w_img)),
                   np.broadcast_to(dy, (h_img, w_img))) / norm
    if abs(k1) < 1e-9:
        scale = np.ones_like(r_u)
    else:
        disc = np.clip(1.0 - 4.0 * k1 * r_u * r_u, 0.0, None)
        with np.errstate(divide='ignore', invalid='ignore'):
            r_d = (1.0 - np.sqrt(disc)) / (2.0 * k1 * r_u)
        scale = np.where(r_u > 1e-9, r_d / r_u, 1.0).astype('float32')
    map_x = (cx + dx * scale).astype('float32')
    map_y = (cy + dy * scale).astype('float32')
    return cv2.remap(img, map_x, map_y, cv2.INTER_LINEAR)


def _fit_plane_homography(markers, side_px, refine=True, iters=2):
    """Homography image->plane (side_px pixels per marker side) from one or
    more SAME-SIZE square markers. With several markers the fit is refined so
    every marker comes out square at the right size wherever it lies; returns
    (h_mat, rms_px, per_marker stats). Stats are empty for a single marker —
    one marker fits its own corners exactly."""
    import numpy as np
    areas = [abs(cv2.contourArea(np.asarray(c, dtype='float32')))
             for _, c in markers]
    base = np.asarray(markers[int(np.argmax(areas))][1], dtype='float32')
    dst = np.array([[0, 0], [side_px, 0], [side_px, side_px], [0, side_px]],
                   dtype='float32')
    h_mat = cv2.getPerspectiveTransform(base, dst)
    if len(markers) == 1:
        return h_mat, 0.0, []
    offs = np.array([[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]],
                    dtype='float64') * float(side_px)
    src_all = np.concatenate([np.asarray(c, dtype='float64')
                              for _, c in markers])

    def ideal_quads(current):
        warped = cv2.perspectiveTransform(
            src_all.reshape(-1, 1, 2), current).reshape(-1, 4, 2)
        ideal = []
        for quad in warped:
            centre = quad.mean(axis=0)
            vx = (quad[1] - quad[0]) + (quad[2] - quad[3])
            ang = math.atan2(vx[1], vx[0])
            rot = np.array([[math.cos(ang), -math.sin(ang)],
                            [math.sin(ang), math.cos(ang)]])
            ideal.append(centre + offs @ rot.T)
        return warped, np.concatenate(ideal)

    if refine:
        for _ in range(max(1, int(iters))):
            _, ideal = ideal_quads(h_mat)
            h_new, _ = cv2.findHomography(src_all, ideal, 0)
            if h_new is None:
                break
            h_mat = h_new
    warped, ideal = ideal_quads(h_mat)
    per_marker = []
    for (mid, _), quad in zip(markers, warped, strict=False):
        sides = [float(np.linalg.norm(quad[(k + 1) % 4] - quad[k]))
                 for k in range(4)]
        err = 100.0 * abs(sum(sides) / (4.0 * side_px) - 1.0)
        per_marker.append({'id': mid, 'scale_err_pct': round(err, 2)})
    rms = float(np.sqrt(((warped.reshape(-1, 2) - ideal) ** 2)
                        .sum(axis=1).mean()))
    return h_mat, rms, per_marker


def _solve_k1(markers, side_px, width, height):
    """Grid-search the division-model k1 that makes all markers agree best
    (coarse then fine). Needs 2+ markers; returns (k1, rms_px)."""
    import numpy as np

    def rms_for(k1):
        und = [(mid, _undistort_pts(c, k1, width, height))
               for mid, c in markers]
        _, rms, _ = _fit_plane_homography(und, side_px)
        return rms

    best_k1, best_rms = 0.0, rms_for(0.0)
    for k1 in np.linspace(-0.24, 0.24, 25):
        rms = rms_for(float(k1))
        if rms < best_rms:
            best_k1, best_rms = float(k1), rms
    for k1 in np.linspace(best_k1 - 0.02, best_k1 + 0.02, 21):
        rms = rms_for(float(k1))
        if rms < best_rms:
            best_k1, best_rms = float(k1), rms
    return best_k1, best_rms


def _fit_circle(pts):
    """Least-squares (Kasa) circle through Nx2 points -> (cx, cy, r) or
    None when degenerate."""
    import numpy as np
    pts = np.asarray(pts, dtype='float64')
    a_mat = np.column_stack([2.0 * pts[:, 0], 2.0 * pts[:, 1],
                             np.ones(len(pts))])
    b_vec = (pts ** 2).sum(axis=1)
    try:
        sol, *_ = np.linalg.lstsq(a_mat, b_vec, rcond=None)
    except np.linalg.LinAlgError:
        return None
    cx, cy, c0 = (float(v) for v in sol)
    r2 = c0 + cx * cx + cy * cy
    if r2 <= 0:
        return None
    return cx, cy, math.sqrt(r2)


def rectify(image_path, out_path=None, marker='4x4_50', marker_size_mm=50.0,
            mm_per_px=0.2, margin_mm=10.0, ref_points=None, ref_width_mm=0.0,
            ref_height_mm=0.0, scale_points=None, known_mm=0.0,
            undistort='off', refine=True):
    """Remove perspective from a workshop photo and fix the scale: after
    rectification every pixel is exactly `mm_per_px` millimetres in the
    part's plane. Three ways to establish the plane, best first:

    * printed square marker(s) lying IN the plane of the part — `marker` is
      an ArUco dictionary (default 4x4_50) or "qr", `marker_size_mm` the
      printed side. SEVERAL markers of the same size in the frame refine the
      fit and report how well they agree (scale_spread_pct/residual_mm);
      undistort='auto' also estimates and removes lens distortion (needs 2+
      ArUco markers; a number is taken as division-model k1 directly).
    * `ref_points`: pixel corners [TL,TR,BR,BL] of any rectangle of known
      size (A4 sheet 210x297, bank card 85.6x53.98...) + ref_width_mm /
      ref_height_mm — full rectification without a printed marker.
    * `scale_points`: two pixel points a known `known_mm` apart (ruler) —
      scale ONLY, no perspective correction; last resort for photos shot
      square-on.

    Writes the rectified image (default <name>_rect.png) and returns
    mm_per_px for photo_measure / photo_to_sketch / canvas_add."""
    _require_cv2()
    import numpy as np  # cv2 wheels always bundle numpy
    img, gray = _read_gray(image_path)
    if float(mm_per_px) <= 0:
        raise RuntimeError('mm_per_px must be positive')
    exif = _read_exif(image_path)
    notes = []
    if exif.get('focal_35mm') and exif['focal_35mm'] <= 23:
        notes.append('EXIF reports a %d mm-equivalent ultra-wide lens — '
                     'expect barrel distortion: re-shoot with the main (1x) '
                     "camera or use undistort='auto' with 2+ markers."
                     % exif['focal_35mm'])
    h_img, w_img = gray.shape[:2]

    # --- scale_points: known distance, similarity scale only, no warp. ----- #
    if scale_points is not None and len(scale_points):
        pts = np.asarray(scale_points, dtype='float64')
        if pts.shape != (2, 2):
            raise RuntimeError('scale_points must be exactly two [x,y] '
                               'pixel points')
        if float(known_mm) <= 0:
            raise RuntimeError('known_mm must be positive with scale_points')
        dist_px = float(np.hypot(*(pts[1] - pts[0])))
        if dist_px < 5:
            raise RuntimeError('scale_points are %.1f px apart — too close '
                               'to give a usable scale' % dist_px)
        measured = float(known_mm) / dist_px
        result = {
            'input': image_path,
            'output': image_path,
            'mode': 'scale_only',
            'mm_per_px': round(measured, 6),
            'size_px': [w_img, h_img],
            'size_mm': [round(w_img * measured, 2),
                        round(h_img * measured, 2)],
            'note': ('Similarity scale only — NO perspective correction; '
                     'accurate only where the camera looked straight at the '
                     'plane. For real rectification use a printed marker or '
                     'ref_points of a known rectangle.'),
        }
        if exif:
            result['exif'] = exif
        if notes:
            result['note'] += ' ' + ' '.join(notes)
        return result

    # --- Establish the image->plane homography. ---------------------------- #
    k1 = None
    per_marker, rms_px = [], 0.0
    if ref_points is not None and len(ref_points):
        pts = np.asarray(ref_points, dtype='float32')
        if pts.shape != (4, 2):
            raise RuntimeError('ref_points must be 4 [x,y] pixel pairs in '
                               'TL, TR, BR, BL order')
        if float(ref_width_mm) <= 0 or float(ref_height_mm) <= 0:
            raise RuntimeError('ref_width_mm and ref_height_mm must be '
                               'positive with ref_points')
        w_px = float(ref_width_mm) / float(mm_per_px)
        h_px = float(ref_height_mm) / float(mm_per_px)
        dst = np.array([[0, 0], [w_px, 0], [w_px, h_px], [0, h_px]],
                       dtype='float32')
        h_mat = cv2.getPerspectiveTransform(pts, dst)
        kind, ids = 'ref_rect', []
    else:
        if float(marker_size_mm) <= 0:
            raise RuntimeError('marker_size_mm must be positive')
        marker = (marker or '4x4_50').lower()
        side_px = float(marker_size_mm) / float(mm_per_px)

        def detect(gray_img):
            if marker == 'qr':
                quad = _detect_qr(gray_img)
                return [] if quad is None else [(None, quad)]
            return _detect_aruco_all(gray_img, marker)

        kind = 'qr' if marker == 'qr' else 'aruco:%s' % marker
        markers = detect(gray)
        if not markers:
            raise RuntimeError(
                'No %s marker found in %r. Print a marker, lay it flat in '
                'the same plane as the part outline, and re-shoot; or pass '
                'a different `marker` dictionary.' % (kind, image_path))
        if undistort not in (None, '', 'off', False):
            if undistort == 'auto':
                if len(markers) < 2 or marker == 'qr':
                    notes.append("undistort='auto' needs 2+ ArUco markers "
                                 'in the frame — skipped.')
                else:
                    k1, _ = _solve_k1(markers, side_px, w_img, h_img)
            else:
                try:
                    k1 = float(undistort)
                except (TypeError, ValueError):
                    raise RuntimeError("undistort must be 'off', 'auto' or "
                                       'a division-model k1 number') from None
        if k1 is not None and abs(k1) < 0.005:
            k1 = None  # not worth a resample
        if k1 is not None:
            img = _undistort_image(img, k1)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            markers = detect(gray)
            if not markers:
                raise RuntimeError('Marker lost after undistortion '
                                   "(k1=%g) — retry with undistort='off'."
                                   % k1)
        ids = [mid for mid, _ in markers if mid is not None]
        h_mat, rms_px, per_marker = _fit_plane_homography(
            markers, side_px, refine=bool(refine))

    # Keep the WHOLE photo: shift the mapping so every warped image corner
    # (plus margin) has positive coordinates, and size the canvas to fit.
    corners = np.array([[[0, 0]], [[w_img, 0]], [[w_img, h_img]], [[0, h_img]]],
                       dtype='float32')
    warped = cv2.perspectiveTransform(corners, h_mat).reshape(4, 2)
    margin_px = float(margin_mm) / float(mm_per_px)
    shift_x = margin_px - float(warped[:, 0].min())
    shift_y = margin_px - float(warped[:, 1].min())
    shift = np.array([[1, 0, shift_x], [0, 1, shift_y], [0, 0, 1]],
                     dtype='float64')
    h_mat = shift @ h_mat
    out_w = int(math.ceil(float(warped[:, 0].max()) + shift_x + margin_px))
    out_h = int(math.ceil(float(warped[:, 1].max()) + shift_y + margin_px))
    if out_w * out_h > 64_000_000:
        raise RuntimeError(
            'Rectified canvas would be %dx%d px — the marker is tiny in the '
            'frame or mm_per_px is too small. Re-shoot closer or raise '
            'mm_per_px.' % (out_w, out_h))

    rectified = cv2.warpPerspective(img, h_mat, (out_w, out_h))
    if not out_path:
        out_path = os.path.splitext(image_path)[0] + '_rect.png'
    if not cv2.imwrite(out_path, rectified):
        raise RuntimeError('Could not write %r' % out_path)
    result = {
        'input': image_path,
        'output': out_path,
        'marker': kind,
        'marker_ids': ids,
        'mode': 'ref_rect' if kind == 'ref_rect' else 'marker',
        'mm_per_px': float(mm_per_px),
        'size_px': [out_w, out_h],
        'size_mm': [round(out_w * mm_per_px, 2), round(out_h * mm_per_px, 2)],
        'note': ('Scale is exact only in the reference plane. Next: '
                 'photo_measure to take dimensions, photo_to_sketch'
                 '(mm_per_px=%g) to vectorise, or canvas_add with '
                 'width_mm=%g.' % (mm_per_px, round(out_w * mm_per_px, 2))),
    }
    if kind != 'ref_rect':
        result['marker_size_mm'] = float(marker_size_mm)
        result['marker_count'] = max(1, len(per_marker) or 1)
    if per_marker:
        result['marker_count'] = len(per_marker)
        result['markers'] = per_marker
        spread = max(m['scale_err_pct'] for m in per_marker)
        result['scale_spread_pct'] = spread
        result['residual_mm'] = round(rms_px * float(mm_per_px), 3)
        if spread > 2.0:
            notes.append('Markers disagree by %.1f%% — they are not '
                         'coplanar with the part, or the lens distorts '
                         "(try undistort='auto')." % spread)
    if k1 is not None:
        result['k1'] = round(float(k1), 4)
    if exif:
        result['exif'] = exif
    if notes:
        result['note'] += ' ' + ' '.join(notes)
    return result


def to_sketch_dxf(image_path, mm_per_px, dxf_path=None, threshold=-1,
                  invert=False, epsilon_mm=0.3, min_area_mm2=4.0, holes=True,
                  blur_px=3):
    """Vectorise a part silhouette photo/drawing into a DXF of closed
    polylines (mm, Y up) ready for import_file(format="dxf"). Expects a
    rectified image (photo_rectify) or a flat scan plus its mm_per_px.
    Dark shapes on light background by default (invert=True for the
    opposite); threshold -1 = automatic Otsu. epsilon_mm controls polyline
    simplification, min_area_mm2 drops specks, holes=False keeps only outer
    outlines."""
    _require_cv2()
    _require_ezdxf()
    if float(mm_per_px) <= 0:
        raise RuntimeError('mm_per_px must be positive — run photo_rectify '
                           'first or derive it from a known dimension')
    _, gray = _read_gray(image_path)
    if blur_px and int(blur_px) > 1:
        k = int(blur_px) | 1  # kernel must be odd
        gray = cv2.GaussianBlur(gray, (k, k), 0)
    flags = cv2.THRESH_BINARY if invert else cv2.THRESH_BINARY_INV
    if threshold is None or float(threshold) < 0:
        _, binary = cv2.threshold(gray, 0, 255, flags + cv2.THRESH_OTSU)
    else:
        _, binary = cv2.threshold(gray, float(threshold), 255, flags)

    retrieval = cv2.RETR_CCOMP if holes else cv2.RETR_EXTERNAL
    contours, _ = cv2.findContours(binary, retrieval, cv2.CHAIN_APPROX_SIMPLE)

    mm = float(mm_per_px)
    eps_px = max(0.5, float(epsilon_mm) / mm)
    min_area_px = float(min_area_mm2) / (mm * mm)
    h_img = gray.shape[0]

    doc = ezdxf.new('R2010')
    doc.units = 4  # millimetres
    msp = doc.modelspace()
    kept, total_pts = 0, 0
    lo = [float('inf'), float('inf')]
    hi = [float('-inf'), float('-inf')]
    for contour in contours:
        if cv2.contourArea(contour) < min_area_px:
            continue
        approx = cv2.approxPolyDP(contour, eps_px, True)
        if len(approx) < 3:
            continue
        # Image Y grows downward; CAD Y grows upward.
        pts = [(float(p[0][0]) * mm, (h_img - float(p[0][1])) * mm)
               for p in approx]
        for x, y in pts:
            lo[0], lo[1] = min(lo[0], x), min(lo[1], y)
            hi[0], hi[1] = max(hi[0], x), max(hi[1], y)
        msp.add_lwpolyline(pts, close=True)
        kept += 1
        total_pts += len(pts)
    if not kept:
        raise RuntimeError(
            'No contours above min_area_mm2=%g found — check invert/threshold '
            '(is the part darker than the background?), or lower '
            'min_area_mm2.' % min_area_mm2)

    if not dxf_path:
        dxf_path = os.path.splitext(image_path)[0] + '.dxf'
    doc.saveas(dxf_path)
    return {
        'input': image_path,
        'output': dxf_path,
        'contours': kept,
        'points': total_pts,
        'min_mm': [round(lo[0], 2), round(lo[1], 2)],
        'max_mm': [round(hi[0], 2), round(hi[1], 2)],
        'mm_per_px': mm,
        'note': ("Import onto a plane with import_file(path=..., "
                 "format='dxf', plane='XY') — closed polylines become "
                 "sketch profiles ready to extrude."),
    }


def measure(image_path, mm_per_px, segments=None, holes=False,
            annotate_path=None, snap_px=0, min_diameter_mm=2.0,
            max_diameter_mm=60.0, sensitivity=30, max_holes=24):
    """Millimetre measurements straight off a rectified photo: distances
    between pixel points (segments=[[[x1,y1],[x2,y2]], ...]) and automatic
    circular-hole detection (holes=True: centres, diameters, bolt-pattern
    spacing via Hough + least-squares edge refine). Writes an annotated
    preview PNG so every measured line and circle can be checked by eye.
    Coordinates are pixels of the INPUT image (y down); take mm_per_px from
    rectify(). snap_px > 0 snaps segment endpoints to the nearest detected
    edge within that radius."""
    _require_cv2()
    import numpy as np
    mm = float(mm_per_px)
    if mm <= 0:
        raise RuntimeError('mm_per_px must be positive — take it from '
                           'photo_rectify')
    segments = list(segments) if segments else []
    if np.asarray(segments, dtype=object).shape == (2, 2) \
            and not isinstance(segments[0][0], (list, tuple)):
        segments = [segments]  # a single bare [[x1,y1],[x2,y2]] segment
    if not segments and not holes:
        raise RuntimeError('Nothing to measure: pass segments and/or '
                           'holes=true')
    img, gray = _read_gray(image_path)
    h_img, w_img = gray.shape[:2]
    canvas = img.copy()
    notes = []
    edges = None

    def edge_map():
        nonlocal edges
        if edges is None:
            blur = cv2.GaussianBlur(gray, (3, 3), 0)
            edges = cv2.Canny(blur, 60, 180)
        return edges

    def snap(pt):
        if not snap_px or int(snap_px) <= 0:
            return [float(pt[0]), float(pt[1])], False
        radius = int(snap_px)
        x0, y0 = int(round(pt[0])), int(round(pt[1]))
        xa, xb = max(0, x0 - radius), min(w_img, x0 + radius + 1)
        ya, yb = max(0, y0 - radius), min(h_img, y0 + radius + 1)
        win = edge_map()[ya:yb, xa:xb]
        ys, xs = np.nonzero(win)
        if not len(xs):
            return [float(pt[0]), float(pt[1])], False
        d2 = (xs + xa - pt[0]) ** 2 + (ys + ya - pt[1]) ** 2
        best = int(np.argmin(d2))
        return [float(xs[best] + xa), float(ys[best] + ya)], True

    font = cv2.FONT_HERSHEY_SIMPLEX
    fscale = max(0.4, min(w_img, h_img) / 900.0)
    thick = max(1, int(round(fscale * 2)))

    def label(pos, text):
        org = (int(round(pos[0])) + 5, int(round(pos[1])) - 5)
        cv2.putText(canvas, text, org, font, fscale, (0, 0, 0),
                    thick + 2, cv2.LINE_AA)
        cv2.putText(canvas, text, org, font, fscale, (255, 255, 255),
                    thick, cv2.LINE_AA)

    seg_out = []
    for i, seg in enumerate(segments):
        arr = np.asarray(seg, dtype='float64')
        if arr.shape != (2, 2):
            raise RuntimeError('segment %d must be [[x1,y1],[x2,y2]] in '
                               'pixels, got %r' % (i, seg))
        pt_a, snapped_a = snap(arr[0])
        pt_b, snapped_b = snap(arr[1])
        dist = math.hypot(pt_b[0] - pt_a[0], pt_b[1] - pt_a[1]) * mm
        entry = {'id': 's%d' % (i + 1),
                 'from_px': [round(pt_a[0], 1), round(pt_a[1], 1)],
                 'to_px': [round(pt_b[0], 1), round(pt_b[1], 1)],
                 'mm': round(dist, 2)}
        if snapped_a or snapped_b:
            entry['snapped'] = True
        seg_out.append(entry)
        ia = (int(round(pt_a[0])), int(round(pt_a[1])))
        ib = (int(round(pt_b[0])), int(round(pt_b[1])))
        cv2.line(canvas, ia, ib, (0, 190, 0), thick)
        for tip in (ia, ib):
            cv2.circle(canvas, tip, thick + 2, (0, 190, 0), -1)
        label(((ia[0] + ib[0]) / 2.0, (ia[1] + ib[1]) / 2.0),
              '%s %.2f' % (entry['id'], entry['mm']))

    holes_out, pair_out = [], []
    if holes:
        blur = cv2.medianBlur(gray, 5)
        r_min = max(2, int(round(float(min_diameter_mm) / mm / 2.0)))
        r_max = max(r_min + 1, int(round(float(max_diameter_mm) / mm / 2.0)))
        found = cv2.HoughCircles(
            blur, cv2.HOUGH_GRADIENT, dp=1.2,
            minDist=max(8, int(r_min * 1.6)), param1=120,
            param2=max(10, int(sensitivity)), minRadius=r_min,
            maxRadius=r_max)
        circles = [] if found is None else [tuple(map(float, c))
                                            for c in found.reshape(-1, 3)]
        band = max(20.0, r_min * 2.0)
        circles.sort(key=lambda c: (round(c[1] / band), c[0]))
        if len(circles) > int(max_holes):
            circles = circles[:int(max_holes)]
            notes.append('Hole list truncated to max_holes=%d.' % max_holes)
        edge_pts = None
        if circles:
            eys, exs = np.nonzero(edge_map())
            if len(exs):
                edge_pts = np.stack([exs, eys], axis=1).astype('float64')
        for i, (cx, cy, radius) in enumerate(circles):
            refined = False
            if edge_pts is not None:
                dist = np.hypot(edge_pts[:, 0] - cx, edge_pts[:, 1] - cy)
                ring = edge_pts[(dist > 0.75 * radius)
                                & (dist < 1.25 * radius)]
                if len(ring) >= 12:
                    fit = _fit_circle(ring)
                    if fit is not None:
                        fx, fy, fr = fit
                        if (math.hypot(fx - cx, fy - cy) < 0.35 * radius
                                and abs(fr - radius) < 0.35 * radius):
                            cx, cy, radius, refined = fx, fy, fr, True
            holes_out.append({
                'id': 'h%d' % (i + 1),
                'center_px': [round(cx, 1), round(cy, 1)],
                'center_mm': [round(cx * mm, 2), round(cy * mm, 2)],
                'diameter_mm': round(2.0 * radius * mm, 2),
                'refined': refined,
            })
            ic = (int(round(cx)), int(round(cy)))
            cv2.circle(canvas, ic, int(round(radius)), (0, 0, 230), thick)
            cv2.drawMarker(canvas, ic, (0, 0, 230), cv2.MARKER_CROSS,
                           max(8, thick * 6), thick)
            label((cx + radius, cy), 'h%d D%.2f' % (i + 1,
                                                    2.0 * radius * mm))
        if not holes_out:
            notes.append('No circles found — tune sensitivity (lower = more '
                         'hits) or the min/max diameter window.')
        if 2 <= len(holes_out) <= 12:
            for a in range(len(holes_out)):
                for b in range(a + 1, len(holes_out)):
                    pa = holes_out[a]['center_px']
                    pb = holes_out[b]['center_px']
                    pair_out.append({
                        'from': holes_out[a]['id'],
                        'to': holes_out[b]['id'],
                        'mm': round(math.hypot(pb[0] - pa[0],
                                               pb[1] - pa[1]) * mm, 2)})
        elif len(holes_out) > 12:
            notes.append('Centre-to-centre table omitted for >12 holes.')

    # A printed scale bar makes the preview self-checking.
    for bar_mm in (5, 10, 20, 50, 100, 200):
        bar_px = bar_mm / mm
        if 60 <= bar_px <= 0.5 * w_img:
            y_bar = h_img - max(15, int(round(fscale * 20)))
            cv2.line(canvas, (15, y_bar), (15 + int(round(bar_px)), y_bar),
                     (255, 160, 0), thick + 1)
            label((15, y_bar - 4), '%d mm' % bar_mm)
            break

    if not annotate_path:
        annotate_path = os.path.splitext(image_path)[0] + '_measured.png'
    if not cv2.imwrite(annotate_path, canvas):
        raise RuntimeError('Could not write %r' % annotate_path)
    result = {
        'input': image_path,
        'mm_per_px': mm,
        'annotated': annotate_path,
        'note': ('Coordinates are pixels of the input image (y down); '
                 'center_mm uses the same origin. Check the annotated '
                 'preview — every measurement is drawn where it was '
                 'taken.'),
    }
    if seg_out:
        result['segments'] = seg_out
    if holes:
        result['holes'] = holes_out
        result['hole_count'] = len(holes_out)
    if pair_out:
        result['hole_distances_mm'] = pair_out
    if notes:
        result['note'] += ' ' + ' '.join(notes)
    return result

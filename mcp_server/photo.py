"""Photo -> CAD helpers (2D): perspective rectification with a printed marker
and bitmap-to-DXF vectorisation for sketch tracing.

Everything runs in the MCP server process. The heavy dependencies are optional
(the "photo" extras: opencv-contrib-python-headless, ezdxf); every public
function raises a RuntimeError with install instructions when they are missing.

Units: photos are pixels; the bridge to millimetres is mm_per_px, established
by rectify() from a marker of known size (or supplied by the caller from a
known dimension). DXF output is millimetres, Y up (image Y is flipped).
"""
import math
import os

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


def _detect_aruco(gray, dict_name):
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
        return None, []
    found = [int(i) for i in ids.flatten()]
    # corners[i]: (1, 4, 2) float32, order TL TR BR BL.
    return corners[0].reshape(4, 2), found


def _detect_qr(gray):
    ok, points = cv2.QRCodeDetector().detect(gray)
    if not ok or points is None:
        return None
    return points.reshape(4, 2)


def rectify(image_path, out_path=None, marker='4x4_50', marker_size_mm=50.0,
            mm_per_px=0.2, margin_mm=10.0):
    """Remove perspective from a workshop photo using a printed square marker
    lying IN the plane of the part, and fix the scale: after rectification
    every pixel is exactly `mm_per_px` millimetres in that plane. marker:
    an ArUco dictionary (default 4x4_50 — print one from any generator) or
    "qr"; marker_size_mm is the printed side length. Writes the rectified
    image (default <name>_rect.png) and returns mm_per_px for
    photo_to_sketch / canvas_add."""
    _require_cv2()
    img, gray = _read_gray(image_path)
    if float(marker_size_mm) <= 0 or float(mm_per_px) <= 0:
        raise RuntimeError('marker_size_mm and mm_per_px must be positive')

    marker = (marker or '4x4_50').lower()
    if marker == 'qr':
        src, ids = _detect_qr(gray), []
        kind = 'qr'
    else:
        src, ids = _detect_aruco(gray, marker)
        kind = 'aruco:%s' % marker
    if src is None:
        raise RuntimeError(
            'No %s marker found in %r. Print a marker, lay it flat in the '
            'same plane as the part outline, and re-shoot; or pass a '
            'different `marker` dictionary.' % (kind, image_path))

    side_px = float(marker_size_mm) / float(mm_per_px)
    import numpy as np  # cv2 wheels always bundle numpy
    dst = np.array([[0, 0], [side_px, 0], [side_px, side_px], [0, side_px]],
                   dtype='float32')
    h_mat = cv2.getPerspectiveTransform(src.astype('float32'), dst)

    # Keep the WHOLE photo: shift the mapping so every warped image corner
    # (plus margin) has positive coordinates, and size the canvas to fit.
    h_img, w_img = gray.shape[:2]
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
    return {
        'input': image_path,
        'output': out_path,
        'marker': kind,
        'marker_ids': ids,
        'marker_size_mm': float(marker_size_mm),
        'mm_per_px': float(mm_per_px),
        'size_px': [out_w, out_h],
        'size_mm': [round(out_w * mm_per_px, 2), round(out_h * mm_per_px, 2)],
        'note': ('Scale is exact only in the marker plane. Next: '
                 'photo_to_sketch(mm_per_px=%g) to vectorise, or canvas_add '
                 'with width_mm=%g.' % (mm_per_px, round(out_w * mm_per_px, 2))),
    }


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

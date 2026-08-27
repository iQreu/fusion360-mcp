"""Tests for the v1.14.0 photo-RE wave: measurements off rectified photos
(photo.measure), the rectify upgrades (multi-marker refinement, known-
rectangle / known-distance fallbacks, EXIF, division-model undistortion) and
real-scale recovery for photogrammetry (RealityScan CLI marker flags +
Meshroom cameras.sfm triangulation). OpenCV-dependent tests skip cleanly
when the optional "photo" extras are missing; CI installs them so nothing
skips there."""
import math
import os
import struct

import photo
import photogrammetry
import pytest

requires_cv = pytest.mark.skipif(
    photo.cv2 is None, reason="optional 'photo' extras not installed")


# --------------------------------------------------------------------------- #
# photo._read_exif (stdlib, no cv2)
# --------------------------------------------------------------------------- #
def _mini_exif_jpeg(path):
    """Handcraft the smallest JPEG whose APP1 carries Model, FocalLength
    and FocalLengthIn35mmFilm (little-endian TIFF)."""

    def u16(v):
        return struct.pack('<H', v)

    def u32(v):
        return struct.pack('<I', v)

    model = b'Pixel 9\x00'
    tiff = b'II' + u16(0x2A) + u32(8)
    ifd0 = u16(2)
    ifd0 += u16(0x0110) + u16(2) + u32(len(model)) + u32(38)   # Model -> ptr
    ifd0 += u16(0x8769) + u16(4) + u32(1) + u32(46)            # Exif IFD
    ifd0 += u32(0)
    exif_ifd = u16(2)
    exif_ifd += u16(0x920A) + u16(5) + u32(1) + u32(76)        # FocalLength
    exif_ifd += u16(0xA405) + u16(3) + u32(1) + u16(20) + u16(0)  # 35 mm eq
    exif_ifd += u32(0)
    tiff = tiff + ifd0 + model + exif_ifd + u32(27) + u32(5)   # 27/5 = 5.4
    assert len(tiff) == 84  # layout offsets above depend on this
    payload = b'Exif\x00\x00' + tiff
    app1 = b'\xff\xe1' + struct.pack('>H', len(payload) + 2) + payload
    with open(path, 'wb') as fh:
        fh.write(b'\xff\xd8' + app1 + b'\xff\xd9')


def test_read_exif_parses_model_and_focal_lengths(tmp_path):
    path = tmp_path / 'phone.jpg'
    _mini_exif_jpeg(str(path))
    assert photo._read_exif(str(path)) == {
        'camera': 'Pixel 9', 'focal_mm': 5.4, 'focal_35mm': 20}


def test_read_exif_never_raises_on_garbage(tmp_path):
    path = tmp_path / 'not_a.jpg'
    path.write_bytes(b'\xff\xd8\xff\xe1\x00\x04ab')
    assert photo._read_exif(str(path)) == {}
    assert photo._read_exif(str(tmp_path / 'missing.jpg')) == {}


# --------------------------------------------------------------------------- #
# photo.rectify v2
# --------------------------------------------------------------------------- #
def _marker_canvas(ids_pos, size_px=150, canvas_wh=(1200, 900)):
    import numpy as np
    aruco = photo.cv2.aruco
    dictionary = aruco.getPredefinedDictionary(aruco.DICT_4X4_50)
    gen = getattr(aruco, 'generateImageMarker', None) or aruco.drawMarker
    canvas = np.full((canvas_wh[1], canvas_wh[0]), 255, dtype=np.uint8)
    for mid, (x, y) in ids_pos:
        canvas[y:y + size_px, x:x + size_px] = gen(dictionary, mid, size_px)
    return canvas


@requires_cv
def test_rectify_multi_marker_refines_and_reports(tmp_path):
    import numpy as np
    cv2 = photo.cv2
    pos = [(3, (100, 100)), (7, (900, 120)), (11, (450, 600))]
    canvas = _marker_canvas(pos)
    src = np.array([[0, 0], [1200, 0], [1200, 900], [0, 900]],
                   dtype='float32')
    dst = np.array([[60, 40], [1150, 90], [1080, 860], [30, 800]],
                   dtype='float32')
    warped = cv2.warpPerspective(
        canvas, cv2.getPerspectiveTransform(src, dst), (1200, 900),
        borderMode=cv2.BORDER_CONSTANT, borderValue=255)
    path = tmp_path / 'multi.png'
    cv2.imwrite(str(path), warped)

    rep = photo.rectify(str(path), marker_size_mm=30.0, mm_per_px=0.2)
    assert rep['marker_ids'] == [3, 7, 11]
    assert rep['marker_count'] == 3
    assert rep['scale_spread_pct'] < 1.5
    assert rep['residual_mm'] < 0.4
    # Ground truth: marker centres live on a 0.2 mm/px plane — re-detect in
    # the rectified output and check every pairwise distance in mm.
    out_gray = cv2.imread(rep['output'], cv2.IMREAD_GRAYSCALE)
    centres = {mid: quad.mean(axis=0)
               for mid, quad in photo._detect_aruco_all(out_gray, '4x4_50')}
    truth = {mid: np.array([x + 75.0, y + 75.0]) for mid, (x, y) in pos}
    assert sorted(centres) == [3, 7, 11]
    for a, b in ((3, 7), (3, 11), (7, 11)):
        got = float(np.hypot(*(centres[a] - centres[b]))) * 0.2
        want = float(np.hypot(*(truth[a] - truth[b]))) * 0.2
        assert abs(got - want) < 0.6


@requires_cv
def test_rectify_undistort_auto_recovers_k1(tmp_path):
    import numpy as np
    cv2 = photo.cv2
    canvas = _marker_canvas([(3, (80, 80)), (7, (950, 120)),
                             (11, (400, 620))])
    h_img, w_img = canvas.shape[:2]
    ys, xs = np.mgrid[0:h_img, 0:w_img]
    pts = np.stack([xs.ravel(), ys.ravel()], axis=1).astype('float64')
    # D(x) = I(undistort(x)) is EXACTLY the distortion _undistort_image
    # inverts, so a matching k1 restores the original pixel-for-pixel.
    und = photo._undistort_pts(pts, 0.12, w_img, h_img)
    distorted = cv2.remap(
        canvas, und[:, 0].reshape(h_img, w_img).astype('float32'),
        und[:, 1].reshape(h_img, w_img).astype('float32'), cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=255)
    path = tmp_path / 'distorted.png'
    cv2.imwrite(str(path), distorted)

    plain = photo.rectify(str(path), marker_size_mm=30.0, mm_per_px=0.2,
                          out_path=str(tmp_path / 'plain.png'))
    fixed = photo.rectify(str(path), marker_size_mm=30.0, mm_per_px=0.2,
                          out_path=str(tmp_path / 'fixed.png'),
                          undistort='auto')
    assert 0.04 < fixed['k1'] < 0.2
    assert fixed['residual_mm'] < plain['residual_mm'] * 0.7


@requires_cv
def test_rectify_ref_rect_known_rectangle(tmp_path):
    import numpy as np
    cv2 = photo.cv2
    img = np.full((300, 400), 255, dtype=np.uint8)
    quad = [[50, 40], [330, 60], [310, 240], [70, 220]]
    cv2.fillConvexPoly(img, np.array(quad, dtype=np.int32), 128)
    path = tmp_path / 'card.png'
    cv2.imwrite(str(path), img)

    rep = photo.rectify(str(path), ref_points=quad, ref_width_mm=200.0,
                        ref_height_mm=100.0, mm_per_px=0.5)
    assert rep['mode'] == 'ref_rect'
    out = cv2.imread(rep['output'], cv2.IMREAD_GRAYSCALE)
    # The photo/border boundary anti-aliases through the same grey band —
    # erode to keep only the solid quad blob.
    mask = ((out > 100) & (out < 200)).astype(np.uint8)
    mask = cv2.erode(mask, np.ones((5, 5), np.uint8))
    ys, xs = np.nonzero(mask)
    assert abs((xs.max() - xs.min()) - 400) <= 8   # 200 mm / 0.5 mm/px
    assert abs((ys.max() - ys.min()) - 200) <= 8   # 100 mm / 0.5 mm/px


@requires_cv
def test_rectify_scale_points_similarity_only(tmp_path):
    import numpy as np
    path = tmp_path / 'ruler.png'
    photo.cv2.imwrite(str(path), np.full((80, 200), 255, dtype=np.uint8))
    rep = photo.rectify(str(path), scale_points=[[10, 10], [110, 10]],
                        known_mm=25.0)
    assert rep['mode'] == 'scale_only'
    assert rep['mm_per_px'] == pytest.approx(0.25)
    assert rep['output'] == str(path)  # nothing was warped or written
    assert 'NO perspective correction' in rep['note']


@requires_cv
def test_rectify_single_marker_stays_compatible(tmp_path):
    import numpy as np
    canvas = np.full((600, 800), 255, dtype=np.uint8)
    canvas[200:400, 300:500] = _marker_canvas(
        [(7, (0, 0))], size_px=200, canvas_wh=(200, 200))
    path = tmp_path / 'photo.png'
    photo.cv2.imwrite(str(path), canvas)
    rep = photo.rectify(str(path), marker='4x4_50', marker_size_mm=40.0,
                        mm_per_px=0.4, margin_mm=10.0)
    assert rep['marker_ids'] == [7]
    assert rep['marker_count'] == 1
    assert 'markers' not in rep  # agreement stats need 2+ markers


# --------------------------------------------------------------------------- #
# photo.measure
# --------------------------------------------------------------------------- #
@requires_cv
def test_measure_segments_snap_and_bare_segment(tmp_path):
    import numpy as np
    img = np.full((600, 800), 255, dtype=np.uint8)
    img[100:400, 100:500] = 0  # 400 x 300 px black rectangle
    path = tmp_path / 'rect.png'
    photo.cv2.imwrite(str(path), img)

    rep = photo.measure(str(path), 0.25, segments=[[[100, 100], [500, 100]]])
    assert rep['segments'][0]['mm'] == pytest.approx(100.0, abs=0.5)
    assert os.path.exists(rep['annotated'])

    bare = photo.measure(str(path), 0.25, segments=[[100, 100], [500, 100]])
    assert bare['segments'][0]['mm'] == pytest.approx(100.0, abs=0.5)

    snapped = photo.measure(str(path), 0.25,
                            segments=[[[97, 103], [503, 97]]], snap_px=8)
    assert snapped['segments'][0].get('snapped') is True
    assert abs(snapped['segments'][0]['mm'] - 100.0) < 1.3

    with pytest.raises(RuntimeError) as err:
        photo.measure(str(path), 0.25)
    assert 'Nothing to measure' in str(err.value)


@requires_cv
def test_measure_holes_diameters_and_spacing(tmp_path):
    import numpy as np
    img = np.full((600, 800), 255, dtype=np.uint8)
    photo.cv2.circle(img, (250, 200), 30, 0, -1)
    photo.cv2.circle(img, (550, 400), 30, 0, -1)
    path = tmp_path / 'holes.png'
    photo.cv2.imwrite(str(path), img)

    rep = photo.measure(str(path), 0.25, holes=True, min_diameter_mm=10.0,
                        max_diameter_mm=20.0)
    assert rep['hole_count'] == 2
    for hole in rep['holes']:
        assert hole['diameter_mm'] == pytest.approx(15.0, abs=0.8)
    truth = math.hypot(300, 200) * 0.25
    assert len(rep['hole_distances_mm']) == 1
    assert rep['hole_distances_mm'][0]['mm'] == pytest.approx(truth, abs=0.8)
    assert os.path.exists(rep['annotated'])


# --------------------------------------------------------------------------- #
# photogrammetry: RealityScan marker flags
# --------------------------------------------------------------------------- #
def test_build_command_marker_distances_in_metres():
    cmd = photogrammetry.build_command(
        'realityscan', 'RS.exe', 'imgs', 'out.obj', 500000,
        detect_markers=True, distances=[['1x12:012', '1x12:013', 250.0]])
    i = cmd.index('-detectMarkers')
    assert cmd.index('-align') < i < cmd.index('-setReconstructionRegionAuto')
    j = cmd.index('-defineDistance')
    assert cmd[j + 1:j + 4] == ['1x12:012', '1x12:013', '0.25']
    assert '-update' in cmd


def test_build_command_default_grammar_unchanged():
    cmd = photogrammetry.build_command('realityscan', 'RS.exe', 'imgs',
                                       'out.obj', 500000)
    assert cmd == ['RS.exe', '-headless', '-addFolder', 'imgs', '-align',
                   '-setReconstructionRegionAuto', '-calculateNormalModel',
                   '-simplify', '500000', '-exportSelectedModel', 'out.obj',
                   '-quit']


def test_run_rejects_malformed_distances(tmp_path):
    with pytest.raises(RuntimeError) as err:
        photogrammetry.run(str(tmp_path), str(tmp_path / 'o.obj'),
                           distances=[['a', 'b']])
    assert 'distances items' in str(err.value)


# --------------------------------------------------------------------------- #
# photogrammetry: cameras.sfm parsing + scale recovery
# --------------------------------------------------------------------------- #
def test_parse_sfm_handles_both_focal_conventions(tmp_path):
    import json
    ident = [str(v) for v in (1, 0, 0, 0, 1, 0, 0, 0, 1)]
    sfm = {
        'views': [
            {'viewId': '10', 'poseId': '10', 'intrinsicId': '1',
             'path': '/a/img1.jpg'},
            {'viewId': '11', 'poseId': '11', 'intrinsicId': '2',
             'path': '/a/img2.jpg'},
            {'viewId': '99', 'poseId': 'missing', 'intrinsicId': '1',
             'path': '/a/img3.jpg'},
        ],
        'intrinsics': [
            {'intrinsicId': '1', 'width': '4000', 'height': '3000',
             'pxFocalLength': '2500', 'principalPoint': ['12.5', '-8'],
             'distortionParams': ['0.01', '0', '0']},
            {'intrinsicId': '2', 'width': '4000', 'height': '3000',
             'focalLength': '4.5', 'sensorWidth': '9.0',
             'principalPoint': ['2000', '1500']},
        ],
        'poses': [
            {'poseId': '10', 'pose': {'transform': {
                'rotation': ident, 'center': ['0', '0', '5']}}},
            {'poseId': '11', 'pose': {'transform': {
                'rotation': ident, 'center': ['1', '0', '5']}}},
        ],
    }
    path = tmp_path / 'cameras.sfm'
    path.write_text(json.dumps(sfm), encoding='utf-8')
    views = photogrammetry._parse_sfm(str(path))
    assert [v['viewId'] for v in views] == ['10', '11']  # orphan dropped
    assert views[0]['intr']['fx'] == 2500.0
    # principalPoint as offset-from-centre form:
    assert views[0]['intr']['pp'] == (2012.5, 1492.0)
    # focalLength(mm) + sensorWidth form: 4.5/9 * 4000 px
    assert views[1]['intr']['fx'] == pytest.approx(2000.0)
    assert views[1]['intr']['pp'] == (2000.0, 1500.0)  # absolute kept


def test_scale_from_detections_recovers_scale_in_both_conventions():
    np = pytest.importorskip('numpy')

    def rot_z(deg):
        a = math.radians(deg)
        return np.array([[math.cos(a), -math.sin(a), 0.0],
                         [math.sin(a), math.cos(a), 0.0], [0.0, 0.0, 1.0]])

    def rot_x(deg):
        a = math.radians(deg)
        return np.array([[1.0, 0.0, 0.0],
                         [0.0, math.cos(a), -math.sin(a)],
                         [0.0, math.sin(a), math.cos(a)]])

    # World: marker id 5, square side 0.4 units, on z=0. Cameras look down
    # from z~3. Rotations must NOT be symmetric (R != R^T), or both pose
    # conventions produce identical matrices and the auto-pick is a coin
    # toss — hence the extra z/x rotations.
    corners = np.array([[-0.2, -0.2, 0.0], [0.2, -0.2, 0.0],
                        [0.2, 0.2, 0.0], [-0.2, 0.2, 0.0]])
    down = np.diag([1.0, -1.0, -1.0])
    r_by_view = {'1': rot_z(5.0) @ down,
                 '2': rot_x(6.0) @ rot_z(-12.0) @ down}
    k_mat = np.array([[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0],
                      [0.0, 0.0, 1.0]])
    centres = {'1': np.array([0.0, 0.0, 3.0]),
               '2': np.array([0.9, 0.3, 3.2])}

    def project(vid, point):
        cam = r_by_view[vid] @ (point - centres[vid])
        px = k_mat @ cam
        return px[:2] / px[2]

    detections = {
        vid: [(5, np.array([project(vid, p) for p in corners]))]
        for vid in centres}
    intr = {'fx': 1000.0, 'fy': 1000.0, 'pp': (960.0, 540.0), 'dist': [],
            'wh': (1920.0, 1080.0)}
    for storage in ('w2c', 'c2w'):
        views = []
        for vid, centre in centres.items():
            stored = r_by_view[vid] if storage == 'w2c' \
                else r_by_view[vid].T
            views.append({'viewId': vid, 'path': 'img%s.jpg' % vid,
                          'pose': (list(stored.flatten()), list(centre)),
                          'intr': dict(intr)})
        stats = photogrammetry._scale_from_detections(views, detections, 50.0)
        # 50 mm printed side / 0.4 reconstruction units = 125 mm per unit.
        assert stats['scale_mm_per_unit'] == pytest.approx(125.0, abs=0.5)
        assert stats['spread_pct'] < 1.0
        assert stats['reproj_px'] < 0.5
        assert stats['convention'] == storage
        assert stats['markers_used'][0]['id'] == 5


def test_versions_in_sync_v114():
    import commands
    from _version import __version__
    assert commands.VERSION == __version__
    pyproject = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'mcp_server', 'pyproject.toml')
    with open(pyproject, encoding='utf-8') as fh:
        assert 'version = "%s"' % __version__ in fh.read()
    changelog = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'CHANGELOG.md')
    with open(changelog, encoding='utf-8') as fh:
        assert '## v%s' % __version__ in fh.read()


def test_scale_mesh_obj_text_transform_keeps_extras(tmp_path):
    src = tmp_path / 'scan.obj'
    src.write_text('# photogrammetry\n'
                   'v 1 2 3\n'
                   'v 1.5 -2 0.25 255 0 0\n'
                   'vt 0.5 0.5\n'
                   'vn 0 0 1\n'
                   'f 1/1/1 2/1/1 1/1/1\n', encoding='utf-8')
    out = tmp_path / 'scan_mm.obj'
    photogrammetry._scale_mesh(str(src), str(out), 2.0)
    lines = out.read_text(encoding='utf-8').splitlines()
    assert lines[1] == 'v 2.000000 4.000000 6.000000'
    assert lines[2] == 'v 3.000000 -4.000000 0.500000 255 0 0'
    assert lines[3] == 'vt 0.5 0.5'
    assert lines[4] == 'vn 0 0 1'
    assert lines[5] == 'f 1/1/1 2/1/1 1/1/1'

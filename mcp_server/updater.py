"""Self-update helper: check GitHub for a newer FusionMCP release and, on
explicit user consent, download and apply it.

Design notes
------------
* Stdlib only (urllib/json/zipfile/subprocess) — no extra dependencies.
* Every function returns a JSON-serialisable dict and never raises to the MCP
  layer; transport/parse failures are reported as ``{"error": ...}``.
* Version comes from GitHub Releases (tag_name); if the repo has no releases we
  fall back to reading the version out of mcp_server/pyproject.toml on the
  default branch.
* ``apply`` is gated on ``confirm=True`` (the user's consent) and prefers a
  fast-forward ``git pull`` when the install is a clean git checkout; otherwise
  it downloads the release/branch zip and overwrites the working copy.
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile

from _version import __version__ as LOCAL_VERSION

REPO = os.environ.get('FUSION_MCP_REPO', 'iQreu/fusion360-mcp')
DEFAULT_BRANCH = os.environ.get('FUSION_MCP_BRANCH', 'main')
# Startup behaviour: "download" (check + pre-download, default), "notify"
# (check only), "off" (no network at startup).
AUTO_MODE = os.environ.get('FUSION_MCP_AUTO_UPDATE', 'download').strip().lower()
_TIMEOUT = 15

# Files/dirs never overwritten by the zip updater.
_SKIP = {'.git', '.venv', 'venv', '__pycache__', '.python-version'}


def _repo_root():
    """The FusionMCP checkout root (parent of this mcp_server/ directory)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _version_on_disk(root):
    """Read __version__ straight from mcp_server/_version.py on disk (not the
    imported module, which is frozen at server startup)."""
    path = os.path.join(root, 'mcp_server', '_version.py')
    try:
        with open(path, encoding='utf-8') as fh:
            m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', fh.read())
        return m.group(1) if m else None
    except OSError:
        return None


def _sha256(blob):
    return hashlib.sha256(blob).hexdigest()


# --------------------------------------------------------------------------- #
# Version helpers
# --------------------------------------------------------------------------- #
def parse_version(text):
    """Parse "v1.2.3"/"1.2" into a comparable tuple of ints, ignoring any
    pre-release suffix. Returns () if nothing numeric is found."""
    if not text:
        return ()
    m = re.search(r'(\d+(?:\.\d+)*)', str(text))
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split('.'))


def is_newer(candidate, current):
    """True if `candidate` version string is strictly newer than `current`."""
    a, b = parse_version(candidate), parse_version(current)
    if not a:
        return False
    # Pad to equal length so (1,2) and (1,2,0) compare equal.
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _http_get(url, accept='application/vnd.github+json'):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'FusionMCP-Updater',
        'Accept': accept,
    })
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 - fixed GitHub hosts
        return resp.read()


def _latest_from_releases():
    url = 'https://api.github.com/repos/%s/releases/latest' % REPO
    data = json.loads(_http_get(url))
    tag = data.get('tag_name')
    if not tag:
        return None
    return {
        'version': tag,
        'source': 'release',
        'url': data.get('html_url'),
        'notes': (data.get('body') or '').strip()[:2000] or None,
        'zip_url': data.get('zipball_url'),
    }


def _latest_from_pyproject():
    url = ('https://raw.githubusercontent.com/%s/%s/mcp_server/pyproject.toml'
           % (REPO, DEFAULT_BRANCH))
    text = _http_get(url, accept='text/plain').decode('utf-8', 'replace')
    m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not m:
        return None
    return {
        'version': m.group(1),
        'source': 'pyproject@%s' % DEFAULT_BRANCH,
        'url': 'https://github.com/%s' % REPO,
        'notes': None,
        'zip_url': 'https://github.com/%s/archive/refs/heads/%s.zip'
                   % (REPO, DEFAULT_BRANCH),
    }


def latest():
    """Best-effort lookup of the latest published version."""
    try:
        rel = _latest_from_releases()
        if rel:
            return rel
    except urllib.error.HTTPError as exc:
        if exc.code != 404:  # 404 just means "no releases yet"
            return {'error': 'GitHub releases lookup failed: %s' % exc}
    except Exception as exc:  # noqa: BLE001 - network/parse errors reported, not raised
        return {'error': 'GitHub releases lookup failed: %s' % exc}
    try:
        pp = _latest_from_pyproject()
        if pp:
            return pp
        return {'error': 'Could not determine the latest version from GitHub.'}
    except Exception as exc:  # noqa: BLE001
        return {'error': 'GitHub version lookup failed: %s' % exc}


# --------------------------------------------------------------------------- #
# Public: check
# --------------------------------------------------------------------------- #
def check():
    """Compare the installed version with the latest on GitHub."""
    info = latest()
    if info.get('error'):
        return {'current_version': LOCAL_VERSION, 'error': info['error']}
    newer = is_newer(info['version'], LOCAL_VERSION)
    out = {
        'current_version': LOCAL_VERSION,
        'latest_version': info['version'],
        'update_available': newer,
        'source': info['source'],
        'url': info.get('url'),
        'release_notes': info.get('notes'),
        'repo': REPO,
        'message': ('Update available: %s -> %s. Ask the user before calling '
                    'apply_update(confirm=True).' % (LOCAL_VERSION, info['version']))
        if newer else 'FusionMCP is up to date.',
    }
    if newer:
        staged = staged_zip_path(info['version'])
        if os.path.isfile(staged):
            out['downloaded'] = staged
    return out


# --------------------------------------------------------------------------- #
# Startup auto-check: check GitHub once in the background, pre-download the
# new version into a staging area, and expose a one-shot notice (version +
# release notes) that the server attaches to the first tool result. Installing
# still requires the user's consent via apply_update(confirm=True).
# --------------------------------------------------------------------------- #
_pending_lock = threading.Lock()
_pending = None      # result of the startup check once the thread finishes
_announced = False   # the one-shot notice was already delivered


def staging_dir():
    return os.path.join(tempfile.gettempdir(), 'FusionMCP', 'updates')


def staged_zip_path(version):
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', str(version))
    return os.path.join(staging_dir(), 'fusionmcp-%s.zip' % safe)


def download_to_staging(info):
    """Pre-download the release zip for `info` so a later apply_update is
    instant and works offline. Returns the staged path, or None on failure.
    Writes a .sha256 sidecar so apply_update can detect a staged file that was
    corrupted or tampered with (a different process planting a zip) between
    pre-download and install."""
    zip_url = info.get('zip_url')
    if not zip_url:
        return None
    path = staged_zip_path(info['version'])
    if os.path.isfile(path) and os.path.getsize(path) > 0 and _staged_ok(path):
        return path
    try:
        blob = _http_get(zip_url, accept='application/zip')
        # Reject a non-zip / truncated download before promoting it.
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            if zf.testzip() is not None:
                return None
        os.makedirs(staging_dir(), exist_ok=True)
        # Unique temp name so two concurrent server instances don't interleave
        # writes into the same file and promote a spliced, corrupt zip.
        tmp = '%s.part.%d' % (path, os.getpid())
        with open(tmp, 'wb') as fh:
            fh.write(blob)
        os.replace(tmp, path)
        with open(path + '.sha256', 'w', encoding='utf-8') as fh:
            fh.write(_sha256(blob))
        return path
    except Exception:  # noqa: BLE001 - pre-download is best-effort
        return None


def _staged_ok(path):
    """True if a staged zip matches its recorded digest and is a valid archive."""
    try:
        with open(path + '.sha256', encoding='utf-8') as fh:
            expected = fh.read().strip()
        with open(path, 'rb') as fh:
            blob = fh.read()
        if not expected or _sha256(blob) != expected:
            return False
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            return zf.testzip() is None
    except Exception:  # noqa: BLE001
        return False


def _background_check():
    global _pending
    result = {'update_available': False, 'current_version': LOCAL_VERSION}
    try:
        info = latest()
        if info.get('error'):
            result['error'] = info['error']
        elif is_newer(info['version'], LOCAL_VERSION):
            result.update({
                'update_available': True,
                'latest_version': info['version'],
                'release_notes': info.get('notes'),
                'url': info.get('url'),
                'source': info.get('source'),
            })
            if AUTO_MODE == 'download':
                root = _repo_root()
                if os.path.isdir(os.path.join(root, '.git')):
                    fetch = _git(root, 'fetch', '--tags', '--quiet')
                    # Only claim "downloaded" if the fetch actually succeeded —
                    # else consume_notice would wrongly tell the user it is ready.
                    if getattr(fetch, 'returncode', 1) == 0:
                        result['downloaded'] = 'git fetch (apply runs git pull --ff-only)'
                else:
                    result['downloaded'] = download_to_staging(info)
    except Exception as exc:  # noqa: BLE001 - never break the server at startup
        result['error'] = 'startup update check failed: %s' % exc
    with _pending_lock:
        _pending = result


def start_background_check():
    """Kick off the startup update check without blocking server startup.
    Honours FUSION_MCP_AUTO_UPDATE=off. Returns the thread (or None)."""
    if AUTO_MODE == 'off':
        return None
    t = threading.Thread(target=_background_check,
                         name='fusionmcp-update-check', daemon=True)
    t.start()
    return t


def consume_notice():
    """One-shot pending-update notice for the first tool result of a session,
    or None. Includes the release notes so the user sees what changed."""
    global _announced
    with _pending_lock:
        if _announced or not _pending or not _pending.get('update_available'):
            return None
        _announced = True
        notice = dict(_pending)
    notice['message'] = (
        'FusionMCP %s is available (installed: %s)%s. Tell the user, show the '
        'release notes above, and ask whether to install now with '
        'apply_update(confirm=True). They must restart the Fusion add-in and '
        'Claude Desktop afterwards.'
        % (notice.get('latest_version'), LOCAL_VERSION,
           '; it is already downloaded' if notice.get('downloaded') else ''))
    return notice


# --------------------------------------------------------------------------- #
# Public: apply (requires consent)
# --------------------------------------------------------------------------- #
_NEXT_STEPS = [
    'Restart the FusionMCP add-in in Fusion (Shift+S -> Add-Ins -> Stop, then Run).',
    'Restart Claude Desktop so the MCP server reloads.',
]


def _git(root, *args):
    return subprocess.run(['git', '-C', root, *args],
                          capture_output=True, text=True, timeout=120)


def _apply_git(root):
    dirty = _git(root, 'status', '--porcelain')
    if dirty.stdout.strip():
        return {'applied': False, 'method': 'git',
                'reason': 'The install has uncommitted local changes. Commit/stash '
                          'them or call apply_update(method="zip") to overwrite.'}
    _git(root, 'fetch', '--tags', '--quiet')
    pull = _git(root, 'pull', '--ff-only')
    out = (pull.stdout + pull.stderr).strip()
    if pull.returncode != 0:
        return {'applied': False, 'method': 'git', 'reason': out or 'git pull failed'}
    _sync_addin(root)
    # git pull rewrote _version.py on disk; report the NEW version, not the
    # value captured in memory at server startup.
    new_version = _version_on_disk(root) or LOCAL_VERSION
    return {'applied': True, 'method': 'git', 'output': out,
            'new_version': new_version, 'next_steps': _NEXT_STEPS}


def _apply_zip(root):
    info = latest()
    if info.get('error'):
        return {'applied': False, 'method': 'zip', 'reason': info['error']}
    zip_url = info.get('zip_url')
    if not zip_url:
        return {'applied': False, 'method': 'zip', 'reason': 'No download URL available'}
    def _clear_staged():
        for leftover in (staged, staged + '.sha256'):
            try:
                os.remove(leftover)
            except OSError:
                pass

    staged = staged_zip_path(info['version'])
    # Trust the staged file only if it still matches the digest recorded at
    # download time — otherwise a corrupted or planted zip would be extracted.
    from_staging = (os.path.isfile(staged) and os.path.getsize(staged) > 0
                    and _staged_ok(staged))
    if from_staging:
        with open(staged, 'rb') as fh:
            blob = fh.read()
    else:
        # A stale/corrupt/mismatched staged file is worthless — remove it and
        # its sidecar so we don't re-validate-and-reject it on every apply.
        if os.path.isfile(staged):
            _clear_staged()
        try:
            blob = _http_get(zip_url, accept='application/zip')
        except Exception as exc:  # noqa: BLE001
            return {'applied': False, 'method': 'zip', 'reason': 'Download failed: %s' % exc}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            if zf.testzip() is not None:
                return {'applied': False, 'method': 'zip',
                        'reason': 'Update archive is corrupt (failed CRC check).'}
    except zipfile.BadZipFile:
        return {'applied': False, 'method': 'zip',
                'reason': 'Downloaded file is not a valid zip archive.'}
    written = _extract_over(blob, root)
    _sync_addin(root)
    _clear_staged()
    return {'applied': True, 'method': 'zip', 'files_updated': written,
            'new_version': info['version'], 'from_staging': from_staging,
            'next_steps': _NEXT_STEPS}


def _extract_over(blob, root):
    """Extract a GitHub zip (single top-level dir) over `root`, skipping _SKIP.
    Guards against zip-slip: any entry whose resolved path escapes `root` (via
    '..' or absolute components) is refused, so a crafted archive cannot write
    outside the install."""
    written = 0
    root_real = os.path.realpath(root)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
        top = names[0].split('/', 1)[0] + '/' if names else ''
        for name in names:
            if name.endswith('/'):
                continue
            rel = name[len(top):] if name.startswith(top) else name
            parts = rel.split('/')
            if not rel or any(part in _SKIP for part in parts):
                continue
            if any(part in ('..', '') for part in parts) or os.path.isabs(rel):
                raise RuntimeError('Refusing unsafe archive path %r (zip-slip).' % name)
            dest = os.path.join(root, *parts)
            if os.path.realpath(dest) != root_real and \
                    not os.path.realpath(dest).startswith(root_real + os.sep):
                raise RuntimeError('Refusing archive path %r outside the install '
                                   'root (zip-slip).' % name)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(name) as src, open(dest, 'wb') as out:
                shutil.copyfileobj(src, out)
            written += 1
    return written


def _addin_target():
    """The Fusion AddIns location for the FusionMCP add-in, or None."""
    appdata = os.environ.get('APPDATA')
    if not appdata:
        return None
    return os.path.join(appdata, 'Autodesk', 'Autodesk Fusion 360', 'API',
                        'AddIns', 'FusionMCP')


def _sync_addin(root):
    """Copy the freshly updated add-in into Fusion's AddIns folder if present."""
    target = _addin_target()
    src = os.path.join(root, 'fusion_addin', 'FusionMCP')
    if not target or not os.path.isdir(src) or not os.path.isdir(os.path.dirname(target)):
        return False
    try:
        for entry in os.listdir(src):
            if entry in _SKIP:
                continue
            s = os.path.join(src, entry)
            d = os.path.join(target, entry)
            if os.path.isfile(s):
                os.makedirs(target, exist_ok=True)
                shutil.copy2(s, d)
        return True
    except Exception:  # noqa: BLE001 - add-in sync is best-effort
        return False


def apply(confirm=False, method='auto'):
    """Download and apply the latest version. Requires confirm=True (the user's
    consent). method: auto|git|zip."""
    if not confirm:
        return {'applied': False,
                'reason': 'Confirmation required. Show the user check_for_updates() '
                          'first and only call apply_update(confirm=True) once they '
                          'agree.'}
    root = _repo_root()
    has_git = os.path.isdir(os.path.join(root, '.git'))
    method = (method or 'auto').lower()
    if method == 'git' or (method == 'auto' and has_git):
        if not has_git:
            return {'applied': False, 'reason': 'Not a git checkout; use method="zip".'}
        return _apply_git(root)
    if method in ('zip', 'auto'):
        return _apply_zip(root)
    return {'applied': False, 'reason': 'method must be auto|git|zip, got %r' % method}

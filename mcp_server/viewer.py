"""The MCP Apps viewer panel: a self-contained HTML app rendered by MCP-Apps-
capable clients (Claude Desktop and others) in a sandboxed iframe next to the
chat.

Kept in its own module so tests can check the app without importing the MCP
SDK. Protocol (MCP Apps spec 2026-01-26): the iframe talks JSON-RPC 2.0 over
postMessage — `ui/initialize` handshake, then `tools/call` requests back
through the host; the host may also push `ui/notifications/tool-result`.
"""

VIEWER_URI = 'ui://fusionmcp/viewer'
VIEWER_MIME = 'text/html;profile=mcp-app'

VIEWER_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; font: 13px system-ui, sans-serif;
         background: Canvas; color: CanvasText; }
  #bar { display: flex; flex-wrap: wrap; gap: 4px; padding: 8px; }
  button { font: inherit; padding: 4px 10px; border-radius: 6px;
           border: 1px solid color-mix(in srgb, CanvasText 25%, transparent);
           background: transparent; color: inherit; cursor: pointer; }
  button:hover { background: color-mix(in srgb, CanvasText 10%, transparent); }
  #view { display: block; width: 100%; height: auto; min-height: 200px; }
  #status { padding: 4px 8px; opacity: 0.7; min-height: 1.2em; }
  table { border-collapse: collapse; margin: 8px; }
  td, th { border: 1px solid color-mix(in srgb, CanvasText 25%, transparent);
           padding: 2px 8px; text-align: left; }
</style>
</head>
<body>
<div id="bar">
  <button data-dir="iso">Iso</button>
  <button data-dir="front">Front</button>
  <button data-dir="back">Back</button>
  <button data-dir="top">Top</button>
  <button data-dir="left">Left</button>
  <button data-dir="right">Right</button>
  <button id="fit">Fit</button>
  <button id="bom">BOM</button>
</div>
<div id="status"></div>
<img id="view" alt="Fusion viewport">
<div id="panel"></div>
<script>
(function () {
  'use strict';
  var nextId = 1;
  var pending = {};
  var img = document.getElementById('view');
  var statusEl = document.getElementById('status');
  var panel = document.getElementById('panel');

  function send(msg) { window.parent.postMessage(msg, '*'); }
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
               '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function request(method, params) {
    return new Promise(function (resolve, reject) {
      var id = nextId++;
      pending[id] = { resolve: resolve, reject: reject };
      send({ jsonrpc: '2.0', id: id, method: method, params: params || {} });
    });
  }
  function toolError(r) {
    // FastMCP reports a failed tool as a result with isError:true (relayed to
    // the .then branch), or the add-in returns {error: ...} in the payload.
    if (!r) return null;
    if (r.isError) {
      var c = r.content || [];
      for (var i = 0; i < c.length; i++) {
        if (c[i].type === 'text') return c[i].text;
      }
      return 'tool error';
    }
    var sc = r.structuredContent;
    if (sc && sc.error) return sc.error;
    return null;
  }
  window.addEventListener('message', function (ev) {
    // Only trust messages from the host frame — otherwise any window with a
    // reference to this iframe could spoof tool results or resolve requests.
    if (ev.source !== window.parent) return;
    var m = ev.data;
    if (!m || m.jsonrpc !== '2.0') return;
    if (m.id !== undefined && (m.result !== undefined || m.error !== undefined)) {
      var p = pending[m.id];
      if (p) { delete pending[m.id]; m.error ? p.reject(m.error) : p.resolve(m.result); }
      return;
    }
    if (m.method === 'ui/notifications/tool-result' && m.params) {
      showImage(m.params.result || m.params);
    }
  });

  function status(text) { statusEl.textContent = text || ''; }
  function callTool(name, args) {
    return request('tools/call', { name: name, arguments: args || {} });
  }
  function showImage(result) {
    var content = (result && result.content) || [];
    for (var i = 0; i < content.length; i++) {
      if (content[i].type === 'image') {
        img.src = 'data:' + (content[i].mimeType || 'image/png') +
                  ';base64,' + content[i].data;
        return true;
      }
    }
    return false;
  }
  function refresh(direction) {
    status('Rendering ' + direction + '\\u2026');
    callTool('screenshot', { direction: direction, fit: true,
                             width: 800, height: 500 })
      .then(function (r) {
        var err = toolError(r);
        if (err) { status('Error: ' + err); return; }
        if (!showImage(r)) { status('No image returned (is Fusion running?)'); return; }
        status('');
      })
      .catch(function (e) {
        status('Error: ' + (e && e.message ? e.message : JSON.stringify(e)));
      });
  }
  function loadBom() {
    status('Loading BOM\\u2026');
    callTool('bom', { include_mass: true })
      .then(function (r) {
        var err = toolError(r);
        if (err) { status('Error: ' + err); return; }
        var data = r && r.structuredContent;
        if (!data && r && r.content && r.content[0] && r.content[0].type === 'text') {
          try { data = JSON.parse(r.content[0].text); } catch (e) { data = null; }
        }
        renderBom(data);
        status('');
      })
      .catch(function (e) {
        status('Error: ' + (e && e.message ? e.message : JSON.stringify(e)));
      });
  }
  function renderBom(data) {
    var items = (data && data.items) || [];
    // esc() every design-derived field: component/material names can contain
    // HTML metacharacters, and this panel can issue tool calls (XSS = tool RCE).
    var html = '<table><tr><th>Component</th><th>Qty</th>' +
               '<th>Material</th><th>Mass [kg]</th></tr>';
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      html += '<tr><td>' + esc(it.component) + '</td><td>' +
              esc(it.quantity) + '</td><td>' +
              esc((it.materials || []).join('; ')) + '</td><td>' +
              esc(it.unit_mass_kg != null ? it.unit_mass_kg : '') + '</td></tr>';
    }
    panel.innerHTML = html + '</table>';
  }

  document.getElementById('bar').addEventListener('click', function (ev) {
    var dir = ev.target.getAttribute && ev.target.getAttribute('data-dir');
    if (dir) refresh(dir);
  });
  document.getElementById('fit').addEventListener('click', function () {
    callTool('fit_view', {}).then(function () { refresh('current'); });
  });
  document.getElementById('bom').addEventListener('click', loadBom);

  request('ui/initialize', { protocolVersion: '2026-01-26' })
    .then(function () {
      send({ jsonrpc: '2.0', method: 'ui/notifications/initialized' });
      refresh('iso');
    })
    .catch(function () { status('Host did not accept ui/initialize'); });
})();
</script>
</body>
</html>
"""

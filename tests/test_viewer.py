"""Tests for the MCP Apps viewer panel: the HTML must speak the MCP Apps
protocol (spec 2026-01-26) and stay self-contained (sandboxed iframe, no
external requests)."""
import re

import viewer


def test_constants_follow_the_spec():
    assert viewer.VIEWER_URI.startswith('ui://')
    assert viewer.VIEWER_MIME == 'text/html;profile=mcp-app'


def test_html_speaks_the_mcp_apps_protocol():
    html = viewer.VIEWER_HTML
    assert "request('ui/initialize'" in html
    assert "'ui/notifications/initialized'" in html
    assert "'ui/notifications/tool-result'" in html
    assert "request('tools/call'" in html
    assert 'postMessage' in html


def test_html_is_self_contained():
    html = viewer.VIEWER_HTML
    assert html.lstrip().startswith('<!doctype html>')
    # No external fetches — a sandboxed iframe cannot load them anyway.
    assert not re.search(r'src="https?://', html)
    assert not re.search(r'href="https?://', html)
    assert 'fetch(' not in html


def test_html_wires_the_expected_tools():
    html = viewer.VIEWER_HTML
    for tool in ('screenshot', 'bom', 'fit_view'):
        assert "'%s'" % tool in html

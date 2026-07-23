"""Import smoke test for the MCP server entry point.

server.py registers ~120 FastMCP tools at import time; an un-schematizable
return annotation or a bad decorator crashes that registration and takes the
whole server down at startup (this exact class of failure shipped once, commit
16aa6b2, while pytest + ruff stayed green because nothing imported server.py).

This test needs the real `mcp` SDK, so it is skipped where the SDK is absent
(local dev without extras) and runs in CI, which installs it. It must NOT use
the fake-adsk conftest shim indirectly — server.py does not import adsk.
"""
import pytest

pytest.importorskip('mcp', reason='MCP SDK not installed (install to run the '
                                   'server-import smoke test)')


def test_server_imports_and_registers_tools():
    import asyncio

    import server

    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    # A representative spread across stages — catches a decorator/annotation
    # regression that would drop tools or crash registration entirely.
    for expected in ('get_state', 'extrude', 'export', 'run_fusion_code',
                     'mesh_compare', 'fold', 'configurations', 'print_check',
                     'list_materials', 'contact_set', 'version_history'):
        assert expected in names, 'missing tool: %s' % expected
    # Sanity floor: the server exposes well over a hundred tools.
    assert len(names) >= 110, 'only %d tools registered' % len(names)

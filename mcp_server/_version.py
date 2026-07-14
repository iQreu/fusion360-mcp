"""Single source of truth for the FusionMCP server package version.

Keep this in sync with mcp_server/pyproject.toml and the add-in's VERSION in
fusion_addin/FusionMCP/commands.py. The updater compares this against the latest
version published on GitHub.
"""
__version__ = '1.7.0'

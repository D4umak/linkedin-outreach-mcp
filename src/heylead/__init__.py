"""HeyLead — MCP-native autonomous LinkedIn SDR."""

from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("heylead")
except Exception:
    __version__ = "0.0.0-dev"

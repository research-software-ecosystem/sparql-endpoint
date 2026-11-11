"""Unit tests: run with `python -m unittest` from the mcp/ directory."""

import logging
import os

import fastmcp  # noqa: F401  (configures its logging on import, so import it first)

# Keep the output readable: the server logs data loading, and fastmcp logs
# the errors that some tests provoke on purpose.
# (RSEC_LOG_LEVEL=INFO python -m unittest shows the server logs again.)
os.environ.setdefault("RSEC_LOG_LEVEL", "WARNING")
logging.getLogger("fastmcp").setLevel(logging.CRITICAL)

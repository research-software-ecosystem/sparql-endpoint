"""Shared helpers for the unit tests (standard library unittest only)."""

import functools
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


@functools.cache
def mini_graph():
    """EDAM subset + a few RSEc records, parsed once for the whole test run."""
    from rdflib import Graph

    g = Graph()
    g.parse(FIXTURES / "edam_mini.ttl", format="turtle")
    g.parse(FIXTURES / "rsec_mini.ttl", format="turtle")
    return g


class TempDirMixin:
    def make_temp_dir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

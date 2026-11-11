"""Shared helpers for the unit tests (standard library unittest only)."""

import functools
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import graph_store
import server

FIXTURES = Path(__file__).parent / "fixtures"

SAMPLE_TTL = """
@prefix schema: <http://schema.org/> .
<http://x/blast> a schema:SoftwareApplication ; schema:identifier "https://bio.tools/blast" .
<http://x/samtools> a schema:SoftwareApplication ; schema:identifier <https://bio.tools/samtools> .
<http://x/other> a schema:SoftwareApplication ; schema:identifier "https://example.org/other" .
"""


@functools.cache
def mini_graph():
    """EDAM subset + a few RSEc records, parsed once for the whole test run."""
    from rdflib import Graph

    g = Graph()
    g.parse(FIXTURES / "edam_mini.ttl", format="turtle")
    g.parse(FIXTURES / "rsec_mini.ttl", format="turtle")
    return g


def mini_knowledge_graph():
    import knowledge_graph

    return knowledge_graph.KnowledgeGraph(mini_graph())


class _MiniStore:
    """Serves the mini graph (for MCP-level tests of the search/composition tools)."""

    data_dir = FIXTURES

    async def get(self):
        return mini_graph()


class TempDirMixin:
    def make_temp_dir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)


class ServerTestCase(TempDirMixin, unittest.IsolatedAsyncioTestCase):
    """Base class for tests that use the server without network or the real dumps.

    The server reads a tiny local graph (three records, exposed as `sample_store`);
    `use_store` swaps in another store for the duration of the test.
    """

    def use_store(self, store) -> None:
        patcher = mock.patch.object(server, "store", store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def setUp(self) -> None:
        self.data_dir = self.make_temp_dir()
        (self.data_dir / "sample.ttl").write_text(SAMPLE_TTL)
        self.sample_store = graph_store.GraphStore(
            self.data_dir,
            sources=(graph_store.Source("sample.ttl", "http://unused/sample.ttl"),),
        )
        self.use_store(self.sample_store)


class MiniServerTestCase(ServerTestCase):
    """Same, with the server reading the mini graph."""

    def setUp(self) -> None:
        super().setUp()
        self.use_store(_MiniStore())

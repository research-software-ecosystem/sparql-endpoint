import threading
import unittest

import httpx
from rdflib import Graph

import graph_store
from tests.support import TempDirMixin

SRC = graph_store.Source


def _src(name: str, base: str = "https://example.org/d") -> graph_store.Source:
    return SRC(name, f"{base}/{name}")


class EnsureDumpsTests(TempDirMixin, unittest.TestCase):
    def test_downloads_only_missing_files(self):
        tmp_path = self.make_temp_dir()
        (tmp_path / "a.ttl").write_text("# already here")
        requested = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            return httpx.Response(200, text="# fresh")

        paths = graph_store.ensure_dumps(
            tmp_path,
            sources=(_src("a.ttl"), _src("b.ttl")),
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(requested, ["/d/b.ttl"])
        self.assertEqual([p.name for p in paths], ["a.ttl", "b.ttl"])
        self.assertEqual((tmp_path / "a.ttl").read_text(), "# already here")
        self.assertEqual((tmp_path / "b.ttl").read_text(), "# fresh")
        self.assertEqual(list(tmp_path.glob("*.part")), [])

    def test_keeps_no_partial_file_on_failure(self):
        tmp_path = self.make_temp_dir()
        transport = httpx.MockTransport(lambda r: httpx.Response(404))
        with self.assertRaises(httpx.HTTPStatusError):
            graph_store.ensure_dumps(tmp_path / "dl", sources=(_src("a.ttl"),), transport=transport)
        self.assertEqual(list((tmp_path / "dl").iterdir()), [])


class SourcesTests(TempDirMixin, unittest.TestCase):
    def test_load_graph_uses_each_sources_format(self):
        tmp_path = self.make_temp_dir()
        (tmp_path / "a.ttl").write_text("<http://x/s> <http://x/p> <http://x/o> .")
        (tmp_path / "b.rdf").write_text(
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="http://x/s2"><rdf:type rdf:resource="http://x/T"/>'
            "</rdf:Description></rdf:RDF>"
        )
        sources = (_src("a.ttl"), SRC("b.rdf", "https://example.org/b.rdf", "xml"))
        g = graph_store.load_graph([tmp_path / "a.ttl", tmp_path / "b.rdf"], sources)
        self.assertEqual(len(g), 2)


class GraphStoreTests(TempDirMixin, unittest.IsolatedAsyncioTestCase):
    async def test_retries_after_failed_load(self):
        tmp_path = self.make_temp_dir()
        transport = httpx.MockTransport(lambda r: httpx.Response(503))
        store = graph_store.GraphStore(tmp_path, sources=(_src("a.ttl"),), transport=transport)
        with self.assertRaises(httpx.HTTPStatusError):
            await store.get()
        (tmp_path / "a.ttl").write_text("<http://x/s> <http://x/p> <http://x/o> .")
        self.assertEqual(len(await store.get()), 1)

class RunQueryTests(unittest.TestCase):
    def setUp(self):
        self.g = Graph().parse(data="<http://x/s> <http://x/p> <http://x/o> .", format="turtle")

    def test_concurrent_queries_do_not_crash(self):
        errors = []

        def work():
            try:
                for _ in range(30):
                    graph_store.run_query(self.g, "SELECT ?s ?o WHERE { ?s <http://x/p> ?o } ORDER BY ?s")
            except Exception as e:  # only on regression
                errors.append(e)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()

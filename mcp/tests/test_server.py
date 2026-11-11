"""MCP-level tests of server.py: one SPARQL query per tool (mini graph, no network)."""

import unittest
from unittest import mock

from fastmcp import Client
from fastmcp.exceptions import ToolError

import server
from tests.support import FIXTURES, mini_graph

TOOLS = {
    "resolve_edam_term",
    "get_direct_term_parents",
    "get_direct_term_child",
    "search_software_by_topic",
    "search_software_by_operation",
    "get_license",
    "get_software_io",
    "describe_software",
}


class _MiniStore:
    data_dir = FIXTURES

    async def get(self):
        return mini_graph()


class ServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.object(server, "store", _MiniStore())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def call(self, name, args):
        async with Client(server.mcp) as c:
            return (await c.call_tool(name, args)).data

    async def fails(self, name, args, message):
        with self.assertRaises(ToolError) as ctx:
            await self.call(name, args)
        self.assertIn(message, str(ctx.exception))

    async def test_tools_are_registered_and_read_only(self):
        async with Client(server.mcp) as c:
            tools = await c.list_tools()
        self.assertEqual({t.name for t in tools}, TOOLS)
        self.assertTrue(all(t.annotations.read_only_hint for t in tools))

    # ---- EDAM

    async def test_resolve_edam_term_ranks_and_filters_by_kind(self):
        res = await self.call("resolve_edam_term", {"text": "sequence alignment"})
        self.assertEqual([r["id"] for r in res["results"]], ["data_0863", "operation_0292"])
        self.assertEqual(res["results"][1]["kind"], "operation")
        self.assertEqual(res["results"][1]["definition"], "Arrange sequences to identify similarity.")
        res = await self.call("resolve_edam_term", {"text": "sequence alignment", "kind": "operation"})
        self.assertEqual([r["id"] for r in res["results"]], ["operation_0292"])

    async def test_resolve_edam_term_finds_synonyms_and_definitions(self):
        res = await self.call("resolve_edam_term", {"text": "read mapping"})
        self.assertEqual([(r["id"], r["matched"]) for r in res["results"]], [("topic_0102", "synonym")])
        res = await self.call("resolve_edam_term", {"text": "similarity"})
        self.assertEqual([(r["id"], r["matched"]) for r in res["results"]], [("operation_0292", "definition")])

    async def test_resolve_edam_term_matches_whole_words_only(self):
        res = await self.call("resolve_edam_term", {"text": "sequences", "kind": "data"})
        self.assertEqual(res["results"], [])  # "Sequence" is not "sequences"

    async def test_resolve_edam_term_needs_a_word(self):
        await self.fails("resolve_edam_term", {"text": "  !! "}, "at least one word")

    async def test_direct_parents(self):
        res = await self.call("get_direct_term_parents", {"term": "edam:topic_0102"})
        self.assertEqual(res["term"], {"id": "topic_0102", "label": "Mapping"})
        self.assertEqual(res["parents"], [{"id": "topic_0080", "label": "Sequence analysis"}])
        self.assertEqual((await self.call("get_direct_term_parents", {"term": "topic_0091"}))["parents"], [])

    async def test_direct_children(self):
        res = await self.call("get_direct_term_child", {"term": "topic_0091"})
        self.assertEqual([c["id"] for c in res["children"]], ["topic_0003", "topic_0080"])  # direct only
        self.assertEqual((await self.call("get_direct_term_child", {"term": "topic_0102"}))["children"], [])

    async def test_unknown_term_is_an_error_not_an_empty_list(self):
        for tool in ("get_direct_term_parents", "get_direct_term_child"):
            with self.subTest(tool=tool):
                await self.fails(tool, {"term": "topic_9999"}, "not a term of the EDAM ontology")

    # ---- search

    async def test_search_by_topic_includes_narrower_topics(self):
        res = await self.call("search_software_by_topic", {"topic": "topic_0080"})
        self.assertEqual(
            [(r["biotools_id"], r["name"]) for r in res["results"]],
            [("alnviewer", "AlnViewer"), ("blast", "BLAST"), ("clustal", "Clustal Omega"), ("protfold", "ProtFold")],
        )  # protfold has topic_0102, narrower than topic_0080; the Galaxy record is not a bio.tools tool
        self.assertEqual(res["count"], 4)

    async def test_search_paging(self):
        page = await self.call("search_software_by_topic", {"topic": "topic_0080", "limit": 1, "offset": 1})
        self.assertEqual([r["biotools_id"] for r in page["results"]], ["blast"])
        self.assertEqual((page["limit"], page["offset"], page["count"]), (1, 1, 1))

    async def test_search_by_operation(self):
        res = await self.call("search_software_by_operation", {"operation": "operation_0292"})
        self.assertEqual([r["biotools_id"] for r in res["results"]], ["blast", "clustal"])

    async def test_wrong_kind_or_malformed_id_is_rejected_before_any_query(self):
        for tool, arg, value in [
            ("search_software_by_topic", "topic", "operation_0292"),
            ("search_software_by_operation", "operation", "topic_0080"),
            ("get_direct_term_parents", "term", 'topic_0080> } # '),
            ("get_license", "tool", 'x" } injected'),
            ("get_software_io", "tool", "a b"),
        ]:
            with self.subTest(tool=tool, value=value):
                with mock.patch.object(server, "run_select") as run:
                    with self.assertRaises(ToolError):
                        await self.call(tool, {arg: value})
                    run.assert_not_called()

    # ---- the queries

    def test_every_query_is_a_self_contained_constant_that_parses(self):
        from rdflib.plugins.sparql import prepareQuery

        queries = {n: q for n, q in vars(server).items() if n.startswith("q_")}
        self.assertEqual(len(queries), 8)
        values = {"term": "<http://edamontology.org/topic_0080>", "tool": "<https://bio.tools/blast>",
                  "name": '"x"', "iri": '"https://bio.tools/x"', "exact": '"x"', "pattern": '"x"', "prefix": '"http://edamontology.org/"',
                  "limit": 5, "offset": 0}
        for name, query in queries.items():
            with self.subTest(query=name):
                self.assertIn("PREFIX", query)
                prepareQuery(server.render(query, **values))

    def test_render_refuses_a_missing_value(self):
        with self.assertRaises(KeyError):
            server.render(server.q_license)

    # ---- one tool

    async def test_get_license(self):
        cases = {
            "blast": ["MIT"],
            "alnviewer": ["gpl-3.0"],
            "seqstats": ["Not licensed"],
            "noio": ["Apache-2.0"],  # from the linked biocontainers record
            "anyviewer": [],  # a known tool without license is not an unknown tool
        }
        for tool, expected in cases.items():
            with self.subTest(tool=tool):
                res = await self.call("get_license", {"tool": tool})
                self.assertEqual(res["licenses"], expected)
        res = await self.call("get_license", {"tool": "https://bio.tools/blast"})
        self.assertEqual((res["biotools_id"], res["name"]), ("blast", "BLAST"))

    async def test_get_license_unknown_tool(self):
        await self.fails("get_license", {"tool": "nope"}, "Unknown bio.tools id")
        await self.fails("get_license", {"tool": "BLAST"}, "case-sensitive")

    async def test_get_software_io(self):
        res = await self.call("get_software_io", {"tool": "blast"})
        self.assertEqual(res["name"], "BLAST")
        self.assertEqual(res["inputs"], [{
            "name": "Query",
            "data": {"id": "data_2044", "label": "Sequence"},
            "formats": [{"id": "format_1929", "label": "FASTA"}],
        }])
        self.assertEqual(res["outputs"][0]["data"], {"id": "data_0863", "label": "Sequence alignment"})
        self.assertEqual(res["outputs"][0]["formats"], [{"id": "format_1982", "label": "ClustalW format"}])
        self.assertNotIn("note", res)

    async def test_get_software_io_without_format_or_annotation(self):
        res = await self.call("get_software_io", {"tool": "anyviewer"})
        self.assertEqual(res["inputs"][0]["formats"], [])
        self.assertEqual(res["outputs"], [])
        res = await self.call("get_software_io", {"tool": "noio"})
        self.assertEqual((res["inputs"], res["outputs"]), ([], []))
        self.assertIn("No input/output annotation", res["note"])

    async def test_get_software_io_unknown_tool(self):
        await self.fails("get_software_io", {"tool": "nope"}, "Unknown bio.tools id")

    async def test_describe_software_by_name_or_id_in_any_case(self):
        for request in ("BLAST", "blast", " Blast "):
            with self.subTest(request=request):
                res = await self.call("describe_software", {"name": request})
                self.assertEqual(res["count"], 1)
                self.assertEqual(res["tools"][0]["biotools_id"], "blast")

    async def test_describe_software_lists_properties_and_nested_inputs(self):
        props = (await self.call("describe_software", {"name": "blast"}))["tools"][0]["properties"]
        self.assertEqual(props["schema:name"], ["BLAST"])
        self.assertEqual(props["schema:license"], ["MIT"])
        self.assertEqual(props["schema:applicationSubCategory"], ["http://edamontology.org/topic_0080"])
        self.assertEqual(props["rdf:type"], ["http://schema.org/SoftwareApplication"])
        (inp,) = props["bioschemas:input"]  # a blank node, nested
        self.assertEqual(inp["schema:name"], ["Query"])
        self.assertEqual(inp["schema:additionalType"], ["http://edamontology.org/data_2044"])

    async def test_describe_software_ignores_other_dumps_and_unknown_names(self):
        await self.fails("describe_software", {"name": "Galaxy BLAST"}, "No bio.tools record named")
        await self.fails("describe_software", {"name": "nope"}, "No bio.tools record named")

    async def test_describe_software_name_cannot_inject_sparql(self):
        await self.fails("describe_software", {"name": 'x" ) } # '}, "No bio.tools record named")

    async def test_data_load_failure_is_a_tool_error(self):
        class Broken:
            data_dir = "/nowhere"

            async def get(self):
                raise OSError("no network")

        with mock.patch.object(server, "store", Broken()):
            await self.fails("get_license", {"tool": "blast"}, "Could not load the RSEc RDF data")


if __name__ == "__main__":
    unittest.main()

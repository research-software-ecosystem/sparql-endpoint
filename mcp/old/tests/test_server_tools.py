"""MCP-level tests of the search / composition tools (mini graph, no network)."""

import unittest

from fastmcp import Client
from fastmcp.exceptions import ToolError

import server
from tests.support import MiniServerTestCase

TOOLS = {
    "list_software_with_biotools_ids",
    "resolve_edam_term",
    "search_software",
    "list_licenses",
    "get_software_io",
    "check_composability",
}


class SearchAndCompositionToolTests(MiniServerTestCase):
    async def call(self, name, args):
        async with Client(server.mcp) as c:
            return (await c.call_tool(name, args)).data

    async def test_all_tools_are_registered_and_read_only(self):
        async with Client(server.mcp) as c:
            tools = await c.list_tools()
        self.assertEqual({t.name for t in tools}, TOOLS)
        self.assertTrue(all(t.annotations.read_only_hint for t in tools))

    async def test_search_software_takes_topics_operations_and_name_only(self):
        async with Client(server.mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
        props = tools["search_software"].input_schema["properties"]
        self.assertEqual(set(props), {"topics", "operations", "name_contains", "limit", "offset"})

    async def test_search_software_end_to_end(self):
        res = await self.call(
            "search_software", {"topics": ["topic_0080"], "operations": ["operation_0292"]}
        )
        self.assertEqual([r["biotools_id"] for r in res["results"]], ["blast", "clustal"])
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["results"][0]["licenses"], ["MIT"])  # shown even though not searchable

    async def test_composability_end_to_end(self):
        res = await self.call("check_composability", {"producer": "clustal", "consumer": "alnviewer"})
        self.assertEqual(res["compatibility"], "direct")
        self.assertEqual((await self.call("get_software_io", {"tool": "blast"}))["name"], "BLAST")

    async def test_bad_input_is_a_tool_error_with_a_helpful_message(self):
        cases = [
            ("search_software", {"topics": ["operation_0292"]}, "topic"),
            ("search_software", {"topics": ["bogus"]}, "EDAM"),
            ("get_software_io", {"tool": "nope"}, "search_software"),
            ("check_composability", {"producer": "blast", "consumer": "nope"}, "Unknown bio.tools id"),
        ]
        for name, args, msg in cases:
            with self.subTest(name=name, args=args):
                async with Client(server.mcp) as c:
                    with self.assertRaisesRegex(ToolError, msg):
                        await c.call_tool(name, args)

if __name__ == "__main__":
    unittest.main()

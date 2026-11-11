import unittest

import httpx
from fastmcp import Client
from fastmcp.exceptions import ToolError

import graph_store
import server
from tests.support import ServerTestCase


class ListSoftwareToolTests(ServerTestCase):
    async def test_returns_only_biotools_records(self):
        async with Client(server.mcp) as c:
            res = await c.call_tool("list_software_with_biotools_ids", {})

        self.assertEqual(
            res.data,
            {
                "results": [
                    {"application": "http://x/blast", "biotools_id": "https://bio.tools/blast"},
                    {"application": "http://x/samtools", "biotools_id": "https://bio.tools/samtools"},
                ],
                "limit": 100,
                "offset": 0,
                "count": 2,
            },
        )

    async def test_pages_with_limit_and_offset(self):
        async with Client(server.mcp) as c:
            res = await c.call_tool("list_software_with_biotools_ids", {"limit": 1, "offset": 1})
        self.assertEqual(
            [r["biotools_id"] for r in res.data["results"]], ["https://bio.tools/samtools"]
        )

    async def test_reports_data_loading_failure(self):
        bad = graph_store.GraphStore(
            self.make_temp_dir(),
            sources=(graph_store.Source("missing.ttl", "https://example.org/missing.ttl"),),
            transport=httpx.MockTransport(lambda r: httpx.Response(404)),
        )
        self.use_store(bad)
        with self.assertLogs("rsec.server", level="ERROR") as logs:  # the background warm-up fails too
            async with Client(server.mcp) as c:
                with self.assertRaisesRegex(ToolError, "RSEc RDF data"):
                    await c.call_tool("list_software_with_biotools_ids", {})
        self.assertIn("could not load RSEc data", "\n".join(logs.output))

if __name__ == "__main__":
    unittest.main()

# RSEc SPARQL MCP server

FastMCP server that lets an LLM agent query the RSEc RDF data. The data is loaded in-process with [rdflib](https://rdflib.readthedocs.io), so nothing else (Fuseki, Docker) has to be deployed.

Each tool runs **one SPARQL query**, written out in full as a `q_<tool>` constant in [`server.py`](server.py). The agent cannot send its own queries or change the data.

## Tools

An agent typically goes: find EDAM ids from words -> list the software annotated with them -> inspect one tool.

**Find EDAM terms** (software is searched by EDAM id, not by words):

| Tool | Description |
|------|-------------|
| `resolve_edam_term(text, kind?, limit)` | EDAM terms (topic, operation, data, format) from free text such as "sequence alignment", via labels, synonyms and definitions, best match first. Returns `id`, `label`, `kind`, `matched` and `definition` |
| `get_direct_term_parents(term)` | The broader terms, one level up |
| `get_direct_term_child(term)` | The narrower terms, one level down |

**List software:**

| Tool | Description |
|------|-------------|
| `search_software_by_topic(topic, limit, offset)` | bio.tools software annotated with an EDAM topic (`topic_NNNN`), narrower topics included, sorted by name |
| `search_software_by_operation(operation, limit, offset)` | Same for an EDAM operation (`operation_NNNN`) |

**Describe one software tool:**

| Tool | Description |
|------|-------------|
| `get_license(tool)` | Its license(s), as written in the data |
| `get_software_io(tool)` | Its inputs and outputs, each with a name, an EDAM data type and EDAM formats. A `note` says when the tool has no annotation |
| `describe_software(name)` | Everything the data holds about the tool (a SPARQL `DESCRIBE`): description, homepage, EDAM topics and operations, inputs and outputs. Found by name or bio.tools id, in any case |

Tools are identified by their bio.tools id (`blast`, or `https://bio.tools/blast`, **case-sensitive**). EDAM terms are written `topic_0080`, `operation_0292`, `data_2044` or `format_1929`. Arguments are validated before a query is built.

### What the tools do not do

- Filter software by license or by name, or combine several terms. Search with one EDAM id, then use `get_license` or `describe_software`.
- Check whether two tools can be chained. Compare their `get_software_io` results.

### What the data can and cannot answer

Measured on the current dumps (see [`docs/data-model.md`](docs/data-model.md)):

- About 34,000 bio.tools records; most have EDAM topics and operations, about 45% a license.
- Only about 4,000 tools have input/output annotations and 3,268 have both. For the others `get_software_io` returns a `note`. Well-known tools can fall in that gap (for example `ncbi_blast_plus` has none).
- A matching data type and format means the annotations are compatible, not that the tools have been run together.

More: the design is in [`docs/specification.md`](docs/specification.md), the queries and the data model in [`docs/data-model.md`](docs/data-model.md), and a real session with a local model in [`docs/demo.md`](docs/demo.md).

## How to run

Prerequisites: [uv](https://docs.astral.sh/uv/) (it installs Python and the dependencies), internet access on first start, and about 2 GB of free RAM.

```bash
cd mcp
uv sync                   # install dependencies once
uv run python server.py   # start the server (stdio)
```

The server talks MCP over stdio, so started by hand it just waits silently for a client. Normally you do not run it yourself: an MCP client launches it (see [Connect an MCP client](#connect-an-mcp-client)). To try it without any client, use the next section.

On first start the server downloads the RSEc bioschemas dump (36.5 MB) and the EDAM ontology (3.4 MB, latest release from edamontology.org) into `mcp/data/`, then parses them (about 740k triples, roughly 7 s). Nothing else is precomputed: each tool call runs its SPARQL query on the loaded graph (set `RSEC_LOG_LEVEL=DEBUG` to log every query with its time). Loading starts in the background at startup and the first tool call waits for it; after that a call takes from a few milliseconds to about 2 seconds. Existing files are never re-downloaded, so to refresh the data delete `mcp/data/`.

| Variable | Default |
|----------|---------|
| `RSEC_DATA_DIR` | `mcp/data` (set it to `../data` to reuse the dumps of the Docker setup) |
| `RSEC_LOG_LEVEL` | `INFO` (use `WARNING` to silence the loading logs) |

Progress is logged to stderr (stdout carries the MCP protocol), so clients show it in the server's log pane. Example:

```
10:46:02 INFO rsec.server: loading RSEc data in the background (first tool call waits for it)
10:46:02 INFO rsec.graph_store: data directory .../mcp/data: 2 file(s) present, 0 to download
10:46:02 INFO rsec.graph_store: [1/2] parsing bioschemas-dump.ttl (36.5 MB)
10:46:09 INFO rsec.graph_store: [1/2] parsed bioschemas-dump.ttl: +701235 triples (701235 total, 6.8 s)
10:46:09 INFO rsec.graph_store: [2/2] parsing EDAM.owl (3.4 MB)
10:46:10 INFO rsec.graph_store: [2/2] parsed EDAM.owl: +38938 triples (740173 total, 0.5 s)
10:46:10 INFO rsec.graph_store: loaded 740173 triples from 2 files in 7.3 s
10:46:10 INFO rsec.server: RSEc data ready: server can answer queries
```

## Quick test (no MCP client needed)

The `fastmcp` command line, installed with the project, starts the server, calls it and prints the result:

```bash
cd mcp
uv run fastmcp list server.py                                                 # show the tools
uv run fastmcp call server.py resolve_edam_term text="sequence alignment" limit=2   # call one
uv run fastmcp call server.py search_software_by_operation \
  --input-json '{"operation": "operation_0292", "limit": 2}'
```

The first run is slower while the data downloads and loads. The second call prints the two EDAM terms that best match, with their `id` (use it in the other tools). The third prints two tools, like this:

```json
{
  "results": [
    { "biotools_id": "3gold", "name": "3GOLD" },
    { "biotools_id": "a-prot", "name": "A-Prot" }
  ],
  "limit": 2, "offset": 0, "count": 2
}
```

Add `offset=2` to get the next page. Add `--json` for raw JSON output.

## Connect an MCP client

The server speaks stdio: the client launches it. Most clients (Claude Desktop, Claude Code, Cursor, VS Code, LM Studio...) read the same `mcpServers` JSON. Replace `/absolute/path/to` with the location of this repo.

```json
{
  "mcpServers": {
    "rsec-sparql": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/RSEc_SPARQL_endpoint/mcp",
        "run", "python", "server.py"
      ]
    }
  }
}
```

Where to put it:

| Client | File |
|--------|------|
| Claude Code (project, shareable) | `.mcp.json` at the repo root |
| Claude Desktop (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Cursor | `.cursor/mcp.json` |

If `uv` is not found by a GUI client, use its full path (`which uv`, e.g. `/opt/homebrew/bin/uv`) in `command`.

To use another data directory, add `"env": { "RSEC_DATA_DIR": "/path/to/dumps" }` next to `args`. (The `fastmcp list/call` quick test above does not forward environment variables to the server; use the defaults there.)

Equivalent one-liner for Claude Code:

```bash
claude mcp add rsec-sparql -- uv --directory /absolute/path/to/RSEc_SPARQL_endpoint/mcp run python server.py
```

### Check it works

Ask the agent, for example: *"Which tools do sequence alignment, and what are the licenses of the first three?"* It should call `resolve_edam_term`, `search_software_by_operation`, then `get_license`. The first call waits for the data to download and load (about 7 s once the files are cached, more on the very first run). If it reports it could not load the data, check network access to `raw.githubusercontent.com`, or put the `*.ttl` dumps in `RSEC_DATA_DIR`.

## Unit tests

```bash
./run_tests.sh           # unit tests only (fast, no network)
./run_tests.sh --smoke   # + call the tools on the real data (downloads it on first run)
```

or directly: `uv run python -m unittest` (add `-v` to list each test, or name a module: `python -m unittest tests.test_server`).
The tests use only the standard library `unittest`. They use a tiny local graph and a mocked HTTP layer; they need neither network nor the real dumps.

The code of the earlier design (a `KnowledgeGraph` class, composability checks, a demo notebook) is archived in [`old/`](old/); it is not run by the tests.

"""FastMCP server exposing RSEC SPARQL queries to LLM agents.

The RSEc RDF dumps are loaded in-process with rdflib (downloaded on first run
into RSEC_DATA_DIR), so no SPARQL endpoint has to be deployed.
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Callable, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from graph_store import GraphStore, run_query
from knowledge_graph import KnowledgeGraph

log = logging.getLogger("rsec.server")


def _setup_logging() -> None:
    """Log to stderr (stdout carries the MCP protocol). RSEC_LOG_LEVEL=WARNING mutes it."""
    root = logging.getLogger("rsec")
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    )
    root.addHandler(handler)
    root.setLevel(os.environ.get("RSEC_LOG_LEVEL", "INFO").upper())
    root.propagate = False


_setup_logging()

DATA_DIR = Path(os.environ.get("RSEC_DATA_DIR", Path(__file__).parent / "data"))
store = GraphStore(DATA_DIR)


async def get_knowledge_graph() -> KnowledgeGraph:
    """The knowledge layer over the loaded graph (every call is a SPARQL query)."""
    try:
        graph = await store.get()
    except Exception as e:
        raise ToolError(
            f"Could not load the RSEc RDF data into {store.data_dir} ({e!r}). "
            "Check network access to raw.githubusercontent.com and edamontology.org, "
            "or place the dumps in that directory (see RSEC_DATA_DIR)."
        ) from e
    return KnowledgeGraph(graph)


async def _ask(method: Callable[[KnowledgeGraph], dict]) -> dict:
    """Run a KnowledgeGraph method off the event loop; bad input becomes a ToolError."""
    kg = await get_knowledge_graph()
    try:
        return await asyncio.to_thread(method, kg)
    except ValueError as e:
        raise ToolError(str(e)) from e


@asynccontextmanager
async def lifespan(_server):
    # Warm up in the background so the MCP handshake is not delayed by the
    # ~10 s parsing (plus the first-run download).
    async def warm():
        log.info("loading RSEc data in the background (first tool call waits for it)")
        try:
            await get_knowledge_graph()
            log.info("RSEc data ready: server can answer queries")
        except Exception:
            log.exception("could not load RSEc data; will retry on first tool call")

    task = asyncio.create_task(warm())
    try:
        yield
    finally:
        task.cancel()


mcp = FastMCP(
    "rsec-sparql",
    lifespan=lifespan,
    instructions=(
        "Access to the Research Software Ecosystem (RSEc) RDF data: bio.tools "
        "records described with schema.org / Bioschemas and typed with EDAM terms "
        "(topic_NNNN, operation_NNNN, data_NNNN, format_NNNN).\n"
        "Typical workflow: (1) resolve_edam_term turns words like 'sequence alignment' "
        "into EDAM ids; (2) search_software finds tools by topic, operation or name "
        "(each result shows its licenses; list_licenses gives an overview of the licenses "
        "in use); (3) get_software_io shows a tool's inputs and outputs; (4) "
        "check_composability tests whether the outputs of tool A can feed tool B. "
        "Tools are identified by their bio.tools id, e.g. 'blast'. "
        "Input/output annotations exist for only a few thousand tools, so composability "
        "answers can be 'unknown'."
    ),
)

# SPARQL query 5 from ui/index.html ("SoftwareApplication & bio.tools IDs").
BIOTOOLS_IDS_QUERY = """\
PREFIX schema: <http://schema.org/>
SELECT ?application ?biotools_ID
WHERE {{
  ?application schema:identifier ?biotools_ID .
  FILTER STRSTARTS(STR(?biotools_ID), "https://bio.tools")
}}
ORDER BY STR(?biotools_ID) ?application
LIMIT {limit} OFFSET {offset}
"""


def build_query(limit: int, offset: int) -> str:
    # limit/offset are ints validated by the tool signature, never free text.
    return BIOTOOLS_IDS_QUERY.format(limit=int(limit), offset=int(offset))


async def run_select(query: str) -> list[dict[str, str]]:
    """Run a SELECT query on the RSEc graph, return rows as {var: value}."""
    try:
        graph = await store.get()
    except Exception as e:
        raise ToolError(
            f"Could not load the RSEc RDF data into {store.data_dir} ({e!r}). "
            "Check network access to raw.githubusercontent.com, or place the "
            "*.ttl dumps in that directory (see RSEC_DATA_DIR)."
        ) from e
    rows = await asyncio.to_thread(run_query, graph, query)
    return [{str(var): str(value) for var, value in row.asdict().items()} for row in rows]


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def list_software_with_biotools_ids(
    limit: Annotated[int, Field(ge=1, le=1000, description="Max rows to return")] = 100,
    offset: Annotated[int, Field(ge=0, description="Rows to skip, for paging")] = 0,
) -> dict:
    """List software applications of the RSEc RDF data that have a bio.tools identifier.

    Returns schema:SoftwareApplication resources whose schema:identifier starts
    with https://bio.tools, ordered by identifier. Page with limit/offset;
    if `count` equals `limit`, more rows may be available.
    """
    rows = await run_select(build_query(limit, offset))
    results = [
        {"application": r["application"], "biotools_id": r["biotools_ID"]} for r in rows
    ]
    return {"results": results, "limit": limit, "offset": offset, "count": len(results)}


_READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
EdamKind = Literal["topic", "operation", "data", "format"]


@mcp.tool(annotations=_READ_ONLY)
async def resolve_edam_term(
    text: Annotated[str, Field(description="Words to look for, e.g. 'sequence alignment'")],
    kind: Annotated[
        EdamKind | None,
        Field(description="Restrict to one EDAM branch: topic, operation, data or format"),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict:
    """Find EDAM terms (topics, operations, data types, formats) from free text.

    Searches labels, synonyms and definitions, best match first. Use the returned
    `id` (e.g. 'operation_0292') in search_software. `matched` tells whether the
    label or a synonym matched; `n_tools` is how many tools are annotated directly
    with a topic/operation (narrower terms not counted); `broader` are parent terms.
    """
    return await _ask(lambda kg: kg.resolve_edam_term(text, kind, limit))


@mcp.tool(annotations=_READ_ONLY)
async def search_software(
    topics: Annotated[
        list[str] | None,
        Field(description="EDAM topic ids, e.g. ['topic_0080']. A tool matches if it has ANY of them"),
    ] = None,
    operations: Annotated[
        list[str] | None,
        Field(description="EDAM operation ids, e.g. ['operation_0292']. ANY of them"),
    ] = None,
    name_contains: Annotated[
        str | None, Field(description="Case-insensitive text found in the tool name or id")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """Search bio.tools software of the RSEc data by EDAM topic, EDAM operation and name.

    Filters of different kinds are combined with AND; several ids inside one
    filter with OR. Narrower EDAM terms are included: 'topic_0080' also matches
    tools annotated with its sub-topics. Get the ids from resolve_edam_term. With
    no filter, lists all tools. Results are sorted by name; `total` is the number
    of matches (page with limit/offset). Each result has the bio.tools id (use it
    in get_software_io / check_composability), a short description, its licenses
    (shown, not searchable) and its EDAM topics/operations.
    """
    return await _ask(
        lambda kg: kg.search_software(
            topics=topics, operations=operations, name_contains=name_contains,
            limit=limit, offset=offset,
        )
    )


@mcp.tool(annotations=_READ_ONLY)
async def list_licenses(
    contains: Annotated[str | None, Field(description="Only licenses containing this text, e.g. 'gpl'")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict:
    """List the licenses used by tools, with the number of tools, most used first.

    License values are free text and messy ('GPL-3', 'GPL-3.0', 'GPL-3.0-or-later',
    'Not licensed', 'unknown'...). Case variants are merged. This is an overview of
    the licenses in use: search_software shows the licenses of each tool but cannot
    filter on them.
    """
    return await _ask(lambda kg: kg.list_licenses(contains, limit))


@mcp.tool(annotations=_READ_ONLY)
async def get_software_io(
    tool: Annotated[str, Field(description="bio.tools id, e.g. 'blast' (or 'https://bio.tools/blast')")],
) -> dict:
    """Show the annotated inputs and outputs of a tool.

    Each input/output has a name, an EDAM data type and zero or more EDAM formats.
    Only a few thousand tools have this annotation; a `note` says when it is missing.
    """
    return await _ask(lambda kg: kg.get_software_io(tool))


@mcp.tool(annotations=_READ_ONLY)
async def check_composability(
    producer: Annotated[str, Field(description="bio.tools id of the tool that runs first")],
    consumer: Annotated[str, Field(description="bio.tools id of the tool that runs second")],
) -> dict:
    """Check whether the outputs of `producer` can feed the inputs of `consumer`.

    An output fits an input when its EDAM data type is the same or narrower (a
    'Protein sequence' fits a 'Sequence' input, not the reverse). Formats then
    decide `compatibility`:
    - direct: a shared format, or a narrower one (FASTA fits 'FASTA-like')
    - probable: data types fit but a format is not annotated on one side
    - needs_conversion: data types fit but formats differ; a converter is needed
    - generic: the only fit is an input typed with EDAM's root type 'Data', which
      accepts anything and proves little
    - none: no output fits any input
    - unknown: one tool has no input/output annotation (see `note`)
    `matches` lists each fitting output/input pair with its `kind`
    (exact, subtype, format_unspecified, needs_conversion, generic_input).
    """
    return await _ask(lambda kg: kg.check_composability(producer, consumer))


if __name__ == "__main__":
    mcp.run(show_banner=False)  # stdio

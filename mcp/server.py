"""FastMCP server exposing the RSEc knowledge graph to LLM agents: one SPARQL query per tool.

The RSEc RDF dumps and the EDAM ontology are loaded in-process with rdflib
(downloaded on first run into RSEC_DATA_DIR), so no SPARQL endpoint has to be
deployed. Each tool validates its arguments with a pydantic pattern (so nothing
unchecked reaches a query), runs exactly one SELECT with `run_select`, and
shapes the rows.
"""

import asyncio
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from string import Template
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from rdflib import BNode, URIRef
from rdflib import Literal as RdfLiteral

import edam
from graph_store import GraphStore, run_query

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

BIOTOOLS = "https://bio.tools/"

# ---------------------------------------------------------------- SPARQL queries
#
# One constant per tool, each a complete query. `%name` marks a value filled in by
# `render` (the only place values enter a query): `%term` and `%tool` are IRIs in
# angle brackets built from a validated id, `%limit` and `%offset` are ints, `%exact`,
# `%pattern` and `%prefix` are escaped string literals.

# %exact: the lower-cased request; %pattern: regex with one lookahead per word (every
# word must appear as a whole word); %prefix: IRI prefix of the wanted EDAM kind.
# Rank: 0 exact label, 1 exact synonym, 2 words in the label, 3 words in a synonym,
# 4 words in the definition.
q_resolve_edam_term = """
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX oio: <http://www.geneontology.org/formats/oboInOwl#>
SELECT ?term (MIN(?rank) AS ?best) (SAMPLE(?lab) AS ?label) (SAMPLE(?def) AS ?definition) WHERE {
  { ?term rdfs:label ?text BIND(IF(LCASE(STR(?text)) = %exact, 0, 2) AS ?rank) }
  UNION { ?term oio:hasExactSynonym ?text BIND(IF(LCASE(STR(?text)) = %exact, 1, 3) AS ?rank) }
  UNION { ?term oio:hasNarrowSynonym ?text BIND(IF(LCASE(STR(?text)) = %exact, 1, 3) AS ?rank) }
  UNION { ?term oio:hasBroadSynonym ?text BIND(IF(LCASE(STR(?text)) = %exact, 1, 3) AS ?rank) }
  UNION { ?term oio:hasDefinition ?text BIND(4 AS ?rank) }
  FILTER (REGEX(STR(?term), "^http://edamontology.org/(topic|operation|data|format)_[0-9]{4}$")
          && STRSTARTS(STR(?term), %prefix))
  FILTER REGEX(LCASE(STR(?text)), %pattern, "s")
  FILTER NOT EXISTS { ?term owl:deprecated "true"^^xsd:boolean }
  ?term rdfs:label ?lab .
  OPTIONAL { ?term oio:hasDefinition ?def }
}
GROUP BY ?term
ORDER BY ?best STRLEN(?label) ?term
LIMIT %limit
"""

# %term: the EDAM term. The label is selected so that a known term without parent
# still gives a row (rdflib drops a row whose variables are all unbound).
q_direct_term_parents = """
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?term_label ?parent ?label WHERE {
  %term rdfs:label ?term_label .
  OPTIONAL {
    %term rdfs:subClassOf ?parent .
    FILTER REGEX(STR(?parent), "^http://edamontology.org/(topic|operation|data|format)_[0-9]{4}$")
    ?parent rdfs:label ?label .
  }
}
ORDER BY ?parent
"""

q_direct_term_children = """
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT DISTINCT ?term_label ?child ?label WHERE {
  %term rdfs:label ?term_label .
  OPTIONAL {
    ?child rdfs:subClassOf %term .
    FILTER REGEX(STR(?child), "^http://edamontology.org/(topic|operation|data|format)_[0-9]{4}$")
    ?child rdfs:label ?label .
    FILTER NOT EXISTS { ?child owl:deprecated "true"^^xsd:boolean }
  }
}
ORDER BY ?child
"""

# %term: the EDAM topic. Starts from the term and walks down to the narrower topics.
q_software_by_topic = """
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?tool (MIN(?name) AS ?tool_name) WHERE {
  ?topic rdfs:subClassOf* %term .
  ?tool schema:applicationSubCategory ?topic .
  ?tool a schema:SoftwareApplication ; schema:name ?name .
  FILTER REGEX(STR(?tool), "^https://bio.tools/[A-Za-z0-9_.~+-]+$")
}
GROUP BY ?tool
ORDER BY LCASE(?tool_name) ?tool
LIMIT %limit OFFSET %offset
"""

# %term: the EDAM operation, same walk down.
q_software_by_operation = """
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?tool (MIN(?name) AS ?tool_name) WHERE {
  ?operation rdfs:subClassOf* %term .
  ?tool schema:featureList ?operation .
  ?tool a schema:SoftwareApplication ; schema:name ?name .
  FILTER REGEX(STR(?tool), "^https://bio.tools/[A-Za-z0-9_.~+-]+$")
}
GROUP BY ?tool
ORDER BY LCASE(?tool_name) ?tool
LIMIT %limit OFFSET %offset
"""

# %tool: the bio.tools record. The name is selected so that a known tool without
# license still gives a row. The license is the tool's own, or the one of a record
# from another dump (biocontainers, Galaxy...) that points to it.
q_license = """
PREFIX schema: <http://schema.org/>
SELECT DISTINCT ?name ?license WHERE {
  %tool a schema:SoftwareApplication ; schema:name ?name .
  OPTIONAL {
    { %tool schema:license ?license }
    UNION
    { ?record schema:identifier %tool ; schema:license ?license }
  }
}
"""

q_software_io = """
PREFIX schema: <http://schema.org/>
PREFIX bs: <http://bioschemas.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?name ?dir ?param ?pname ?dtype ?dlabel ?fmt ?flabel WHERE {
  %tool a schema:SoftwareApplication ; schema:name ?name .
  OPTIONAL {
    { %tool bs:input ?param . BIND("input" AS ?dir) }
    UNION { %tool bs:output ?param . BIND("output" AS ?dir) }
    ?param schema:additionalType ?dtype .
    OPTIONAL { ?param schema:name ?pname }
    OPTIONAL { ?dtype rdfs:label ?dlabel }
    OPTIONAL { ?param schema:encodingFormat ?fmt . OPTIONAL { ?fmt rdfs:label ?flabel } }
  }
}
"""


# %name: the lower-cased request as a string literal; %iri: https://bio.tools/<request>,
# lower-cased, as a string literal. A DESCRIBE returns the triples of the matching
# records, including the blank nodes (inputs, outputs...) they point to. The request is
# compared with the schema:name (any case) or with the bio.tools id (any case).
q_describe_software = """
PREFIX schema: <http://schema.org/>
DESCRIBE ?tool WHERE {
  ?tool a schema:SoftwareApplication ; schema:name ?name .
  FILTER REGEX(STR(?tool), "^https://bio.tools/[A-Za-z0-9_.~+-]+$")
  FILTER (LCASE(STR(?name)) = %name || LCASE(STR(?tool)) = %iri)
}
"""

class _Query(Template):
    delimiter = "%"


def render(query: str, **values) -> str:
    """Fill the `%name` placeholders of a q_* query (a missing value is an error)."""
    return _Query(query).substitute({k: str(v) for k, v in values.items()})


MAX_DEFINITION = 300

# Argument types. The patterns are the only validation: an id that passes is safe to
# put in a query, and one that does not is rejected before the tool runs.
_EDAM_PREFIX = r"(edam:|http://edamontology\.org/)?"
ToolId = Annotated[
    str,
    Field(
        pattern=r"^(https://bio\.tools/)?[A-Za-z0-9_.~+-]+$",
        description="bio.tools id, case-sensitive, e.g. 'blast' (or 'https://bio.tools/blast')",
    ),
]
TopicId = Annotated[
    str,
    Field(pattern=rf"^{_EDAM_PREFIX}topic_[0-9]{{4}}$", description="EDAM topic id, e.g. 'topic_0080'"),
]
OperationId = Annotated[
    str,
    Field(
        pattern=rf"^{_EDAM_PREFIX}operation_[0-9]{{4}}$",
        description="EDAM operation id, e.g. 'operation_0292'",
    ),
]
TermId = Annotated[
    str,
    Field(
        pattern=rf"^{_EDAM_PREFIX}(topic|operation|data|format)_[0-9]{{4}}$",
        description="EDAM term id, e.g. 'topic_0080', 'operation_0292', 'data_2044' or 'format_1929'",
    ),
]
EdamKind = Literal["topic", "operation", "data", "format"]
Limit = Annotated[int, Field(ge=1, le=100, description="Max rows to return")]
Offset = Annotated[int, Field(ge=0, description="Rows to skip, for paging")]


def tool_iri(tool: str) -> str:
    return f"<{BIOTOOLS}{tool.removeprefix(BIOTOOLS)}>"


def term_iri(term: str) -> str:
    return f"<{edam.normalize(term)}>"


def term_ref(iri: str, label: str | None) -> dict:
    return {"id": iri.removeprefix(edam.NS), "label": label or ""}


async def get_graph():
    try:
        return await store.get()
    except Exception as e:
        raise ToolError(
            f"Could not load the RSEc RDF data into {store.data_dir} ({e!r}). "
            "Check network access to raw.githubusercontent.com and edamontology.org, "
            "or place the dumps in that directory (see RSEC_DATA_DIR)."
        ) from e


async def run_select(query: str) -> list[dict[str, str]]:
    """Run a SELECT query on the RSEc graph; rows are {variable: value} (unbound ones are absent).

    rdflib drops a row whose projected variables are all unbound, so an OPTIONAL query
    must also project a variable that is always bound (otherwise "no match" and "unknown
    id" look the same).
    """
    rows = await asyncio.to_thread(run_query, await get_graph(), query)
    return [{str(var): str(value) for var, value in row.asdict().items()} for row in rows]


async def run_describe(query: str) -> list[tuple]:
    """Run a DESCRIBE query on the RSEc graph; returns its (subject, predicate, object) triples."""
    return await asyncio.to_thread(run_query, await get_graph(), query)


@asynccontextmanager
async def lifespan(_server):
    # Warm up in the background so the MCP handshake is not delayed by the
    # ~10 s parsing (plus the first-run download).
    async def warm():
        log.info("loading RSEc data in the background (first tool call waits for it)")
        try:
            await store.get()
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
    instructions="""\
Read-only access to the Research Software Ecosystem (RSEc) data: bio.tools software records
annotated with EDAM terms. EDAM ids look like topic_0080 (what a tool is about),
operation_0292 (what it does), data_2044 (a type of data) and format_1929 (a file format).
A software tool is identified by its bio.tools id (case-sensitive), e.g. 'blast'.

Find EDAM ids (you cannot search software by words, only by EDAM id):
- resolve_edam_term(text, kind?): words such as 'sequence alignment' -> matching EDAM terms
  (id, label, kind, definition), best first. Always start here when you only have words.
- get_direct_term_parents(term): the broader terms one level up.
- get_direct_term_child(term): the narrower terms one level down.
  Use these two to widen or narrow a search, or to browse around a term.

List software:
- search_software_by_topic(topic): tools annotated with a topic_NNNN id.
- search_software_by_operation(operation): tools annotated with an operation_NNNN id.
  Both include the tools of the narrower terms, return only the bio.tools id and name, sort by
  name, and page with limit/offset (there is no total: if `count` equals `limit`, ask for
  the next page). They accept only their own kind of id; data and format ids do not search.

Describe one software tool (take its id from a search):
- get_license(tool): its license(s), free text as written in the data, possibly empty.
- get_software_io(tool): its inputs and outputs, each with a name, an EDAM data type and
  EDAM formats. Only a few thousand tools are annotated; a `note` says when one is not.
- describe_software(name): everything the data holds about a tool (a SPARQL DESCRIBE), found by its
  name or bio.tools id in any case: description, homepage, version, EDAM topics and
  operations as IRIs, inputs/outputs nested. Several records can match a name (see `count`).
  Long and unlabelled; prefer the tools above for a specific question.

Not available: filtering software by license or by name, and checking whether two tools can
be chained (compare their get_software_io results yourself).
""",
)
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}


#####################################################
# SEARCHING the EDAM ontology for terms, and moving up/down the hierarchy.
#####################################################

@mcp.tool(annotations=READ_ONLY)
async def resolve_edam_term(
    text: Annotated[str, Field(description="Words to look for, e.g. 'sequence alignment'")],
    kind: Annotated[EdamKind | None, Field(description="Restrict to one EDAM branch")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict:
    """Find EDAM terms (topics, operations, data types, formats) from free text.

    Looks for terms whose label, synonym or definition contains every word, best
    match first: exact label, exact synonym, words in the label, words in a synonym,
    words in the definition (ties: shorter label first). Deprecated terms are left
    out. Use the returned `id` (e.g. 'operation_0292') in the other tools.
    `matched` tells which field matched best.
    """
    words = re.findall(r"\w+", text.lower())
    if not words:
        raise ToolError("Give at least one word to look for.")
    rows = await run_select(render(
        q_resolve_edam_term,
        exact=RdfLiteral(text.strip().lower()).n3(),
        pattern=RdfLiteral("^" + "".join(rf"(?=.*(^|\W){re.escape(w)}(\W|$))" for w in words)).n3(),
        prefix=RdfLiteral(edam.NS + (f"{kind}_" if kind else "")).n3(),
        limit=limit,
    ))
    matched = {0: "label", 1: "synonym", 2: "label", 3: "synonym", 4: "definition"}
    return {
        "results": [
            {
                **term_ref(r["term"], r.get("label")),
                "kind": r["term"].removeprefix(edam.NS).split("_")[0],
                "matched": matched[int(r["best"])],
                "definition": r.get("definition", "")[:MAX_DEFINITION],
            }
            for r in rows
        ]
    }


@mcp.tool(annotations=READ_ONLY)
async def get_direct_term_parents(term: TermId) -> dict:
    """List the direct broader terms of an EDAM term (one level up).

    A root term has no parent. Call it again on a parent to keep going up.
    """
    iri = term_iri(term)
    rows = await run_select(render(q_direct_term_parents, term=iri))
    if not rows:
        raise unknown_term(term)
    return {
        "term": term_ref(iri[1:-1], min(r["term_label"] for r in rows)),
        "parents": [term_ref(r["parent"], r.get("label")) for r in rows if "parent" in r],
    }


@mcp.tool(annotations=READ_ONLY)
async def get_direct_term_child(term: TermId) -> dict:
    """List the direct narrower terms of an EDAM term (one level down), deprecated ones left out.

    A leaf term has no child. Call it again on a child to keep going down.
    """
    iri = term_iri(term)
    rows = await run_select(render(q_direct_term_children, term=iri))
    if not rows:
        raise unknown_term(term)
    return {
        "term": term_ref(iri[1:-1], min(r["term_label"] for r in rows)),
        "children": [term_ref(r["child"], r.get("label")) for r in rows if "child" in r],
    }


def unknown_term(term: str) -> ToolError:
    return ToolError(f"{term!r} is not a term of the EDAM ontology. Use resolve_edam_term to find ids.")


#####################################################
# SEARCHING for software, by EDAM topic or operation.
#####################################################

def tool_page(rows: list[dict[str, str]], limit: int, offset: int) -> dict:
    results = [{"biotools_id": r["tool"].removeprefix(BIOTOOLS), "name": r["tool_name"]} for r in rows]
    return {"results": results, "limit": limit, "offset": offset, "count": len(results)}


@mcp.tool(annotations=READ_ONLY)
async def search_software_by_topic(topic: TopicId, limit: Limit = 20, offset: Offset = 0) -> dict:
    """List bio.tools software annotated with an EDAM topic, sorted by name.

    The narrower topics count too: 'topic_0080' also matches tools annotated with
    its sub-topics. Page with limit/offset; if `count` equals `limit`, more tools
    may be available. Get topic ids from resolve_edam_term.
    """
    rows = await run_select(render(q_software_by_topic, term=term_iri(topic), limit=limit, offset=offset))
    return tool_page(rows, limit, offset)


@mcp.tool(annotations=READ_ONLY)
async def search_software_by_operation(operation: OperationId, limit: Limit = 20, offset: Offset = 0) -> dict:
    """List bio.tools software annotated with an EDAM operation, sorted by name.

    The narrower operations count too. Page with limit/offset; if `count` equals
    `limit`, more tools may be available. Get operation ids from resolve_edam_term.
    """
    rows = await run_select(render(q_software_by_operation, term=term_iri(operation), limit=limit, offset=offset))
    return tool_page(rows, limit, offset)


#####################################################
# DESCRIBING individual software
#####################################################

def unknown_tool(tool: str) -> ToolError:
    return ToolError(
        f"Unknown bio.tools id {tool!r} (ids are case-sensitive). "
        "Use search_software_by_topic or search_software_by_operation to find the id of a tool."
    )


@mcp.tool(annotations=READ_ONLY)
async def get_license(tool: ToolId) -> dict:
    """Give the license(s) of a tool, as written in the data (free text).

    Values are messy ('MIT', 'GPL-3.0', 'GPL (>= 2)', 'Not licensed', 'unknown'...).
    The license of a linked record from another dump (biocontainers, Galaxy...) counts
    too. `licenses` is empty when none is given.
    """
    rows = await run_select(render(q_license, tool=tool_iri(tool)))
    if not rows:
        raise unknown_tool(tool)
    licenses = sorted({r["license"].strip() for r in rows if r.get("license", "").strip()})
    return {"biotools_id": tool.removeprefix(BIOTOOLS), "name": min(r["name"] for r in rows), "licenses": licenses}


@mcp.tool(annotations=READ_ONLY)
async def get_software_io(tool: ToolId) -> dict:
    """Show the annotated inputs and outputs of a tool.

    Each input/output has a name, an EDAM data type and zero or more EDAM formats
    (id and label). Only a few thousand tools have this annotation; a `note` says
    when it is missing.
    """
    rows = await run_select(render(q_software_io, tool=tool_iri(tool)))
    if not rows:
        raise unknown_tool(tool)
    # One parameter per (parameter node, data type); its formats are the rows' ?fmt.
    params: dict[tuple[str, str], dict] = {}
    for r in rows:
        if "param" not in r:
            continue
        p = params.setdefault(
            (r["param"], r["dtype"]),
            {"dir": r["dir"], "name": r.get("pname", ""), "data": term_ref(r["dtype"], r.get("dlabel")), "formats": {}},
        )
        if "fmt" in r:
            p["formats"][r["fmt"]] = r.get("flabel")
    io: dict[str, list[dict]] = {"input": [], "output": []}
    for p in params.values():
        formats = [term_ref(f, label) for f, label in sorted(p["formats"].items())]
        io[p["dir"]].append({"name": p["name"], "data": p["data"], "formats": formats})
    for items in io.values():
        items.sort(key=lambda p: (p["name"], p["data"]["id"]))
    result = {
        "biotools_id": tool.removeprefix(BIOTOOLS),
        "name": min(r["name"] for r in rows),
        "inputs": io["input"],
        "outputs": io["output"],
    }
    if not params:
        result["note"] = "No input/output annotation for this tool in the RSEc data."
    return result


# Short names for the predicates of a description (anything else is shown as its IRI).
_PREDICATES = {
    "http://schema.org/": "schema:",
    "http://bioschemas.org/": "bioschemas:",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#": "rdf:",
}
MAX_VALUE = 1000


def _short(iri: str) -> str:
    for base, prefix in _PREDICATES.items():
        if iri.startswith(base):
            return prefix + iri.removeprefix(base)
    return iri


def _describe_node(node, by_subject: dict, seen: frozenset = frozenset()) -> dict:
    """The properties of a resource as {predicate: [values]}, blank nodes nested."""
    props: dict[str, list] = {}
    for _, p, o in by_subject.get(node, ()):
        if isinstance(o, BNode):
            value = _describe_node(o, by_subject, seen | {node}) if o not in seen else {}
        else:
            value = str(o)[:MAX_VALUE]
        props.setdefault(_short(str(p)), []).append(value)
    return {p: sorted(vs, key=str) for p, vs in sorted(props.items())}


@mcp.tool(annotations=READ_ONLY)
async def describe_software(
    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=200,
            description="Name of the tool (e.g. 'SAMtools') or its bio.tools id (e.g. 'samtools'), any case",
        ),
    ],
) -> dict:
    """Describe a software tool with all its metadata (a SPARQL DESCRIBE).

    The name is compared, ignoring case, with the tool's name and with its bio.tools
    id, so several records can match (see `count`). Each record lists every property in
    the data: `schema:` and `bioschemas:` properties are shown with those prefixes,
    EDAM terms and URLs as IRIs, and inputs/outputs as nested objects. Long values are cut
    after 1000 characters. Use get_license and get_software_io for a readable license and
    inputs/outputs with EDAM labels.
    """
    request = name.strip().lower()
    triples = await run_describe(render(
        q_describe_software,
        name=RdfLiteral(request).n3(),
        iri=RdfLiteral(BIOTOOLS + request).n3(),
    ))
    by_subject: dict = {}
    for s, p, o in triples:
        by_subject.setdefault(s, []).append((s, p, o))
    tools = sorted(s for s in by_subject if isinstance(s, URIRef) and str(s).startswith(BIOTOOLS))
    if not tools:
        raise ToolError(
            f"No bio.tools record named {name!r}. "
            "Use search_software_by_topic or search_software_by_operation to find a tool."
        )
    return {
        "count": len(tools),
        "tools": [
            {"biotools_id": str(t).removeprefix(BIOTOOLS), "iri": str(t), "properties": _describe_node(t, by_subject)}
            for t in tools
        ],
    }


if __name__ == "__main__":
    mcp.run(show_banner=False)  # stdio

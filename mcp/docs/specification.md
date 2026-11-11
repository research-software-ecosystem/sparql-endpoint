# RSEc MCP server: specification

Status: draft, describes the server as implemented on branch `mcp-draft` (2026-11-11).

## 1. Purpose

Let an LLM agent explore the Research Software Ecosystem (RSEc) RDF data without writing SPARQL: find EDAM terms from words, list bio.tools software by EDAM topic or operation, and read a tool's license, inputs, outputs or full metadata. Data model and queries: [data-model.md](data-model.md).

## 2. Scope

| In scope | Out of scope |
|----------|--------------|
| Read-only questions on bio.tools records (`https://bio.tools/<id>`) typed with EDAM | Writing or updating data |
| EDAM term lookup (topic, operation, data, format) and one-level moves in the hierarchy | Running tools, or checking that two tools can be chained (the agent compares their `get_software_io` results) |
| Software lists by EDAM topic or operation, narrower terms included | Filtering software by license or by name, or by several terms at once |
| Single user, local process | Multi-user serving, authentication, remote deployment |
| Records of other dumps (biocontainers, Galaxy, Debian...) only as extra license sources | Searching or returning those records |
| | Free-form SPARQL from the agent |

## 3. Architecture

- FastMCP server, stdio transport, one process ([server.py](../server.py)).
- Data in memory: the RSEc dumps and `EDAM.owl` parsed into one rdflib `Graph` ([graph_store.py](../graph_store.py)). No SPARQL endpoint, no Docker.
- **One SPARQL query per tool.** Each query is a constant named `q_<tool>` in `server.py`, with `%name` placeholders filled by `render`. A tool validates its arguments, runs its query with `run_select` (or `run_describe`) and shapes the rows into JSON. Nothing is precomputed. The only EDAM code used is `edam.NS` and `edam.normalize` ([edam.py](../edam.py)).
- All queries go through `graph_store.run_query`, which serialises them (rdflib's SPARQL parser is not thread-safe). Tool calls run off the event loop.

```
MCP client --stdio--> server.py (tool: validate -> render q_* -> run_select -> shape) --> run_query --> rdflib Graph
```

The previous design (a `KnowledgeGraph` class building queries from many arguments, with composability checks) is archived in [old/](../old/).

## 4. Tools

All tools are read-only (`readOnlyHint`) and closed-world (`openWorldHint: false`). Results are JSON objects.

**Identifiers.**

- A tool is identified by its bio.tools id, **case-sensitive**: `blast` or `https://bio.tools/blast`.
- An EDAM term is `topic_0080`, `edam:topic_0080` or the full IRI, in lower case.
- Arguments are checked by a pattern, and each search tool accepts only its own kind of id (`topic_NNNN` for topics, `operation_NNNN` for operations).

| Tool | Query | Arguments | Returns |
| --- | --- | --- | --- |
| `resolve_edam_term` | `q_resolve_edam_term` | `text`, `kind?` (`topic`, `operation`, `data`, `format`), `limit` (1-50, default 10) | `results[]`: `id`, `label`, `kind`, `matched` (`label`, `synonym`, `definition`), `definition` (300 characters at most) |
| `get_direct_term_parents` | `q_direct_term_parents` | `term` (any EDAM kind) | `term` (`{id, label}`), `parents[]` (`{id, label}`) |
| `get_direct_term_child` | `q_direct_term_children` | `term` (any EDAM kind) | `term`, `children[]` |
| `search_software_by_topic` | `q_software_by_topic` | `topic`, `limit` (1-100, default 20), `offset` (default 0) | `results[]`: `biotools_id`, `name`; `limit`, `offset`, `count` |
| `search_software_by_operation` | `q_software_by_operation` | `operation`, `limit`, `offset` | same as above |
| `get_license` | `q_license` | `tool` | `biotools_id`, `name`, `licenses[]` (empty when none) |
| `get_software_io` | `q_software_io` | `tool` | `biotools_id`, `name`, `inputs[]`, `outputs[]` (each `name`, `data`, `formats[]`; `data` and formats are `{id, label}`), `note` when the tool has no annotation |
| `describe_software` | `q_describe_software` | `name` (tool name or bio.tools id, any case) | `count`, `tools[]`: `biotools_id`, `iri`, `properties` |

### Rules

**EDAM lookup.** A term is a candidate when its label, a synonym (exact, narrow or broad) or its definition contains every word as a whole word. Deprecated terms are left out. The sort is: exact label, exact synonym, all words in the label, all words in a synonym, all words in the definition; ties by shorter label, then IRI. Ranking is done in the query. `matched` reports the best field.

**Hierarchy.** `get_direct_term_parents` and `get_direct_term_child` move one level. A root has no parent and a leaf no child (an empty list, not an error). Deprecated children are left out. To go further, call the tool again on a result.

**Search.**

- Narrower EDAM terms always count: `topic_0080` also matches tools annotated with its sub-topics.
- Results are sorted by name (case-insensitive), then IRI, and one row is returned per tool.
- Only bio.tools records are returned.
- Paging is done by the query. There is no `total`: if `count` equals `limit`, ask for the next page.
- Data and format ids do not search software.

**License.** Values are free text, as written in the data (`MIT`, `GPL-3.0`, `None`, `unknown`...). The license of a tool is its own, or that of a linked record from another dump (when loaded). Values are trimmed, de-duplicated and sorted, with no other cleaning.

**Inputs and outputs.** One entry per parameter and data type, with the EDAM data type and its formats (labelled). Formats may be empty (31% of parameters have none).

**Describe.** `name` is compared, ignoring case, with the tool's `schema:name` and with its bio.tools id, so `SAMtools` and `samtools` both work, and several records may match (see `count`). `properties` maps each predicate to a sorted list of values: `schema:`, `bioschemas:` and `rdf:` prefixes are shortened, EDAM terms and URLs are IRIs, inputs and outputs are nested objects, and values are cut after 1000 characters. The answer is long and unlabelled; the specific tools above are preferred for a given question.

## 5. Errors

Bad input becomes an MCP tool error with a message the agent can act on, never a stack trace:

- malformed id, or an id of the wrong kind (e.g. an operation passed to `search_software_by_topic`): rejected before the tool runs, because the argument does not match its pattern (the expected form is in the argument description, e.g. `topic_0080`).
- unknown tool id: `Unknown bio.tools id 'x' (ids are case-sensitive). Use search_software_by_topic or search_software_by_operation to find the id of a tool.`
- well-formed but unknown EDAM id: `'x' is not a term of the EDAM ontology. Use resolve_edam_term to find ids.`
- `describe_software` with no match: `No bio.tools record named 'x'. ...`
- `resolve_edam_term` with no word in `text`: `Give at least one word to look for.`
- invalid `kind`, `limit` or `offset`: rejected by validation with the allowed values or range.
- data not loadable (no network, no files): the message names `RSEC_DATA_DIR` and the hosts to reach.

Missing annotations are not errors: they are answered with `note` (`get_software_io`) or an empty `licenses` list (`get_license`).

## 6. Data and startup

| Source | Content |
|--------|---------|
| `bioschemas-dump.ttl` (RSEc `datasets/`, raw.githubusercontent.com) | bio.tools records as schema.org / Bioschemas |
| `EDAM.owl` (edamontology.org, latest release) | labels, synonyms, definitions, hierarchy |

- Missing files are downloaded on first start; existing files are never re-downloaded (delete the data directory to refresh). Downloads are written to a `.part` file and renamed, so a failed download leaves no half-written file.
- Loading runs in the background at startup, so the MCP handshake is not delayed; the first tool call waits for it. A failed load is retried on the next call.
- Other dumps (biocontainers, Galaxy, Debian...) are listed but disabled in `DUMPS` in `graph_store.py`.

| Variable | Default | Role |
|----------|---------|------|
| `RSEC_DATA_DIR` | `mcp/data` | where the files are stored |
| `RSEC_LOG_LEVEL` | `INFO` | logs go to stderr (stdout carries the protocol); `DEBUG` logs every SPARQL query with its time |

## 7. Non-functional requirements

| Requirement | Target | Status (2026-11-11, 740k triples) |
|-------------|--------|-----------------------------------|
| Time of a tool call, once the data is loaded | under 10 s | under 0.01 s for hierarchy, license and inputs/outputs; about 1 to 2 s for EDAM lookup, software search and `describe_software` |
| Start-up load | background, one time | about 7 s plus the first download |
| Memory | about 2 GB free | 1.5 GB measured with all dumps |
| Injection | nothing from the caller reaches a query unescaped | ids pass a strict pydantic pattern and `render` only inserts IRIs built from them, ints, and `Literal.n3()` string literals |
| Query shape | start from a tool IRI or an EDAM term; no `VALUES` list of terms joined to the whole tool pattern (over 10 s on the full data) | see [data-model.md](data-model.md) |
| Tests | no network, no real data | `./run_tests.sh`, tiny fixtures in `tests/fixtures`, a test that every `q_*` query parses, and a test that fails if triple-traversal helpers replace SPARQL |

## 8. Known limits

- About 34,000 tools; only about 4,000 have input/output annotations (`get_software_io` then answers with a `note`; well-known tools can be in that gap).
- The software search takes one EDAM id and always includes the narrower terms; there is no exact-term option, no combination of terms and no name or license filter.
- Whether two tools fit together is left to the agent, which compares their inputs and outputs. Compatible annotations would not prove that the tools have been run together.
- Search has no total, only `count` and paging.
- License text is not normalised beyond trimming.
- EDAM IRIs used in the dumps but absent from EDAM (about 135 junk ids) are ignored.
- One query at a time: concurrent tool calls queue behind each other.

# RSEc data model (as used by the MCP tools)

Observed on the dumps downloaded on 2026-11-03. Re-check these numbers when the upstream dumps change.

`graph_store.py` currently loads only `bioschemas-dump.ttl` and `EDAM.owl` (about 740k triples; the other dumps are commented out in `DUMPS`). The counts in the table below were taken with all the dumps loaded (about 1.04M triples), so the linked-records license path (the `UNION` in `q_license`) returns nothing until they are re-enabled.

| Need | Where it is | Notes |
|------|-------------|-------|
| Tool identity | `schema:SoftwareApplication` with IRI `https://bio.tools/<id>` (34,177) | Other dumps (biocontainers, Galaxy, Debian...) have their own IRIs and point to the bio.tools record with `schema:identifier` |
| Name, description, homepage | `schema:name`, `schema:description`, `schema:url` | |
| EDAM topics | `schema:applicationSubCategory` -> `edam:topic_NNNN` | A few hundred values are operations/data instead; ignored |
| EDAM operations | `schema:featureList` -> `edam:operation_NNNN` | |
| License | `schema:license`, free-text literal | `MIT` 7.3k, `GPL-3.0` 5.2k, but also `None`, `Not licensed`, `unknown`, `GPL (>= 2)`, `perl_5`... Linked records (biocontainers) often carry the license when the bio.tools record has none |
| Inputs / outputs | `bioschemas:input` / `bioschemas:output` (namespace `http://bioschemas.org/`, not https) -> blank node of type `bioschemas:types/FormalParameter` | 4,079 tools with inputs, 3,388 with outputs, 3,268 with both |
| Parameter typing | `schema:additionalType` -> `edam:data_NNNN` (exactly one), `schema:encodingFormat` -> `edam:format_NNNN` (0 to 7; 31% have none), `schema:name` | Some inputs/outputs of Galaxy/Debian records are plain literals (`fasta`, `tabular`); ignored |
| EDAM labels and hierarchy | not in the dumps; loaded from `https://edamontology.org/EDAM.owl` | 3,541 classes, 1,120 deprecated. About 135 IRIs used in the dumps do not exist in EDAM (junk ids such as `operation__R9Fv...`) |

## How the tools query the graph

Each MCP tool in [server.py](../server.py) runs **one** SPARQL query, stored in a constant named `q_<tool>` (`q_resolve_edam_term`, `q_software_io`...). No index is built at startup, and nothing is precomputed: every call runs its query with rdflib through `graph_store.run_query`, which serialises the runs because rdflib's SPARQL parser is not thread-safe. The server only parses the dumps in the background at startup (about 7 s for the 740k triples).

- **Placeholders.** A `q_*` constant is a complete query with `%name` placeholders. `render(query, **values)` fills them and is the only place where a value enters a query. A missing value is an error.
- **What goes in.** An IRI built from a validated id (`%term`, `%tool`), an int (`%limit`, `%offset`), or a string literal escaped by `rdflib.Literal.n3()` (`%exact`, `%pattern`, `%prefix`, `%name`, `%iri`).
- **Validation.** Tool arguments carry a pydantic pattern (`ToolId`, `TopicId`, `OperationId`, `TermId`), so an id that is not well formed is rejected before the tool runs. An id that passes is safe to embed.
- **Shaping.** Python only reshapes the rows into the JSON answer: grouping the inputs and outputs of a tool, mapping a rank to `matched`, nesting the blank nodes of a DESCRIBE. There is no ranking, filtering or paging in Python.
- **Unbound variables.** rdflib drops a result row whose projected variables are all unbound. A query with an `OPTIONAL` therefore also selects a variable that is always bound (`?term_label`, `?name`), otherwise "unknown id" and "no match" would look the same.

rdflib has no query optimiser, so each query starts from something selective (a tool IRI or an EDAM term) and reaches the rest through a property path. A `VALUES` list of terms joined to the whole tool pattern took more than 10 s on the full data; no current query uses one.

Fragments shared by several queries:

```sparql
# a tool is a bio.tools record
?tool a schema:SoftwareApplication ; schema:name ?name .

# ignore the records of the other dumps
FILTER REGEX(STR(?tool), "^https://bio.tools/[A-Za-z0-9_.~+-]+$")

# only numbered EDAM classes (not annotation properties, root classes or junk ids)
FILTER REGEX(STR(?term), "^http://edamontology.org/(topic|operation|data|format)_[0-9]{4}$")
```

Timings were measured on 2026-11-11 on the 740k-triple graph (10 s budget per call). Each is the time of the query alone, once the data is loaded.

## Queries by tool

Every query below is complete and can be pasted into a SPARQL console on the loaded graph (dumps + `EDAM.owl`). The values (`operation_0292`, `blast`...) are examples: the code substitutes the tool arguments in the `%` placeholders of the `q_*` constant.

### `resolve_edam_term` (`q_resolve_edam_term`)

Finds EDAM terms from words, best match first, in one query. Example: `text="sequence alignment"`, no `kind`. 1.0 s (1.3 s with `kind="operation"`).

- A term matches when its label, a synonym (exact, narrow or broad) or its definition contains every word as a whole word. `%pattern` is a regex with one lookahead per word, so the order of the words does not matter.
- Each `UNION` branch gives the match a rank: 0 exact label, 1 exact synonym, 2 words in the label, 3 words in a synonym, 4 words in the definition. `MIN(?rank)` keeps the best one per term, and `matched` in the answer is `label`, `synonym` or `definition` accordingly.
- `kind` restricts `%prefix` (`http://edamontology.org/operation_`...); without it the prefix is `http://edamontology.org/`.
- Deprecated terms are left out. The sort is rank, then shorter label, then IRI.

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX oio: <http://www.geneontology.org/formats/oboInOwl#>
SELECT ?term (MIN(?rank) AS ?best) (SAMPLE(?lab) AS ?label) (SAMPLE(?def) AS ?definition) WHERE {
  { ?term rdfs:label ?text BIND(IF(LCASE(STR(?text)) = "sequence alignment", 0, 2) AS ?rank) }
  UNION { ?term oio:hasExactSynonym ?text BIND(IF(LCASE(STR(?text)) = "sequence alignment", 1, 3) AS ?rank) }
  UNION { ?term oio:hasNarrowSynonym ?text BIND(IF(LCASE(STR(?text)) = "sequence alignment", 1, 3) AS ?rank) }
  UNION { ?term oio:hasBroadSynonym ?text BIND(IF(LCASE(STR(?text)) = "sequence alignment", 1, 3) AS ?rank) }
  UNION { ?term oio:hasDefinition ?text BIND(4 AS ?rank) }
  FILTER (REGEX(STR(?term), "^http://edamontology.org/(topic|operation|data|format)_[0-9]{4}$")
          && STRSTARTS(STR(?term), "http://edamontology.org/"))
  FILTER REGEX(LCASE(STR(?text)), "^(?=.*(^|\\W)sequence(\\W|$))(?=.*(^|\\W)alignment(\\W|$))", "s")
  FILTER NOT EXISTS { ?term owl:deprecated "true"^^xsd:boolean }
  ?term rdfs:label ?lab .
  OPTIONAL { ?term oio:hasDefinition ?def }
}
GROUP BY ?term
ORDER BY ?best STRLEN(?label) ?term
LIMIT 10
```

### `get_direct_term_parents` (`q_direct_term_parents`)

The broader terms one level up. Example: `term="operation_0292"`. Under 0.01 s, 3 rows.

The term's own label is selected outside the `OPTIONAL`, so a root term (no parent) still gives a row and an unknown term gives none (which the tool turns into an error).

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?term_label ?parent ?label WHERE {
  <http://edamontology.org/operation_0292> rdfs:label ?term_label .
  OPTIONAL {
    <http://edamontology.org/operation_0292> rdfs:subClassOf ?parent .
    FILTER REGEX(STR(?parent), "^http://edamontology.org/(topic|operation|data|format)_[0-9]{4}$")
    ?parent rdfs:label ?label .
  }
}
ORDER BY ?parent
```

### `get_direct_term_child` (`q_direct_term_children`)

The narrower terms one level down, deprecated ones left out. Example: `term="operation_0292"`. Under 0.01 s, 8 rows. Same shape as the parents query.

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT DISTINCT ?term_label ?child ?label WHERE {
  <http://edamontology.org/operation_0292> rdfs:label ?term_label .
  OPTIONAL {
    ?child rdfs:subClassOf <http://edamontology.org/operation_0292> .
    FILTER REGEX(STR(?child), "^http://edamontology.org/(topic|operation|data|format)_[0-9]{4}$")
    ?child rdfs:label ?label .
    FILTER NOT EXISTS { ?child owl:deprecated "true"^^xsd:boolean }
  }
}
ORDER BY ?child
```

### `search_software_by_topic` (`q_software_by_topic`)

Tools annotated with a topic or any narrower topic, sorted by name and paged by the query itself. Example: `topic="topic_0080"`, `limit=20`, `offset=0`. About 2 s.

The query starts from the term and walks down with `rdfs:subClassOf*`, which includes the term itself. `MIN(?name)` and `GROUP BY ?tool` give one row per tool even if the record has several names. The answer has no total: `count` equal to `limit` means another page may exist.

```sparql
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?tool (MIN(?name) AS ?tool_name) WHERE {
  ?topic rdfs:subClassOf* <http://edamontology.org/topic_0080> .
  ?tool schema:applicationSubCategory ?topic .
  ?tool a schema:SoftwareApplication ; schema:name ?name .
  FILTER REGEX(STR(?tool), "^https://bio.tools/[A-Za-z0-9_.~+-]+$")
}
GROUP BY ?tool
ORDER BY LCASE(?tool_name) ?tool
LIMIT 20 OFFSET 0
```

### `search_software_by_operation` (`q_software_by_operation`)

Same as the topic search, with `schema:featureList` in place of `schema:applicationSubCategory`. Example: `operation="operation_0292"`. 1.5 s.

```sparql
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?tool (MIN(?name) AS ?tool_name) WHERE {
  ?operation rdfs:subClassOf* <http://edamontology.org/operation_0292> .
  ?tool schema:featureList ?operation .
  ?tool a schema:SoftwareApplication ; schema:name ?name .
  FILTER REGEX(STR(?tool), "^https://bio.tools/[A-Za-z0-9_.~+-]+$")
}
GROUP BY ?tool
ORDER BY LCASE(?tool_name) ?tool
LIMIT 20 OFFSET 0
```

Combining several topics or operations, filtering by name or license, and showing the license in the list are no longer part of the search. Use `get_license` on each tool, or `describe_software`.

### `get_license` (`q_license`)

The license(s) of a tool. Example: `tool="blast"`. Under 0.01 s.

The license is the tool's own, or the one of a record from another dump (biocontainers, Galaxy...) that points to it with `schema:identifier`. The name is selected outside the `OPTIONAL`, so a known tool without license still gives a row. The values are cleaned (trimmed, de-duplicated, sorted) in Python.

```sparql
PREFIX schema: <http://schema.org/>
SELECT DISTINCT ?name ?license WHERE {
  <https://bio.tools/blast> a schema:SoftwareApplication ; schema:name ?name .
  OPTIONAL {
    { <https://bio.tools/blast> schema:license ?license }
    UNION
    { ?record schema:identifier <https://bio.tools/blast> ; schema:license ?license }
  }
}
```

### `get_software_io` (`q_software_io`)

Every annotated input and output of a tool, with the label of its EDAM data type and formats. Example: `tool="clustalw"`. 0.01 s.

The tool and its name are selected outside the `OPTIONAL`, so an unannotated tool still gives a row (the answer then carries a `note`). Python groups the rows into one parameter per (parameter node, data type) with its set of formats. Parameters without `schema:additionalType` are not returned.

```sparql
PREFIX schema: <http://schema.org/>
PREFIX bs: <http://bioschemas.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?name ?dir ?param ?pname ?dtype ?dlabel ?fmt ?flabel WHERE {
  <https://bio.tools/clustalw> a schema:SoftwareApplication ; schema:name ?name .
  OPTIONAL {
    { <https://bio.tools/clustalw> bs:input ?param . BIND("input" AS ?dir) }
    UNION { <https://bio.tools/clustalw> bs:output ?param . BIND("output" AS ?dir) }
    ?param schema:additionalType ?dtype .
    OPTIONAL { ?param schema:name ?pname }
    OPTIONAL { ?dtype rdfs:label ?dlabel }
    OPTIONAL { ?param schema:encodingFormat ?fmt . OPTIONAL { ?fmt rdfs:label ?flabel } }
  }
}
```

### `describe_software` (`q_describe_software`)

Everything the data holds about a tool: a SPARQL `DESCRIBE`. Example: `name="SAMtools"`. 1.4 s, 87 triples.

The request is lower-cased and compared with the `schema:name` (any case) or with the bio.tools id (any case), so a name such as `SAMtools`, an id such as `samtools` and a differently cased id all work. Several records can match, hence `count` in the answer. rdflib's `DESCRIBE` follows blank nodes, so the inputs and outputs come with the record. Python turns the triples into nested objects: `schema:`, `bioschemas:` and `rdf:` prefixes on the predicates, blank nodes nested, values cut after 1000 characters.

```sparql
PREFIX schema: <http://schema.org/>
DESCRIBE ?tool WHERE {
  ?tool a schema:SoftwareApplication ; schema:name ?name .
  FILTER REGEX(STR(?tool), "^https://bio.tools/[A-Za-z0-9_.~+-]+$")
  FILTER (LCASE(STR(?name)) = "samtools" || LCASE(STR(?tool)) = "https://bio.tools/samtools")
}
```

## EDAM in the server

EDAM is not in the dumps; `EDAM.owl` is loaded in the same graph and queried by the tool queries above. Only numbered classes are used (`topic|operation|data|format` followed by four digits), which leaves out annotation properties, root classes and the junk ids found in the dumps.

[edam.py](../edam.py) now serves `server.py` with two names only: `edam.NS` (the namespace) and `edam.normalize`, which turns `operation_0292`, `edam:operation_0292` or a full IRI into the IRI that `term_iri` embeds in a query. Its other functions (`terms`, `labels`, `ancestors`, `search`) are kept and tested but no tool uses them: each tool now has its own query.

## What the tools do not do

| Not available | Why |
| --- | --- |
| Filter or search software by license or by name | `search_software_by_*` take one EDAM id; use `describe_software` to find a tool by name |
| Search by several terms at once | one fixed query per tool |
| Exclude the narrower terms of a search | the walk down (`rdfs:subClassOf*`) is part of the query |
| Check that two tools can be chained | compare the `get_software_io` results |
| Suggest an id for an unknown tool | the error only says the id is unknown (ids are case-sensitive); `describe_software` accepts any case |

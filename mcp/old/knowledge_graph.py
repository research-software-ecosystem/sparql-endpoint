"""Knowledge layer over the RSEc graph: search, EDAM lookup and tool composition.

Every public method answers with SPARQL, executed by rdflib through
`graph_store.run_query` (never the triple-traversal helpers), on each call: there is no
precomputed index. Queries are written to start from something selective (an EDAM
term or a tool IRI, reached through a property path) because rdflib has no query
optimiser: a `VALUES` list joined to a large pattern took more than 10 s on the
real data. Measurements: docs/data-model.md.
"""

import difflib
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import NamedTuple

from rdflib import Graph, Literal

import edam as edam_mod
from graph_store import run_query

log = logging.getLogger("rsec.knowledge_graph")

BIOTOOLS = "https://bio.tools/"
_TOOL_ID = re.compile(r"^[A-Za-z0-9_.~+-]+$")

_PREFIXES = """\
PREFIX schema: <http://schema.org/>
PREFIX bs: <http://bioschemas.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""
# A "tool" is a bio.tools record: a SoftwareApplication whose IRI is https://bio.tools/<id>.
_TOOL = "?tool a schema:SoftwareApplication ; schema:name ?name ."
_ONLY_BIOTOOLS = f'FILTER REGEX(STR(?tool), "^{BIOTOOLS}[A-Za-z0-9_.~+-]+$")'

# The license of a tool: its own, or the one of a record from another dump
# (biocontainers, Galaxy...) that points to it with schema:identifier.
_LICENSE = "{ ?tool schema:license ?license } UNION { ?record schema:identifier ?tool ; schema:license ?license }"

# How well an output can feed an input, best first.
KIND_RANK = {
    "exact": 0,
    "subtype": 1,
    "format_unspecified": 2,
    "needs_conversion": 3,
    "generic_input": 4,
}
LEVEL_OF_KIND = {
    "exact": "direct",
    "subtype": "direct",
    "format_unspecified": "probable",
    "needs_conversion": "needs_conversion",
    "generic_input": "generic",
}
LEVELS = ("direct", "probable", "needs_conversion", "generic")  # cumulative, strictest first
# EDAM's root data type: an input typed "Data" accepts every output, which says
# nothing about real compatibility (644 inputs use it), so it is a separate, weakest tier.
DATA_ROOT = edam_mod.NS + "data_0006"
MAX_DESCRIPTION = 300


class Tool(NamedTuple):
    iri: str
    name: str

    @property
    def id(self) -> str:
        return self.iri.removeprefix(BIOTOOLS)


@dataclass(frozen=True)
class Param:
    name: str
    data: str  # EDAM data IRI
    formats: tuple[str, ...]  # EDAM format IRIs, possibly empty


def _lit(text: str) -> str:
    return Literal(text).n3()


def _group_params(rows) -> dict[tuple, Param]:
    """Rows (?param ?dtype ?name ?fmt, optionally ?tool) -> one Param per (tool, param, type)."""
    grouped: dict[tuple, dict] = {}
    for row in rows:
        key = (str(row.get("tool", "")), str(row.param), str(row.dtype))
        p = grouped.setdefault(key, {"name": "", "formats": set()})
        if row.name is not None:
            p["name"] = min(p["name"], str(row.name)) if p["name"] else str(row.name)
        if row.fmt is not None:
            p["formats"].add(str(row.fmt))
    return {k: Param(v["name"], k[2], tuple(sorted(v["formats"]))) for k, v in grouped.items()}


class KnowledgeGraph:
    def __init__(self, graph: Graph):
        self.graph = graph

    def _select(self, what: str, query: str) -> list:
        t0 = time.perf_counter()
        rows = run_query(self.graph, query)
        log.debug("%s: %d rows in %.2f s", what, len(rows), time.perf_counter() - t0)
        return rows

    # ---------------------------------------------------------------- tools

    def resolve_tool(self, raw: str) -> Tool:
        s = raw.strip()
        for prefix in (BIOTOOLS, "biotools:"):
            if s.lower().startswith(prefix):
                s = s[len(prefix):]
                break
        if _TOOL_ID.match(s):
            names = {str(r.name): None for r in self._select("tool by id", _PREFIXES + f"""\
SELECT ?name WHERE {{ <{BIOTOOLS}{s}> a schema:SoftwareApplication ; schema:name ?name }}""")}
            if names:
                return Tool(BIOTOOLS + s, min(names))
            # ids are case-sensitive in the IRIs, users are not
            rows = self._select("tool by lower-case id", _PREFIXES + f"""\
SELECT ?tool ?name WHERE {{
  {_TOOL}
  FILTER (LCASE(STR(?tool)) = {_lit((BIOTOOLS + s).lower())})
}}""")
            if rows:
                first = min(str(r.tool) for r in rows)
                return Tool(first, min(str(r.name) for r in rows if str(r.tool) == first))
        ids = sorted({str(r.tool).removeprefix(BIOTOOLS) for r in self._select("tool ids", _PREFIXES + f"""\
SELECT DISTINCT ?tool WHERE {{ ?tool a schema:SoftwareApplication . {_ONLY_BIOTOOLS} }}""")})
        close = difflib.get_close_matches(s.lower(), {i.lower(): i for i in ids}, n=3, cutoff=0.8)
        by_lower = {i.lower(): i for i in ids}
        hint = f" Did you mean: {', '.join(by_lower[c] for c in close)}?" if close else ""
        raise ValueError(
            f"Unknown bio.tools id {raw!r}.{hint} "
            "Use search_software (name_contains=...) to find the id of a tool."
        )

    @staticmethod
    def _ref(tool: Tool) -> dict:
        return {"biotools_id": tool.id, "name": tool.name}

    # ------------------------------------------------------------- EDAM terms

    def _edam_iri(self, raw: str, kind: str) -> str:
        iri = edam_mod.normalize(raw)
        if not iri.removeprefix(edam_mod.NS).startswith(kind + "_"):
            raise ValueError(f"{raw!r} is not a {kind} term (expected '{kind}_NNNN')")
        return iri

    @staticmethod
    def _term(iri: str, labels: dict[str, str]) -> dict:
        return {"id": iri.removeprefix(edam_mod.NS), "label": labels.get(iri, "")}

    def _param(self, p: Param, labels: dict[str, str]) -> dict:
        return {
            "name": p.name,
            "data": self._term(p.data, labels),
            "formats": [self._term(f, labels) for f in p.formats],
        }

    def _param_labels(self, params) -> dict[str, str]:
        return edam_mod.labels(self.graph, {i for p in params for i in (p.data, *p.formats)})

    def resolve_edam_term(self, text: str, kind: str | None = None, limit: int = 10) -> dict:
        if kind is not None and kind not in edam_mod.KINDS:
            raise ValueError(f"kind must be one of {', '.join(edam_mod.KINDS)}")
        hits = edam_mod.search(self.graph, text, kind=kind, limit=limit)
        parent_labels = edam_mod.labels(self.graph, {p for h in hits for p in h.term.parents})
        n_tools = self._n_tools([h.term.iri for h in hits if h.term.kind in ("topic", "operation")])
        out = []
        for hit in hits:
            t = hit.term
            out.append({
                "id": t.id,
                "label": t.label,
                "kind": t.kind,
                "matched": hit.matched,
                "definition": t.definition[:MAX_DESCRIPTION],
                "synonyms": list(t.synonyms[:5]),
                "broader": [self._term(p, parent_labels) for p in t.parents],
                # data and format terms type parameters, not tools
                "n_tools": n_tools.get(t.iri, 0) if t.kind in ("topic", "operation") else None,
            })
        return {"results": out}

    def _n_tools(self, iris: list[str]) -> dict[str, int]:
        """Tools annotated directly with each topic/operation (narrower terms not counted).

        One UNION branch per term, each starting from the term itself: a VALUES list
        joined to the tool pattern took 30 s on the real data.
        """
        predicate = {"topic": "applicationSubCategory", "operation": "featureList"}
        branches = [
            f"{{ ?tool schema:{predicate[i.removeprefix(edam_mod.NS).split('_')[0]]} <{i}> . {_TOOL} {_ONLY_BIOTOOLS} BIND(<{i}> AS ?term) }}"
            for i in iris if edam_mod.is_edam(i)
        ]
        if not branches:
            return {}
        rows = self._select("tools per term", _PREFIXES + f"""\
SELECT ?term (COUNT(DISTINCT ?tool) AS ?n) WHERE {{
  {" UNION ".join(branches)}
}} GROUP BY ?term""")
        return {str(r.term): int(r.n) for r in rows}

    # --------------------------------------------------------------- search

    def _edam_pattern(self, var: str, predicate: str, kind: str, ids: list[str], subterms: bool) -> str:
        """Tools annotated with any of the terms (or, with `subterms`, any narrower term)."""
        iris = [self._edam_iri(raw, kind) for raw in ids]
        if subterms:  # start from the selective end: the term, then its narrower terms
            branches = [f"?{var} rdfs:subClassOf* <{i}> . ?tool schema:{predicate} ?{var} ." for i in iris]
        else:
            branches = [f"?tool schema:{predicate} <{i}> ." for i in iris]
        return branches[0] if len(branches) == 1 else " UNION ".join("{ " + b + " }" for b in branches)

    def search_software(
        self,
        topics: list[str] | None = None,
        operations: list[str] | None = None,
        licenses: list[str] | None = None,
        name_contains: str | None = None,
        include_subterms: bool = True,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        patterns, filters = [], []
        if topics:
            patterns.append(self._edam_pattern("topic", "applicationSubCategory", "topic", topics, include_subterms))
        if operations:
            patterns.append(self._edam_pattern("op", "featureList", "operation", operations, include_subterms))
        patterns.append(_TOOL)
        if licenses:
            patterns.append(_LICENSE)
            wanted = ", ".join(_lit(lic.strip().lower()) for lic in licenses)
            filters.append(f"LCASE(STR(?license)) IN ({wanted})")
        if name_contains:
            needle = _lit(name_contains.strip().lower())
            filters.append(f"(CONTAINS(LCASE(?name), {needle}) || CONTAINS(LCASE(STR(?tool)), {needle}))")
        body = "\n  ".join(patterns)
        where_filter = " && ".join(filters)
        rows = self._select("search", _PREFIXES + f"""\
SELECT DISTINCT ?tool ?name WHERE {{
  {body}
  {_ONLY_BIOTOOLS}
  {f"FILTER ({where_filter})" if where_filter else ""}
}}""")
        names: dict[str, str] = {}
        for r in rows:  # a tool with several names keeps the smallest, so the choice is stable
            tool, name = str(r.tool), str(r.name)
            if tool not in names or name < names[tool]:
                names[tool] = name
        ordered = sorted(names, key=lambda t: (names[t].lower(), t))
        page = ordered[offset: offset + limit]
        return {
            "total": len(ordered),
            "limit": limit,
            "offset": offset,
            "results": self._summaries([Tool(t, names[t]) for t in page]),
        }

    def _summaries(self, tools: list[Tool]) -> list[dict]:
        """Description, url, licenses, topics and operations of the tools of one page."""
        if not tools:
            return []
        edam_ns = edam_mod.NS
        values = " ".join(f"<{t.iri}>" for t in tools)
        rows = self._select("page details", _PREFIXES + f"""\
SELECT ?tool ?field ?val ?label WHERE {{
  VALUES ?tool {{ {values} }}
  {{ ?tool schema:description ?val BIND("description" AS ?field) }}
  UNION {{ ?tool schema:url ?val BIND("url" AS ?field) }}
  UNION {{ ?tool schema:applicationSubCategory ?val
          FILTER STRSTARTS(STR(?val), "{edam_ns}topic_") OPTIONAL {{ ?val rdfs:label ?label }}
          BIND("topic" AS ?field) }}
  UNION {{ ?tool schema:featureList ?val
          FILTER STRSTARTS(STR(?val), "{edam_ns}operation_") OPTIONAL {{ ?val rdfs:label ?label }}
          BIND("operation" AS ?field) }}
  UNION {{ {_LICENSE.replace("?license", "?val")} BIND("license" AS ?field) }}
}}""")
        text: dict[tuple[str, str], str] = {}
        licenses: dict[str, set[str]] = defaultdict(set)
        terms: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
        for r in rows:
            tool, field, value = str(r.tool), str(r.field), str(r.val)
            if field in ("description", "url"):  # several values: keep the smallest
                if (tool, field) not in text or value < text[(tool, field)]:
                    text[(tool, field)] = value
            elif field == "license":
                if value.strip():
                    licenses[tool].add(value.strip())
            else:
                label = str(r.label) if r.label is not None else ""
                old = terms[(tool, field)].get(value)
                terms[(tool, field)][value] = min(old, label) if old is not None else label
        return [
            {
                "biotools_id": t.id,
                "iri": t.iri,
                "name": t.name,
                "description": text.get((t.iri, "description"), "")[:MAX_DESCRIPTION],
                "url": text.get((t.iri, "url"), ""),
                "licenses": sorted(licenses.get(t.iri, ())),
                "topics": self._sorted_terms(terms.get((t.iri, "topic"), {})),
                "operations": self._sorted_terms(terms.get((t.iri, "operation"), {})),
            }
            for t in tools
        ]

    @staticmethod
    def _sorted_terms(labelled: dict[str, str]) -> list[dict]:
        return [KnowledgeGraph._term(iri, labelled) for iri in sorted(labelled)]

    def list_licenses(self, contains: str | None = None, limit: int = 50) -> dict:
        needle = f"FILTER CONTAINS(LCASE(STR(?license)), {_lit(contains.strip().lower())})" if contains else ""
        rows = self._select("licenses", _PREFIXES + f"""\
SELECT DISTINCT ?tool ?license WHERE {{
  {_LICENSE}
  {_TOOL}
  {_ONLY_BIOTOOLS}
  {needle}
}}""")
        tools: dict[str, set[str]] = defaultdict(set)
        spellings: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for r in rows:
            text = str(r.license).strip()
            if text:
                tools[text.lower()].add(str(r.tool))
                spellings[text.lower()][text] += 1
        counted = [
            (min(spellings[k], key=lambda s: (-spellings[k][s], s)), len(v)) for k, v in tools.items()
        ]
        counted.sort(key=lambda r: (-r[1], r[0]))
        return {
            "total": len(counted),
            "licenses": [{"license": lic, "tools": n} for lic, n in counted[:limit]],
        }

    # --------------------------------------------------------- input/output

    def _params(self, tool: Tool) -> dict[str, list[Param]]:
        rows = self._select("input/output", _PREFIXES + f"""\
SELECT ?dir ?param ?name ?dtype ?fmt WHERE {{
  {{ <{tool.iri}> bs:input ?param . BIND("input" AS ?dir) }}
  UNION {{ <{tool.iri}> bs:output ?param . BIND("output" AS ?dir) }}
  ?param schema:additionalType ?dtype .
  OPTIONAL {{ ?param schema:name ?name }}
  OPTIONAL {{ ?param schema:encodingFormat ?fmt }}
}}""")
        by_dir: dict[str, list[Param]] = {"input": [], "output": []}
        directions = {str(r.param): str(r.dir) for r in rows}
        for (_, param, _), p in _group_params(rows).items():
            by_dir[directions[param]].append(p)
        for params in by_dir.values():
            params.sort(key=lambda p: (p.name, p.data, p.formats))
        return by_dir

    def get_software_io(self, tool: str) -> dict:
        t = self.resolve_tool(tool)
        io = self._params(t)
        labels = self._param_labels(io["input"] + io["output"])
        res = {
            **self._ref(t),
            "inputs": [self._param(p, labels) for p in io["input"]],
            "outputs": [self._param(p, labels) for p in io["output"]],
        }
        if not io["input"] and not io["output"]:
            res["note"] = "No input/output annotation for this tool in the RSEc data."
        return res

    @staticmethod
    def _match(out: Param, inp: Param, broader: dict[str, frozenset[str]]) -> str | None:
        """How `out` can feed `inp`, or None. Output may be narrower, never broader.

        `broader` maps each EDAM IRI of the output (data type and formats) to itself
        and its broader terms.
        """
        if inp.data not in broader[out.data]:
            return None
        if inp.data == DATA_ROOT and out.data != DATA_ROOT:
            return "generic_input"
        if not out.formats or not inp.formats:
            return "format_unspecified"
        if set(out.formats) & set(inp.formats):
            return "exact" if out.data == inp.data else "subtype"
        if any(f in broader[of] for of in out.formats for f in inp.formats):
            return "subtype"
        return "needs_conversion"

    def _broader(self, params: list[Param]) -> dict[str, frozenset[str]]:
        return edam_mod.ancestors(self.graph, {i for p in params for i in (p.data, *p.formats)})

    def check_composability(self, producer: str, consumer: str) -> dict:
        p, c = self.resolve_tool(producer), self.resolve_tool(consumer)
        res = {"producer": self._ref(p), "consumer": self._ref(c)}
        outs, ins = self._params(p)["output"], self._params(c)["input"]
        if not outs or not ins:
            missing = []
            if not outs:
                missing.append(f"{p.id} has no annotated output")
            if not ins:
                missing.append(f"{c.id} has no annotated input")
            return {**res, "compatibility": "unknown", "matches": [],
                    "note": "Cannot decide: " + " and ".join(missing) + " in the RSEc data."}
        broader = self._broader(outs)
        found = [(kind, o, i) for o in outs for i in ins if (kind := self._match(o, i, broader))]
        found.sort(key=lambda m: KIND_RANK[m[0]])
        labels = self._param_labels([x for _, o, i in found for x in (o, i)])
        matches = [{"kind": k, "output": self._param(o, labels), "input": self._param(i, labels)}
                   for k, o, i in found]
        compat = LEVEL_OF_KIND[matches[0]["kind"]] if matches else "none"
        return {**res, "compatibility": compat, "matches": matches}

    def _candidate_inputs(self, producer: Tool) -> dict[Tool, list[Param]]:
        """Inputs of other tools typed like an output of `producer`, or with a broader type."""
        rows = self._select("candidate inputs", _PREFIXES + f"""\
SELECT DISTINCT ?tool ?tname ?param ?name ?dtype ?fmt WHERE {{
  <{producer.iri}> bs:output ?out .
  ?out schema:additionalType ?outtype .
  ?outtype rdfs:subClassOf* ?dtype .
  ?param schema:additionalType ?dtype .
  ?tool bs:input ?param .
  ?tool a schema:SoftwareApplication ; schema:name ?tname .
  OPTIONAL {{ ?param schema:name ?name }}
  OPTIONAL {{ ?param schema:encodingFormat ?fmt }}
  {_ONLY_BIOTOOLS}
  FILTER (?tool != <{producer.iri}>)
}}""")
        names: dict[str, str] = {}
        for r in rows:
            tool, name = str(r.tool), str(r.tname)
            if tool not in names or name < names[tool]:
                names[tool] = name
        inputs: dict[Tool, list[Param]] = defaultdict(list)
        for (tool, _, _), p in _group_params(rows).items():
            inputs[Tool(tool, names[tool])].append(p)
        for params in inputs.values():
            params.sort(key=lambda p: (p.name, p.data, p.formats))
        return inputs

    def find_downstream_tools(
        self, producer: str, level: str = "probable", limit: int = 20, offset: int = 0
    ) -> dict:
        if level not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}")
        p = self.resolve_tool(producer)
        outs = self._params(p)["output"]
        res = {"producer": self._ref(p), "level": level, "limit": limit, "offset": offset}
        if not outs:
            return {**res, "total": 0, "results": [],
                    "note": f"{p.id} has no annotated output in the RSEc data."}
        allowed = {k for k, lv in LEVEL_OF_KIND.items() if LEVELS.index(lv) <= LEVELS.index(level)}
        broader = self._broader(outs)
        best: dict[Tool, tuple[int, Param, Param, str]] = {}
        for tool, inputs in self._candidate_inputs(p).items():
            for o in outs:
                for inp in inputs:
                    kind = self._match(o, inp, broader)
                    if kind in allowed and (tool not in best or KIND_RANK[kind] < best[tool][0]):
                        best[tool] = (KIND_RANK[kind], o, inp, kind)
        ordered = sorted(best, key=lambda t: (best[t][0], t.name.lower(), t.iri))
        page = ordered[offset: offset + limit]
        labels = self._param_labels([x for t in page for x in best[t][1:3]])
        results = []
        for t in page:
            _, o, inp, kind = best[t]
            results.append({
                **self._ref(t),
                "compatibility": LEVEL_OF_KIND[kind],
                "kind": kind,
                "output": self._param(o, labels),
                "input": self._param(inp, labels),
            })
        return {**res, "total": len(ordered), "results": results}

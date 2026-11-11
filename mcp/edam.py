"""EDAM ontology queries: term lookup by label/synonym and is-a hierarchy.

EDAM (http://edamontology.org) types the topics, operations, data and formats
used by bio.tools. The RSEc dumps only reference its IRIs, so labels and the
hierarchy come from the EDAM ontology file loaded next to the dumps.

Every function answers with SPARQL, executed by rdflib through `graph_store.run_query`
(never the triple-traversal helpers), on each call: nothing is precomputed.
"""

import re
from dataclasses import dataclass
from functools import cached_property

from rdflib import Graph, Literal

from graph_store import run_query

NS = "http://edamontology.org/"
KINDS = ("topic", "operation", "data", "format")
_ID = re.compile(r"^(topic|operation|data|format)_\d{4}$")
_EDAM_IRI = re.compile(rf"^{re.escape(NS)}(topic|operation|data|format)_\d{{4}}$")
_OIO = "http://www.geneontology.org/formats/oboInOwl#"

# Only numbered EDAM classes (topic_0091...), not annotation properties, root
# classes or the junk ids found in the dumps.
ID_REGEX = f"^{NS}(topic|operation|data|format)_[0-9]{{4}}$"

PREFIXES = f"""\
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX oio: <{_OIO}>
"""


def normalize(raw: str) -> str:
    """'operation_0292' | 'edam:operation_0292' | full IRI -> full IRI.

    Strict on purpose: the result is safe to embed in a SPARQL query.
    """
    s = raw.strip().lower()
    for prefix in (NS, "edam:"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if not _ID.match(s):
        raise ValueError(
            f"{raw!r} is not an EDAM term id; expected e.g. 'topic_0091', "
            "'operation_0292', 'data_2044' or 'format_1929'"
        )
    return NS + s


def is_edam(iri: str) -> bool:
    """True for a numbered EDAM class IRI (and so safe to embed in a query)."""
    return bool(_EDAM_IRI.match(iri))


def _values(var: str, iris) -> str:
    """A VALUES clause over EDAM IRIs; anything else is dropped, so it is safe to embed."""
    kept = sorted({i for i in iris if _EDAM_IRI.match(i)})
    return f"VALUES ?{var} {{ {' '.join(f'<{i}>' for i in kept)} }}"


@dataclass(frozen=True)
class Term:
    iri: str
    label: str
    definition: str = ""
    synonyms: tuple[str, ...] = ()
    parents: tuple[str, ...] = ()

    @cached_property
    def id(self) -> str:
        return self.iri[len(NS):]

    @cached_property
    def kind(self) -> str:
        return self.id.split("_")[0]


@dataclass(frozen=True)
class Hit:
    term: Term
    matched: str  # "label" | "synonym" | "definition"


# ---- lookup ------------------------------------------------------------------


def terms(g: Graph, iris) -> dict[str, Term]:
    """Label, definition, synonyms and direct parents of the given EDAM terms."""
    iris = [i for i in set(iris) if _EDAM_IRI.match(i)]
    if not iris:
        return {}
    query = PREFIXES + f"""\
SELECT ?term ?field ?val WHERE {{
  {_values("term", iris)}
  {{ ?term a owl:Class ; rdfs:label ?val BIND("label" AS ?field) }}
  UNION {{ ?term oio:hasDefinition ?val BIND("definition" AS ?field) }}
  UNION {{ ?term oio:hasExactSynonym ?val BIND("synonym" AS ?field) }}
  UNION {{ ?term oio:hasNarrowSynonym ?val BIND("synonym" AS ?field) }}
  UNION {{ ?term oio:hasBroadSynonym ?val BIND("synonym" AS ?field) }}
  UNION {{ ?term rdfs:subClassOf ?val
          FILTER REGEX(STR(?val), "{ID_REGEX}") BIND("parent" AS ?field) }}
}}
"""
    fields: dict[str, dict[str, set[str]]] = {}
    for row in run_query(g, query):
        fields.setdefault(str(row.term), {}).setdefault(str(row.field), set()).add(str(row.val))
    return {
        iri: Term(
            iri,
            min(f["label"]),
            min(f.get("definition", {""})),
            tuple(sorted(f.get("synonym", ()))),
            tuple(sorted(f.get("parent", ()))),
        )
        for iri, f in fields.items()
        if "label" in f
    }


def labels(g: Graph, iris) -> dict[str, str]:
    return {iri: t.label for iri, t in terms(g, iris).items()}


def ancestors(g: Graph, iris) -> dict[str, frozenset[str]]:
    """For each IRI, the term itself and all its broader terms (transitive).

    An IRI that is not an EDAM term (the dumps contain a few) only has itself.
    """
    found: dict[str, set[str]] = {i: {i} for i in iris}
    valid = [i for i in found if _EDAM_IRI.match(i)]
    if valid:
        query = PREFIXES + f"""\
SELECT ?term ?ancestor WHERE {{
  {_values("term", valid)}
  ?term rdfs:subClassOf* ?ancestor .
  FILTER REGEX(STR(?ancestor), "{ID_REGEX}")
}}
"""
        for row in run_query(g, query):
            found[str(row.term)].add(str(row.ancestor))
    return {k: frozenset(v) for k, v in found.items()}


# ---- search ------------------------------------------------------------------


def _q_search(words: list[str], kind: str | None, include_deprecated: bool) -> str:
    """Terms where ONE field (label, synonym or definition) contains every word."""
    contains = " && ".join(
        f"CONTAINS(LCASE(STR(?text)), {Literal(w).n3()})" for w in words
    )
    prefix = NS + (f"{kind}_" if kind else "")
    not_deprecated = (
        "" if include_deprecated
        else 'FILTER NOT EXISTS { ?term owl:deprecated "true"^^xsd:boolean }'
    )
    return PREFIXES + f"""\
SELECT DISTINCT ?term WHERE {{
  {{ ?term rdfs:label ?text }}
  UNION {{ ?term oio:hasExactSynonym ?text }}
  UNION {{ ?term oio:hasNarrowSynonym ?text }}
  UNION {{ ?term oio:hasBroadSynonym ?text }}
  UNION {{ ?term oio:hasDefinition ?text }}
  FILTER (REGEX(STR(?term), "{ID_REGEX}") && STRSTARTS(STR(?term), {Literal(prefix).n3()}))
  FILTER ({contains})
  {not_deprecated}
}}
"""


def search(
    g: Graph,
    text: str,
    kind: str | None = None,
    limit: int = 10,
    include_deprecated: bool = False,
) -> list[Hit]:
    """Find terms by label, synonym or definition, best matches first.

    SPARQL selects the candidates (a field contains every word); they are
    then ranked: exact label, exact synonym, all words in label, all words in
    a synonym, all words in the definition. Ties: shorter label first.
    """
    q = text.strip().lower()
    words = re.findall(r"\w+", q)
    if not words:
        return []
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    candidates = [str(r.term) for r in run_query(g, _q_search(words, kind, include_deprecated))]
    scored: list[tuple[int, int, str, Hit]] = []
    for t in terms(g, candidates).values():
        label = t.label.lower()
        syns = [s.lower() for s in t.synonyms]

        def has_all(s: str) -> bool:
            toks = set(re.findall(r"\w+", s))
            return all(w in toks for w in words)

        if label == q:
            rank, how = 0, "label"
        elif q in syns:
            rank, how = 1, "synonym"
        elif has_all(label):
            rank, how = 2, "label"
        elif any(has_all(s) for s in syns):
            rank, how = 3, "synonym"
        elif has_all(t.definition.lower()):
            rank, how = 4, "definition"
        else:
            continue  # substring hit only (e.g. "sequences" for "sequence")
        scored.append((rank, len(label), t.id, Hit(t, how)))
    scored.sort(key=lambda x: x[:3])
    return [h for *_, h in scored[:limit]]

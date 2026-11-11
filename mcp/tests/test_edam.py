import ast
import unittest
from pathlib import Path

from rdflib import Graph

import edam

EDAM_TTL = """
@prefix e: <http://edamontology.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix oio: <http://www.geneontology.org/formats/oboInOwl#> .

e:topic_0091 a owl:Class ; rdfs:label "Bioinformatics" .
e:topic_0080 a owl:Class ; rdfs:label "Sequence analysis" ; rdfs:subClassOf e:topic_0091 ;
    oio:hasExactSynonym "Sequence analysis (general)" ; oio:hasDefinition "Analysing sequences." .
e:topic_0102 a owl:Class ; rdfs:label "Mapping" ; rdfs:subClassOf e:topic_0080 ;
    oio:hasNarrowSynonym "Read mapping" .
e:topic_9999 a owl:Class ; rdfs:label "Old mapping" ; owl:deprecated true ; rdfs:subClassOf e:topic_0080 .
e:operation_0292 a owl:Class ; rdfs:label "Sequence alignment" .
e:data_0849 a owl:Class ; rdfs:label "Sequence record" .
e:data_2044 a owl:Class ; rdfs:label "Sequence" ; rdfs:subClassOf e:data_0849 .
e:data_2976 a owl:Class ; rdfs:label "Protein sequence" ; rdfs:subClassOf e:data_2044 .
e:format_1929 a owl:Class ; rdfs:label "FASTA" .
<http://example.org/Other> a owl:Class ; rdfs:label "not EDAM" .
[] a owl:Restriction .
"""


def build_graph() -> Graph:
    return Graph().parse(data=EDAM_TTL, format="turtle")


def t(raw: str) -> str:
    return edam.normalize(raw)


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.g = build_graph()

    def test_terms_have_label_definition_synonyms_and_parents(self):
        term = edam.terms(self.g, [t("topic_0080")])[t("topic_0080")]
        self.assertEqual(term.label, "Sequence analysis")
        self.assertEqual(term.definition, "Analysing sequences.")
        self.assertEqual(term.synonyms, ("Sequence analysis (general)",))
        self.assertEqual(term.parents, (t("topic_0091"),))
        self.assertEqual((term.id, term.kind), ("topic_0080", "topic"))

    def test_only_edam_terms_are_known(self):
        found = edam.terms(self.g, [t("topic_0091"), "http://example.org/Other", t("topic_8888")])
        self.assertEqual(set(found), {t("topic_0091")})
        self.assertEqual(edam.labels(self.g, ["http://example.org/Other"]), {})

    def test_ancestors_include_the_term_itself(self):
        up = edam.ancestors(self.g, [t("topic_0102"), t("operation_0292")])
        self.assertEqual(up[t("topic_0102")], {t("topic_0102"), t("topic_0080"), t("topic_0091")})
        self.assertEqual(up[t("operation_0292")], {t("operation_0292")})

class NormalizeTests(unittest.TestCase):
    def test_accepts_common_forms(self):
        for raw in ["topic_0080", "http://edamontology.org/topic_0080", "edam:topic_0080", " Topic_0080 "]:
            with self.subTest(raw=raw):
                self.assertEqual(edam.normalize(raw), "http://edamontology.org/topic_0080")

    def test_rejects_anything_else(self):
        for raw in ["topic_80", "foo", "http://example.org/topic_0080", "topic_0080; DROP"]:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    edam.normalize(raw)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.g = build_graph()

    def test_ranks_exact_label_before_synonym_before_partial(self):
        hits = edam.search(self.g, "mapping")
        self.assertEqual([h.term.id for h in hits], ["topic_0102"])  # deprecated "Old mapping" excluded
        self.assertEqual(hits[0].matched, "label")

        self.assertEqual(edam.search(self.g, "read mapping")[0].term.id, "topic_0102")
        self.assertEqual(edam.search(self.g, "read mapping")[0].matched, "synonym")

        hits = edam.search(self.g, "sequence")
        self.assertEqual(hits[0].term.label, "Sequence")  # exact label first
        self.assertTrue(
            {"topic_0080", "data_2044", "data_2976", "operation_0292"} <= {h.term.id for h in hits}
        )

class SparqlOnlyTests(unittest.TestCase):
    """The implementation must go through SPARQL, not graph traversal."""

    def test_edam_py_does_not_traverse_the_graph_directly(self):
        """All access to the graph is SPARQL executed through graph_store.run_query."""
        tree = ast.parse(Path(edam.__file__).read_text())
        banned = {
            "subjects", "objects", "predicates", "subject_objects", "predicate_objects",
            "subject_predicates", "value", "triples", "transitive_objects",
            "transitive_subjects", "query",
        }
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse(used & banned, f"direct graph access in edam.py: {used & banned}")
        called = {
            n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        self.assertIn("run_query", called)

    def test_graph_with_unrelated_triples_is_fine(self):
        # the real graph mixes EDAM with a million bio.tools triples
        g = build_graph()
        g.parse(
            data="<http://x/a> <http://www.w3.org/2000/01/rdf-schema#subClassOf> <http://x/b> .\n"
            '<http://x/a> <http://www.w3.org/2000/01/rdf-schema#label> "Sequence" .',
            format="turtle",
        )
        self.assertEqual([h.term.id for h in edam.search(g, "sequence", kind="data")][0], "data_2044")
        self.assertEqual(edam.terms(g, ["http://x/a"]), {})


if __name__ == "__main__":
    unittest.main()

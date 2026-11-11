import ast
import unittest
from pathlib import Path
from unittest import mock

import knowledge_graph
from tests.support import mini_graph, mini_knowledge_graph

BT = "https://bio.tools/"


def ids(res):
    return [r["biotools_id"] for r in res["results"]]


class KnowledgeGraphTestCase(unittest.TestCase):
    def setUp(self):
        self.kg = mini_knowledge_graph()


class SearchSoftwareTests(KnowledgeGraphTestCase):
    def test_by_topic_includes_narrower_terms_by_default(self):
        res = self.kg.search_software(topics=["topic_0080"])
        # protfold is annotated with topic_0102 (Mapping), a child of topic_0080
        self.assertEqual(ids(res), ["alnviewer", "blast", "clustal", "protfold"])  # sorted by name
        self.assertEqual(res["total"], 4)

    def test_ands_topic_operation_and_license(self):
        res = self.kg.search_software(topics=["topic_0080"], operations=["operation_0292"])
        self.assertEqual(set(ids(res)), {"blast", "clustal"})
        res = self.kg.search_software(
            topics=["topic_0080"], operations=["operation_0292"], licenses=["MIT"]
        )
        self.assertEqual(ids(res), ["blast"])

    def test_ors_values_within_one_filter(self):
        res = self.kg.search_software(operations=["operation_0292", "operation_0295"])
        self.assertEqual(set(ids(res)), {"blast", "clustal", "protfold"})

    def test_license_is_case_insensitive_and_follows_identifier_links(self):
        self.assertEqual(
            set(ids(self.kg.search_software(licenses=["GPL-3.0"]))), {"clustal", "alnviewer"}
        )
        # NoIO has no license of its own; a biocontainers record linked to it has one
        self.assertEqual(ids(self.kg.search_software(licenses=["apache-2.0"])), ["noio"])

    def test_by_name_and_sorted_case_insensitively(self):
        res = self.kg.search_software(name_contains="VIEW")
        self.assertEqual(
            [r["name"] for r in res["results"]], ["AlnViewer", "AnyViewer", "PhylipViewer"]
        )

    def test_pages_and_reports_total(self):
        page1 = self.kg.search_software(limit=2, offset=0)
        page2 = self.kg.search_software(limit=2, offset=2)
        self.assertEqual(page1["total"], 11)
        self.assertEqual(page2["total"], 11)
        self.assertFalse(set(ids(page1)) & set(ids(page2)))
        self.assertEqual([r["name"] for r in page1["results"]], ["AlnViewer", "AnyViewer"])

    def test_result_shape(self):
        r = self.kg.search_software(name_contains="blast")["results"][0]
        self.assertEqual(
            r,
            {
                "biotools_id": "blast",
                "iri": BT + "blast",
                "name": "BLAST",
                "description": "Find similar sequences.",
                "url": "https://blast.example.org",
                "licenses": ["MIT"],
                "topics": [{"id": "topic_0080", "label": "Sequence analysis"}],
                "operations": [{"id": "operation_0292", "label": "Sequence alignment"}],
            },
        )

    def test_rejects_wrong_kind_of_term(self):
        with self.assertRaisesRegex(ValueError, "topic"):
            self.kg.search_software(topics=["operation_0292"])
        with self.assertRaisesRegex(ValueError, "operation"):
            self.kg.search_software(operations=["topic_0080"])

    def test_rejects_malformed_term(self):
        with self.assertRaisesRegex(ValueError, "EDAM"):
            self.kg.search_software(topics=["topic_0080> } DROP"])


class ListLicensesTests(KnowledgeGraphTestCase):
    def test_counts_tools_and_merges_case_variants(self):
        res = self.kg.list_licenses(contains="gpl")
        self.assertEqual(res, {"total": 1, "licenses": [{"license": "GPL-3.0", "tools": 2}]})

class ResolveEdamTermTests(KnowledgeGraphTestCase):
    def test_by_label_with_tool_counts(self):
        res = self.kg.resolve_edam_term("sequence alignment", kind="operation")["results"]
        self.assertEqual(res[0]["id"], "operation_0292")
        self.assertEqual(res[0]["matched"], "label")
        self.assertEqual(res[0]["n_tools"], 2)
        self.assertTrue(res[0]["definition"].startswith("Arrange sequences"))

    def test_by_synonym_and_broader(self):
        r = self.kg.resolve_edam_term("read mapping")["results"][0]
        self.assertEqual((r["id"], r["matched"]), ("topic_0102", "synonym"))
        self.assertEqual(r["broader"], [{"id": "topic_0080", "label": "Sequence analysis"}])

class ToolLookupAndIoTests(KnowledgeGraphTestCase):
    def test_lookup_accepts_common_forms(self):
        for raw in ["blast", "BLAST", "https://bio.tools/blast", "biotools:blast", " blast "]:
            with self.subTest(raw=raw):
                self.assertEqual(self.kg.get_software_io(raw)["biotools_id"], "blast")

    def test_unknown_tool_suggests_close_ids(self):
        with self.assertRaisesRegex(ValueError, "blast"):
            self.kg.get_software_io("blastt")

    def test_get_software_io(self):
        io = self.kg.get_software_io("blast")
        self.assertEqual(io["name"], "BLAST")
        self.assertEqual(
            io["inputs"],
            [{
                "name": "Query",
                "data": {"id": "data_2044", "label": "Sequence"},
                "formats": [{"id": "format_1929", "label": "FASTA"}],
            }],
        )
        self.assertEqual(io["outputs"][0]["data"]["id"], "data_0863")

class CheckComposabilityTests(KnowledgeGraphTestCase):
    def test_exact_match(self):
        res = self.kg.check_composability("clustal", "alnviewer")
        self.assertEqual(res["compatibility"], "direct")
        m = res["matches"][0]
        self.assertEqual(m["kind"], "exact")
        self.assertEqual(m["output"]["name"], "Alignment")
        self.assertEqual(m["input"]["name"], "Alignment")
        self.assertEqual(res["producer"], {"biotools_id": "clustal", "name": "Clustal Omega"})

    def test_needs_conversion_when_formats_differ(self):
        res = self.kg.check_composability("clustal", "phylipviewer")
        self.assertEqual(res["compatibility"], "needs_conversion")
        self.assertEqual(res["matches"][0]["kind"], "needs_conversion")

    def test_probable_when_consumer_format_unspecified(self):
        res = self.kg.check_composability("clustal", "anyviewer")
        self.assertEqual(res["compatibility"], "probable")
        self.assertEqual(res["matches"][0]["kind"], "format_unspecified")

    def test_subtype_data_and_format(self):
        # protein sequence (FASTA) -> sequence (FASTA-like): narrower data and format
        res = self.kg.check_composability("protgen", "clustal")
        self.assertEqual(res["compatibility"], "direct")
        self.assertEqual(res["matches"][0]["kind"], "subtype")

    def test_rejects_broader_output_for_narrower_input(self):
        # a generic Sequence is not necessarily a Protein sequence
        self.assertEqual(self.kg.check_composability("seqgen", "protfold")["compatibility"], "none")

    def test_unknown_when_io_is_missing(self):
        res = self.kg.check_composability("noio", "blast")
        self.assertEqual(res["compatibility"], "unknown")
        self.assertIn("noio", res["note"])
        self.assertIn("output", res["note"])
        res = self.kg.check_composability("blast", "noio")
        self.assertEqual(res["compatibility"], "unknown")
        self.assertIn("input", res["note"])


class FindDownstreamToolsTests(KnowledgeGraphTestCase):
    def test_default_level_is_direct_plus_probable(self):
        res = self.kg.find_downstream_tools("clustal")
        self.assertEqual(
            [(r["biotools_id"], r["compatibility"]) for r in res["results"]],
            [("alnviewer", "direct"), ("anyviewer", "probable")],
        )
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["level"], "probable")

    def test_considers_broader_input_types_and_ranks_exact_first(self):
        res = self.kg.find_downstream_tools("protgen")
        self.assertEqual(
            [(r["biotools_id"], r["kind"]) for r in res["results"]],
            [("protfold", "exact"), ("blast", "subtype"), ("clustal", "subtype"), ("seqstats", "subtype")],
        )

class GenericInputTests(KnowledgeGraphTestCase):
    """Inputs typed with the EDAM root 'Data' accept anything: a separate, weakest tier."""

    def test_root_data_input_is_a_separate_weakest_tier(self):
        res = self.kg.check_composability("blast", "zeta")
        self.assertEqual(res["compatibility"], "generic")
        self.assertEqual(res["matches"][0]["kind"], "generic_input")

class SparqlOnlyTests(unittest.TestCase):
    """The implementation must go through SPARQL, not graph traversal."""

    def test_knowledge_graph_py_does_not_traverse_the_graph_directly(self):
        """All access to the graph is SPARQL executed through graph_store.run_query."""
        tree = ast.parse(Path(knowledge_graph.__file__).read_text())
        banned = {
            "subjects", "objects", "predicates", "subject_objects", "predicate_objects",
            "subject_predicates", "value", "triples", "transitive_objects",
            "transitive_subjects", "query",
        }
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse(used & banned, f"direct graph access in knowledge_graph.py: {used & banned}")
        called = {
            n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        self.assertIn("run_query", called)

    def test_every_call_is_answered_by_sparql_queries_and_nothing_is_precomputed(self):
        kg = knowledge_graph.KnowledgeGraph(mini_graph())
        self.assertEqual(vars(kg), {"graph": mini_graph()})  # no index, only the graph
        with mock.patch.object(knowledge_graph, "run_query", wraps=knowledge_graph.run_query) as spy:
            kg.search_software(topics=["topic_0080"], licenses=["MIT"])
        queries = [c.args[1] for c in spy.call_args_list]
        self.assertGreaterEqual(len(queries), 2)  # the search, then the details of the page
        self.assertTrue(all(q.lstrip().startswith("PREFIX") and "SELECT" in q for q in queries))

if __name__ == "__main__":
    unittest.main()

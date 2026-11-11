"""Build notebooks/demo.ipynb, the demo of the RSEc MCP server.

    cd mcp
    uv run --with nbformat --with nbconvert --with ipykernel --with pandas --with seaborn \
        python notebooks/build_demo.py            # writes notebooks/demo.ipynb
    uv run --with nbformat --with nbconvert --with ipykernel --with pandas --with seaborn \
        jupyter nbconvert --to notebook --execute --inplace notebooks/demo.ipynb

The notebook calls the real MCP tools in-process, exactly as an LLM client would.
"""

from pathlib import Path

import nbformat as nbf

MD, CODE = nbf.v4.new_markdown_cell, nbf.v4.new_code_cell

cells = [
    MD("""\
# RSEc MCP server: demo

An LLM agent that needs bioinformatics tools can ask this server, which answers with SPARQL queries over
the [Research Software Ecosystem](https://github.com/research-software-ecosystem/content) RDF data
(bio.tools records typed with [EDAM](https://edamontology.org) terms). Here we play the agent on two questions:

1. *"I need to trim reads, align my reads, perform quality control and quantify RNA-seq data. Can you list bioinformatics tools doing that?"*
2. *"Can I compose together HISAT2 and Kallisto? Justify the composition based on EDAM terms."*

Every cell below calls the **MCP tools** of `server.py`, in-process, as an MCP client would.
The first call loads about one million triples (about 10 s once the files are downloaded); later calls are SPARQL queries on the loaded graph."""),
    CODE("""\
import os, sys, time

os.environ.setdefault("RSEC_LOG_LEVEL", "WARNING")  # keep the loading logs out of the notebook
sys.path.insert(0, "..")                            # the project modules live in mcp/

import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
from fastmcp import Client

import edam
import server

sns.set_theme(context="notebook", style="whitegrid")
pd.set_option("display.max_colwidth", 80)
pd.set_option("display.width", 140)


CALLS = []  # (tool, arguments, seconds), shown at the end


async def tool(name, **arguments):
    \"\"\"Call one MCP tool and return its result, as an agent would.\"\"\"
    t0 = time.perf_counter()
    async with Client(server.mcp) as client:
        result = (await client.call_tool(name, arguments)).data
    CALLS.append((name, str(arguments)[:70], time.perf_counter() - t0))
    return result


t0 = time.perf_counter()
async with Client(server.mcp) as client:
    catalogue = await client.list_tools()
    first = await client.call_tool("list_software_with_biotools_ids", {"limit": 1})  # waits for the data
print(f"server ready in {time.perf_counter() - t0:.1f} s")
pd.DataFrame(
    [(t.name, (t.description or "").strip().splitlines()[0]) for t in catalogue],
    columns=["tool", "what it does"],
)"""),
    MD("""\
## 1. Which tools trim, align, check and quantify RNA-seq reads?

The agent starts by turning each step into an **EDAM operation**. `resolve_edam_term` searches labels,
synonyms and definitions, and returns the id to use in the next calls."""),
    CODE("""\
STEPS = {
    "1. trim reads": "trimming",
    "2. align reads": "read mapping",
    "3. quality control": "sequencing quality control",
    "4. quantify RNA-seq": "RNA-Seq quantification",
}

resolved = {}
rows = []
for step, phrase in STEPS.items():
    res = await tool("resolve_edam_term", text=phrase, kind="operation", limit=3)
    best = res["results"][0]
    resolved[step] = best
    rows.append({
        "step": step,
        "asked for": phrase,
        "EDAM operation": f"{best['id']}  {best['label']}",
        "matched on": best["matched"],
        "other candidates": ", ".join(f"{r['id']} {r['label']}" for r in res["results"][1:]),
    })
pd.DataFrame(rows)"""),
    MD("""\
Now one search per step (this is the slowest part of the demo: each search is a SPARQL query that walks the EDAM hierarchy and the annotations of tens of thousands of tools). `search_software` always includes the **narrower** EDAM terms
(for example *Split read mapping* under *Read mapping*). The EDAM topic **RNA-Seq** (`topic_3170`)
narrows each list to tools that bio.tools describes as RNA-seq tools."""),
    CODE("""\
RNASEQ = "topic_3170"

found = {}
counts = []
for step, term in resolved.items():
    everything = await tool("search_software", operations=[term["id"]], limit=1)
    rnaseq = await tool("search_software", operations=[term["id"]], topics=[RNASEQ], limit=100)
    found[step] = rnaseq
    counts += [
        {"step": step, "scope": "any topic", "tools": everything["total"]},
        {"step": step, "scope": "topic RNA-Seq", "tools": rnaseq["total"]},
    ]
counts = pd.DataFrame(counts)

fig, ax = plt.subplots(figsize=(7, 3.6))
sns.barplot(data=counts, y="step", x="tools", hue="scope", ax=ax)
ax.set(xlabel="bio.tools records annotated with the operation", ylabel="")
for bars in ax.containers:
    ax.bar_label(bars, padding=3)
plt.tight_layout()
plt.show()"""),
    MD("""\
### The tools, step by step

The first tools of each list, sorted by name, with their licenses and what bio.tools says they do.
These lists reflect how the bio.tools curators and tool authors **annotated** their records: a broad operation such as
*Sequence trimming* is attached to many tools, so the lists are candidates to review, not a ranking."""),
    CODE("""\
from IPython.display import display, Markdown

for step, res in found.items():
    op = resolved[step]
    display(Markdown(f"**{step}**: {op['id']} *{op['label']}*, {res['total']} tools tagged RNA-Seq"))
    display(pd.DataFrame(
        [{
            "tool": r["biotools_id"],
            "license": ", ".join(r["licenses"][:2]),
            "description": r["description"][:100],
        } for r in res["results"][:8]]
    ))"""),
    MD("""\
## 2. Can HISAT2 and Kallisto be composed?

Composition is decided on **inputs and outputs**: an output fits an input when its EDAM data type is the same
or narrower, and the formats then say how directly. Let us ask the server."""),
    CODE("""\
io = {name: await tool("get_software_io", tool=name) for name in ("hisat2", "kallisto")}
for name, res in io.items():
    print(f"{res['name']:9} inputs: {len(res['inputs'])}  outputs: {len(res['outputs'])}  {res.get('note', '')}")

verdict = await tool("check_composability", producer="hisat2", consumer="kallisto")
print()
print("compatibility:", verdict["compatibility"])
print(verdict["note"])"""),
    MD("""\
The input/output annotations of these two records are missing from the RSEc data (only about 4,000 of 34,000 tools
have any), so the server answers `unknown` instead of guessing. What the data **does** hold for both tools
are EDAM **topics** and **operations**. Let us see what they say."""),
    CODE("""\
rows = []
annotated = {}
for name in ("hisat2", "kallisto"):
    res = await tool("search_software", name_contains=name, limit=100)
    record = next(r for r in res["results"] if r["biotools_id"].lower() == name)
    annotated[name] = record
    for kind in ("topics", "operations"):
        for term in record[kind]:
            rows.append({"tool": record["name"], "kind": kind[:-1], "EDAM id": term["id"], "label": term["label"]})
terms = pd.DataFrame(rows)
terms.pivot_table(index=["kind", "EDAM id", "label"], columns="tool", aggfunc="size", fill_value=0).replace({1: "yes", 0: ""})"""),
    MD("""\
### What the two tools share in EDAM

Two tools can be related at three levels: the **same term**, a **common broader term**
(walking up the EDAM hierarchy), or a **place in the same pipeline**."""),
    CODE("""\
graph = await server.store.get()
ROOTS = {"topic_0003", "operation_0004"}  # EDAM's top terms say nothing

def ids_of(record, kind):
    return {edam.NS + t["id"] for t in record[kind]}

shared = {}
for kind in ("topics", "operations"):
    a, b = ids_of(annotated["hisat2"], kind), ids_of(annotated["kallisto"], kind)
    up_a = set().union(*edam.ancestors(graph, a).values())
    up_b = set().union(*edam.ancestors(graph, b).values())
    names = edam.labels(graph, up_a & up_b)
    shared[kind] = {
        "same term": sorted(f"{i.removeprefix(edam.NS)} {names.get(i, '')}" for i in a & b),
        "common broader term": sorted(
            f"{i.removeprefix(edam.NS)} {names.get(i, '')}"
            for i in (up_a & up_b) - (a & b) if i.removeprefix(edam.NS) not in ROOTS
        ),
    }
pd.DataFrame(shared).T.map(lambda terms: "; ".join(terms) or "-")"""),
    MD("""\
### Do they belong to the four steps of part 1?

We ask whether each tool is annotated with the operation of each step (or a narrower one)."""),
    CODE("""\
# Where do they sit in the four-step pipeline of part 1?
membership = {}
for name in ("hisat2", "kallisto"):
    row = {}
    for step, term in resolved.items():
        res = await tool("search_software", operations=[term["id"]], name_contains=name, limit=100)
        row[step] = any(r["biotools_id"].lower() == name for r in res["results"])
    membership[annotated[name]["name"]] = row
pd.DataFrame(membership).T.replace({True: "yes", False: "no"})"""),
    MD("""\
### Verdict for HISAT2 and Kallisto

With the RSEc data used for this run:

- **Same domain**: both records carry the topic `topic_3170` *RNA-Seq*, and share the broader topics *Sequencing* and *Data acquisition*.
- **Different operations**: HISAT2 is annotated *Sequence alignment* (`operation_0292`), Kallisto *Gene expression profiling* (`operation_0314`).
  They only share EDAM's very general *Data analysis*, which says little. An aligner followed by an expression profiler is a plausible reading of these two terms.
- **Not the steps of part 1**: neither record carries the step operations *Read mapping* or *RNA-Seq quantification*, they use broader ones.
- **Inputs and outputs are not annotated** for either tool, so the server cannot check data types and formats and answers `unknown`.
  EDAM terms on tools support a *"could belong to the same RNA-seq workflow"* argument, not a proof of composition.

Domain knowledge, **not** from the RSEc data: Kallisto estimates transcript abundance directly from sequencing reads (pseudo-alignment),
so it does not take HISAT2 alignments as input. The two are alternative routes rather than consecutive steps. A counting tool such as
featureCounts or StringTie is the usual step after HISAT2.
"""),
    MD("""\
### What a justified verdict looks like when inputs and outputs *are* annotated

For comparison, the same tool on a pair of tools that carry EDAM data types and formats:
the CLUSTALW alignment and the Jalview alignment viewer."""),
    CODE("""\
example = await tool("check_composability", producer="clustalw", consumer="jalview")
print(f"{example['producer']['name']} -> {example['consumer']['name']}: {example['compatibility']}")
best = example["matches"][0]
print(f"kind: {best['kind']}")
print(f"  output '{best['output']['name']}': {best['output']['data']['id']} ({best['output']['data']['label']}), "
      f"formats {[f['label'] for f in best['output']['formats']]}")
print(f"  input  '{best['input']['name']}': {best['input']['data']['id']} ({best['input']['data']['label']}), "
      f"formats {[f['label'] for f in best['input']['formats']][:5]} ...")"""),
    MD("""\
## What to remember

- The server answers from **annotations**, and says `unknown` when they are missing rather than inventing a verdict.
- Every answer is a SPARQL query on the loaded graph; the EDAM hierarchy comes from a property path (`rdfs:subClassOf*`).
- To run the server and connect it to an agent, see the README (`mcp/README.md`)."""),
    MD("""\
### Appendix: what each call cost

Each MCP call above ran SPARQL queries on the loaded graph (the 10 s of graph loading are not included)."""),
    CODE("""\
calls = pd.DataFrame(CALLS, columns=["tool", "arguments", "seconds"])
print(f"{len(calls)} calls, {calls.seconds.sum():.0f} s in total, slowest {calls.seconds.max():.1f} s")
calls.groupby("tool").seconds.agg(["count", "sum", "max"]).round(1).sort_values("sum", ascending=False)"""),
]

nb = nbf.v4.new_notebook()
nb.cells = cells
nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
nbf.validate(nb)
out = Path(__file__).with_name("demo.ipynb")
nbf.write(nb, out)
print("wrote", out)

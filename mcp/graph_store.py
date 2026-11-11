"""Load the RSEc RDF dumps into an in-memory rdflib graph.

Dumps are the same files the root docker-compose.yml feeds to Fuseki. Missing
files are downloaded on first use; existing files are never re-downloaded.
"""

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import NamedTuple

import httpx
from rdflib import Graph

log = logging.getLogger("rsec.graph_store")

BASE_URL = (
    "https://raw.githubusercontent.com/research-software-ecosystem/content"
    "/refs/heads/master/datasets"
)


class Source(NamedTuple):
    name: str  # local file name inside the data directory
    url: str
    format: str = "turtle"  # rdflib parser name


# Same dumps as the root docker-compose.yml.
DUMPS = tuple(
    Source(name, f"{BASE_URL}/{name}")
    for name in (
        "bioschemas-dump.ttl",
        #"bioschemas-biii-dump.ttl",
        #"bioconda-dump.ttl",
        #"biocontainers-dump.ttl",
        #"debian-dump.ttl",
        #"galaxy-dump.ttl",
        #"galaxyworkflow-dump.ttl",
        #"workflowhub-dump.ttl",
    )
)
# EDAM ontology: term labels, synonyms and the is-a hierarchy (not in the dumps).
EDAM = Source("EDAM.owl", "https://edamontology.org/EDAM.owl", "xml")
SOURCES = DUMPS + (EDAM,)


# rdflib's SPARQL parser (pyparsing) is not thread-safe: two queries parsed at the
# same time (e.g. the startup indexing and an early tool call) crash with a
# misleading "TypeError: ... missing 1 required positional argument".
QUERY_LOCK = threading.Lock()


def run_query(g: Graph, query: str) -> list:
    """Run a SPARQL query, one at a time, and return all result rows."""
    with QUERY_LOCK:
        return list(g.query(query))


def _mb(path: Path) -> str:
    return f"{path.stat().st_size / 1e6:.1f} MB"


def ensure_dumps(
    data_dir: Path,
    sources: tuple[Source, ...] = SOURCES,
    transport: httpx.BaseTransport | None = None,
) -> list[Path]:
    """Return local paths of all sources, downloading the missing ones."""
    data_dir.mkdir(parents=True, exist_ok=True)
    paths = [data_dir / src.name for src in sources]
    missing = [(src, p) for src, p in zip(sources, paths) if not p.exists()]
    log.info(
        "data directory %s: %d file(s) present, %d to download",
        data_dir, len(paths) - len(missing), len(missing),
    )
    if missing:
        with httpx.Client(
            transport=transport, follow_redirects=True, timeout=120
        ) as client:
            for i, (src, path) in enumerate(missing, 1):
                log.info("[%d/%d] downloading %s", i, len(missing), src.url)
                t0 = time.perf_counter()
                resp = client.get(src.url)
                resp.raise_for_status()
                tmp = path.with_name(path.name + ".part")
                tmp.write_bytes(resp.content)
                tmp.replace(path)  # atomic: no half-written file is ever kept
                log.info(
                    "[%d/%d] downloaded %s (%s, %.1f s)",
                    i, len(missing), path.name, _mb(path), time.perf_counter() - t0,
                )
    return paths


def load_graph(paths: list[Path], sources: tuple[Source, ...] = SOURCES) -> Graph:
    g = Graph()
    t_start = time.perf_counter()
    for i, (path, src) in enumerate(zip(paths, sources), 1):
        before, t0 = len(g), time.perf_counter()
        log.info("[%d/%d] parsing %s (%s)", i, len(paths), path.name, _mb(path))
        g.parse(path, format=src.format)
        log.info(
            "[%d/%d] parsed %s: +%d triples (%d total, %.1f s)",
            i, len(paths), path.name, len(g) - before, len(g), time.perf_counter() - t0,
        )
    log.info(
        "loaded %d triples from %d files in %.1f s",
        len(g), len(paths), time.perf_counter() - t_start,
    )
    return g


class GraphStore:
    """Loads the graph once, lazily, off the event loop."""

    def __init__(
        self,
        data_dir: Path,
        sources: tuple[Source, ...] = SOURCES,
        transport: httpx.BaseTransport | None = None,
    ):
        self.data_dir = data_dir
        self.sources = sources
        self._transport = transport
        self._graph: Graph | None = None
        self._lock = asyncio.Lock()

    def _load(self) -> Graph:
        paths = ensure_dumps(self.data_dir, self.sources, self._transport)
        return load_graph(paths, self.sources)

    async def get(self) -> Graph:
        async with self._lock:
            if self._graph is None:  # a failed load is retried on the next call
                self._graph = await asyncio.to_thread(self._load)
            return self._graph

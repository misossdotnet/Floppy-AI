#!/usr/bin/env python3
"""Local scale test for the REST and MCP connectors.

Run from the application container, for example:

    python /app/tests/load_api_connectors.py --base-url http://127.0.0.1:8000

Project setup and cleanup use the service layer because project creation is an
administrative UI operation. Every corpus operation under measurement uses an
actual HTTP REST or MCP connector.
"""

import argparse
import itertools
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from db import get_db_connection
from psycopg2 import sql
from services import create_project, delete_project


READ_OPERATIONS = {"rest.chunks", "rest.lineage", "mcp.list", "mcp.search"}


def percentile(values, ratio):
    """Return the nearest-rank percentile for a non-empty sequence."""
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return ordered[index]


def summarize_metrics(metrics):
    """Build stable latency and status summaries grouped by operation."""
    result = {}
    for operation in sorted({metric["operation"] for metric in metrics}):
        selected = [metric for metric in metrics if metric["operation"] == operation]
        durations = [metric["duration_ms"] for metric in selected]
        statuses = {}
        for metric in selected:
            key = str(metric["status"])
            statuses[key] = statuses.get(key, 0) + 1
        result[operation] = {
            "requests": len(selected),
            "successes": sum(1 for metric in selected if metric["ok"]),
            "failures": sum(1 for metric in selected if not metric["ok"]),
            "status_counts": statuses,
            "latency_ms": {
                "min": round(min(durations), 2),
                "mean": round(statistics.fmean(durations), 2),
                "p50": round(percentile(durations, 0.50), 2),
                "p95": round(percentile(durations, 0.95), 2),
                "p99": round(percentile(durations, 0.99), 2),
                "max": round(max(durations), 2),
            },
        }
    return result


def make_document(project_index, document_index, word_count):
    """Create deterministic mixed content large enough to produce chunks."""
    words = " ".join(
        f"concept-{project_index}-{document_index}-{index}"
        for index in range(word_count)
    )
    return {
        "source_document": "connector-scale-test",
        "title_document": f"Projet {project_index} document {document_index}",
        "autor_document": "automated-scale-suite",
        "content_document": f"""# Projet {project_index} document {document_index}

{words}.

| Mesure | Valeur |
| --- | --- |
| projet | {project_index} |
| document | {document_index} |

```python
project_index = {project_index}
document_index = {document_index}
result = project_index + document_index
```
""",
    }


class ConnectorLoadRun:
    """Coordinate one isolated scale run and collect client-visible metrics."""

    def __init__(self, args):
        self.args = args
        self.base_url = args.base_url.rstrip("/")
        self.metrics = []
        self.projects = []
        self.document_ids = {}
        self.build_ids = {}
        self.sequence = itertools.count()
        self.random = random.Random(20260804)
        self.started_at = time.time()

    def request_json(self, operation, method, path, payload=None, expected=(200,), validator=None):
        """Perform one real HTTP request and record its status and latency."""
        sequence = next(self.sequence)
        headers = {
            "Accept": "application/json",
        }
        if self.args.use_forwarded_ip_pool:
            headers["X-Forwarded-For"] = (
                f"198.51.{(sequence // 250) % 100}."
                f"{(sequence % self.args.ip_pool_size) + 1}"
            )
        if self.args.token:
            headers["Authorization"] = f"Bearer {self.args.token}"
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        started = time.perf_counter()
        status = 0
        body = None
        error = ""
        try:
            with urllib.request.urlopen(request, timeout=self.args.timeout) as response:
                status = response.status
                raw_body = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw_body = exc.read()
            error = f"HTTP {exc.code}"
        except Exception as exc:
            raw_body = b""
            error = f"{type(exc).__name__}: {exc}"
        duration_ms = (time.perf_counter() - started) * 1000
        if raw_body:
            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError:
                error = error or "invalid JSON response"
        ok = status in expected and not error
        if ok and validator:
            try:
                validator(body)
            except Exception as exc:
                ok = False
                error = f"contract: {exc}"
        metric = {
            "operation": operation,
            "status": status,
            "duration_ms": duration_ms,
            "ok": ok,
            "error": error,
        }
        self.metrics.append(metric)
        return body

    def run_parallel(self, callables):
        """Execute callables with bounded client concurrency."""
        results = []
        with ThreadPoolExecutor(max_workers=self.args.concurrency) as executor:
            futures = [executor.submit(callable_) for callable_ in callables]
            for future in as_completed(futures):
                results.append(future.result())
        return results

    def assert_phase_clean(self, phase):
        """Abort dependent work immediately when a phase produced a failure."""
        failures = [
            metric
            for metric in self.metrics
            if metric["operation"].startswith(phase) and not metric["ok"]
        ]
        if failures:
            raise RuntimeError(f"Phase {phase} en echec: {failures[:3]}")

    def create_projects(self):
        """Create isolated projects for setup, outside measured connectors."""
        run_id = uuid4().hex[:10]
        for index in range(self.args.projects):
            _, slug = create_project(f"Scale API {run_id} {index}")
            self.projects.append(slug)

    def import_documents(self):
        """Import one bounded batch per project through REST."""
        def task(project_index, slug):
            documents = [
                make_document(project_index, index, self.args.words_per_document)
                for index in range(self.args.documents_per_project)
            ]
            body = self.request_json(
                "rest.import",
                "POST",
                f"/api/v1/projects/{slug}/imports",
                {"documents": documents},
                expected=(201,),
                validator=lambda value: (
                    value["imported_count"] == self.args.documents_per_project
                    or (_ for _ in ()).throw(AssertionError("imported_count"))
                ),
            )
            return slug, [item["document_id"] for item in (body or {}).get("documents", [])]

        results = self.run_parallel(
            [
                lambda project_index=index, slug=slug: task(project_index, slug)
                for index, slug in enumerate(self.projects)
            ]
        )
        self.assert_phase_clean("rest.import")
        self.document_ids = dict(results)

    def normalize_documents(self):
        """Normalize every imported document through REST."""
        calls = []
        for slug, document_ids in self.document_ids.items():
            for document_id in document_ids:
                calls.append(
                    lambda slug=slug, document_id=document_id: self.request_json(
                        "rest.normalize",
                        "POST",
                        f"/api/v1/documents/{document_id}/normalize",
                        {"project_slug": slug},
                        validator=lambda value: (
                            value.get("normalization_version") == "v2"
                            or (_ for _ in ()).throw(AssertionError("normalization_version"))
                        ),
                    )
                )
        self.run_parallel(calls)
        self.assert_phase_clean("rest.normalize")

    def chunk_projects(self):
        """Generate all chunks concurrently across independent projects."""
        calls = [
            lambda slug=slug: self.request_json(
                "rest.chunk",
                "POST",
                f"/api/v1/projects/{slug}/chunk",
                {
                    "chunkMaxTokens": 90,
                    "chunkOverlapTokens": 12,
                    "hardMaxTokens": 110,
                    "codeAware": True,
                    "tableAware": True,
                    "mergeSmallParagraphs": True,
                },
                validator=lambda value: (
                    value.get("generated_chunks", 0) > 0
                    or (_ for _ in ()).throw(AssertionError("generated_chunks"))
                ),
            )
            for slug in self.projects
        ]
        self.run_parallel(calls)
        self.assert_phase_clean("rest.chunk")

    def approve_sample(self):
        """Approve a representative subset of documents through REST."""
        calls = []
        for slug, document_ids in self.document_ids.items():
            for document_id in document_ids[:: max(1, self.args.documents_per_project // 5)]:
                calls.append(
                    lambda slug=slug, document_id=document_id: self.request_json(
                        "rest.approve",
                        "POST",
                        f"/api/v1/documents/{document_id}/approve",
                        {
                            "project_slug": slug,
                            "status": "approved",
                            "approved_by": "connector-scale-suite",
                        },
                        validator=lambda value: (
                            value.get("status") == "approved"
                            or (_ for _ in ()).throw(AssertionError("approval status"))
                        ),
                    )
                )
        self.run_parallel(calls)
        self.assert_phase_clean("rest.approve")

    def build_datasets(self):
        """Build one dataset snapshot per project through REST."""
        def task(slug):
            body = self.request_json(
                "rest.build",
                "POST",
                f"/api/v1/projects/{slug}/build-dataset",
                {"quality_min": 0, "approved_only": False, "limit": 10000},
                expected=(201,),
                validator=lambda value: (
                    value.get("stats", {}).get("selected_chunks", 0) > 0
                    or (_ for _ in ()).throw(AssertionError("selected_chunks"))
                ),
            )
            return slug, (body or {}).get("build_id")

        results = self.run_parallel([lambda slug=slug: task(slug) for slug in self.projects])
        self.assert_phase_clean("rest.build")
        self.build_ids = dict(results)

    def exercise_reads(self):
        """Mix REST and MCP reads under sustained concurrency."""
        calls = []
        for index in range(self.args.read_requests):
            slug = self.projects[index % len(self.projects)]
            document_ids = self.document_ids[slug]
            document_id = document_ids[index % len(document_ids)]
            operation_index = index % 4
            if operation_index == 0:
                calls.append(
                    lambda slug=slug: self.request_json(
                        "rest.chunks",
                        "GET",
                        f"/api/v1/chunks?project={slug}&limit=50&offset=0",
                        validator=lambda value: (
                            value.get("count", 0) > 0
                            or (_ for _ in ()).throw(AssertionError("chunk count"))
                        ),
                    )
                )
            elif operation_index == 1:
                calls.append(
                    lambda slug=slug, document_id=document_id: self.request_json(
                        "rest.lineage",
                        "GET",
                        f"/api/v1/documents/{document_id}/lineage?project={slug}",
                        validator=lambda value: (
                            value.get("lineage", {}).get("chunk_count", 0) > 0
                            or (_ for _ in ()).throw(AssertionError("lineage"))
                        ),
                    )
                )
            elif operation_index == 2:
                calls.append(
                    lambda: self.request_json(
                        "mcp.list",
                        "POST",
                        "/mcp",
                        {"jsonrpc": "2.0", "id": "load-list", "method": "tools/list", "params": {}},
                        validator=lambda value: (
                            len(value.get("result", {}).get("tools", [])) == 8
                            or (_ for _ in ()).throw(AssertionError("MCP tools/list"))
                        ),
                    )
                )
            else:
                calls.append(
                    lambda slug=slug: self.request_json(
                        "mcp.search",
                        "POST",
                        "/mcp",
                        {
                            "jsonrpc": "2.0",
                            "id": "load-search",
                            "method": "tools/call",
                            "params": {
                                "name": "floppy.search_chunks",
                                "arguments": {
                                    "project_slug": slug,
                                    "quality_min": 0,
                                    "limit": 25,
                                    "offset": 0,
                                },
                            },
                        },
                        validator=lambda value: (
                            value.get("result", {}).get("isError") is False
                            and value.get("result", {}).get("structuredContent", {}).get("count", 0) > 0
                            or (_ for _ in ()).throw(AssertionError("MCP search"))
                        ),
                    )
                )
        self.run_parallel(calls)
        self.assert_phase_clean("rest.chunks")
        self.assert_phase_clean("rest.lineage")
        self.assert_phase_clean("mcp.list")
        self.assert_phase_clean("mcp.search")

    def verify_database(self):
        """Check row counts, metadata and lineage after the HTTP workload."""
        expected_documents = self.args.projects * self.args.documents_per_project
        totals = {
            "projects": len(self.projects),
            "documents": 0,
            "chunks": 0,
            "chunk_metadata": 0,
            "registry_rows": 0,
            "dataset_builds": 0,
            "broken_previous_links": 0,
            "broken_next_links": 0,
            "invalid_position_ratios": 0,
        }
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for slug in self.projects:
                    shard_table = f"{slug}_shard"
                    chunk_table = f"{slug}_chunk"
                    cur.execute(
                        sql.SQL("SELECT COUNT(*)::int FROM {};").format(
                            sql.Identifier("public", shard_table)
                        )
                    )
                    totals["documents"] += cur.fetchone()[0]
                    cur.execute(
                        sql.SQL("SELECT COUNT(*)::int FROM {};").format(
                            sql.Identifier("public", chunk_table)
                        )
                    )
                    totals["chunks"] += cur.fetchone()[0]

                cur.execute(
                    """
                    SELECT COUNT(*)::int
                    FROM public.chunk_metadata
                    WHERE project_slug = ANY(%s);
                    """,
                    (self.projects,),
                )
                totals["chunk_metadata"] = cur.fetchone()[0]
                cur.execute(
                    """
                    SELECT COUNT(*)::int
                    FROM public.document_registry
                    WHERE project_slug = ANY(%s);
                    """,
                    (self.projects,),
                )
                totals["registry_rows"] = cur.fetchone()[0]
                cur.execute(
                    """
                    SELECT COUNT(*)::int
                    FROM public.dataset_build
                    WHERE project_slug = ANY(%s);
                    """,
                    (self.projects,),
                )
                totals["dataset_builds"] = cur.fetchone()[0]
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE m.previous_chunk_id IS NOT NULL
                              AND previous.chunk_id IS NULL
                        )::int,
                        COUNT(*) FILTER (
                            WHERE m.next_chunk_id IS NOT NULL
                              AND following.chunk_id IS NULL
                        )::int,
                        COUNT(*) FILTER (
                            WHERE m.document_position_ratio < 0
                               OR m.document_position_ratio > 1
                        )::int
                    FROM public.chunk_metadata AS m
                    LEFT JOIN public.chunk_metadata AS previous
                      ON previous.chunk_id = m.previous_chunk_id
                    LEFT JOIN public.chunk_metadata AS following
                      ON following.chunk_id = m.next_chunk_id
                    WHERE m.project_slug = ANY(%s);
                    """,
                    (self.projects,),
                )
                (
                    totals["broken_previous_links"],
                    totals["broken_next_links"],
                    totals["invalid_position_ratios"],
                ) = cur.fetchone()

        if totals["documents"] != expected_documents:
            raise AssertionError(f"documents: {totals['documents']} != {expected_documents}")
        if totals["registry_rows"] != expected_documents:
            raise AssertionError(f"registry: {totals['registry_rows']} != {expected_documents}")
        if totals["chunks"] <= expected_documents:
            raise AssertionError("Le scenario attendu doit produire plusieurs chunks par document.")
        if totals["chunk_metadata"] != totals["chunks"]:
            raise AssertionError("Le nombre de metadata ne correspond pas aux chunks.")
        if totals["dataset_builds"] != self.args.projects:
            raise AssertionError("Un dataset build est attendu par projet.")
        if any(
            totals[key]
            for key in (
                "broken_previous_links",
                "broken_next_links",
                "invalid_position_ratios",
            )
        ):
            raise AssertionError(f"Integrite lineage invalide: {totals}")
        return totals

    def cleanup(self):
        """Delete only the exact temporary projects created by this run."""
        deleted = []
        cleanup_errors = []
        for slug in reversed(self.projects):
            try:
                delete_project(slug)
                deleted.append(slug)
            except Exception as exc:
                cleanup_errors.append(f"{slug}: {type(exc).__name__}: {exc}")
        return {"deleted_projects": deleted, "errors": cleanup_errors}

    def execute(self):
        """Run the complete scale scenario and return its report."""
        integrity = None
        run_error = ""
        cleanup = None
        try:
            self.create_projects()
            self.import_documents()
            self.normalize_documents()
            self.chunk_projects()
            self.approve_sample()
            self.build_datasets()
            self.exercise_reads()
            integrity = self.verify_database()
        except Exception as exc:
            run_error = f"{type(exc).__name__}: {exc}"
        finally:
            cleanup = self.cleanup()

        duration_seconds = time.time() - self.started_at
        summaries = summarize_metrics(self.metrics) if self.metrics else {}
        failures = [metric for metric in self.metrics if not metric["ok"]]
        read_durations = [
            metric["duration_ms"]
            for metric in self.metrics
            if metric["operation"] in READ_OPERATIONS and metric["ok"]
        ]
        write_durations = [
            metric["duration_ms"]
            for metric in self.metrics
            if metric["operation"] not in READ_OPERATIONS and metric["ok"]
        ]
        thresholds = {
            "read_p95_ms": round(percentile(read_durations, 0.95), 2) if read_durations else None,
            "read_p95_limit_ms": self.args.read_p95_ms,
            "write_p95_ms": round(percentile(write_durations, 0.95), 2) if write_durations else None,
            "write_p95_limit_ms": self.args.write_p95_ms,
        }
        passed = (
            not run_error
            and not failures
            and not cleanup["errors"]
            and thresholds["read_p95_ms"] is not None
            and thresholds["read_p95_ms"] <= self.args.read_p95_ms
            and thresholds["write_p95_ms"] is not None
            and thresholds["write_p95_ms"] <= self.args.write_p95_ms
        )
        return {
            "passed": passed,
            "configuration": {
                "base_url": self.base_url,
                "projects": self.args.projects,
                "documents_per_project": self.args.documents_per_project,
                "words_per_document": self.args.words_per_document,
                "read_requests": self.args.read_requests,
                "concurrency": self.args.concurrency,
                "ip_pool_size": self.args.ip_pool_size,
                "use_forwarded_ip_pool": self.args.use_forwarded_ip_pool,
            },
            "duration_seconds": round(duration_seconds, 2),
            "throughput_requests_per_second": round(
                len(self.metrics) / duration_seconds if duration_seconds else 0,
                2,
            ),
            "total_requests": len(self.metrics),
            "successful_requests": sum(1 for metric in self.metrics if metric["ok"]),
            "failed_requests": len(failures),
            "thresholds": thresholds,
            "operations": summaries,
            "integrity": integrity,
            "run_error": run_error,
            "failure_samples": failures[:10],
            "cleanup": cleanup,
            "note": (
                "Le contrat rate-limit est teste par la suite d'integration. Pour un "
                "run de charge, augmentez ses seuils cote serveur. Le pool X-Forwarded-For "
                "n'est utilise que sur demande et requiert FLOPPY_TRUST_PROXY_HEADERS=true."
            ),
        }


def parse_args():
    """Parse bounded scale-test options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.getenv("CONNECTOR_TEST_TOKEN", "dev-token"))
    parser.add_argument("--projects", type=int, default=8)
    parser.add_argument("--documents-per-project", type=int, default=25)
    parser.add_argument("--words-per-document", type=int, default=160)
    parser.add_argument("--read-requests", type=int, default=400)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--ip-pool-size", type=int, default=64)
    parser.add_argument("--use-forwarded-ip-pool", action="store_true")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--read-p95-ms", type=float, default=2000)
    parser.add_argument("--write-p95-ms", type=float, default=30000)
    args = parser.parse_args()
    args.projects = min(max(args.projects, 1), 30)
    args.documents_per_project = min(max(args.documents_per_project, 1), 200)
    args.words_per_document = min(max(args.words_per_document, 40), 2000)
    args.read_requests = min(max(args.read_requests, 4), 10000)
    args.concurrency = min(max(args.concurrency, 1), 64)
    args.ip_pool_size = min(max(args.ip_pool_size, 1), 200)
    args.timeout = min(max(args.timeout, 1), 360)
    return args


def main():
    """Execute the run, print a machine-readable report and set exit status."""
    report = ConnectorLoadRun(parse_args()).execute()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

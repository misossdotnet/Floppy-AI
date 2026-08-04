"""Integration and contract tests for the REST and MCP connectors."""

import json
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

# The connector suite deliberately exercises the production authorization
# path even when the surrounding test process uses APP_ENV=test.
os.environ["APP_ENV"] = "test"
os.environ["FLASK_SECRET_KEY"] = "connector-contract-flask-secret"
os.environ["FLOPPY_JWT_SECRET"] = "connector-contract-jwt-secret"
os.environ["FLOPPY_REQUIRE_AUTH"] = "true"
os.environ["FLOPPY_RATE_LIMIT_ENABLED"] = "false"
os.environ["FLOPPY_TRUST_PROXY_HEADERS"] = "false"
os.environ["FLOPPY_CORS_ORIGINS"] = "http://client.test"
os.environ["FLOPPY_AUTH_TOKENS"] = json.dumps(
    {
        "connector-admin": ["admin"],
        "connector-imports": ["imports"],
        "connector-normalize": ["normalize"],
        "connector-chunk": ["chunk"],
        "connector-build": ["build_dataset"],
        "connector-approve": ["approve"],
        "connector-mcp": ["mcp"],
    }
)
os.environ["FLOPPY_API_USERS"] = json.dumps(
    {
        "client-api": {
            "password": "client-password",
            "scopes": [
                "imports",
                "normalize",
                "chunk",
                "build_dataset",
                "approve",
                "mcp",
            ],
        }
    }
)

from app import app
from security import _RATE_BUCKETS
from db import get_db_connection
from services import create_project, delete_project


ADMIN_HEADERS = {"Authorization": "Bearer connector-admin"}
MCP_HEADERS = {
    **ADMIN_HEADERS,
    "Content-Type": "application/json",
}


def sample_document(index=1):
    """Return a stable mixed Markdown document for connector tests."""
    return {
        "source_document": "connector-contract",
        "title_document": f"Guide connecteur {index}",
        "autor_document": "qa",
        "content_document": f"""# Guide connecteur {index}

Paragraphe de validation REST et MCP avec une information utile au client.

| Cle | Valeur |
| --- | --- |
| numero | {index} |

```python
value = {index}
result = value + 1
```
""",
    }


class ApiConnectorContractTest(unittest.TestCase):
    """Exercise client-visible contracts against Flask and PostgreSQL."""

    def setUp(self):
        project_name = f"Connector Contract {uuid4().hex[:12]}"
        _, self.project_slug = create_project(project_name)
        self.client = app.test_client()
        _RATE_BUCKETS.clear()

    def tearDown(self):
        os.environ["FLOPPY_RATE_LIMIT_ENABLED"] = "false"
        os.environ["FLOPPY_TRUST_PROXY_HEADERS"] = "false"
        _RATE_BUCKETS.clear()
        delete_project(self.project_slug)

    def request_json(self, method, path, payload=None, headers=None):
        """Perform one JSON request and return its response and decoded body."""
        response = self.client.open(
            path,
            method=method,
            json=payload,
            headers=ADMIN_HEADERS if headers is None else headers,
        )
        return response, response.get_json(silent=True)

    def mcp(self, method, params=None, token="connector-admin", request_id="contract"):
        """Call the MCP JSON-RPC endpoint."""
        return self.request_json(
            "POST",
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            },
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    def mcp_tool(self, name, arguments, token="connector-admin"):
        """Call one MCP tool and assert the outer JSON-RPC contract."""
        response, body = self.mcp(
            "tools/call",
            {"name": name, "arguments": arguments},
            token=token,
            request_id=name,
        )
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertEqual(body["id"], name)
        self.assertIn("result", body)
        return body["result"]

    def test_rest_authentication_scope_and_token_transport_matrix(self):
        path = f"/api/v1/projects/{self.project_slug}/imports"
        payload = {"documents": [sample_document()]}

        response, body = self.request_json("POST", path, payload, headers={})
        self.assertEqual(response.status_code, 401, body)
        self.assertEqual(body["error"]["code"], "unauthorized")

        response, body = self.request_json(
            "POST",
            path,
            payload,
            headers={"Authorization": "Bearer invalid-token"},
        )
        self.assertEqual(response.status_code, 401, body)

        response, body = self.request_json(
            "POST",
            path,
            payload,
            headers={"Authorization": "Bearer connector-chunk"},
        )
        self.assertEqual(response.status_code, 403, body)
        self.assertEqual(body["error"]["code"], "forbidden")

        response, body = self.request_json(
            "POST",
            f"{path}?api_token=connector-admin",
            payload,
            headers={},
        )
        self.assertEqual(response.status_code, 401, body)

        response, body = self.request_json("POST", path, payload)
        self.assertEqual(response.status_code, 201, body)
        self.assertEqual(body["imported_count"], 1)
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT actor
                    FROM public.business_audit_event
                    WHERE resource_path = %s AND status_code = 201
                    ORDER BY created_at DESC
                    LIMIT 1;
                    """,
                    (path,),
                )
                actor = cur.fetchone()[0]
        self.assertTrue(actor.startswith("static:"), actor)
        self.assertNotIn("connector-admin", actor)

    def test_rest_full_corpus_flow_and_legacy_alias(self):
        import_response, imported = self.request_json(
            "POST",
            f"/api/v1/projects/{self.project_slug}/imports",
            {"documents": [sample_document(1), sample_document(2)]},
        )
        self.assertEqual(import_response.status_code, 201, imported)
        document_ids = [item["document_id"] for item in imported["documents"]]

        for document_id in document_ids:
            response, normalized = self.request_json(
                "POST",
                f"/api/v1/documents/{document_id}/normalize",
                {"project_slug": self.project_slug},
            )
            self.assertEqual(response.status_code, 200, normalized)
            self.assertEqual(normalized["normalization_version"], "v2")
            self.assertTrue(normalized["normalized_content"])

        response, chunked = self.request_json(
            "POST",
            f"/api/v1/projects/{self.project_slug}/chunk",
            {
                "chunkMaxTokens": 24,
                "hardMaxTokens": 30,
                "codeAware": True,
                "tableAware": True,
                "strictZoneTypes": ["code", "table", "strict"],
            },
        )
        self.assertEqual(response.status_code, 200, chunked)
        self.assertGreaterEqual(chunked["generated_chunks"], 6)

        response, chunks = self.request_json(
            "GET",
            f"/api/v1/chunks?project={self.project_slug}&limit=100",
        )
        self.assertEqual(response.status_code, 200, chunks)
        self.assertEqual(chunks["count"], chunked["generated_chunks"])
        self.assertTrue(
            all("document_position_ratio" in item["metadata"] for item in chunks["items"])
        )

        legacy_response, legacy_chunks = self.request_json(
            "GET",
            f"/chunks?project={self.project_slug}&limit=1",
        )
        self.assertEqual(legacy_response.status_code, 200, legacy_chunks)
        self.assertEqual(legacy_chunks["count"], 1)

        response, lineage = self.request_json(
            "GET",
            f"/api/v1/documents/{document_ids[0]}/lineage?project={self.project_slug}",
        )
        self.assertEqual(response.status_code, 200, lineage)
        self.assertGreater(lineage["lineage"]["chunk_count"], 0)

        response, approval = self.request_json(
            "POST",
            f"/api/v1/documents/{document_ids[0]}/approve",
            {
                "project_slug": self.project_slug,
                "status": "approved",
                "comment": "Contrat client valide.",
                "approved_by": "connector-suite",
            },
        )
        self.assertEqual(response.status_code, 200, approval)
        self.assertEqual(approval["status"], "approved")

        response, built = self.request_json(
            "POST",
            f"/api/v1/projects/{self.project_slug}/build-dataset",
            {"quality_min": 0, "approved_only": False, "limit": 100},
        )
        self.assertEqual(response.status_code, 201, built)
        self.assertGreater(built["stats"]["selected_chunks"], 0)

        response, fetched_build = self.request_json(
            "GET",
            f"/api/v1/dataset-builds/{built['build_id']}",
        )
        self.assertEqual(response.status_code, 200, fetched_build)
        self.assertEqual(fetched_build["build_id"], built["build_id"])

    def test_every_sensitive_rest_route_enforces_its_scope(self):
        response, imported = self.request_json(
            "POST",
            f"/api/v1/projects/{self.project_slug}/imports",
            {"documents": [sample_document()]},
        )
        self.assertEqual(response.status_code, 201, imported)
        document_id = imported["documents"][0]["document_id"]

        def assert_scope(method, path, token, payload=None, expected=200):
            for denied_headers, denied_status in (
                ({}, 401),
                ({"Authorization": "Bearer connector-mcp"}, 403),
            ):
                denied, denied_body = self.request_json(
                    method,
                    path,
                    payload,
                    headers=denied_headers,
                )
                self.assertEqual(denied.status_code, denied_status, denied_body)
            accepted, accepted_body = self.request_json(
                method,
                path,
                payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(accepted.status_code, expected, accepted_body)
            return accepted_body

        imported_by_scope = assert_scope(
            "POST",
            f"/api/v1/projects/{self.project_slug}/imports",
            "connector-imports",
            {"documents": [sample_document(2)]},
            expected=201,
        )
        self.assertEqual(imported_by_scope["imported_count"], 1)
        normalized = assert_scope(
            "POST",
            f"/api/v1/documents/{document_id}/normalize",
            "connector-normalize",
            {"project_slug": self.project_slug},
        )
        self.assertEqual(normalized["normalization_version"], "v2")
        chunked = assert_scope(
            "POST",
            f"/api/v1/projects/{self.project_slug}/chunk",
            "connector-chunk",
            {"chunkMaxTokens": 24, "hardMaxTokens": 30},
        )
        self.assertGreater(chunked["generated_chunks"], 0)
        chunks = assert_scope(
            "GET",
            f"/api/v1/chunks?project={self.project_slug}&limit=100",
            "connector-chunk",
        )
        self.assertGreater(chunks["count"], 0)
        lineage = assert_scope(
            "GET",
            f"/api/v1/documents/{document_id}/lineage?project={self.project_slug}",
            "connector-chunk",
        )
        self.assertGreater(lineage["lineage"]["chunk_count"], 0)
        approved = assert_scope(
            "POST",
            f"/api/v1/documents/{document_id}/approve",
            "connector-approve",
            {"project_slug": self.project_slug, "status": "approved"},
        )
        self.assertEqual(approved["status"], "approved")
        built = assert_scope(
            "POST",
            f"/api/v1/projects/{self.project_slug}/build-dataset",
            "connector-build",
            {"quality_min": 0, "approved_only": False, "limit": 100},
            expected=201,
        )
        fetched = assert_scope(
            "GET",
            f"/api/v1/dataset-builds/{built['build_id']}",
            "connector-build",
        )
        self.assertEqual(fetched["build_id"], built["build_id"])

    def test_rest_validation_errors_are_stable_and_safe(self):
        response, body = self.request_json(
            "POST",
            f"/api/v1/projects/{self.project_slug}/chunk",
            {"strictZoneTypes": "code,table"},
        )
        self.assertEqual(response.status_code, 400, body)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertNotIn("traceback", json.dumps(body).lower())

        response, body = self.request_json(
            "GET",
            "/api/v1/chunks?limit=not-an-integer",
        )
        self.assertEqual(response.status_code, 400, body)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_concurrent_normalization_does_not_deadlock_schema_checks(self):
        response, imported = self.request_json(
            "POST",
            f"/api/v1/projects/{self.project_slug}/imports",
            {"documents": [sample_document(index) for index in range(8)]},
        )
        self.assertEqual(response.status_code, 201, imported)
        document_ids = [item["document_id"] for item in imported["documents"]]

        def normalize(index_and_document_id):
            index, document_id = index_and_document_id
            with app.test_client() as concurrent_client:
                result = concurrent_client.post(
                    f"/api/v1/documents/{document_id}/normalize",
                    json={"project_slug": self.project_slug},
                    headers={
                        **ADMIN_HEADERS,
                        "X-Forwarded-For": f"198.51.100.{index + 1}",
                    },
                )
                return result.status_code, result.get_json(silent=True)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(normalize, enumerate(document_ids)))

        self.assertEqual(
            [status for status, _ in results],
            [200] * len(document_ids),
            results,
        )
        self.assertTrue(
            all(body["normalization_version"] == "v2" for _, body in results)
        )

    def test_concurrent_import_and_chunk_are_consistent_for_one_project(self):
        def import_one(index):
            with app.test_client() as concurrent_client:
                result = concurrent_client.post(
                    f"/api/v1/projects/{self.project_slug}/imports",
                    json={"documents": [sample_document(index)]},
                    headers=ADMIN_HEADERS,
                )
                return result.status_code, result.get_json(silent=True)

        with ThreadPoolExecutor(max_workers=6) as executor:
            imports = list(executor.map(import_one, range(6)))

        self.assertEqual([status for status, _ in imports], [201] * 6, imports)
        self.assertEqual(
            len({body["documents"][0]["document_id"] for _, body in imports}),
            6,
        )

        def chunk_once(_index):
            with app.test_client() as concurrent_client:
                result = concurrent_client.post(
                    f"/api/v1/projects/{self.project_slug}/chunk",
                    json={"chunkMaxTokens": 24, "hardMaxTokens": 30},
                    headers=ADMIN_HEADERS,
                )
                return result.status_code, result.get_json(silent=True)

        with ThreadPoolExecutor(max_workers=3) as executor:
            chunk_runs = list(executor.map(chunk_once, range(3)))

        self.assertEqual([status for status, _ in chunk_runs], [200] * 3, chunk_runs)
        generated_counts = {body["generated_chunks"] for _, body in chunk_runs}
        self.assertEqual(len(generated_counts), 1, chunk_runs)

        response, chunks = self.request_json(
            "GET",
            f"/api/v1/chunks?project={self.project_slug}&limit=1000",
        )
        self.assertEqual(response.status_code, 200, chunks)
        self.assertEqual(chunks["count"], generated_counts.pop())

    def test_mcp_protocol_acl_and_all_business_tools(self):
        response, initialized = self.mcp("initialize", request_id="initialize")
        self.assertEqual(response.status_code, 200, initialized)
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "floppy-ai-mcp")

        response, tools = self.mcp("tools/list", request_id="tools")
        self.assertEqual(response.status_code, 200, tools)
        tool_names = {item["name"] for item in tools["result"]["tools"]}
        self.assertEqual(
            tool_names,
            {
                "floppy.import_documents",
                "floppy.normalize_document",
                "floppy.chunk_project",
                "floppy.build_dataset",
                "floppy.get_dataset_build",
                "floppy.search_chunks",
                "floppy.get_document_lineage",
                "floppy.approve_document",
            },
        )

        denied = self.mcp_tool(
            "floppy.import_documents",
            {"project_slug": self.project_slug, "documents": [sample_document()]},
            token="connector-mcp",
        )
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["structuredContent"]["error"]["code"], "forbidden")

        imported = self.mcp_tool(
            "floppy.import_documents",
            {"project_slug": self.project_slug, "documents": [sample_document()]},
        )
        self.assertFalse(imported["isError"])
        document_id = imported["structuredContent"]["documents"][0]["document_id"]

        normalized = self.mcp_tool(
            "floppy.normalize_document",
            {"document_id": document_id, "project_slug": self.project_slug},
        )
        self.assertEqual(normalized["structuredContent"]["normalization_version"], "v2")

        chunked = self.mcp_tool(
            "floppy.chunk_project",
            {"project_slug": self.project_slug, "chunkMaxTokens": 24, "hardMaxTokens": 30},
        )
        self.assertGreater(chunked["structuredContent"]["generated_chunks"], 0)

        searched = self.mcp_tool(
            "floppy.search_chunks",
            {"project_slug": self.project_slug, "quality_min": 0, "limit": 100, "offset": 0},
        )
        self.assertGreater(searched["structuredContent"]["count"], 0)

        lineage = self.mcp_tool(
            "floppy.get_document_lineage",
            {"document_id": document_id, "project_slug": self.project_slug},
        )
        self.assertGreater(lineage["structuredContent"]["lineage"]["chunk_count"], 0)

        approval = self.mcp_tool(
            "floppy.approve_document",
            {
                "document_id": document_id,
                "project_slug": self.project_slug,
                "status": "approved",
                "approved_by": "mcp-contract",
            },
        )
        self.assertEqual(approval["structuredContent"]["status"], "approved")

        built = self.mcp_tool(
            "floppy.build_dataset",
            {"project_slug": self.project_slug, "quality_min": 0, "limit": 100},
        )
        build_id = built["structuredContent"]["build_id"]
        fetched = self.mcp_tool("floppy.get_dataset_build", {"build_id": build_id})
        self.assertEqual(fetched["structuredContent"]["build_id"], build_id)

        notification = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=MCP_HEADERS,
        )
        self.assertEqual(notification.status_code, 204)

        response, unknown = self.mcp("unknown/method", request_id="unknown")
        self.assertEqual(response.status_code, 404, unknown)
        self.assertEqual(unknown["error"]["code"], -32601)

    def test_jwt_issue_rotation_revocation_and_scope_use(self):
        response, body = self.request_json(
            "POST",
            "/api/v1/auth/token",
            {"username": "client-api", "password": "wrong"},
            headers={},
        )
        self.assertEqual(response.status_code, 401, body)

        response, issued = self.request_json(
            "POST",
            "/api/v1/auth/token",
            {"username": "client-api", "password": "client-password"},
            headers={},
        )
        self.assertEqual(response.status_code, 200, issued)
        access_token = issued["access_token"]
        refresh_token = issued["refresh_token"]

        response, chunks = self.request_json(
            "GET",
            f"/api/v1/chunks?project={self.project_slug}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200, chunks)

        response, rotated = self.request_json(
            "POST",
            "/api/v1/auth/refresh",
            {"refresh_token": refresh_token},
            headers={},
        )
        self.assertEqual(response.status_code, 200, rotated)

        response, reused = self.request_json(
            "POST",
            "/api/v1/auth/refresh",
            {"refresh_token": refresh_token},
            headers={},
        )
        self.assertEqual(response.status_code, 401, reused)

        new_access = rotated["access_token"]
        response, revoked = self.request_json(
            "POST",
            "/api/v1/auth/revoke",
            {},
            headers={"Authorization": f"Bearer {new_access}"},
        )
        self.assertEqual(response.status_code, 200, revoked)

        response, denied = self.request_json(
            "GET",
            f"/api/v1/chunks?project={self.project_slug}",
            headers={"Authorization": f"Bearer {new_access}"},
        )
        self.assertEqual(response.status_code, 401, denied)

    def test_cors_allow_list_and_rate_limit_contract(self):
        allowed_response = self.client.get(
            f"/api/v1/chunks?project={self.project_slug}",
            headers={**ADMIN_HEADERS, "Origin": "http://client.test"},
        )
        self.assertEqual(allowed_response.status_code, 200)
        self.assertEqual(
            allowed_response.headers.get("Access-Control-Allow-Origin"),
            "http://client.test",
        )
        self.assertEqual(allowed_response.headers.get("Vary"), "Origin")

        denied_response = self.client.get(
            f"/api/v1/chunks?project={self.project_slug}",
            headers={**ADMIN_HEADERS, "Origin": "http://untrusted.test"},
        )
        self.assertEqual(denied_response.status_code, 200)
        self.assertIsNone(denied_response.headers.get("Access-Control-Allow-Origin"))

        os.environ["FLOPPY_RATE_LIMIT_ENABLED"] = "true"
        os.environ["FLOPPY_RATE_LIMIT_API"] = "2"
        _RATE_BUCKETS.clear()
        for index in range(2):
            response = self.client.get(
                f"/api/v1/chunks?project={self.project_slug}",
                headers={**ADMIN_HEADERS, "X-Forwarded-For": f"198.51.100.{index + 1}"},
            )
            self.assertEqual(response.status_code, 200)
        limited = self.client.get(
            f"/api/v1/chunks?project={self.project_slug}",
            headers={**ADMIN_HEADERS, "X-Forwarded-For": "198.51.100.3"},
        )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers.get("Retry-After"), "60")
        self.assertEqual(limited.get_json()["error"]["code"], "rate_limited")

        os.environ["FLOPPY_TRUST_PROXY_HEADERS"] = "true"
        _RATE_BUCKETS.clear()
        for index in range(3):
            response = self.client.get(
                f"/api/v1/chunks?project={self.project_slug}",
                headers={**ADMIN_HEADERS, "X-Forwarded-For": f"203.0.113.{index + 1}"},
            )
            self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()

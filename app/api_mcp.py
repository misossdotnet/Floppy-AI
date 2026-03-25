"""MCP JSON-RPC route registration and dispatch."""

from services import *


def register_api_mcp_routes(app):
    """Register MCP routes on the Flask app."""
    @app.get("/mcp")
    @require_scopes("mcp")
    def mcp_info():
        """Handle the mcp info request."""
        return {
            "name": "floppy-ai-mcp",
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "tools_count": len(mcp_tools_catalog()),
            "health": "ok",
        }


    @app.post("/mcp")
    @require_scopes("mcp")
    def mcp_endpoint():
        """Handle the mcp endpoint request."""
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return mcp_response_payload(
                None,
                error={"code": -32600, "message": "Invalid Request"},
            ), 400

        request_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

        if request_id is None and isinstance(method, str) and method.startswith("notifications/"):
            return "", 204

        if method == "initialize":
            return mcp_response_payload(
                request_id,
                result={
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "serverInfo": {
                        "name": "floppy-ai-mcp",
                        "version": "1.0.0",
                    },
                    "capabilities": {
                        "tools": {},
                    },
                },
            )

        if method == "tools/list":
            return mcp_response_payload(
                request_id,
                result={"tools": mcp_tools_catalog()},
            )

        if method == "tools/call":
            tool_name = (params.get("name") or "").strip()
            tool_arguments = params.get("arguments")
            if not tool_name:
                return mcp_response_payload(
                    request_id,
                    result=mcp_error_result(
                        "validation_error",
                        "Le parametre 'name' est obligatoire pour tools/call.",
                    ),
                )
            try:
                enforce_mcp_tool_acl(tool_name)
                tool_result = execute_mcp_tool(tool_name, tool_arguments)
                return mcp_response_payload(
                    request_id,
                    result=mcp_tool_result_payload(tool_result),
                )
            except PermissionError as exc:
                return mcp_response_payload(
                    request_id,
                    result=mcp_error_result(
                        "forbidden",
                        public_exception_message(exc, DEFAULT_ERROR_MESSAGES["forbidden"]),
                    ),
                )
            except ValueError as exc:
                return mcp_response_payload(
                    request_id,
                    result=mcp_error_result(
                        "validation_error",
                        public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                    ),
                )
            except Exception as exc:
                error_id = log_internal_error("mcp.tools_call", exc)
                return mcp_response_payload(
                    request_id,
                    result=mcp_error_result(
                        "internal_error",
                        DEFAULT_ERROR_MESSAGES["internal_error"],
                        details={"error_id": error_id},
                    ),
                )

        return mcp_response_payload(
            request_id,
            error={"code": -32601, "message": f"Method not found: {method}"},
        ), 404

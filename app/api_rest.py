"""REST API route registration for business endpoints."""

from services import (
    DEFAULT_CHUNK_OPTIONS,
    DEFAULT_ERROR_MESSAGES,
    api_error_response,
    api_internal_error_response,
    approve_document_by_id,
    build_dataset_for_project,
    chunk_project_for_api,
    get_dataset_build_by_id,
    get_document_lineage,
    import_documents_for_project,
    list_chunks_for_api,
    merge_payloads,
    normalize_document_by_id,
    public_exception_message,
    read_json_payload,
    request,
    require_any_scope,
    require_scopes,
    validate_operation_payload,
)


def register_api_rest_routes(app):
    """Register REST API routes on the Flask app."""
    @app.post("/projects/<project_slug>/imports")
    @app.post("/api/v1/projects/<project_slug>/imports")
    @require_scopes("imports")
    def api_project_imports(project_slug):
        """Handle the api project imports request."""
        payload = read_json_payload()
        request_payload = merge_payloads({"project_slug": project_slug}, payload)
        documents = request_payload.get("documents")
        if documents is None:
            documents = [payload] if payload else []
        request_payload["documents"] = documents

        try:
            parsed_payload = validate_operation_payload("import_documents", request_payload)
            result = import_documents_for_project(
                parsed_payload["project_slug"],
                parsed_payload["documents"],
            )
            return {
                "ok": True,
                **result,
            }, 201
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                status_code=400,
                code="validation_error",
            )
        except Exception as exc:
            return api_internal_error_response("api_project_imports", exc)


    @app.post("/documents/<document_id>/normalize")
    @app.post("/api/v1/documents/<document_id>/normalize")
    @require_scopes("normalize")
    def api_document_normalize(document_id):
        """Handle the api document normalize request."""
        payload = read_json_payload()
        request_payload = {
            "document_id": document_id,
            "project_slug": (
            payload.get("project_slug")
            or payload.get("project")
            or request.args.get("project")
            or ""
            ),
            "normalization_version": payload.get("normalization_version"),
            "normalization_options": payload.get("normalization_options") or {},
        }

        try:
            parsed_payload = validate_operation_payload("normalize_document", request_payload)
            result = normalize_document_by_id(
                document_id=parsed_payload["document_id"],
                project_slug=parsed_payload.get("project_slug", ""),
                normalization_version=parsed_payload.get("normalization_version", ""),
                normalization_options=parsed_payload.get("normalization_options", {}),
            )
            return {
                "ok": True,
                **result,
            }
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                status_code=400,
                code="validation_error",
            )
        except Exception as exc:
            return api_internal_error_response("api_document_normalize", exc)


    @app.post("/projects/<project_slug>/chunk")
    @app.post("/api/v1/projects/<project_slug>/chunk")
    @require_scopes("chunk")
    def api_project_chunk(project_slug):
        """Handle the api project chunk request."""
        payload = read_json_payload()
        request_payload = merge_payloads({"project_slug": project_slug}, payload)
        try:
            parsed_payload = validate_operation_payload("chunk_project", request_payload)
            chunk_options_payload = {
                key: value
                for key, value in parsed_payload.items()
                if key in DEFAULT_CHUNK_OPTIONS
            }
            result = chunk_project_for_api(parsed_payload["project_slug"], chunk_options_payload)
            return {
                "ok": True,
                **result,
            }
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                status_code=400,
                code="validation_error",
            )
        except Exception as exc:
            return api_internal_error_response("api_project_chunk", exc)


    @app.post("/projects/<project_slug>/build-dataset")
    @app.post("/api/v1/projects/<project_slug>/build-dataset")
    @require_scopes("build_dataset")
    def api_project_build_dataset(project_slug):
        """Handle the api project build dataset request."""
        payload = read_json_payload()
        request_payload = merge_payloads({"project_slug": project_slug}, payload)
        try:
            parsed_payload = validate_operation_payload("build_dataset", request_payload)
            result = build_dataset_for_project(parsed_payload["project_slug"], parsed_payload)
            return {
                "ok": True,
                **result,
            }, 201
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                status_code=400,
                code="validation_error",
            )
        except Exception as exc:
            return api_internal_error_response("api_project_build_dataset", exc)


    @app.get("/dataset-builds/<build_id>")
    @app.get("/api/v1/dataset-builds/<build_id>")
    @require_scopes("build_dataset")
    def api_dataset_build_get(build_id):
        """Handle the api dataset build get request."""
        try:
            parsed_payload = validate_operation_payload(
                "get_dataset_build",
                {"build_id": build_id},
            )
            result = get_dataset_build_by_id(parsed_payload["build_id"])
            return {
                "ok": True,
                **result,
            }
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["not_found"]),
                status_code=404,
                code="not_found",
            )
        except Exception as exc:
            return api_internal_error_response("api_dataset_build_get", exc)


    @app.get("/chunks")
    @app.get("/api/v1/chunks")
    @require_any_scope("chunk", "build_dataset", "approve")
    def api_chunks_list():
        """Handle the api chunks list request."""
        try:
            parsed_payload = validate_operation_payload(
                "search_chunks",
                {
                    "project_slug": request.args.get("project"),
                    "quality_min": request.args.get("quality_min"),
                    "limit": request.args.get("limit"),
                    "offset": request.args.get("offset"),
                },
            )
            result = list_chunks_for_api(
                project_slug=parsed_payload["project_slug"],
                quality_min=parsed_payload.get("quality_min", 0.0),
                limit=parsed_payload.get("limit", 100),
                offset=parsed_payload.get("offset", 0),
            )
            return {
                "ok": True,
                **result,
            }
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                status_code=400,
                code="validation_error",
            )
        except Exception as exc:
            return api_internal_error_response("api_chunks_list", exc)


    @app.get("/documents/<document_id>/lineage")
    @app.get("/api/v1/documents/<document_id>/lineage")
    @require_any_scope("chunk", "approve")
    def api_document_lineage(document_id):
        """Handle the api document lineage request."""
        try:
            parsed_payload = validate_operation_payload(
                "get_document_lineage",
                {
                    "document_id": document_id,
                    "project_slug": request.args.get("project"),
                },
            )
            result = get_document_lineage(
                parsed_payload["document_id"],
                parsed_payload.get("project_slug", ""),
            )
            return {
                "ok": True,
                **result,
            }
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["not_found"]),
                status_code=404,
                code="not_found",
            )
        except Exception as exc:
            return api_internal_error_response("api_document_lineage", exc)


    @app.post("/documents/<document_id>/approve")
    @app.post("/api/v1/documents/<document_id>/approve")
    @require_scopes("approve")
    def api_document_approve(document_id):
        """Handle the api document approve request."""
        payload = read_json_payload()
        request_payload = merge_payloads({"document_id": document_id}, payload)
        if payload.get("project") and not request_payload.get("project_slug"):
            request_payload["project_slug"] = payload.get("project")
        if payload.get("approval_comment") and not request_payload.get("comment"):
            request_payload["comment"] = payload.get("approval_comment")
        try:
            parsed_payload = validate_operation_payload("approve_document", request_payload)
            result = approve_document_by_id(parsed_payload["document_id"], parsed_payload)
            return {
                "ok": True,
                **result,
            }
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                status_code=400,
                code="validation_error",
            )
        except Exception as exc:
            return api_internal_error_response("api_document_approve", exc)

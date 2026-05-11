"""UI route registration for HTML views and form actions."""

from agentai_docs import (
    generate_agentai_documents,
    get_agentai_docs_payload,
    save_agentai_documents,
)
from flask import Response
from document_vision import (
    analyze_project_document,
    document_vision_status,
    get_document_vision_config,
    get_document_vision_project_payload,
    save_document_vision_config,
)
from shard_quality import (
    analyze_shard_quality,
    get_shard_quality_config,
    get_shard_quality_index_payload,
    get_shard_quality_payload,
    save_shard_quality_config,
    shard_quality_status,
)
from llm_gateway import (
    LLM_PROFILE_TYPES,
    delete_llm_config,
    effective_llm_config,
    list_llm_audit_sessions,
    get_llm_audit_session,
    list_llm_configs,
    llm_audit_stats,
    llm_connection_status,
    save_llm_config,
    set_default_llm_config,
    test_llm_config,
)
from llm_comparator import (
    export_run as export_llm_comparator_run,
    get_llm_comparator_payload,
    get_run_detail as get_llm_comparator_run_detail,
    run_llm_comparison,
)
from services import *
from webchat import (
    add_pipeline_step,
    delete_pipeline_step,
    get_webchat_config,
    get_webchat_session_detail,
    list_recent_webchat_sessions,
    process_public_webchat_message,
    update_pipeline_step,
    update_webchat_config,
    webchat_public_status,
)
from quizbot import (
    QuizbotUnavailableError,
    archive_quizbot_topic,
    create_quizbot_topic,
    delete_quizbot_topic,
    get_quizbot_config,
    get_quizbot_dashboard,
    get_quizbot_session_detail,
    list_quizbot_audit_events,
    list_quizbot_sessions,
    list_quizbot_topics,
    quizbot_public_status,
    save_quizbot_config,
    start_quiz_session,
    submit_quiz_answer,
    submit_quiz_feedback,
    update_quizbot_topic,
)
from shard_to_chunk_llm import (
    generate_chunks_with_llm,
    get_shard_to_chunk_payload,
)
from task_sequencer import (
    generate_task_sequence,
    get_task_sequencer_config,
    get_task_sequencer_payload,
    save_task_sequencer_config,
    suggest_task_sequence_axes,
    task_sequencer_status,
)
from tools import html_to_markdown
from vectorization import (
    get_vectorization_admin_payload,
    save_vectorization_config,
    test_vectorization_config,
    vectorize_project_data,
)


ADMIN_UI_ENDPOINTS = {
    "admin_dashboard",
    "admin_logout",
    "admin_llm_config",
    "admin_llm_config_default",
    "admin_llm_config_delete",
    "admin_llm_config_test",
    "admin_llm_audit",
    "admin_llm_audit_detail",
    "admin_llm_comparator",
    "admin_llm_comparator_detail",
    "admin_llm_comparator_export",
    "admin_webchat",
    "admin_webchat_config",
    "admin_webchat_pipeline_add",
    "admin_webchat_pipeline_update",
    "admin_webchat_pipeline_delete",
    "admin_webchat_sessions",
    "admin_webchat_session_detail",
    "admin_quizbot_dashboard",
    "admin_quizbot_config",
    "admin_quizbot_topics",
    "admin_quizbot_topic_add",
    "admin_quizbot_topic_update",
    "admin_quizbot_topic_archive",
    "admin_quizbot_topic_delete",
    "admin_quizbot_sessions",
    "admin_quizbot_session_detail",
    "admin_quizbot_audit",
    "admin_chat_evaluations",
    "admin_faq",
    "admin_workflow_sequencer",
    "admin_workflow_sequencer_config",
    "admin_workflow_sequencer_config_save",
    "admin_workflow_sequencer_suggest_axes",
    "admin_workflow_sequencer_generate",
    "admin_task_sequencer_legacy",
    "admin_task_sequencer_config_legacy",
    "admin_task_sequencer_config_save_legacy",
    "admin_task_sequencer_suggest_axes_legacy",
    "admin_task_sequencer_generate_legacy",
    "admin_chunking_config",
    "admin_shard_to_chunk_generate",
    "admin_vectorization",
    "admin_vectorization_test",
    "admin_document_review",
    "admin_document_vision_config",
    "admin_document_vision_config_save",
    "admin_shard_quality_config",
    "admin_shard_quality_config_save",
    "project_document_vision",
    "project_document_vision_analyze",
    "project_shard_quality",
    "project_shard_quality_analyze",
    "html_to_md_tool",
    "add_project",
    "remove_project",
    "project_shard_new",
    "project_shard_view",
    "project_shard_list",
    "add_shard_item",
    "remove_shard_item",
    "project_train_list",
    "project_train_new",
    "remove_train_item",
    "vote_train_item",
    "project_chunk_list",
    "project_chunk_new",
    "project_chunk_view",
    "project_document_review",
    "project_document_review_normalize",
    "project_document_review_approve",
    "project_document_review_annotation_add",
    "project_document_review_annotation_delete",
    "project_document_review_exclusion_add",
    "project_document_review_exclusion_delete",
    "project_vectorize",
    "add_chunk_item",
    "remove_chunk_item",
    "project_chat_list",
    "project_chat_session",
    "project_chat_dashboard",
    "submit_chat_evaluation",
    "projects_shards",
    "projects_chunks",
    "projects_document_vision",
    "projects_shard_quality",
    "projects_train",
    "chunkify_project",
    "add_train_item",
    "agentai_docs",
    "generate_agentai_docs",
    "save_agentai_docs",
}


def register_ui_routes(app):
    """Register UI routes on the Flask app."""
    @app.context_processor
    def inject_auth_context():
        """Expose UI authentication state to templates."""
        authenticated = is_admin_authenticated()
        return {
            "admin_authenticated": authenticated,
            "admin_username": session.get("admin_username", "") if authenticated else "",
        }


    @app.before_request
    def protect_admin_ui_routes():
        """Protect administration UI routes with the browser session."""
        if request.endpoint in ADMIN_UI_ENDPOINTS and not is_admin_authenticated():
            return admin_auth_required_response()
        return None


    @app.get("/")
    def home():
        """Render the public home page."""
        try:
            llm_status = llm_connection_status()
            status_error = None
        except Exception as exc:
            llm_status = {
                "configured": False,
                "connected": False,
                "status_label": "deconnecte",
                "provider": "",
                "model": "",
                "api_url": "",
                "source": "",
                "checked_at": "",
                "error": "",
            }
            status_error = ui_internal_error_message("home_llm_status", exc)
        try:
            webchat_status = webchat_public_status(llm_status)
        except Exception as exc:
            webchat_status = {
                "enabled": False,
                "available": False,
                "llm_connected": False,
                "db_error": str(exc),
            }
        try:
            quizbot_status = quizbot_public_status()
        except Exception as exc:
            quizbot_status = {
                "enabled": False,
                "configured": False,
                "available": False,
                "active_topic_count": 0,
                "db_error": str(exc) or exc.__class__.__name__,
            }
        return render_template(
            "index.html",
            llm_status=llm_status,
            status_error=status_error,
            webchat_status=webchat_status,
            quizbot_status=quizbot_status,
        )


    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        """Authenticate an administrator."""
        next_url = safe_next_url(request.values.get("next", ""))
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            try:
                if verify_admin_credentials(username, password):
                    login_admin_user(username)
                    return redirect(next_url)
                flash("Identifiants administration invalides.", "error")
            except Exception as exc:
                flash_internal_error("admin_login", exc, prefix="Erreur authentification.")
        return render_template("admin_login.html", next_url=next_url)


    @app.post("/admin/logout")
    def admin_logout():
        """Logout from the admin area."""
        logout_admin_user()
        flash("Session administration fermee.", "success")
        return redirect(url_for("home"))


    @app.get("/admin")
    def admin_dashboard():
        """Render the administration dashboard."""
        try:
            projects = list_projects()
            db_error = None
        except Exception as exc:
            projects = []
            db_error = ui_internal_error_message("admin_dashboard_projects", exc)

        try:
            llm_status = llm_connection_status()
        except Exception as exc:
            llm_status = {"connected": False, "configured": False, "status_label": "deconnecte", "error": str(exc)}

        try:
            audit_stats = llm_audit_stats()
            audit_error = None
        except Exception as exc:
            audit_stats = None
            audit_error = ui_internal_error_message("admin_dashboard_audit", exc)

        try:
            quizbot_stats = get_quizbot_dashboard()
            quizbot_error = None
        except Exception as exc:
            quizbot_stats = None
            quizbot_error = ui_internal_error_message("admin_dashboard_quizbot", exc)

        return render_template(
            "admin_dashboard.html",
            projects=projects,
            db_error=db_error,
            llm_status=llm_status,
            audit_stats=audit_stats,
            audit_error=audit_error,
            quizbot_stats=quizbot_stats,
            quizbot_error=quizbot_error,
        )


    @app.get("/admin/faq")
    def admin_faq():
        """Render the module FAQ page."""
        return render_template("faq.html")


    @app.get("/admin/task-sequencer")
    def admin_task_sequencer_legacy():
        """Redirect legacy task sequencer URL to Workflow Sequencer."""
        return redirect(url_for("admin_workflow_sequencer"))


    @app.get("/admin/task-sequencer/config")
    def admin_task_sequencer_config_legacy():
        """Redirect legacy task sequencer config URL to Workflow Sequencer config."""
        return redirect(url_for("admin_workflow_sequencer_config"))


    @app.post("/admin/task-sequencer/config")
    def admin_task_sequencer_config_save_legacy():
        """Redirect legacy config POST to Workflow Sequencer config POST."""
        return redirect(url_for("admin_workflow_sequencer_config_save"), code=308)


    @app.post("/admin/task-sequencer/suggest-axes")
    def admin_task_sequencer_suggest_axes_legacy():
        """Redirect legacy axes endpoint to Workflow Sequencer axes endpoint."""
        return redirect(url_for("admin_workflow_sequencer_suggest_axes"), code=308)


    @app.post("/admin/task-sequencer/generate")
    def admin_task_sequencer_generate_legacy():
        """Redirect legacy generation endpoint to Workflow Sequencer generation endpoint."""
        return redirect(url_for("admin_workflow_sequencer_generate"), code=308)


    @app.get("/admin/workflow-sequencer")
    def admin_workflow_sequencer():
        """Render the connected Workflow Sequencer module."""
        try:
            payload = get_task_sequencer_payload()
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("admin_workflow_sequencer", exc)
        return render_template(
            "task_sequencer.html",
            payload=payload,
            db_error=db_error,
            result=None,
            form_values={},
        )


    @app.route("/admin/workflow-sequencer/config", methods=["GET"])
    def admin_workflow_sequencer_config():
        """Render Workflow Sequencer configuration."""
        try:
            config = get_task_sequencer_config()
            status = task_sequencer_status(config)
            db_error = None
        except Exception as exc:
            config = None
            status = None
            db_error = ui_internal_error_message("admin_workflow_sequencer_config", exc)
        try:
            llm_configs = list_llm_configs(redact_key=True)
            llm_config_error = None
        except Exception as exc:
            llm_configs = []
            llm_config_error = ui_internal_error_message("admin_workflow_sequencer_llm_configs", exc)
        return render_template(
            "task_sequencer_config.html",
            config=config,
            status=status,
            db_error=db_error,
            llm_configs=llm_configs,
            llm_config_error=llm_config_error,
        )


    @app.post("/admin/workflow-sequencer/config")
    def admin_workflow_sequencer_config_save():
        """Persist Workflow Sequencer configuration."""
        try:
            save_task_sequencer_config(request.form.to_dict())
            flash("Configuration Workflow Sequencer enregistree.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_workflow_sequencer_config_save", exc, prefix="Erreur configuration Workflow Sequencer.")
        return redirect(url_for("admin_workflow_sequencer_config"))


    @app.post("/admin/workflow-sequencer/suggest-axes")
    def admin_workflow_sequencer_suggest_axes():
        """Suggest sequencing axes from the configured LLM."""
        incoming_payload = request.get_json(silent=True)
        if incoming_payload is None:
            incoming_payload = request.form.to_dict()
        try:
            payload = suggest_task_sequence_axes(
                incoming_payload,
                actor=session.get("admin_username", "admin"),
            )
            return {"ok": True, **payload}
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                status_code=400,
                code="validation_error",
            )
        except Exception as exc:
            return api_internal_error_response("admin_workflow_sequencer_suggest_axes", exc)


    @app.post("/admin/workflow-sequencer/generate")
    def admin_workflow_sequencer_generate():
        """Generate a sequenced task plan from user input."""
        incoming_payload = request.get_json(silent=True)
        if incoming_payload is None:
            incoming_payload = request.form.to_dict()
        try:
            result = generate_task_sequence(
                incoming_payload,
                actor=session.get("admin_username", "admin"),
            )
            if request.is_json:
                return {"ok": True, "result": result}
            payload = get_task_sequencer_payload()
            return render_template(
                "task_sequencer.html",
                payload=payload,
                db_error=None,
                result=result,
                form_values=incoming_payload,
            )
        except ValueError as exc:
            if request.is_json:
                return api_error_response(
                    message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                    status_code=400,
                    code="validation_error",
                )
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            if request.is_json:
                return api_internal_error_response("admin_workflow_sequencer_generate", exc)
            flash_internal_error("admin_workflow_sequencer_generate", exc, prefix="Erreur generation Workflow Sequencer.")

        try:
            payload = get_task_sequencer_payload()
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("admin_workflow_sequencer_generate_reload", exc)
        return render_template(
            "task_sequencer.html",
            payload=payload,
            db_error=db_error,
            result=None,
            form_values=incoming_payload,
        )


    @app.get("/admin/document-vision/config")
    def admin_document_vision_config():
        """Render Document Vision configuration."""
        try:
            config = get_document_vision_config()
            status = document_vision_status(config)
            db_error = None
        except Exception as exc:
            config = None
            status = None
            db_error = ui_internal_error_message("admin_document_vision_config", exc)
        try:
            llm_configs = list_llm_configs(redact_key=True)
            llm_config_error = None
        except Exception as exc:
            llm_configs = []
            llm_config_error = ui_internal_error_message("admin_document_vision_llm_configs", exc)
        return render_template(
            "document_vision_config.html",
            config=config,
            status=status,
            db_error=db_error,
            llm_configs=llm_configs,
            llm_config_error=llm_config_error,
        )


    @app.post("/admin/document-vision/config")
    def admin_document_vision_config_save():
        """Persist Document Vision configuration."""
        try:
            save_document_vision_config(request.form.to_dict())
            flash("Configuration Document Vision enregistree.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_document_vision_config_save", exc, prefix="Erreur configuration Document Vision.")
        return redirect(url_for("admin_document_vision_config"))


    @app.get("/admin/shard-quality/config")
    def admin_shard_quality_config():
        """Render Shard Quality configuration."""
        try:
            config = get_shard_quality_config()
            status = shard_quality_status(config)
            db_error = None
        except Exception as exc:
            config = None
            status = None
            db_error = ui_internal_error_message("admin_shard_quality_config", exc)
        try:
            llm_configs = list_llm_configs(redact_key=True)
            llm_config_error = None
        except Exception as exc:
            llm_configs = []
            llm_config_error = ui_internal_error_message("admin_shard_quality_llm_configs", exc)
        return render_template(
            "shard_quality_config.html",
            config=config,
            status=status,
            db_error=db_error,
            llm_configs=llm_configs,
            llm_config_error=llm_config_error,
        )


    @app.post("/admin/shard-quality/config")
    def admin_shard_quality_config_save():
        """Persist Shard Quality configuration."""
        try:
            save_shard_quality_config(request.form.to_dict())
            flash("Configuration Shard Quality enregistree.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_shard_quality_config_save", exc, prefix="Erreur configuration Shard Quality.")
        return redirect(url_for("admin_shard_quality_config"))


    @app.route("/admin/llm", methods=["GET", "POST"])
    def admin_llm_config():
        """Render and update the LLM configuration registry."""
        if request.method == "POST":
            try:
                save_llm_config(request.form.to_dict())
                flash("Configuration LLM enregistree.", "success")
                return redirect(url_for("admin_llm_config"))
            except ValueError as exc:
                flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
            except Exception as exc:
                flash_internal_error("admin_llm_config", exc, prefix="Erreur configuration LLM.")

        try:
            configs = list_llm_configs(redact_key=True)
            active_config = effective_llm_config(redact_key=True)
            config_error = None
        except Exception as exc:
            configs = []
            active_config = effective_llm_config(redact_key=True)
            config_error = ui_internal_error_message("admin_llm_config_load", exc)

        try:
            status = llm_connection_status()
        except Exception as exc:
            status = None
            config_error = config_error or ui_internal_error_message("admin_llm_status", exc)

        return render_template(
            "llm_config.html",
            config=active_config,
            configs=configs,
            profile_options=LLM_PROFILE_TYPES,
            status=status,
            config_error=config_error,
        )


    @app.post("/admin/llm/<config_id>/default")
    def admin_llm_config_default(config_id):
        """Set the default LLM configuration."""
        try:
            set_default_llm_config(config_id)
            flash("Configuration LLM par defaut mise a jour.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_llm_config_default", exc, prefix="Erreur configuration LLM.")
        return redirect(url_for("admin_llm_config"))


    @app.post("/admin/llm/<config_id>/delete")
    def admin_llm_config_delete(config_id):
        """Delete one LLM configuration."""
        try:
            delete_llm_config(config_id)
            flash("Configuration LLM supprimee.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_llm_config_delete", exc, prefix="Erreur suppression configuration LLM.")
        return redirect(url_for("admin_llm_config"))


    @app.post("/admin/llm/<config_id>/test")
    def admin_llm_config_test(config_id):
        """Run a real test request against one persisted LLM configuration."""
        try:
            result = test_llm_config(config_id, request.form.get("test_prompt", ""))
            preview = result["content"][:240]
            flash(
                f"Test LLM reussi pour {result['name'] or result['config_id']}: {preview}",
                "success",
            )
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_llm_config_test", exc, prefix="Erreur test LLM.")
        return redirect(url_for("admin_llm_config"))


    @app.get("/admin/llm/audit")
    def admin_llm_audit():
        """List LLM audit sessions."""
        try:
            sessions = list_llm_audit_sessions()
            db_error = None
        except Exception as exc:
            sessions = []
            db_error = ui_internal_error_message("admin_llm_audit", exc)
        return render_template("llm_audit.html", sessions=sessions, db_error=db_error)


    @app.get("/admin/llm/audit/<session_id>")
    def admin_llm_audit_detail(session_id):
        """Show one LLM audit session."""
        try:
            payload = get_llm_audit_session(session_id)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("admin_llm_audit_detail", exc)
        return render_template("llm_audit_detail.html", payload=payload, db_error=db_error)


    @app.route("/admin/llm-comparator", methods=["GET", "POST"])
    def admin_llm_comparator():
        """Render and run the Local LLM Comparator."""
        form_values = {}
        if request.method == "POST":
            form_values = request.form.to_dict(flat=False)
            try:
                comparison = run_llm_comparison(
                    request.form,
                    files=request.files,
                    actor=session.get("admin_username", "admin"),
                )
                flash("Comparaison LLM terminee.", "success")
                return redirect(
                    url_for(
                        "admin_llm_comparator_detail",
                        run_id=comparison["run_id"],
                    )
                )
            except ValueError as exc:
                flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
            except Exception as exc:
                flash_internal_error("admin_llm_comparator", exc, prefix="Erreur Comparator LLM.")

        try:
            payload = get_llm_comparator_payload()
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("admin_llm_comparator", exc)
        return render_template(
            "llm_comparator.html",
            payload=payload,
            db_error=db_error,
            form_values=form_values,
        )


    @app.get("/admin/llm-comparator/runs/<run_id>")
    def admin_llm_comparator_detail(run_id):
        """Show one Local LLM Comparator run."""
        try:
            payload = get_llm_comparator_run_detail(run_id)
            db_error = None
        except ValueError as exc:
            payload = None
            db_error = public_exception_message(exc, DEFAULT_ERROR_MESSAGES["not_found"])
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("admin_llm_comparator_detail", exc)
        return render_template("llm_comparator_detail.html", payload=payload, db_error=db_error)


    @app.get("/admin/llm-comparator/runs/<run_id>/export")
    def admin_llm_comparator_export(run_id):
        """Export one Local LLM Comparator run."""
        try:
            filename, mimetype, body = export_llm_comparator_run(
                run_id,
                request.args.get("format", "json"),
            )
            return Response(
                body,
                mimetype=mimetype,
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["not_found"]), "error")
        except Exception as exc:
            flash_internal_error("admin_llm_comparator_export", exc, prefix="Erreur export Comparator LLM.")
        return redirect(url_for("admin_llm_comparator_detail", run_id=run_id))


    @app.route("/admin/webchat", methods=["GET"])
    def admin_webchat():
        """Render the public webchat administration page."""
        try:
            payload = get_webchat_config()
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("admin_webchat", exc)
        try:
            status = webchat_public_status()
        except Exception as exc:
            status = {"available": False, "enabled": False, "llm_connected": False, "db_error": str(exc)}
        try:
            llm_configs = list_llm_configs(redact_key=True)
            llm_config_error = None
        except Exception as exc:
            llm_configs = []
            llm_config_error = ui_internal_error_message("admin_webchat_llm_configs", exc)
        return render_template(
            "webchat_admin.html",
            payload=payload,
            status=status,
            db_error=db_error,
            llm_configs=llm_configs,
            llm_config_error=llm_config_error,
        )


    @app.post("/admin/webchat/config")
    def admin_webchat_config():
        """Update public webchat configuration."""
        try:
            update_webchat_config(request.form.to_dict())
            flash("Configuration webchat enregistree.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_webchat_config", exc, prefix="Erreur configuration webchat.")
        return redirect(url_for("admin_webchat"))


    @app.post("/admin/webchat/pipeline")
    def admin_webchat_pipeline_add():
        """Add one webchat pipeline step."""
        try:
            add_pipeline_step(request.form.to_dict())
            flash("Etape pipeline ajoutee.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_webchat_pipeline_add", exc, prefix="Erreur ajout etape pipeline.")
        return redirect(url_for("admin_webchat"))


    @app.post("/admin/webchat/pipeline/<step_id>")
    def admin_webchat_pipeline_update(step_id):
        """Update one webchat pipeline step."""
        try:
            update_pipeline_step(step_id, request.form.to_dict())
            flash("Etape pipeline mise a jour.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_webchat_pipeline_update", exc, prefix="Erreur mise a jour etape pipeline.")
        return redirect(url_for("admin_webchat"))


    @app.post("/admin/webchat/pipeline/<step_id>/delete")
    def admin_webchat_pipeline_delete(step_id):
        """Delete one webchat pipeline step."""
        try:
            delete_pipeline_step(step_id)
            flash("Etape pipeline supprimee.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_webchat_pipeline_delete", exc, prefix="Erreur suppression etape pipeline.")
        return redirect(url_for("admin_webchat"))


    @app.get("/admin/webchat/sessions")
    def admin_webchat_sessions():
        """List public webchat sessions."""
        try:
            sessions = list_recent_webchat_sessions()
            db_error = None
        except Exception as exc:
            sessions = []
            db_error = ui_internal_error_message("admin_webchat_sessions", exc)
        return render_template("webchat_sessions.html", sessions=sessions, db_error=db_error)


    @app.get("/admin/webchat/sessions/<session_id>")
    def admin_webchat_session_detail(session_id):
        """Show one public webchat session."""
        try:
            payload = get_webchat_session_detail(session_id)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("admin_webchat_session_detail", exc)
        return render_template("webchat_session_detail.html", payload=payload, db_error=db_error)


    @app.get("/webchat")
    def public_webchat():
        """Render public webchat when enabled and connected."""
        try:
            status = webchat_public_status()
            if not status["available"]:
                return render_template("webchat_unavailable.html", status=status), 503
            return render_template("webchat_public.html", status=status)
        except Exception as exc:
            status = {"available": False, "enabled": False, "llm_connected": False, "db_error": str(exc)}
            return render_template("webchat_unavailable.html", status=status), 503


    @app.post("/webchat/messages")
    def public_webchat_message():
        """Process one public webchat message."""
        incoming_payload = request.get_json(silent=True) or {}
        try:
            payload = process_public_webchat_message(
                incoming_payload,
                user_agent=request.headers.get("User-Agent", ""),
                ip_address=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            )
            return {"ok": True, **payload}
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                status_code=400,
                code="validation_error",
            )
        except Exception as exc:
            return api_internal_error_response("public_webchat_message", exc)


    @app.get("/quizbot")
    def public_quizbot():
        """Render public QuizBot when enabled and configured."""
        try:
            status = quizbot_public_status()
            if not status["available"]:
                return render_template("quizbot_unavailable.html", status=status), 503
            return render_template("quizbot_public.html", status=status)
        except Exception as exc:
            status = {
                "available": False,
                "enabled": False,
                "configured": False,
                "active_topic_count": 0,
                "db_error": str(exc) or exc.__class__.__name__,
            }
            return render_template("quizbot_unavailable.html", status=status), 503


    @app.post("/quizbot/start")
    def public_quizbot_start():
        """Start one public QuizBot session."""
        try:
            payload = start_quiz_session(
                user_agent=request.headers.get("User-Agent", ""),
                ip_address=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            )
            return {"ok": True, **payload}
        except QuizbotUnavailableError as exc:
            return api_error_response(
                message=public_exception_message(exc, "QuizBot indisponible."),
                status_code=503,
                code="quizbot_unavailable",
            )
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                status_code=400,
                code="validation_error",
            )
        except Exception as exc:
            return api_internal_error_response("public_quizbot_start", exc)


    @app.post("/quizbot/answer")
    def public_quizbot_answer():
        """Correct one public QuizBot answer."""
        incoming_payload = request.get_json(silent=True) or {}
        try:
            payload = submit_quiz_answer(incoming_payload)
            return {"ok": True, **payload}
        except QuizbotUnavailableError as exc:
            return api_error_response(
                message=public_exception_message(exc, "QuizBot indisponible."),
                status_code=503,
                code="quizbot_unavailable",
            )
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                status_code=400,
                code="validation_error",
            )
        except Exception as exc:
            return api_internal_error_response("public_quizbot_answer", exc)


    @app.post("/quizbot/feedback")
    def public_quizbot_feedback():
        """Store public QuizBot feedback."""
        incoming_payload = request.get_json(silent=True) or {}
        try:
            payload = submit_quiz_feedback(incoming_payload)
            return {"ok": True, **payload}
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                status_code=400,
                code="validation_error",
            )
        except Exception as exc:
            return api_internal_error_response("public_quizbot_feedback", exc)


    @app.get("/admin/quizbot")
    def admin_quizbot_dashboard():
        """Render QuizBot administration dashboard."""
        try:
            stats = get_quizbot_dashboard()
            status = quizbot_public_status()
            db_error = None
        except Exception as exc:
            stats = None
            status = None
            db_error = ui_internal_error_message("admin_quizbot_dashboard", exc)
        return render_template(
            "quizbot_admin_dashboard.html",
            stats=stats,
            status=status,
            db_error=db_error,
        )


    @app.route("/admin/quizbot/config", methods=["GET", "POST"])
    def admin_quizbot_config():
        """Render and update QuizBot LLM configuration."""
        if request.method == "POST":
            try:
                save_quizbot_config(
                    request.form.to_dict(),
                    actor=session.get("admin_username", "admin"),
                )
                flash("Configuration QuizBot enregistree.", "success")
                return redirect(url_for("admin_quizbot_config"))
            except ValueError as exc:
                flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
            except Exception as exc:
                flash_internal_error("admin_quizbot_config", exc, prefix="Erreur configuration QuizBot.")

        try:
            config = get_quizbot_config(redact_key=True)
            config_error = None
        except Exception as exc:
            config = None
            config_error = ui_internal_error_message("admin_quizbot_config_load", exc)
        try:
            llm_configs = list_llm_configs(redact_key=True)
            llm_config_error = None
        except Exception as exc:
            llm_configs = []
            llm_config_error = ui_internal_error_message("admin_quizbot_llm_configs", exc)
        return render_template(
            "quizbot_config.html",
            config=config,
            config_error=config_error,
            llm_configs=llm_configs,
            llm_config_error=llm_config_error,
        )


    @app.get("/admin/quizbot/topics")
    def admin_quizbot_topics():
        """List and edit QuizBot topics."""
        try:
            topics = list_quizbot_topics()
            db_error = None
        except Exception as exc:
            topics = []
            db_error = ui_internal_error_message("admin_quizbot_topics", exc)
        return render_template("quizbot_topics.html", topics=topics, db_error=db_error)


    @app.post("/admin/quizbot/topics")
    def admin_quizbot_topic_add():
        """Create one QuizBot topic."""
        try:
            topic_id = create_quizbot_topic(
                request.form.to_dict(),
                actor=session.get("admin_username", "admin"),
            )
            flash(f"Sujet QuizBot cree: {topic_id}", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_quizbot_topic_add", exc, prefix="Erreur creation sujet QuizBot.")
        return redirect(url_for("admin_quizbot_topics"))


    @app.post("/admin/quizbot/topics/<topic_id>")
    def admin_quizbot_topic_update(topic_id):
        """Update one QuizBot topic."""
        try:
            update_quizbot_topic(
                topic_id,
                request.form.to_dict(),
                actor=session.get("admin_username", "admin"),
            )
            flash("Sujet QuizBot mis a jour.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_quizbot_topic_update", exc, prefix="Erreur mise a jour sujet QuizBot.")
        return redirect(url_for("admin_quizbot_topics"))


    @app.post("/admin/quizbot/topics/<topic_id>/archive")
    def admin_quizbot_topic_archive(topic_id):
        """Archive one QuizBot topic."""
        try:
            archive_quizbot_topic(topic_id, actor=session.get("admin_username", "admin"))
            flash("Sujet QuizBot archive.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_quizbot_topic_archive", exc, prefix="Erreur archivage sujet QuizBot.")
        return redirect(url_for("admin_quizbot_topics"))


    @app.post("/admin/quizbot/topics/<topic_id>/delete")
    def admin_quizbot_topic_delete(topic_id):
        """Delete one QuizBot topic."""
        try:
            delete_quizbot_topic(topic_id, actor=session.get("admin_username", "admin"))
            flash("Sujet QuizBot supprime.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_quizbot_topic_delete", exc, prefix="Erreur suppression sujet QuizBot.")
        return redirect(url_for("admin_quizbot_topics"))


    @app.get("/admin/quizbot/sessions")
    def admin_quizbot_sessions():
        """List public QuizBot sessions."""
        try:
            sessions_payload = list_quizbot_sessions()
            db_error = None
        except Exception as exc:
            sessions_payload = []
            db_error = ui_internal_error_message("admin_quizbot_sessions", exc)
        return render_template("quizbot_sessions.html", sessions=sessions_payload, db_error=db_error)


    @app.get("/admin/quizbot/sessions/<session_id>")
    def admin_quizbot_session_detail(session_id):
        """Show one public QuizBot session."""
        try:
            payload = get_quizbot_session_detail(session_id)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("admin_quizbot_session_detail", exc)
        return render_template("quizbot_session_detail.html", payload=payload, db_error=db_error)


    @app.get("/admin/quizbot/audit")
    def admin_quizbot_audit():
        """List QuizBot audit events."""
        try:
            events = list_quizbot_audit_events()
            db_error = None
        except Exception as exc:
            events = []
            db_error = ui_internal_error_message("admin_quizbot_audit", exc)
        return render_template("quizbot_audit.html", events=events, db_error=db_error)


    @app.get("/admin/chat-evaluations")
    def admin_chat_evaluations():
        """List project chat evaluation entrypoints."""
        try:
            projects = list_projects_shards()
            db_error = None
        except Exception as exc:
            projects = []
            db_error = ui_internal_error_message("admin_chat_evaluations", exc)
        return render_template("chat_evaluations.html", projects=projects, db_error=db_error)


    @app.route("/admin/tools/html-to-md", methods=["GET", "POST"])
    def html_to_md_tool():
        """Convert HTML snippets to Markdown."""
        html_source = ""
        markdown_result = ""
        if request.method == "POST":
            html_source = request.form.get("html_source", "")
            markdown_result = html_to_markdown(html_source)
        return render_template(
            "html_to_md.html",
            html_source=html_source,
            markdown_result=markdown_result,
        )


    @app.get("/admin/chunking-config")
    @app.get("/admin/shard-to-chunk")
    def admin_chunking_config():
        """Render the Shard-To-Chunk module."""
        selected_project = (request.args.get("project") or "").strip()
        selected_shard = (request.args.get("shard") or "").strip()
        try:
            payload = get_shard_to_chunk_payload(selected_project, selected_shard)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("admin_chunking_config", exc)
        return render_template("chunking_config.html", payload=payload, db_error=db_error)


    @app.post("/admin/shard-to-chunk/generate")
    @require_scopes("write")
    def admin_shard_to_chunk_generate():
        """Generate chunks for one shard with an LLM profile."""
        form_payload = request.form.to_dict()
        project_slug = (form_payload.get("project_slug") or "").strip()
        shard_id = (form_payload.get("shard_id") or "").strip()
        try:
            result = generate_chunks_with_llm(
                project_slug,
                shard_id,
                form_payload,
                actor=session.get("admin_username", "admin"),
            )
            flash(
                f"Shard-To-Chunk LLM termine: {result['generated_chunks']} chunk(s) genere(s).",
                "success",
            )
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_shard_to_chunk_generate", exc, prefix="Erreur Shard-To-Chunk LLM.")
        return redirect(url_for("admin_chunking_config", project=project_slug, shard=shard_id))


    @app.route("/admin/vectorization", methods=["GET", "POST"])
    def admin_vectorization():
        """Render and update pgvector/embedding configuration."""
        if request.method == "POST":
            try:
                save_vectorization_config(request.form.to_dict())
                flash("Configuration vectorisation enregistree.", "success")
                return redirect(url_for("admin_vectorization"))
            except ValueError as exc:
                flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
            except Exception as exc:
                flash_internal_error("admin_vectorization_save", exc, prefix="Erreur configuration vectorisation.")

        try:
            payload = get_vectorization_admin_payload()
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("admin_vectorization", exc)
        return render_template("vectorization_config.html", payload=payload, db_error=db_error)


    @app.post("/admin/vectorization/test")
    def admin_vectorization_test():
        """Test the configured embedding endpoint."""
        try:
            result = test_vectorization_config(request.form.get("test_text", ""))
            flash(
                f"Test vectorisation OK: {result['embedding_dimensions']} dimensions via {result['embedding_model']}.",
                "success",
            )
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("admin_vectorization_test", exc, prefix="Erreur test vectorisation.")
        return redirect(url_for("admin_vectorization"))


    @app.post("/projects/<project_slug>/vectorize")
    @require_scopes("write")
    def project_vectorize(project_slug):
        """Run a vectorization batch on one project."""
        try:
            form_payload = request.form.to_dict()
            form_payload["targets"] = request.form.getlist("targets")
            result = vectorize_project_data(project_slug, form_payload)
            flash(
                f"Vectorisation {project_slug}: {result['embedded']} embedding(s) sur {result['processed']} ligne(s).",
                "success" if not result["errors"] else "error",
            )
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("project_vectorize", exc, prefix="Erreur vectorisation projet.")
        return redirect(url_for("admin_vectorization"))


    @app.get("/admin/document-review")
    def admin_document_review():
        """Render the document review queue."""
        selected_project = (request.args.get("project") or "").strip()
        try:
            projects = list_projects()
            documents = list_document_review_items(selected_project)
            db_error = None
        except Exception as exc:
            projects = []
            documents = []
            db_error = ui_internal_error_message("admin_document_review", exc)
        return render_template(
            "document_review_list.html",
            projects=projects,
            documents=documents,
            selected_project=selected_project,
            db_error=db_error,
        )


    @app.get("/projects/<project_slug>/documents/<document_id>/review")
    def project_document_review(project_slug, document_id):
        """Render one document review workspace."""
        try:
            payload = get_document_review_payload(project_slug, document_id)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("project_document_review", exc)
        return render_template("document_review.html", payload=payload, db_error=db_error)


    @app.post("/projects/<project_slug>/documents/<document_id>/review/normalize")
    @require_scopes("normalize")
    def project_document_review_normalize(project_slug, document_id):
        """Normalize a document from the review UI."""
        try:
            normalize_document_by_id(document_id, project_slug=project_slug)
            flash("Texte normalise mis a jour.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("project_document_review_normalize", exc, prefix="Erreur normalisation.")
        return redirect(url_for("project_document_review", project_slug=project_slug, document_id=document_id))


    @app.post("/projects/<project_slug>/documents/<document_id>/review/approval")
    @require_scopes("approve")
    def project_document_review_approve(project_slug, document_id):
        """Approve, reject, or reset a document from the review UI."""
        reviewer = session.get("admin_username", "")
        payload = request.form.to_dict()
        payload["project_slug"] = project_slug
        if not payload.get("approved_by"):
            payload["approved_by"] = reviewer
        try:
            result = approve_document_by_id(document_id, payload)
            flash(f"Statut document mis a jour: {result['status']}.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("project_document_review_approve", exc, prefix="Erreur revue document.")
        return redirect(url_for("project_document_review", project_slug=project_slug, document_id=document_id))


    @app.post("/projects/<project_slug>/documents/<document_id>/review/annotations")
    @require_scopes("approve")
    def project_document_review_annotation_add(project_slug, document_id):
        """Add an anomaly annotation from the review UI."""
        reviewer = session.get("admin_username", "")
        try:
            annotation_id = add_document_review_annotation(
                project_slug,
                document_id,
                request.form.to_dict(),
                reviewer=reviewer,
            )
            flash(f"Anomalie annotee: {annotation_id}.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("project_document_review_annotation_add", exc, prefix="Erreur annotation.")
        return redirect(url_for("project_document_review", project_slug=project_slug, document_id=document_id))


    @app.post("/projects/<project_slug>/documents/<document_id>/review/annotations/<annotation_id>/delete")
    @require_scopes("approve")
    def project_document_review_annotation_delete(project_slug, document_id, annotation_id):
        """Delete one anomaly annotation from the review UI."""
        try:
            delete_document_review_annotation(project_slug, document_id, annotation_id)
            flash("Annotation supprimee.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("project_document_review_annotation_delete", exc, prefix="Erreur suppression annotation.")
        return redirect(url_for("project_document_review", project_slug=project_slug, document_id=document_id))


    @app.post("/projects/<project_slug>/documents/<document_id>/review/exclusions")
    @require_scopes("approve")
    def project_document_review_exclusion_add(project_slug, document_id):
        """Exclude one section from the review UI."""
        reviewer = session.get("admin_username", "")
        try:
            exclusion_id = add_document_section_exclusion(
                project_slug,
                document_id,
                request.form.to_dict(),
                reviewer=reviewer,
            )
            flash(f"Section exclue: {exclusion_id}.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("project_document_review_exclusion_add", exc, prefix="Erreur exclusion section.")
        return redirect(url_for("project_document_review", project_slug=project_slug, document_id=document_id))


    @app.post("/projects/<project_slug>/documents/<document_id>/review/exclusions/<exclusion_id>/delete")
    @require_scopes("approve")
    def project_document_review_exclusion_delete(project_slug, document_id, exclusion_id):
        """Delete one section exclusion from the review UI."""
        try:
            delete_document_section_exclusion(project_slug, document_id, exclusion_id)
            flash("Exclusion retiree.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("project_document_review_exclusion_delete", exc, prefix="Erreur suppression exclusion.")
        return redirect(url_for("project_document_review", project_slug=project_slug, document_id=document_id))


    @app.post("/projects")
    def add_project():
        """Add project."""
        project_name = request.form.get("project_name", "")
        try:
            project_uuid, slug = create_project(project_name)
            flash(
                f"Projet cree avec succes. UUID: {project_uuid} | slug: {slug}",
                "success",
            )
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("add_project", exc, prefix="Erreur creation projet.")
        return redirect(url_for("admin_dashboard"))


    @app.post("/projects/<project_slug>/delete")
    @require_scopes("delete")
    def remove_project(project_slug):
        """Remove project."""
        try:
            delete_project(project_slug)
            flash(f"Projet '{project_slug}' supprime.", "success")
        except Exception as exc:
            flash_internal_error("remove_project", exc, prefix="Erreur suppression projet.")
        return redirect(url_for("admin_dashboard"))


    @app.get("/projects/<project_slug>/document-vision")
    def project_document_vision(project_slug):
        """Render Document Vision upload/analyze page for one project."""
        try:
            payload = get_document_vision_project_payload(project_slug)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("project_document_vision", exc)
        return render_template(
            "document_vision.html",
            payload=payload,
            db_error=db_error,
            result=None,
        )


    @app.post("/projects/<project_slug>/document-vision/analyze")
    @require_scopes("write")
    def project_document_vision_analyze(project_slug):
        """Analyze an uploaded image/PDF with Document Vision."""
        try:
            result = analyze_project_document(
                project_slug,
                request.files.get("document_file"),
                request.form.to_dict(),
                actor=session.get("admin_username", "admin"),
            )
            payload = get_document_vision_project_payload(project_slug)
            flash("Document analyse par Document Vision.", "success")
            return render_template(
                "document_vision.html",
                payload=payload,
                db_error=None,
                result=result,
            )
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("project_document_vision_analyze", exc, prefix="Erreur Document Vision.")

        try:
            payload = get_document_vision_project_payload(project_slug)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("project_document_vision_reload", exc)
        return render_template(
            "document_vision.html",
            payload=payload,
            db_error=db_error,
            result=None,
        )


    @app.get("/projects/<project_slug>/shards/new")
    def project_shard_new(project_slug):
        """Handle the project shard new request."""
        try:
            payload = get_project_crud_payload(project_slug)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("project_shard_new", exc)
        return render_template("shard_form.html", payload=payload, db_error=db_error)


    @app.get("/projects/<project_slug>/shards/<shard_uuid>")
    def project_shard_view(project_slug, shard_uuid):
        """Handle the project shard view request."""
        try:
            payload = get_project_crud_payload(project_slug)
            shard = next((item for item in payload["shards"] if item["uuid"] == shard_uuid), None)
            if shard is None:
                raise ValueError(f"Shard introuvable pour l'UUID: {shard_uuid}")
            db_error = None
        except Exception as exc:
            payload = None
            shard = None
            db_error = ui_internal_error_message("project_shard_view", exc)
        return render_template("shard_view.html", payload=payload, shard=shard, db_error=db_error)


    @app.get("/projects/<project_slug>/shards/<shard_uuid>/quality")
    def project_shard_quality(project_slug, shard_uuid):
        """Render Shard Quality analysis for one shard."""
        try:
            payload = get_shard_quality_payload(project_slug, shard_uuid)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("project_shard_quality", exc)
        return render_template(
            "shard_quality.html",
            payload=payload,
            db_error=db_error,
            result=None,
        )


    @app.post("/projects/<project_slug>/shards/<shard_uuid>/quality/analyze")
    @require_scopes("write")
    def project_shard_quality_analyze(project_slug, shard_uuid):
        """Analyze one shard with Shard Quality."""
        try:
            result = analyze_shard_quality(
                project_slug,
                shard_uuid,
                request.form.to_dict(),
                actor=session.get("admin_username", "admin"),
            )
            payload = get_shard_quality_payload(project_slug, shard_uuid)
            flash("Analyse Shard Quality terminee.", "success")
            return render_template(
                "shard_quality.html",
                payload=payload,
                db_error=None,
                result=result,
            )
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("project_shard_quality_analyze", exc, prefix="Erreur Shard Quality.")

        try:
            payload = get_shard_quality_payload(project_slug, shard_uuid)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("project_shard_quality_reload", exc)
        return render_template(
            "shard_quality.html",
            payload=payload,
            db_error=db_error,
            result=None,
        )


    @app.get("/projects/<project_slug>/shard-list")
    def project_shard_list(project_slug):
        """Handle the project shard list request."""
        try:
            payload = get_project_crud_payload(project_slug)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("project_shard_list", exc)
        return render_template("shard_list.html", payload=payload, db_error=db_error)


    @app.post("/projects/<project_slug>/shards/add")
    @require_scopes("write")
    def add_shard_item(project_slug):
        """Add shard item."""
        try:
            shard_uuid = add_shard_record(project_slug, request.form.to_dict())
            flash(f"Shard ajoute pour '{project_slug}'. UUID: {shard_uuid}", "success")
        except Exception as exc:
            flash_internal_error("add_shard_item", exc, prefix="Erreur ajout shard.")
        return_to = request.form.get("return_to", "project_shard_list")
        return redirect(resolve_return_url(return_to, "project_shard_list", project_slug))


    @app.post("/projects/<project_slug>/shards/<shard_uuid>/delete")
    @require_scopes("delete")
    def remove_shard_item(project_slug, shard_uuid):
        """Remove shard item."""
        try:
            delete_shard_record(project_slug, shard_uuid)
            flash(f"Shard supprime: {shard_uuid}", "success")
        except Exception as exc:
            flash_internal_error("remove_shard_item", exc, prefix="Erreur suppression shard.")
        return redirect(url_for("project_shard_list", project_slug=project_slug))


    @app.get("/projects/<project_slug>/train-list")
    def project_train_list(project_slug):
        """Handle the project train list request."""
        try:
            payload = get_project_crud_payload(project_slug)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("project_train_list", exc)
        return render_template("train_list.html", payload=payload, db_error=db_error)


    @app.get("/projects/<project_slug>/trains/new")
    def project_train_new(project_slug):
        """Handle the project train new request."""
        try:
            payload = get_project_crud_payload(project_slug)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("project_train_new", exc)
        return render_template("train_form.html", payload=payload, db_error=db_error)


    @app.post("/projects/<project_slug>/trains/<train_uuid>/delete")
    @require_scopes("delete")
    def remove_train_item(project_slug, train_uuid):
        """Remove train item."""
        try:
            delete_train_record(project_slug, train_uuid)
            flash(f"Item train supprime: {train_uuid}", "success")
        except Exception as exc:
            flash_internal_error("remove_train_item", exc, prefix="Erreur suppression train.")
        return redirect(url_for("project_train_list", project_slug=project_slug))


    @app.post("/projects/<project_slug>/trains/<train_uuid>/vote")
    @require_scopes("write")
    def vote_train_item(project_slug, train_uuid):
        """Vote train item."""
        direction = (request.form.get("direction") or "").strip().lower()
        try:
            vote_train_record(project_slug, train_uuid, direction)
            flash(f"Vote enregistre pour l'item train {train_uuid}.", "success")
        except Exception as exc:
            flash_internal_error("vote_train_item", exc, prefix="Erreur vote train.")
        return redirect(url_for("project_train_list", project_slug=project_slug))


    @app.get("/projects/<project_slug>/chunk-list")
    def project_chunk_list(project_slug):
        """Handle the project chunk list request."""
        try:
            payload = get_project_crud_payload(project_slug)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("project_chunk_list", exc)
        return render_template("chunk_list.html", payload=payload, db_error=db_error)


    @app.get("/projects/<project_slug>/chunks/new")
    def project_chunk_new(project_slug):
        """Handle the project chunk new request."""
        selected_shard_id = (request.args.get("shard_id") or "").strip()
        try:
            payload = get_project_crud_payload(project_slug)
            shard_ids = {item["uuid"] for item in payload["shards"]}
            if selected_shard_id and selected_shard_id not in shard_ids:
                selected_shard_id = ""
            db_error = None
        except Exception as exc:
            payload = None
            selected_shard_id = ""
            db_error = ui_internal_error_message("project_chunk_new", exc)
        return render_template(
            "chunk_form.html",
            payload=payload,
            selected_shard_id=selected_shard_id,
            db_error=db_error,
        )


    @app.get("/projects/<project_slug>/chunks/<chunk_uuid>")
    def project_chunk_view(project_slug, chunk_uuid):
        """Handle the project chunk view request."""
        try:
            payload = get_project_crud_payload(project_slug)
            chunk = next((item for item in payload["chunks"] if item["uuid"] == chunk_uuid), None)
            if chunk is None:
                raise ValueError(f"Chunk introuvable pour l'UUID: {chunk_uuid}")
            db_error = None
        except Exception as exc:
            payload = None
            chunk = None
            db_error = ui_internal_error_message("project_chunk_view", exc)
        return render_template("chunk_view.html", payload=payload, chunk=chunk, db_error=db_error)


    @app.post("/projects/<project_slug>/chunks/add")
    @require_scopes("write")
    def add_chunk_item(project_slug):
        """Add chunk item."""
        try:
            chunk_uuid = add_chunk_record(project_slug, request.form.to_dict())
            flash(f"Chunk ajoute pour '{project_slug}'. UUID: {chunk_uuid}", "success")
        except Exception as exc:
            flash_internal_error("add_chunk_item", exc, prefix="Erreur ajout chunk.")
        return_to = request.form.get("return_to", "project_chunk_list")
        return redirect(resolve_return_url(return_to, "project_chunk_list", project_slug))


    @app.post("/projects/<project_slug>/chunks/<chunk_uuid>/delete")
    @require_scopes("delete")
    def remove_chunk_item(project_slug, chunk_uuid):
        """Remove chunk item."""
        try:
            delete_chunk_record(project_slug, chunk_uuid)
            flash(f"Chunk supprime: {chunk_uuid}", "success")
        except Exception as exc:
            flash_internal_error("remove_chunk_item", exc, prefix="Erreur suppression chunk.")
        return redirect(url_for("project_chunk_list", project_slug=project_slug))


    @app.get("/projects/<project_slug>/chat-list")
    def project_chat_list(project_slug):
        """Handle the project chat list request."""
        try:
            payload = get_project_chat_payload(project_slug)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("project_chat_list", exc)
        return render_template("chat_list.html", payload=payload, db_error=db_error)


    @app.get("/projects/<project_slug>/chat-sessions/<session_id>")
    def project_chat_session(project_slug, session_id):
        """Handle the project chat session request."""
        selected_session_id = (session_id or "").strip()
        try:
            payload = get_project_chat_payload(project_slug, selected_session_id)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("project_chat_session", exc)
        return render_template("chat_session.html", payload=payload, db_error=db_error)


    @app.get("/projects/<project_slug>/chat-dashboard")
    def project_chat_dashboard(project_slug):
        """Handle the project chat dashboard request."""
        try:
            payload = get_project_chat_dashboard_payload(project_slug)
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("project_chat_dashboard", exc)
        return render_template("chat_dashboard.html", payload=payload, db_error=db_error)


    @app.post("/projects/<project_slug>/chat-evaluations")
    def submit_chat_evaluation(project_slug):
        """Submit chat evaluation."""
        form_payload = request.form.to_dict()
        session_id = (form_payload.get("session_id") or "").strip()
        try:
            session_id = upsert_chat_evaluation(project_slug, form_payload)
            flash(f"Evaluation enregistree pour la session '{session_id}'.", "success")
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("submit_chat_evaluation", exc, prefix="Erreur evaluation conversation.")

        if session_id:
            return redirect(url_for("project_chat_session", project_slug=project_slug, session_id=session_id))
        return redirect(url_for("project_chat_list", project_slug=project_slug))


    @app.get("/projects/shards")
    def projects_shards():
        """Run projects shards."""
        try:
            projects = list_projects_shards()
            db_error = None
        except Exception as exc:
            projects = []
            db_error = ui_internal_error_message("projects_shards", exc)
        return render_template("project_shards.html", projects=projects, db_error=db_error)


    @app.get("/projects/chunks")
    def projects_chunks():
        """Render the project chunk factory overview."""
        try:
            projects = list_projects_shards()
            db_error = None
        except Exception as exc:
            projects = []
            db_error = ui_internal_error_message("projects_chunks", exc)
        return render_template("project_chunks.html", projects=projects, db_error=db_error)


    @app.get("/projects/document-vision")
    def projects_document_vision():
        """Render the project Document Vision entrypoints."""
        try:
            projects = list_projects_shards()
            db_error = None
        except Exception as exc:
            projects = []
            db_error = ui_internal_error_message("projects_document_vision", exc)
        return render_template("project_document_vision_index.html", projects=projects, db_error=db_error)


    @app.get("/projects/shard-quality")
    def projects_shard_quality():
        """Render the project Shard Quality entrypoints."""
        try:
            payload = get_shard_quality_index_payload()
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("projects_shard_quality", exc)
        return render_template("shard_quality_index.html", payload=payload, db_error=db_error)


    @app.get("/projects/train")
    def projects_train():
        """Run projects train."""
        try:
            projects = list_projects_shards()
            db_error = None
        except Exception as exc:
            projects = []
            db_error = ui_internal_error_message("projects_train", exc)
        return render_template("project_train.html", projects=projects, db_error=db_error)


    @app.post("/projects/<project_slug>/chunkify")
    @require_scopes("write")
    def chunkify_project(project_slug):
        """Chunkify project."""
        incoming_payload = request.get_json(silent=True)
        if incoming_payload is None:
            incoming_payload = request.form.to_dict()
        options = parse_chunk_options(incoming_payload)

        try:
            generated_items = chunkify_project_shards(project_slug, options)
            if request.is_json:
                return {
                    "project_slug": project_slug,
                    "generated_chunks": len(generated_items),
                    "items": generated_items,
                    "options": options,
                }
            flash(
                f"Chunking termine pour '{project_slug}': {len(generated_items)} chunk(s) genere(s).",
                "success",
            )
        except ValueError as exc:
            if request.is_json:
                return api_error_response(
                    message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                    status_code=400,
                    code="validation_error",
                )
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            if request.is_json:
                return api_internal_error_response("chunkify_project", exc)
            flash_internal_error("chunkify_project", exc, prefix="Erreur chunking.")
        return_to = request.form.get("return_to", "project_shard_list")
        return redirect(resolve_return_url(return_to, "project_shard_list", project_slug))


    @app.post("/projects/<project_slug>/train")
    @require_scopes("write")
    def add_train_item(project_slug):
        """Add train item."""
        payload = request.form.to_dict()
        try:
            train_uuid = add_train_record(project_slug, payload)
            flash(
                f"Exemple train ajoute pour '{project_slug}'. UUID: {train_uuid}",
                "success",
            )
        except ValueError as exc:
            flash(public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]), "error")
        except Exception as exc:
            flash_internal_error("add_train_item", exc, prefix="Erreur train.")
        return_to = request.form.get("return_to", "project_train_list")
        return redirect(resolve_return_url(return_to, "project_train_list", project_slug))


    @app.get("/agentai-docs")
    @app.get("/admin/agentai-docs")
    def agentai_docs():
        """Render the AgentAI Markdown docs generator."""
        try:
            payload = get_agentai_docs_payload()
            db_error = None
        except Exception as exc:
            payload = None
            db_error = ui_internal_error_message("agentai_docs", exc)
        return render_template("agentai_docs.html", payload=payload, db_error=db_error)


    @app.post("/agentai-docs/generate")
    @app.post("/admin/agentai-docs/generate")
    @require_scopes("write")
    def generate_agentai_docs():
        """Generate AgentAI Markdown docs with a configured LLM."""
        incoming_payload = request.get_json(silent=True)
        if incoming_payload is None:
            incoming_payload = request.form.to_dict()
        try:
            generated_payload = generate_agentai_documents(incoming_payload)
            return {"ok": True, **generated_payload}
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                status_code=400,
                code="validation_error",
            )
        except Exception as exc:
            return api_internal_error_response("generate_agentai_docs", exc)


    @app.post("/agentai-docs/save")
    @app.post("/admin/agentai-docs/save")
    @require_scopes("write")
    def save_agentai_docs():
        """Persist generated AgentAI Markdown docs into docs/agentai."""
        incoming_payload = request.get_json(silent=True) or {}
        try:
            saved_files = save_agentai_documents(incoming_payload.get("documents", {}))
            return {"ok": True, "saved_files": saved_files}
        except ValueError as exc:
            return api_error_response(
                message=public_exception_message(exc, DEFAULT_ERROR_MESSAGES["validation_error"]),
                status_code=400,
                code="validation_error",
            )
        except Exception as exc:
            return api_internal_error_response("save_agentai_docs", exc)


    @app.get("/health")
    def health():
        """Return a lightweight health check response."""
        return {"status": "ok"}

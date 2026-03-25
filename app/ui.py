"""UI route registration for HTML views and form actions."""

from services import *


def register_ui_routes(app):
    """Register UI routes on the Flask app."""
    @app.get("/")
    def home():
        """Render the home page."""
        try:
            projects = list_projects()
            db_error = None
        except Exception as exc:
            projects = []
            db_error = ui_internal_error_message("home", exc)
        return render_template("index.html", projects=projects, db_error=db_error)


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
        return redirect(url_for("home"))


    @app.post("/projects/<project_slug>/delete")
    @require_scopes("delete")
    def remove_project(project_slug):
        """Remove project."""
        try:
            delete_project(project_slug)
            flash(f"Projet '{project_slug}' supprime.", "success")
        except Exception as exc:
            flash_internal_error("remove_project", exc, prefix="Erreur suppression projet.")
        return redirect(url_for("home"))


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


    @app.get("/projects/tree")
    def projects_tree():
        """Run projects tree."""
        try:
            projects = list_projects_shards()
            db_error = None
        except Exception as exc:
            projects = []
            db_error = ui_internal_error_message("projects_tree", exc)
        return render_template("project_tree.html", projects=projects, db_error=db_error)


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


    @app.get("/health")
    def health():
        """Return a lightweight health check response."""
        return {"status": "ok"}

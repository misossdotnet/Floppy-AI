"""Application bootstrap module for the Flask app."""

from flask import Flask

from api_mcp import register_api_mcp_routes
from api_rest import register_api_rest_routes
from services import is_debug_enabled, resolve_flask_secret_key
from ui import register_ui_routes

app = Flask(__name__)
app.secret_key = resolve_flask_secret_key()

register_api_rest_routes(app)
register_api_mcp_routes(app)
register_ui_routes(app)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=is_debug_enabled())

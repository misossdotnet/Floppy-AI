"""Application bootstrap module for the Flask app."""

import os

from flask import Flask

from api_mcp import register_api_mcp_routes
from api_rest import register_api_rest_routes
from services import is_debug_enabled, parse_env_bool, resolve_flask_secret_key
from security import register_security
from ui import register_ui_routes


def is_interface_enabled(env_name: str, default: bool = True) -> bool:
    """Return whether a given interface should be registered."""
    return parse_env_bool(os.getenv(env_name), default=default)


app = Flask(__name__)
app.secret_key = resolve_flask_secret_key()

app.config["APP_ENABLE_UI"] = is_interface_enabled("APP_ENABLE_UI", default=True)
app.config["APP_ENABLE_API"] = is_interface_enabled("APP_ENABLE_API", default=True)
app.config["APP_ENABLE_MCP"] = is_interface_enabled("APP_ENABLE_MCP", default=True)
register_security(app)

if app.config["APP_ENABLE_API"]:
    register_api_rest_routes(app)

if app.config["APP_ENABLE_MCP"]:
    register_api_mcp_routes(app)

if app.config["APP_ENABLE_UI"]:
    register_ui_routes(app)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=is_debug_enabled())

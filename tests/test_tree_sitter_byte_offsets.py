"""Regression tests for byte offsets vs string offsets.

tree-sitter is fed UTF-8 bytes and reports node offsets in bytes. Slicing those
offsets into a decoded str is only correct while every character is ASCII. One
non-ASCII character (an em dash in a comment, a smart quote, an accented name,
an emoji) shifts every later offset and the extractors start reading garbage,
so routes are silently dropped.

These tests put a single non-ASCII character in a comment on line 1 and then
assert every route below it is still found.
"""

from pathlib import Path

from golang_pipeline.identify_api_functions import find_api_endpoints as go_find_api_endpoints
from rails_pipeline.identify_api_functions import find_api_endpoints as rails_find_api_endpoints

# U+2014 EM DASH: one character, three bytes in UTF-8.
EM_DASH = "—"

GO_SOURCE_TEMPLATE = """package main

// Routes are grouped by resource {sep} see docs/routing.md for the conventions.

import "github.com/gin-gonic/gin"

func listUsers(c *gin.Context) {{}}

func createUser(c *gin.Context) {{}}

func deleteUser(c *gin.Context) {{}}

func RegisterRoutes(r *gin.Engine) {{
	r.GET("/users", listUsers)
	r.POST("/users", createUser)
	r.DELETE("/users/:id", deleteUser)
}}
"""

EXPECTED_GO_ROUTES = {
    ("GET", "/users"),
    ("POST", "/users"),
    ("DELETE", "/users/:id"),
}

RAILS_ROUTES_TEMPLATE = """# API routes {sep} keep them alphabetical.
Rails.application.routes.draw do
  get "/health", to: "health#show"
  post "/users", to: "users#create"
end
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _go_routes(tmp_path: Path, separator: str) -> set:
    source = GO_SOURCE_TEMPLATE.format(sep=separator)
    go_file = _write(tmp_path / "routes.go", source)
    endpoints = go_find_api_endpoints(go_file, str(tmp_path))
    return {(endpoint["http_method"], endpoint["route"]) for endpoint in endpoints}


def test_go_routes_found_with_ascii_comment(tmp_path):
    """Baseline: an all-ASCII file has always worked."""
    assert _go_routes(tmp_path, "-") == EXPECTED_GO_ROUTES


def test_go_routes_found_after_non_ascii_comment(tmp_path):
    """One em dash above the routes must not hide them.

    Before the byte-offset fix this returned an empty set: the three-byte em
    dash pushed every later str index two positions out of alignment, so the
    method and path literals were read as garbage and every route was dropped.
    """
    assert _go_routes(tmp_path, EM_DASH) == EXPECTED_GO_ROUTES


def test_go_handler_names_survive_non_ascii(tmp_path):
    """Handler names come from the same offsets, so check them too."""
    source = GO_SOURCE_TEMPLATE.format(sep=EM_DASH)
    go_file = _write(tmp_path / "routes.go", source)
    endpoints = go_find_api_endpoints(go_file, str(tmp_path))
    handlers = {endpoint["handler_name"] for endpoint in endpoints}
    assert handlers == {"listUsers", "createUser", "deleteUser"}


def _rails_route_map(tmp_path: Path, separator: str) -> dict:
    routes_file = _write(
        tmp_path / "config" / "routes.rb",
        RAILS_ROUTES_TEMPLATE.format(sep=separator),
    )
    route_map: dict = {}
    rails_find_api_endpoints(routes_file, str(tmp_path), route_map)
    return {
        controller: {(route["verb"], route["path"], route["action"]) for route in routes}
        for controller, routes in route_map.items()
    }


EXPECTED_RAILS_ROUTES = {
    "health": {("GET", "/health", "show")},
    "users": {("POST", "/users", "create")},
}


def test_rails_routes_found_with_ascii_comment(tmp_path):
    assert _rails_route_map(tmp_path, "-") == EXPECTED_RAILS_ROUTES


def test_rails_routes_found_after_non_ascii_comment(tmp_path):
    assert _rails_route_map(tmp_path, EM_DASH) == EXPECTED_RAILS_ROUTES

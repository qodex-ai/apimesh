"""Reconciler: one merged document from both lanes, nothing silent.

Contract content wins on colliding route shapes, code routes the contracts
do not cover survive for LLM documentation, cross-spec collisions surface as
conflicts, and every reference lands on a namespaced component.
"""

import os
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("APIMESH_CONFIG_PATH", str(REPO_ROOT / "config.yml"))
os.environ.setdefault("APIMESH_USER_REPO_PATH", str(REPO_ROOT))
os.environ.setdefault(
    "APIMESH_USER_CONFIG_PATH",
    str(Path(tempfile.mkdtemp(prefix="apimesh-user-config-")) / "config.json"),
)

from contract_lane.discovery import discover_contract_documents
from contract_lane.loader import load_operations
from contract_lane.reconcile import (
    contract_candidates,
    join_route,
    reconcile,
    route_shape,
)


def _rows(tmp_path, filename, prefix=""):
    inventory = discover_contract_documents(str(tmp_path))
    entry = next(e for e in inventory["contracts"] if e["path"] == filename)
    operations, unresolved = load_operations(entry, str(tmp_path))
    assert unresolved == []
    verdict = {
        "path": filename,
        "default_prefix": prefix,
        "prefix_by_operation": {},
    }
    return contract_candidates(verdict, operations)


def test_join_route_and_shape():
    assert join_route("/api", "/pets") == "/api/pets"
    assert join_route("", "/pets") == "/pets"
    assert join_route("/api/", "pets") == "/api/pets"
    assert route_shape("get", "/users/{id}") == ("GET", "/users/{}")
    assert route_shape("GET", "/users/{userId}") == ("GET", "/users/{}")


def test_contract_wins_over_code_and_the_rest_survive(tmp_path):
    (tmp_path / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /users/{userId}:\n"
        "    get:\n"
        "      operationId: getUser\n"
        "      responses: {'200': {description: ok}}\n"
    )
    rows = _rows(tmp_path, "api.yaml", prefix="/api")
    code_ops = [
        {"method": "GET", "route": "/api/users/{id}", "source_id": "code:UserController#getUser"},
        {"method": "GET", "route": "/internal/health", "source_id": "code:HealthController#health"},
    ]

    result = reconcile(rows, code_ops, str(tmp_path))

    # The contract's authored parameter name owns the route key.
    assert list(result["paths"]) == ["/api/users/{userId}"]
    operation = result["paths"]["/api/users/{userId}"]["get"]
    assert operation["x-apimesh-source"] == ["spec:api.yaml#get /users/{userId}"]
    assert [op["source_id"] for op in result["superseded_code"]] == [
        "code:UserController#getUser"
    ]
    assert [op["source_id"] for op in result["code_to_generate"]] == [
        "code:HealthController#health"
    ]
    assert result["conflicts"] == []


def test_cross_spec_collision_is_a_reported_conflict(tmp_path):
    (tmp_path / "one.yaml").write_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /health:\n"
        "    get:\n"
        "      operationId: oneHealth\n"
        "      responses: {'200': {description: from one}}\n"
    )
    (tmp_path / "two.yaml").write_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /health:\n"
        "    get:\n"
        "      operationId: twoHealth\n"
        "      responses: {'200': {description: from two}}\n"
    )
    rows = _rows(tmp_path, "one.yaml") + _rows(tmp_path, "two.yaml")

    result = reconcile(rows, [], str(tmp_path))

    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["won"] == "spec:one.yaml#get /health"
    assert conflict["lost"] == ["spec:two.yaml#get /health"]
    operation = result["paths"]["/health"]["get"]
    assert operation["x-apimesh-conflict"] == ["spec:two.yaml#get /health"]
    assert operation["operationId"] == "oneHealth"


def test_components_are_namespaced_per_source_spec(tmp_path):
    for name in ("one", "two"):
        (tmp_path / f"{name}.yaml").write_text(
            "openapi: 3.0.0\n"
            "components:\n"
            "  schemas:\n"
            f"    Error: {{type: object, description: {name} error}}\n"
            "paths:\n"
            f"  /{name}:\n"
            "    get:\n"
            "      responses:\n"
            "        '500':\n"
            "          description: err\n"
            "          content:\n"
            "            application/json:\n"
            "              schema: {$ref: '#/components/schemas/Error'}\n"
        )
    rows = _rows(tmp_path, "one.yaml") + _rows(tmp_path, "two.yaml")

    result = reconcile(rows, [], str(tmp_path))

    schemas = result["components"]["schemas"]
    assert set(schemas) == {"one_Error", "two_Error"}
    assert schemas["one_Error"]["description"] == "one error"
    ref_one = result["paths"]["/one"]["get"]["responses"]["500"]["content"][
        "application/json"
    ]["schema"]
    assert ref_one == {"$ref": "#/components/schemas/one_Error"}


def test_recursive_schema_component_references_itself_after_rewrite(tmp_path):
    (tmp_path / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "components:\n"
        "  schemas:\n"
        "    Node:\n"
        "      type: object\n"
        "      properties:\n"
        "        children:\n"
        "          type: array\n"
        "          items: {$ref: '#/components/schemas/Node'}\n"
        "paths:\n"
        "  /tree:\n"
        "    get:\n"
        "      responses:\n"
        "        '200':\n"
        "          description: ok\n"
        "          content:\n"
        "            application/json:\n"
        "              schema: {$ref: '#/components/schemas/Node'}\n"
    )
    rows = _rows(tmp_path, "api.yaml")

    result = reconcile(rows, [], str(tmp_path))

    node = result["components"]["schemas"]["api_Node"]
    assert node["properties"]["children"]["items"] == {
        "$ref": "#/components/schemas/api_Node"
    }


def test_same_spec_duplicate_shape_is_not_a_conflict(tmp_path):
    """Aliases inside one document collapse quietly to the first definition."""
    (tmp_path / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /items/{id}:\n"
        "    get:\n"
        "      operationId: getItem\n"
        "      responses: {'200': {description: ok}}\n"
        "  /items/{itemId}:\n"
        "    get:\n"
        "      operationId: getItemAgain\n"
        "      responses: {'200': {description: ok}}\n"
    )
    rows = _rows(tmp_path, "api.yaml")

    result = reconcile(rows, [], str(tmp_path))

    assert result["conflicts"] == []
    assert len(result["paths"]) == 1


def test_cross_file_component_reference_is_imported_and_rewritten(tmp_path):
    (tmp_path / "schemas.yaml").write_text(
        "components:\n  schemas:\n    User: {type: object}\n"
    )
    (tmp_path / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /users:\n"
        "    post:\n"
        "      requestBody:\n"
        "        content:\n"
        "          application/json:\n"
        "            schema: {$ref: 'schemas.yaml#/components/schemas/User'}\n"
        "      responses: {'201': {description: created}}\n"
    )
    rows = _rows(tmp_path, "api.yaml")

    result = reconcile(rows, [], str(tmp_path))

    assert result["components"]["schemas"]["schemas_User"] == {"type": "object"}
    schema_ref = result["paths"]["/users"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert schema_ref == {"$ref": "#/components/schemas/schemas_User"}

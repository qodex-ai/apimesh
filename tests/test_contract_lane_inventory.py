"""Discovery and loading of OpenAPI contracts (contract lane, inventory stage).

Discovery must look everywhere the code lane refuses to (docs, vendor, tests),
skip only dependency caches and build output, and never re-ingest ApiMesh's
own swagger.json. The loader must resolve references strictly inside the
repository: URLs, absolute paths, escapes, symlink escapes and cycles are
refused, never guessed around.
"""

import json
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
from contract_lane.loader import ContractLoader, RefError, load_operations

FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "contract_lane"


def _contract_paths(repo):
    return {entry["path"] for entry in discover_contract_documents(str(repo))["contracts"]}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fixture,expected_contracts",
    [
        ("openapi_first_spring", {"app/src/main/resources/api/pets.yaml"}),
        ("maven_delegate_pattern", {"src/main/resources/orders.yaml"}),
        ("client_spec_colliding_ids", {"vendor/crowdstrike/alerts.yaml"}),
        ("stale_docs_spec", {"docs/openapi.yaml"}),
        ("feign_client", set()),
        ("gateway_passthrough", {"vendor/stripe/charges.yaml"}),
        ("no_operation_ids", {"src/main/resources/health.yaml"}),
    ],
)
def test_discovery_finds_each_fixture_contract(fixture, expected_contracts):
    assert _contract_paths(FIXTURES_ROOT / fixture) == expected_contracts


def test_discovery_ignores_non_openapi_yaml():
    """application.yml sniffs nothing OpenAPI and expected.json has no version key."""
    inventory = discover_contract_documents(str(FIXTURES_ROOT / "gateway_passthrough"))
    all_paths = {
        entry["path"]
        for bucket in inventory.values()
        if isinstance(bucket, list)
        for entry in bucket
        if isinstance(entry, dict) and "path" in entry
    }
    assert "src/main/resources/application.yml" not in all_paths
    assert "expected.json" not in all_paths


def test_discovery_skips_dependency_caches_but_not_semantic_dirs(tmp_path):
    spec = "openapi: 3.0.0\ninfo: {title: T, version: '1'}\npaths:\n  /a:\n    get:\n      responses: {'200': {description: ok}}\n"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api.yaml").write_text(spec)
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "api.yaml").write_text(spec)
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "api.yaml").write_text(spec)

    assert _contract_paths(tmp_path) == {"docs/api.yaml"}


def test_discovery_never_reingests_own_output(tmp_path, monkeypatch):
    output_dir = tmp_path / "apimesh"
    output_dir.mkdir()
    (output_dir / "swagger.json").write_text(
        json.dumps({"openapi": "3.0.0", "paths": {"/x": {"get": {}}}})
    )
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(output_dir / "swagger.json"))

    assert _contract_paths(tmp_path) == set()


def test_discovery_classifies_swagger2_and_components_and_errors(tmp_path):
    (tmp_path / "old.yaml").write_text("swagger: '2.0'\npaths: {}\n")
    (tmp_path / "schemas.yaml").write_text(
        "openapi: 3.0.0\ncomponents:\n  schemas:\n    Thing: {type: object}\n"
    )
    (tmp_path / "broken.yaml").write_text("openapi: 3.0.0\npaths: {unclosed\n")

    inventory = discover_contract_documents(str(tmp_path))

    assert {e["path"] for e in inventory["swagger2"]} == {"old.yaml"}
    assert {e["path"] for e in inventory["components"]} == {"schemas.yaml"}
    assert {e["path"] for e in inventory["parse_errors"]} == {"broken.yaml"}
    assert inventory["contracts"] == []


def test_discovery_handles_multi_document_yaml(tmp_path):
    (tmp_path / "both.yaml").write_text(
        "openapi: 3.0.0\npaths:\n  /a:\n    get:\n      responses: {'200': {description: ok}}\n"
        "---\n"
        "openapi: 3.0.0\npaths:\n  /b:\n    get:\n      responses: {'200': {description: ok}}\n"
    )
    inventory = discover_contract_documents(str(tmp_path))
    assert len(inventory["contracts"]) == 2


def test_discovery_skips_symlinked_documents(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    real = outside / "api.yaml"
    real.write_text("openapi: 3.0.0\npaths:\n  /a:\n    get: {}\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "api.yaml").symlink_to(real)

    assert _contract_paths(repo) == set()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _entry_for(repo: Path, relative: str) -> dict:
    inventory = discover_contract_documents(str(repo))
    for entry in inventory["contracts"]:
        if entry["path"] == relative:
            return entry
    raise AssertionError(f"{relative} not discovered")


def test_loader_resolves_internal_and_cross_file_refs(tmp_path):
    (tmp_path / "schemas.yaml").write_text(
        "components:\n  schemas:\n    User:\n      type: object\n"
    )
    (tmp_path / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "info: {title: T, version: '1'}\n"
        "components:\n"
        "  responses:\n"
        "    Ok:\n"
        "      description: ok\n"
        "paths:\n"
        "  /users:\n"
        "    get:\n"
        "      operationId: listUsers\n"
        "      responses:\n"
        "        '200': {$ref: '#/components/responses/Ok'}\n"
        "    post:\n"
        "      requestBody:\n"
        "        content:\n"
        "          application/json:\n"
        "            schema: {$ref: 'schemas.yaml#/components/schemas/User'}\n"
        "      responses: {'201': {description: created}}\n"
    )

    operations, unresolved = load_operations(_entry_for(tmp_path, "api.yaml"), str(tmp_path))

    assert unresolved == []
    by_method = {op["method"]: op for op in operations}
    # References stay in place; the closure carries their validated targets.
    assert by_method["GET"]["operation"]["responses"]["200"] == {
        "$ref": "#/components/responses/Ok"
    }
    assert by_method["GET"]["ref_closure"]["api.yaml#/components/responses/Ok"] == {
        "description": "ok"
    }
    assert by_method["POST"]["ref_closure"][
        "schemas.yaml#/components/schemas/User"
    ] == {"type": "object"}
    assert by_method["GET"]["operation_id"] == "listUsers"


def test_loader_folds_path_level_parameters_without_duplicates(tmp_path):
    (tmp_path / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /users/{id}:\n"
        "    parameters:\n"
        "      - {name: id, in: path, required: true, schema: {type: string}}\n"
        "      - {name: trace, in: header, schema: {type: string}}\n"
        "    get:\n"
        "      parameters:\n"
        "        - {name: id, in: path, required: true, schema: {type: integer}}\n"
        "      responses: {'200': {description: ok}}\n"
    )

    operations, unresolved = load_operations(_entry_for(tmp_path, "api.yaml"), str(tmp_path))

    assert unresolved == []
    parameters = operations[0]["operation"]["parameters"]
    names = [(p["name"], p["in"]) for p in parameters]
    assert names == [("id", "path"), ("trace", "header")]
    # The operation's own declaration wins over the path-level one.
    assert parameters[0]["schema"] == {"type": "integer"}


@pytest.mark.parametrize(
    "reference",
    [
        "https://evil.example.com/spec.yaml#/components/schemas/X",
        "/etc/passwd#/x",
        "../outside.yaml#/components/schemas/X",
    ],
)
def test_loader_refuses_urls_absolute_paths_and_escapes(tmp_path, reference):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.yaml").write_text("components:\n  schemas:\n    X: {}\n")
    (repo / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /a:\n"
        "    get:\n"
        "      responses:\n"
        f"        '200': {{$ref: '{reference}'}}\n"
    )

    operations, unresolved = load_operations(_entry_for(repo, "api.yaml"), str(repo))

    assert operations == []
    assert len(unresolved) == 1
    assert unresolved[0]["path"] == "/a"


def test_loader_refuses_symlink_escapes(tmp_path):
    secret = tmp_path / "secret.yaml"
    secret.write_text("components:\n  schemas:\n    X: {leak: true}\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "linked.yaml").symlink_to(secret)
    (repo / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /a:\n"
        "    get:\n"
        "      responses:\n"
        "        '200': {$ref: 'linked.yaml#/components/schemas/X'}\n"
    )

    operations, unresolved = load_operations(_entry_for(repo, "api.yaml"), str(repo))

    assert operations == []
    assert len(unresolved) == 1
    assert "escape" in unresolved[0]["error"] or "refused" in unresolved[0]["error"]


def test_loader_accepts_legal_recursive_schemas(tmp_path):
    """A tree node referencing itself is valid OpenAPI, not a cycle error."""
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

    operations, unresolved = load_operations(_entry_for(tmp_path, "api.yaml"), str(tmp_path))

    assert unresolved == []
    assert len(operations) == 1
    closure = operations[0]["ref_closure"]
    assert list(closure) == ["api.yaml#/components/schemas/Node"]


def test_loader_rejects_path_item_alias_cycles(tmp_path):
    (tmp_path / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /a: {$ref: '#/paths/~1b'}\n"
        "  /b: {$ref: '#/paths/~1a'}\n"
    )

    operations, unresolved = load_operations(_entry_for(tmp_path, "api.yaml"), str(tmp_path))

    assert operations == []
    assert len(unresolved) == 2
    assert all("cycle" in item["error"] for item in unresolved)


def test_loader_decodes_percent_encoded_pointers(tmp_path):
    """Path-item aliases in the wild write {} as %7B%7D in the fragment."""
    (tmp_path / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /items/{id}:\n"
        "    get:\n"
        "      operationId: getItem\n"
        "      responses: {'200': {description: ok}}\n"
        "  /alias/{id}:\n"
        "    $ref: '#/paths/~1items~1%7Bid%7D'\n"
    )

    operations, unresolved = load_operations(_entry_for(tmp_path, "api.yaml"), str(tmp_path))

    assert unresolved == []
    assert {(op["method"], op["spec_path"]) for op in operations} == {
        ("GET", "/items/{id}"),
        ("GET", "/alias/{id}"),
    }


def test_loader_reports_missing_pointer_targets(tmp_path):
    (tmp_path / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /a:\n"
        "    get:\n"
        "      responses:\n"
        "        '200': {$ref: '#/components/responses/Nope'}\n"
    )

    operations, unresolved = load_operations(_entry_for(tmp_path, "api.yaml"), str(tmp_path))

    assert operations == []
    assert len(unresolved) == 1


def test_loader_handles_the_spring_fixture():
    """The pets fixture loads clean: three operations, ids and tags intact."""
    fixture = FIXTURES_ROOT / "openapi_first_spring"
    entry = _entry_for(fixture, "app/src/main/resources/api/pets.yaml")

    operations, unresolved = load_operations(entry, str(fixture))

    assert unresolved == []
    assert {(op["method"], op["spec_path"]) for op in operations} == {
        ("GET", "/pets"),
        ("POST", "/pets"),
        ("DELETE", "/pets/{petId}"),
    }
    assert all(op["operation_id"] for op in operations)


def test_resolver_unit_refuses_ref_with_sibling_keys_dropped():
    """OpenAPI 3.0 semantics: $ref replaces the whole object."""
    loader = ContractLoader(str(FIXTURES_ROOT))
    loader._documents["doc.yaml"] = {
        "components": {"schemas": {"X": {"type": "string"}}}
    }
    resolved = loader.resolve_node(
        {"$ref": "#/components/schemas/X", "description": "ignored"}, "doc.yaml"
    )
    assert resolved == {"type": "string"}


def test_resolver_unit_depth_limit():
    loader = ContractLoader(str(FIXTURES_ROOT))
    node = {"a": 1}
    for _ in range(200):
        node = {"nested": node}
    with pytest.raises(RefError):
        loader.resolve_node(node, "doc.yaml")


# ---------------------------------------------------------------------------
# Review round 1 regressions
# ---------------------------------------------------------------------------

def test_discovery_finds_compact_json_contracts(tmp_path):
    (tmp_path / "api.json").write_text(
        '{"openapi":"3.0.0","paths":{"/a":{"get":{"responses":{"200":{"description":"ok"}}}}}}'
    )
    assert _contract_paths(tmp_path) == {"api.json"}


def test_output_at_repo_root_does_not_blind_discovery(tmp_path, monkeypatch):
    """Excluding the output's whole directory once emptied entire inventories."""
    (tmp_path / "swagger.json").write_text('{"openapi":"3.0.0","paths":{"/x":{"get":{}}}}')
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api.yaml").write_text(
        "openapi: 3.0.0\npaths:\n  /a:\n    get:\n      responses: {'200': {description: ok}}\n"
    )
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(tmp_path / "swagger.json"))

    assert _contract_paths(tmp_path) == {"docs/api.yaml"}


def test_multi_document_contracts_carry_their_sibling_count(tmp_path):
    (tmp_path / "both.yaml").write_text(
        "openapi: 3.0.0\npaths:\n  /a:\n    get: {}\n"
        "---\n"
        "openapi: 3.0.0\npaths:\n  /b:\n    get: {}\n"
    )
    inventory = discover_contract_documents(str(tmp_path))
    assert [e["contracts_in_file"] for e in inventory["contracts"]] == [2, 2]
    assert [e["doc_index"] for e in inventory["contracts"]] == [0, 1]


@pytest.mark.parametrize(
    "reference",
    ["HTTPS://evil.example.com/x#/a", "ftp://evil/x#/a", "ssh://evil/x#/a"],
)
def test_loader_refuses_every_scheme_case_insensitively(tmp_path, reference):
    (tmp_path / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /a:\n"
        "    get:\n"
        "      responses:\n"
        f"        '200': {{$ref: '{reference}'}}\n"
    )
    operations, unresolved = load_operations(_entry_for(tmp_path, "api.yaml"), str(tmp_path))
    assert operations == []
    assert len(unresolved) == 1


def test_loader_rejects_negative_pointer_indexes(tmp_path):
    """RFC 6901 has no negative indexes; Python's list[-1] must not leak in."""
    (tmp_path / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "servers:\n"
        "  - {url: /v1}\n"
        "paths:\n"
        "  /a:\n"
        "    get:\n"
        "      responses:\n"
        "        '200': {$ref: '#/servers/-1'}\n"
    )
    operations, unresolved = load_operations(_entry_for(tmp_path, "api.yaml"), str(tmp_path))
    assert operations == []
    assert "non-numeric index" in unresolved[0]["error"]


def test_loader_walks_yaml_alias_dags_linearly(tmp_path):
    """A wide alias DAG must terminate promptly, not traverse exponentially."""
    lines = ["openapi: 3.0.0", "components:", "  schemas:", "    L0: &l0 {type: object}"]
    for level in range(1, 24):
        lines.append(
            f"    L{level}: &l{level}"
            + " {allOf: [*l" + str(level - 1) + ", *l" + str(level - 1) + "]}"
        )
    lines += [
        "paths:",
        "  /a:",
        "    get:",
        "      responses:",
        "        '200':",
        "          description: ok",
        "          content:",
        "            application/json:",
        "              schema: {$ref: '#/components/schemas/L23'}",
    ]
    (tmp_path / "api.yaml").write_text("\n".join(lines) + "\n")

    import time as _time
    start = _time.time()
    operations, unresolved = load_operations(_entry_for(tmp_path, "api.yaml"), str(tmp_path))
    assert _time.time() - start < 5
    assert unresolved == []
    assert len(operations) == 1

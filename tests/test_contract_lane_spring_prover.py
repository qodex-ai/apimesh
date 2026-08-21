"""Spring prover: served / consumed / docs / candidate classification.

Each fixture encodes one way the serving question goes wrong; the prover has
to get all of them right at once. Matching never grants eligibility: served
status comes from build evidence alone, and a matched-but-unproven spec is a
candidate for a human, not an inclusion.
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

from contract_lane.build_evidence import collect_build_evidence, evidence_by_spec
from contract_lane.discovery import discover_contract_documents
from contract_lane.loader import load_operations
from contract_lane.spring_prover import build_source_index, classify_contract

FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "contract_lane"


def _classify_fixture(name):
    root = FIXTURES_ROOT / name
    inventory = discover_contract_documents(str(root))
    evidence = evidence_by_spec(collect_build_evidence(str(root)))
    index = build_source_index(str(root))
    results = {}
    for entry in inventory["contracts"]:
        operations, _ = load_operations(entry, str(root))
        results[entry["path"]] = classify_contract(
            entry, operations, evidence.get(entry["path"], []), index
        )
    return results


def test_openapi_first_spring_is_served_with_api_prefix():
    results = _classify_fixture("openapi_first_spring")
    verdict = results["app/src/main/resources/api/pets.yaml"]

    assert verdict["status"] == "served"
    assert verdict["corroborated"] is True
    assert verdict["default_prefix"] == "/api"
    assert set(verdict["prefix_by_operation"].values()) == {"/api"}


def test_delegate_pattern_is_served_and_corroborated():
    results = _classify_fixture("maven_delegate_pattern")
    verdict = results["src/main/resources/orders.yaml"]

    assert verdict["status"] == "served"
    assert verdict["corroborated"] is True
    assert verdict["default_prefix"] == ""


def test_client_spec_is_excluded_no_matter_how_many_names_match():
    results = _classify_fixture("client_spec_colliding_ids")
    verdict = results["vendor/crowdstrike/alerts.yaml"]

    assert verdict["status"] == "excluded"
    assert verdict["reason"] == "client_generator"


def test_stale_docs_spec_is_classified_documentation():
    results = _classify_fixture("stale_docs_spec")
    verdict = results["docs/openapi.yaml"]

    assert verdict["status"] == "excluded"
    assert verdict["reason"] == "covers_routed_handlers"


def test_gateway_upstream_spec_has_no_server_evidence():
    results = _classify_fixture("gateway_passthrough")
    verdict = results["vendor/stripe/charges.yaml"]

    assert verdict["status"] == "excluded"
    assert verdict["reason"] == "no_server_evidence"


def test_spec_without_operation_ids_is_served_via_build_evidence():
    results = _classify_fixture("no_operation_ids")
    verdict = results["src/main/resources/health.yaml"]

    assert verdict["status"] == "served"
    # Derived names (healthGet, healthDeepGet) corroborate the invocation.
    assert verdict["corroborated"] is True
    assert verdict["default_prefix"] == ""


def test_unproven_matching_spec_is_a_candidate_never_included(tmp_path):
    """operationIds match unannotated controller methods, but no build edge."""
    (tmp_path / "api.yaml").write_text(
        "openapi: 3.0.0\n"
        "info: {title: T, version: '1'}\n"
        "paths:\n"
        "  /widgets:\n"
        "    get:\n"
        "      operationId: listWidgets\n"
        "      responses: {'200': {description: ok}}\n"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "WidgetsController.java").write_text(
        "package com.acme.web;\n"
        "import com.acme.generated.api.WidgetsApi;\n"
        "import org.springframework.web.bind.annotation.RestController;\n"
        "@RestController\n"
        "public class WidgetsController implements WidgetsApi {\n"
        "    public Object listWidgets() { return null; }\n"
        "}\n"
    )

    inventory = discover_contract_documents(str(tmp_path))
    index = build_source_index(str(tmp_path))
    entry = inventory["contracts"][0]
    operations, _ = load_operations(entry, str(tmp_path))

    verdict = classify_contract(entry, operations, [], index)

    assert verdict["status"] == "candidate"
    assert verdict["matched_operations"] == 1


def test_source_index_skips_test_sources(tmp_path):
    """A controller under src/test proves nothing."""
    (tmp_path / "src" / "test" / "java").mkdir(parents=True)
    (tmp_path / "src" / "test" / "java" / "FakeController.java").write_text(
        "package com.acme;\n"
        "import org.springframework.web.bind.annotation.RestController;\n"
        "@RestController\n"
        "public class FakeController {\n"
        "    public Object listWidgets() { return null; }\n"
        "}\n"
    )

    index = build_source_index(str(tmp_path))

    assert index.classes == []

"""Adversarial fixtures for the contract lane.

Each fixture under tests/fixtures/contract_lane is a minimal repo encoding one
way the serving question can be answered wrongly: OpenAPI-first codegen, the
delegate pattern, a vendor client spec with colliding operationIds, a stale
documentation spec, a Feign client, a gateway passthrough, and a served spec
with no operationIds. expected.json in each fixture is the ground truth the
contract lane is held to as it is built.

What runs today: integrity checks on every expectation file, and the existing
java code lane against every fixture. The code lane must already produce
exactly the expected lane=code operations and none of the poison (Feign
routes, proxied upstreams, drifted doc extras). Contract-lane expectations
(lane=contract, excluded_specs, candidates) are asserted by the ingestion
tests as each stage lands.
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# Configurations() runs at import time in config.py, so both paths must exist first.
os.environ.setdefault("APIMESH_CONFIG_PATH", str(REPO_ROOT / "config.yml"))
os.environ.setdefault("APIMESH_USER_REPO_PATH", str(REPO_ROOT))
os.environ.setdefault(
    "APIMESH_USER_CONFIG_PATH",
    str(Path(tempfile.mkdtemp(prefix="apimesh-user-config-")) / "config.json"),
)

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "contract_lane"
FIXTURES = sorted(
    p for p in FIXTURES_ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")
)


def _expected(fixture: Path) -> dict:
    return json.loads((fixture / "expected.json").read_text())


def _extract_code_ops(fixture: Path):
    from java_pipeline.find_api_definition_files import find_api_definition_files
    from java_pipeline.identify_api_functions import (
        find_api_endpoints,
        reset_extraction_drops,
        reset_type_index,
    )

    reset_extraction_drops()
    reset_type_index()
    ops = set()
    for file_path in find_api_definition_files(str(fixture)):
        for endpoint in find_api_endpoints(file_path, str(fixture)):
            ops.add((endpoint["method"], endpoint["route"]))
    return ops


def test_fixture_directory_is_not_empty():
    assert len(FIXTURES) >= 7


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_expected_json_is_well_formed(fixture):
    expected = _expected(fixture)
    assert expected["description"]
    for op in expected["included"]:
        assert op["method"] == op["method"].upper()
        assert op["route"].startswith("/")
        assert op["lane"] in {"code", "contract"}
    for excluded in expected["excluded_specs"]:
        assert (fixture / excluded["path"]).is_file(), excluded["path"]
        assert excluded["reason"] in {
            "client_generator",
            "no_server_evidence",
            "covers_routed_handlers",
        }


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_code_lane_matches_expectations_today(fixture):
    expected = _expected(fixture)
    expected_code_ops = {
        (op["method"], op["route"])
        for op in expected["included"]
        if op["lane"] == "code"
    }

    assert _extract_code_ops(fixture) == expected_code_ops


def test_code_lane_never_extracts_the_poison():
    """The named traps stay out of the code lane no matter what changes."""
    poison = {
        "feign_client": ("GET", "/invoices/{id}"),
        "gateway_passthrough": ("POST", "/v1/charges"),
        "stale_docs_spec": ("GET", "/users/export"),
        "client_spec_colliding_ids": ("DELETE", "/alerts/{id}"),
    }
    for name, op in poison.items():
        assert op not in _extract_code_ops(FIXTURES_ROOT / name), name

"""Contract lane wired into the java pipeline.

An OpenAPI-first repo with zero annotated endpoints now produces a full spec
with no LLM involvement; the kill switch restores the old honest zero; spec
operations that disappear leave the document on the next run; and repeated
runs are deterministic.
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

import java_pipeline.run_swagger_generation as rsg

FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "contract_lane"


@pytest.fixture
def contract_only_repo(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "APIMESH_USER_REPO_PATH", str(FIXTURES_ROOT / "openapi_first_spring")
    )
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(tmp_path / "out" / "swagger.json"))
    monkeypatch.delenv("APIMESH_INGEST_SPECS", raising=False)
    return tmp_path


def test_openapi_first_repo_yields_a_spec_with_no_llm(contract_only_repo, monkeypatch):
    """The whole surface comes from the contract; nothing may call OpenAI."""

    def _forbid(*args, **kwargs):
        raise AssertionError("no LLM call may happen for a contract-only repo")

    monkeypatch.setattr(rsg, "get_batch_definition_swagger", _forbid)
    monkeypatch.setattr(rsg, "get_function_definition_swagger", _forbid)

    swagger = rsg.run_swagger_generation("http://api.example.test")

    assert swagger is not None
    ops = {(m.upper(), route) for route, item in swagger["paths"].items() for m in item}
    assert ops == {
        ("GET", "/api/pets"),
        ("POST", "/api/pets"),
        ("DELETE", "/api/pets/{petId}"),
    }
    operation = swagger["paths"]["/api/pets"]["get"]
    assert operation["x-apimesh-source"] == [
        "spec:app/src/main/resources/api/pets.yaml#get /pets"
    ]
    contract_coverage = swagger["info"]["x-apimesh-coverage"]["contract"]
    assert contract_coverage["specs_served"] == 1
    assert contract_coverage["operations"] == 3

    profile_path = contract_only_repo / "out" / "repo_profile.json"
    assert profile_path.is_file()
    profile = json.loads(profile_path.read_text())
    assert profile["contract_lane"]["served"][0]["path"] == (
        "app/src/main/resources/api/pets.yaml"
    )
    assert profile["contract_lane"]["served"][0]["eligibility_hash"]


def test_kill_switch_restores_the_honest_zero(contract_only_repo, monkeypatch):
    monkeypatch.setenv("APIMESH_INGEST_SPECS", "0")

    assert rsg.run_swagger_generation("http://api.example.test") is None
    assert not (contract_only_repo / "out" / "repo_profile.json").exists()


def test_two_runs_are_identical_modulo_timestamp(contract_only_repo):
    first = rsg.run_swagger_generation("http://api.example.test")
    second = rsg.run_swagger_generation("http://api.example.test")

    for document in (first, second):
        document["info"].pop("x-generated-at", None)
    assert first == second


def test_finish_with_contract_removes_stale_spec_operations(tmp_path):
    """A spec op from a previous run that the contract no longer declares
    leaves the document; code-lane ops are untouched."""
    swagger = {
        "info": {"x-apimesh-coverage": {"endpoints_extracted": 1}},
        "paths": {
            "/api/old": {
                "get": {"x-apimesh-source": ["spec:old.yaml#get /old"]},
            },
            "/api/code": {
                "get": {"summary": "from the code lane"},
            },
        },
    }
    reconciled = {
        "paths": {"/api/new": {"get": {"x-apimesh-source": ["spec:new.yaml#get /new"]}}},
        "components": {},
        "conflicts": [],
        "superseded_code": [],
        "code_to_generate": [],
    }
    report = {
        "specs_found": 1,
        "served": [1],
        "excluded": [],
        "candidates": [],
        "unresolved_operations": [],
        "truncated": False,
    }

    import pipeline_common

    merged = pipeline_common.finish_with_contract(
        swagger, reconciled, report, str(tmp_path / "swagger.json")
    )

    assert "/api/old" not in merged["paths"]
    assert "/api/new" in merged["paths"]
    assert merged["paths"]["/api/code"]["get"]["summary"] == "from the code lane"
    assert merged["info"]["x-apimesh-coverage"]["contract"]["operations"] == 1
    # Prior coverage keys survive the merge.
    assert merged["info"]["x-apimesh-coverage"]["endpoints_extracted"] == 1


def test_repo_without_contracts_or_endpoints_still_returns_none(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "Helper.java").write_text(
        "package com.acme;\npublic class Helper {}\n"
    )
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(tmp_path / "out" / "swagger.json"))

    assert rsg.run_swagger_generation("http://api.example.test") is None


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------

def _copy_fixture(src_name, dst):
    import shutil

    shutil.copytree(FIXTURES_ROOT / src_name, dst)
    return dst


def test_exclude_override_beats_a_served_verdict(monkeypatch, tmp_path):
    """Fail-closed: an operator exclude wins even over build-proven serving."""
    from contract_lane.lane import run_lane

    repo = _copy_fixture("openapi_first_spring", tmp_path / "repo")
    (repo / ".apimesh-overrides.json").write_text(json.dumps({
        "specs": [{"path": "app/src/main/resources/api/pets.yaml", "action": "exclude",
                   "reason": "not deployed"}]
    }))

    result = run_lane(str(repo))

    assert result["rows"] == []
    assert result["report"]["excluded"] == [
        {"path": "app/src/main/resources/api/pets.yaml", "reason": "override_exclude"}
    ]
    assert result["report"]["overrides"] == [
        {"path": "app/src/main/resources/api/pets.yaml", "action": "exclude", "state": "applied"}
    ]


def test_include_override_activates_only_on_a_matching_hash(monkeypatch, tmp_path):
    """The include a human writes binds to the evidence they saw."""
    from contract_lane.lane import run_lane

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "api.yaml").write_text(
        "openapi: 3.0.0\npaths:\n  /widgets:\n    get:\n      operationId: listWidgets\n"
        "      responses: {'200': {description: ok}}\n"
    )
    (repo / "src" / "WidgetsController.java").write_text(
        "package com.acme.web;\n"
        "import com.acme.generated.api.WidgetsApi;\n"
        "import org.springframework.web.bind.annotation.RestController;\n"
        "@RestController\n"
        "public class WidgetsController implements WidgetsApi {\n"
        "    public Object listWidgets() { return null; }\n"
        "}\n"
    )

    # First run: candidate, exposes the hash a human would copy.
    first = run_lane(str(repo))
    assert len(first["report"]["candidates"]) == 1
    current_hash = first["report"]["candidates"][0]["eligibility_hash"]
    assert current_hash

    # A stale include stays dormant.
    (repo / ".apimesh-overrides.json").write_text(json.dumps({
        "specs": [{"path": "api.yaml", "action": "include",
                   "eligibility_hash": "deadbeef", "prefix": "/api"}]
    }))
    stale = run_lane(str(repo))
    assert stale["rows"] == []
    assert stale["report"]["overrides"][0]["state"] == "dormant"

    # The correct hash activates it, with the asserted prefix.
    (repo / ".apimesh-overrides.json").write_text(json.dumps({
        "specs": [{"path": "api.yaml", "action": "include",
                   "eligibility_hash": current_hash, "prefix": "/api"}]
    }))
    live = run_lane(str(repo))
    assert [(row["method"], row["route"]) for row in live["rows"]] == [
        ("GET", "/api/widgets")
    ]
    served = live["report"]["served"][0]
    assert served["override"] is True
    assert live["report"]["overrides"][0]["state"] == "applied"


def test_malformed_overrides_are_reported_and_ignored(tmp_path):
    from contract_lane.lane import run_lane

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "api.yaml").write_text("openapi: 3.0.0\npaths:\n  /a:\n    get: {}\n")
    (repo / ".apimesh-overrides.json").write_text("{not json")

    result = run_lane(str(repo))

    assert "unreadable" in result["report"]["overrides_error"]


def test_override_naming_an_unknown_spec_is_flagged(tmp_path):
    from contract_lane.lane import run_lane

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "api.yaml").write_text("openapi: 3.0.0\npaths:\n  /a:\n    get: {}\n")
    (repo / ".apimesh-overrides.json").write_text(json.dumps({
        "specs": [{"path": "vanished.yaml", "action": "exclude"}]
    }))

    result = run_lane(str(repo))

    assert {"path": "vanished.yaml", "action": "exclude", "state": "unmatched"} in (
        result["report"]["overrides"]
    )


# ---------------------------------------------------------------------------
# The lane rides every pipeline
# ---------------------------------------------------------------------------

def test_go_oapi_codegen_repo_yields_a_spec_with_no_llm(monkeypatch, tmp_path):
    import golang_pipeline.run_swagger_generation as go_rsg

    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(FIXTURES_ROOT / "go_oapi_codegen"))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(tmp_path / "out" / "swagger.json"))

    def _forbid(*args, **kwargs):
        raise AssertionError("no LLM call may happen for a contract-only repo")

    monkeypatch.setattr(go_rsg, "get_batch_definition_swagger", _forbid)
    monkeypatch.setattr(go_rsg, "get_function_definition_swagger", _forbid)

    swagger = go_rsg.run_swagger_generation("http://api.example.test")

    assert swagger is not None
    ops = {(m.upper(), route) for route, item in swagger["paths"].items() for m in item}
    assert ops == {("GET", "/widgets"), ("POST", "/widgets")}
    assert swagger["info"]["x-apimesh-coverage"]["contract"]["specs_served"] == 1


def test_connexion_repo_yields_a_spec_under_its_base_path(monkeypatch, tmp_path):
    import python_pipeline.run_swagger_generation as py_rsg

    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(FIXTURES_ROOT / "connexion_app"))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(tmp_path / "out" / "swagger.json"))

    def _forbid(*args, **kwargs):
        raise AssertionError("no LLM call may happen for a contract-only repo")

    monkeypatch.setattr(py_rsg, "get_batch_definition_swagger", _forbid, raising=False)
    monkeypatch.setattr(py_rsg, "get_function_definition_swagger", _forbid, raising=False)

    swagger = py_rsg.run_swagger_generation("http://api.example.test")

    assert swagger is not None
    ops = {(m.upper(), route) for route, item in swagger["paths"].items() for m in item}
    assert ops == {("GET", "/v1/notes")}
    assert (tmp_path / "out" / "repo_profile.json").is_file()

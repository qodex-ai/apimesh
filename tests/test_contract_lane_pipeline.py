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


def test_finish_with_contract_removes_stale_spec_operations():
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

    merged = rsg._finish_with_contract(swagger, reconciled, report)

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

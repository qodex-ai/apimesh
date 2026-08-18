"""Regression tests for the python pipeline audit fixes.

Covers blueprint/router prefix resolution, ignore matching that is relative to
the scanned repo, the per-endpoint error boundary, fragment re-keying, and the
guarantee that resolving an import never executes the scanned repo's code.
"""

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("APIMESH_CONFIG_PATH", str(REPO_ROOT / "config.yml"))

import pytest
from openai import APIError

import python_pipeline.definition_swagger_generator as dsg
import python_pipeline.run_swagger_generation as rsg
from python_pipeline.definition_swagger_generator import parse_swagger_response
from utils import num_tokens_from_string
from python_pipeline.django_urlconf import collect_django_endpoints
from python_pipeline.find_api_definition_files import (
    find_api_definition_files,
    find_python_files,
)
from python_pipeline.generate_file_information import get_module_origin, process_file
from python_pipeline.identify_api_functions import (
    collect_external_prefixes,
    find_api_endpoints,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _routes(repo_root: Path, target: Path, all_files=None) -> set:
    """Route/method pairs the extractor reports for one file of a repo."""
    files = [str(path) for path in (all_files or [target])]
    external = collect_external_prefixes(files, str(repo_root))
    endpoints = find_api_endpoints(target, external.get(os.path.abspath(str(target))))
    return {(endpoint["route"], endpoint["method"]) for endpoint in endpoints}


FLASK_BLUEPRINT = '''
from flask import Blueprint

bp = Blueprint("users", __name__, url_prefix="/api/v1")


@bp.route("/users")
def list_users():
    return []
'''

FLASK_BLUEPRINT_REGISTERED = FLASK_BLUEPRINT + '''

from flask import Flask

app = Flask(__name__)
app.register_blueprint(bp, url_prefix="/v2")
'''

FLASK_PLAIN = '''
from flask import Flask

app = Flask(__name__)


@app.route("/ping")
def ping():
    return "pong"
'''

FASTAPI_ROUTER = '''
from fastapi import APIRouter

router = APIRouter(prefix="/items")


@router.get("/{item_id}")
async def read_item(item_id: int):
    return {"id": item_id}
'''

FASTAPI_PLAIN = '''
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}
'''


def test_flask_blueprint_constructor_prefix(tmp_path):
    target = _write(tmp_path / "users.py", FLASK_BLUEPRINT)
    assert _routes(tmp_path, target) == {("/api/v1/users", "GET")}


def test_flask_register_blueprint_prefix_wins(tmp_path):
    """Flask ignores the Blueprint prefix once register_blueprint supplies one."""
    target = _write(tmp_path / "users.py", FLASK_BLUEPRINT_REGISTERED)
    assert _routes(tmp_path, target) == {("/v2/users", "GET")}


def test_flask_cross_file_register_blueprint_prefix(tmp_path):
    routes_file = _write(tmp_path / "routes.py", FLASK_BLUEPRINT)
    app_file = _write(
        tmp_path / "app.py",
        '''
from flask import Flask
from routes import bp

app = Flask(__name__)
app.register_blueprint(bp, url_prefix="/admin")
''',
    )
    assert _routes(tmp_path, routes_file, [routes_file, app_file]) == {("/admin/users", "GET")}


def test_fastapi_router_prefix(tmp_path):
    target = _write(tmp_path / "items.py", FASTAPI_ROUTER)
    assert _routes(tmp_path, target) == {("/items/{item_id}", "GET")}


def test_fastapi_include_router_composes_prefixes(tmp_path):
    """FastAPI concatenates the include_router prefix with the router's own."""
    router_file = _write(tmp_path / "api" / "items.py", FASTAPI_ROUTER)
    main_file = _write(
        tmp_path / "main.py",
        '''
from fastapi import FastAPI
from api.items import router

app = FastAPI()
app.include_router(router, prefix="/api")
''',
    )
    assert _routes(tmp_path, router_file, [router_file, main_file]) == {
        ("/api/items/{item_id}", "GET")
    }


def test_fastapi_include_router_same_file(tmp_path):
    target = _write(
        tmp_path / "main.py",
        FASTAPI_ROUTER
        + '''

from fastapi import FastAPI

app = FastAPI()
app.include_router(router, prefix="/api")
''',
    )
    assert _routes(tmp_path, target) == {("/api/items/{item_id}", "GET")}


def test_fastapi_relative_import_include_router_prefix(tmp_path):
    """``from .routes import router`` is the standard package layout."""
    router_file = _write(tmp_path / "app" / "routes.py", FASTAPI_ROUTER)
    main_file = _write(
        tmp_path / "app" / "main.py",
        '''
from fastapi import FastAPI
from .routes import router

app = FastAPI()
app.include_router(router, prefix="/api")
''',
    )
    assert _routes(tmp_path, router_file, [router_file, main_file]) == {
        ("/api/items/{item_id}", "GET")
    }


def test_parent_package_relative_import_prefix(tmp_path):
    """A level 2 import counts from the parent package directory."""
    router_file = _write(tmp_path / "app" / "routes.py", FASTAPI_ROUTER)
    main_file = _write(
        tmp_path / "app" / "web" / "main.py",
        '''
from fastapi import FastAPI
from ..routes import router

app = FastAPI()
app.include_router(router, prefix="/api")
''',
    )
    assert _routes(tmp_path, router_file, [router_file, main_file]) == {
        ("/api/items/{item_id}", "GET")
    }


@pytest.mark.parametrize(
    "source, expected",
    [
        (FLASK_PLAIN, {("/ping", "GET")}),
        (FASTAPI_PLAIN, {("/health", "GET")}),
    ],
)
def test_unprefixed_routes_unchanged(tmp_path, source, expected):
    target = _write(tmp_path / "app.py", source)
    assert _routes(tmp_path, target) == expected


def test_variable_prefixes_are_skipped(tmp_path):
    """Only string literals are resolvable, a variable prefix leaves the route alone."""
    target = _write(
        tmp_path / "app.py",
        '''
from fastapi import APIRouter

PREFIX = "/dynamic"
router = APIRouter(prefix=PREFIX)


@router.get("/things")
def things():
    return []
''',
    )
    assert _routes(tmp_path, target) == {("/things", "GET")}


def test_repo_under_ignored_absolute_path_is_still_scanned(tmp_path):
    """"build" and "var" in the absolute path must not hide the whole repo."""
    repo = tmp_path / "build" / "myrepo"
    wanted = _write(repo / "app" / "main.py", FASTAPI_PLAIN)
    ignored = _write(repo / "tests" / "test_main.py", FASTAPI_PLAIN)

    found = {str(path) for path in find_python_files(str(repo))}
    assert str(wanted) in found
    assert str(ignored) not in found

    assert rsg.should_process_directory(str(wanted), str(repo))
    assert not rsg.should_process_directory(str(ignored), str(repo))


def test_module_resolution_never_executes_scanned_code(tmp_path):
    """find_spec imported the target repo's packages, which is arbitrary code execution."""
    package = tmp_path / "scanned_pkg"
    _write(
        package / "__init__.py",
        "from pathlib import Path\n"
        "Path(__file__).with_name('IMPORTED').write_text('yes')\n",
    )
    handlers = _write(package / "handlers.py", "def handler():\n    return 1\n")

    assert Path(get_module_origin("scanned_pkg.handlers", str(tmp_path))) == handlers
    assert Path(get_module_origin("scanned_pkg", str(tmp_path))) == package / "__init__.py"
    assert not (package / "IMPORTED").exists()


def test_module_resolution_reports_external_modules(tmp_path):
    assert get_module_origin("json", str(tmp_path)) == "<built-in>"
    assert get_module_origin("does.not.exist", str(tmp_path)) == "<built-in>"


def test_rekey_fragment_forces_the_extractor_route_and_method():
    fragment = rsg._rekey_fragment(
        {"paths": {"/api/v1/users/{userId}": {"post": {"summary": "create"}}}},
        "/users/<user_id>",
        "POST",
    )
    assert fragment == {"paths": {"/users/{user_id}": {"post": {"summary": "create"}}}}


def test_rekey_fragment_renames_legacy_operation_fields():
    """Bare custom keys are not valid OpenAPI 3.0; a value already re-keyed wins."""
    fragment = rsg._rekey_fragment(
        {
            "paths": {
                "/users": {
                    "get": {
                        "description": "compliant body",
                        "api_description": "legacy body",
                        "authorization_tag": "Authorization Required",
                        "module_tag": "Users",
                        "auth_tag": "Auth API",
                        "sensitive_information": True,
                    }
                }
            }
        },
        "/users",
        "GET",
    )
    assert fragment == {
        "paths": {
            "/users": {
                "get": {
                    "description": "compliant body",
                    "x-authorization-tag": "Authorization Required",
                    "x-module-tag": "Users",
                    "x-auth-tag": "Auth API",
                    "x-sensitive-information": True,
                }
            }
        }
    }


def test_rekey_fragment_falls_back_to_the_model_method():
    """A bare Flask @app.route gives the extractor no method to force."""
    fragment = rsg._rekey_fragment({"paths": {"/x": {"PUT": {"summary": "s"}}}}, "users", None)
    assert fragment == {"paths": {"/users": {"put": {"summary": "s"}}}}


@pytest.mark.parametrize(
    "fragment",
    [None, "text", {}, {"paths": {}}, {"paths": []}, {"paths": {"/x": {}}}, {"paths": {"/x": None}}],
)
def test_rekey_fragment_rejects_unusable_fragments(fragment):
    assert rsg._rekey_fragment(fragment, "/users", "GET") is None


def test_rekey_fragment_needs_a_route():
    assert rsg._rekey_fragment({"paths": {"/x": {"get": {}}}}, None, "GET") is None


def test_route_templates_are_canonical_openapi():
    """The swagger key must be the form the save step writes, or removal breaks."""
    assert rsg._normalize_route("/users/<int:pk>/posts/<slug>") == "/users/{pk}/posts/{slug}"
    assert rsg._normalize_route("/items/{item_id}") == "/items/{item_id}"
    assert rsg._endpoint_key("/users/<int:pk>", "get") == "GET /users/{pk}"


def test_rekey_fragment_prefers_a_real_http_operation():
    """A path item may legally hold non-operation members before the verb."""
    fragment = rsg._rekey_fragment(
        {"paths": {"/x": {"parameters": [], "servers": {}, "get": {"summary": "read"}}}},
        "/x",
        None,
    )
    assert fragment == {"paths": {"/x": {"get": {"summary": "read"}}}}


def test_rekey_fragment_rejects_a_path_item_with_no_http_verb():
    """A vendor extension is a dict, but it is not an operation body."""
    fragment = {"paths": {"/x": {"x-metadata": {"owner": "payments"}}}}
    assert rsg._rekey_fragment(fragment, "/x", "GET") is None
    # Without a method to force, the model's own key is the only one available,
    # so it has to be a verb too.
    assert rsg._rekey_fragment(fragment, "/x", None) is None


def _clean_jobs():
    """Endpoints in an untouched file, so a two endpoint fixture stays under the
    half of all endpoints that sends the run into a full regeneration."""
    return [
        {"route": "/health", "method": "GET", "file_path": "/repo/other.py"},
        {"route": "/ping", "method": "GET", "file_path": "/repo/other.py"},
    ]


def _clean_index():
    return {
        "GET /health": {"files": [{"file_path": "/repo/other.py", "imports": []}]},
        "GET /ping": {"files": [{"file_path": "/repo/other.py", "imports": []}]},
    }


def test_failed_endpoints_are_left_out_of_the_api_index(monkeypatch):
    """A failure must stay dirty, otherwise the next run never retries it."""
    endpoints = [
        {"route": "/users", "method": "GET", "file_path": "/repo/app.py"},
        {"route": "/orders", "method": "POST", "file_path": "/repo/app.py"},
    ] + _clean_jobs()
    written = {}

    def fake_fragment(directory_path, method_info):
        if method_info["route"] == "/users":
            return None
        return {"paths": {"/orders": {"post": {"summary": "create"}}}}

    monkeypatch.setattr(
        rsg,
        "_load_existing_swagger",
        lambda: {"info": {"commit_reference": "old"}, "paths": {}},
    )
    monkeypatch.setattr(
        rsg,
        "_load_existing_api_index",
        lambda: dict(
            _clean_index(),
            **{"GET /users": {"files": [{"file_path": "/repo/stale.py", "imports": []}]}},
        ),
    )
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: {"/repo/app.py"})
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "new")
    monkeypatch.setattr(rsg, "_write_api_index", lambda index: written.update(index))
    # The batch reply is unusable here, so the run takes the per-endpoint path.
    monkeypatch.setattr(rsg, "_generate_batch_payload", lambda directory_path, batch: None)
    monkeypatch.setattr(rsg, "_swagger_fragment_for_endpoint", fake_fragment)

    swagger = rsg._maybe_incremental_update("/repo", endpoints)

    assert swagger["paths"] == {"/orders": {"post": {"summary": "create"}}}
    # The failed endpoint keeps its old entry, the generated one is refreshed.
    # The failed key is dropped so the next run retries it as newly added.
    assert "GET /users" not in written
    assert written["POST /orders"]["files"][0]["file_path"] == "/repo/app.py"


def test_failed_endpoint_is_retried_when_no_files_changed(monkeypatch):
    """A failure has to be retried on the next run even with nothing changed in git."""
    endpoints = [
        {"route": "/users", "method": "GET", "file_path": "/repo/app.py"},
        {"route": "/orders", "method": "POST", "file_path": "/repo/app.py"},
    ] + _clean_jobs()
    # The swagger and the index are the state the pipeline carries between runs.
    swagger = {"info": {"commit_reference": "old"}, "paths": {}}
    index = _clean_index()
    clean_keys = set(index)
    failing = {"/users"}

    def fake_fragment(directory_path, method_info):
        route = method_info["route"]
        if route in failing:
            return None
        return {"paths": {route: {method_info["method"].lower(): {"summary": route}}}}

    monkeypatch.setattr(rsg, "_load_existing_swagger", lambda: swagger)
    monkeypatch.setattr(rsg, "_load_existing_api_index", lambda: index)
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: {"/repo/app.py"})
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "new")
    monkeypatch.setattr(rsg, "_write_api_index", lambda written: index.update(written))
    # The batch reply is unusable here, so the run takes the per-endpoint path.
    monkeypatch.setattr(rsg, "_generate_batch_payload", lambda directory_path, batch: None)
    monkeypatch.setattr(rsg, "_swagger_fragment_for_endpoint", fake_fragment)

    rsg._maybe_incremental_update("/repo", endpoints)
    assert swagger["paths"] == {"/orders": {"post": {"summary": "/orders"}}}
    assert set(index) == clean_keys | {"POST /orders"}

    failing.clear()
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: set())

    rsg._maybe_incremental_update("/repo", endpoints)
    assert swagger["paths"] == {
        "/orders": {"post": {"summary": "/orders"}},
        "/users": {"get": {"summary": "/users"}},
    }
    assert set(index) == clean_keys | {"GET /users", "POST /orders"}


def test_generated_path_can_be_removed_incrementally():
    """The swagger key and the api_index key must be the same string."""
    swagger = {"paths": {}}
    fragment = rsg._rekey_fragment({"paths": {"/model/rewrote/this": {"get": {}}}}, "users", "GET")
    rsg._merge_paths(swagger, fragment)
    assert swagger["paths"] == {"/users": {"get": {}}}

    rsg._remove_endpoint_from_swagger(swagger, rsg._endpoint_key("users", "GET"))
    assert swagger["paths"] == {}


def test_parse_swagger_response_survives_junk():
    assert parse_swagger_response("no json here") is None
    assert parse_swagger_response('{"paths": {') is None
    assert parse_swagger_response("") is None
    assert parse_swagger_response('noise {"paths": {"/x": {"get": {}}}} tail') == {
        "paths": {"/x": {"get": {}}}
    }


def _stub_llm(monkeypatch, responses):
    calls = []
    sleeps = []

    def fake_call(definition, context, route, http_method=None, source_file=None):
        calls.append(route)
        outcome = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(rsg, "get_function_definition_swagger", fake_call)
    monkeypatch.setattr(rsg.time, "sleep", lambda seconds: sleeps.append(seconds))
    return calls, sleeps


def test_llm_call_retries_api_errors(monkeypatch):
    ok = {"paths": {"/x": {"get": {"summary": "ok"}}}}
    calls, sleeps = _stub_llm(
        monkeypatch,
        [APIError("rate limited", request=None, body=None), APIError("boom", request=None, body=None), ok],
    )
    assert rsg._call_swagger_llm(["def f(): pass"], [], "/x") == ok
    assert len(calls) == 3
    assert sleeps == [1, 4]


def test_llm_call_gives_up_after_two_retries(monkeypatch):
    calls, sleeps = _stub_llm(monkeypatch, [APIError("down", request=None, body=None)])
    assert rsg._call_swagger_llm(["def f(): pass"], [], "/x") is None
    assert len(calls) == 3
    assert sleeps == [1, 4]


def test_llm_call_does_not_retry_other_errors(monkeypatch):
    calls, sleeps = _stub_llm(monkeypatch, [ValueError("bad prompt")])
    assert rsg._call_swagger_llm(["def f(): pass"], [], "/x") is None
    assert len(calls) == 1
    assert sleeps == []


def test_one_bad_endpoint_does_not_abort_the_run(monkeypatch, capsys):
    endpoints = [
        {"route": "/users", "method": "GET", "name": "list_users"},
        {"route": "/orders", "method": "POST", "name": "create_order"},
    ]

    def fake_fragment(directory_path, method_info):
        if method_info["route"] == "/users":
            return None
        return {"paths": {"/orders": {"post": {"summary": "create"}}}}

    monkeypatch.setattr(rsg, "_swagger_fragment_for_endpoint", fake_fragment)
    swagger = {"paths": {}}
    generated, failed = rsg._update_swagger_for_endpoints(swagger, "/repo", endpoints)
    rsg._report_generation(len(generated), len(failed))

    assert (len(generated), len(failed)) == (1, 1)
    assert swagger["paths"] == {"/orders": {"post": {"summary": "create"}}}
    assert "generated 1 of 2 endpoints (1 failed)" in capsys.readouterr().out


def test_every_endpoint_failing_raises(capsys):
    with pytest.raises(RuntimeError):
        rsg._report_generation(0, 3)
    assert "generated 0 of 3 endpoints (3 failed)" in capsys.readouterr().out


def test_zero_endpoints_returns_none(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _write(repo / "helpers.py", "def add(a, b):\n    return a + b\n")
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(tmp_path / "out" / "swagger.json"))

    assert rsg.run_swagger_generation("http://localhost:8000") is None
    assert "found 0 endpoints" in capsys.readouterr().out


def _job(route, method, file_path="/repo/app.py"):
    return {
        "route": route,
        "method": method,
        "file_path": file_path,
        "start_line": 1,
        "end_line": 2,
    }


def _stub_context(monkeypatch, context_blocks=None):
    monkeypatch.setattr(
        rsg,
        "provide_context_codeblock",
        lambda directory_path, job: (list(context_blocks or []), ["def handler():\n"]),
    )


def test_batches_are_grouped_by_file_and_capped_at_ten():
    jobs = [_job(f"/a/{index}", "GET", "/repo/a.py") for index in range(12)]
    jobs.append(_job("/b", "POST", "/repo/b.py"))

    batches = rsg._batch_endpoint_jobs(jobs)

    assert [len(batch) for batch in batches] == [10, 2, 1]
    assert {job["file_path"] for job in batches[0]} == {"/repo/a.py"}
    assert batches[2][0]["file_path"] == "/repo/b.py"


def _sized_handler(name: str, target_tokens: int) -> list:
    """A handler body whose token count lands just past target_tokens."""
    lines = [f"def {name}():\n"]
    while num_tokens_from_string("".join(lines)) < target_tokens:
        lines.append("    value = compute(payload)\n")
    lines.append("    return value\n")
    return lines


def _write_sized_handlers(tmp_path: Path, sizes: list):
    """One .py file of handlers that big, and the jobs pointing at each."""
    source = tmp_path / "handlers.py"
    lines: list = []
    jobs = []
    for index, target_tokens in enumerate(sizes):
        body = _sized_handler(f"handler{index}", target_tokens)
        start_line = len(lines) + 1
        lines.extend(body)
        job = _job(f"/r{index}", "GET", str(source))
        job["start_line"] = start_line
        job["end_line"] = len(lines)
        jobs.append(job)
    source.write_text("".join(lines), encoding="utf-8")
    return source, jobs


def _stub_context_from_disk(monkeypatch):
    """The prompt carries the same handler lines the packing priced."""
    monkeypatch.setattr(
        rsg,
        "provide_context_codeblock",
        lambda directory_path, job: ([], rsg._handler_lines(job)),
    )


def test_batches_are_packed_to_fit_the_context_budget(tmp_path, monkeypatch):
    """Three 2500 token sections cannot share one 6000 token prompt."""
    monkeypatch.setattr(dsg, "HANDLER_TOKEN_BUDGET", 4000)
    _, jobs = _write_sized_handlers(tmp_path, [2500, 2500, 2500])

    batches = rsg._batch_endpoint_jobs(jobs)

    assert [len(batch) for batch in batches] == [2, 1]


def test_packed_batches_keep_their_sections_inside_the_budget(tmp_path, monkeypatch):
    """The invariant the packing exists for, read off the prompt entries."""
    monkeypatch.setattr(dsg, "HANDLER_TOKEN_BUDGET", 4000)
    _, jobs = _write_sized_handlers(tmp_path, [2500, 2500, 2500])
    _stub_context_from_disk(monkeypatch)
    calls = []

    def fake_batch(entries, context_blocks, source_file):
        calls.append(entries)
        return {"paths": {}}

    monkeypatch.setattr(rsg, "get_batch_definition_swagger", fake_batch)

    rsg._update_swagger_for_batches({"paths": {}}, str(tmp_path), jobs)

    assert len(calls) == 2
    for entries in calls:
        sections = sum(dsg.section_token_cost(label, body) for label, body in entries)
        assert sections <= dsg.CONTEXT_TOKEN_BUDGET


def test_a_single_oversized_endpoint_gets_a_batch_of_its_own(tmp_path, monkeypatch):
    """One section can fill the budget by itself, and still has to be sent."""
    monkeypatch.setattr(dsg, "HANDLER_TOKEN_BUDGET", 8000)
    _, jobs = _write_sized_handlers(tmp_path, [7000, 100])

    batches = rsg._batch_endpoint_jobs(jobs)

    assert [len(batch) for batch in batches] == [1, 1]
    assert batches[0][0]["route"] == "/r0"


def test_one_batch_call_documents_every_endpoint_of_a_file(monkeypatch):
    """Two endpoints of one file cost one call, and the model's own path
    spelling (/users/{pk} for /users/<int:pk>) still matches the requested key."""
    jobs = [_job("/users/<int:pk>", "GET"), _job("/users", "POST")]
    calls = []
    _stub_context(monkeypatch, [["# shared helper\n"]])

    def fake_batch(entries, context_blocks, source_file):
        calls.append((entries, source_file))
        return {
            "paths": {
                "/users/{pk}": {"get": {"summary": "read"}},
                "/users": {"post": {"summary": "create"}},
            }
        }

    monkeypatch.setattr(rsg, "get_batch_definition_swagger", fake_batch)
    swagger = {"paths": {}}

    generated, failed = rsg._update_swagger_for_batches(swagger, "/repo", jobs)

    assert len(calls) == 1
    assert [label for label, _ in calls[0][0]] == ["GET /users/{pk}", "POST /users"]
    assert calls[0][1] == "/repo/app.py"
    assert (len(generated), len(failed)) == (2, 0)
    assert swagger["paths"] == {
        "/users/{pk}": {"get": {"summary": "read"}},
        "/users": {"post": {"summary": "create"}},
    }


def test_batch_falls_back_to_the_model_method_and_renames_legacy_fields():
    """A bare Flask @app.route gives the extractor no method to force."""
    payload = {
        "paths": {"/users": {"PUT": {"api_description": "legacy", "module_tag": "Users"}}}
    }

    assert rsg._operation_from_batch(payload, "users", None) == {
        "paths": {"/users": {"put": {"description": "legacy", "x-module-tag": "Users"}}}
    }


def test_endpoint_missing_from_the_batch_reply_fails_alone(monkeypatch, capsys):
    """A model that skips an endpoint must leave it out of the index so the
    next run retries it, without taking the other endpoint down with it."""
    jobs = [_job("/users", "GET"), _job("/orders", "POST")] + _clean_jobs()
    written = {}
    _stub_context(monkeypatch)
    monkeypatch.setattr(
        rsg,
        "get_batch_definition_swagger",
        lambda entries, blocks, source_file: {
            "paths": {"/orders": {"post": {"summary": "create"}}}
        },
    )
    monkeypatch.setattr(
        rsg,
        "_load_existing_swagger",
        lambda: {"info": {"x-commit-reference": "old"}, "paths": {}},
    )
    monkeypatch.setattr(rsg, "_load_existing_api_index", lambda: _clean_index())
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: {"/repo/app.py"})
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "new")
    monkeypatch.setattr(rsg, "_write_api_index", lambda index: written.update(index))

    swagger = rsg._maybe_incremental_update("/repo", jobs)

    assert swagger["paths"] == {"/orders": {"post": {"summary": "create"}}}
    assert set(written) == set(_clean_index()) | {"POST /orders"}
    assert "skipping /users" in capsys.readouterr().out


def test_unusable_batch_reply_falls_back_to_per_endpoint_calls(monkeypatch):
    jobs = [_job("/users", "GET"), _job("/orders", "POST")]
    per_endpoint_calls = []
    _stub_context(monkeypatch)
    monkeypatch.setattr(
        rsg, "get_batch_definition_swagger", lambda entries, blocks, source_file: None
    )

    def fake_single(definition, context, route, http_method=None, source_file=None):
        per_endpoint_calls.append(route)
        return {"paths": {"/model/rewrote/this": {http_method.lower(): {"summary": route}}}}

    monkeypatch.setattr(rsg, "get_function_definition_swagger", fake_single)
    swagger = {"paths": {}}

    generated, failed = rsg._update_swagger_for_batches(swagger, "/repo", jobs)

    assert per_endpoint_calls == ["/users", "/orders"]
    assert (len(generated), len(failed)) == (2, 0)
    assert swagger["paths"] == {
        "/users": {"get": {"summary": "/users"}},
        "/orders": {"post": {"summary": "/orders"}},
    }


BIG_BLOCK_LINE = "value = compute(payload)\n"


def _oversized_blocks():
    """Seven distinct blocks, each well past the whole context budget."""
    shared = ["# helper\n" + BIG_BLOCK_LINE * 500]
    return [shared, list(shared)] + [
        [f"# block {index}\n" + BIG_BLOCK_LINE * 500] for index in range(6)
    ]


def test_batch_prompt_dedupes_context_and_stays_inside_the_budget(capsys):
    """The raw context here is ~25k tokens; the prompt has to stay near 6k."""
    handler = "def handler():\n" + "    do(work)\n" * 3000

    prompt = dsg.build_batch_prompt([("GET /users", handler)], _oversized_blocks(), "/repo/app.py")

    assert prompt.count("# helper") == 1
    assert "... truncated" in prompt
    assert num_tokens_from_string(prompt) <= dsg.CONTEXT_TOKEN_BUDGET + 1000
    assert "apimesh: context truncated for /repo/app.py (6 blocks dropped)" in capsys.readouterr().out


def test_per_endpoint_prompt_is_deduped_and_budgeted_too(monkeypatch, capsys):
    captured = {}

    class FakeClient:
        def call_chat_completion(self, messages, temperature=1):
            captured["prompt"] = messages[-1]["content"]
            return '{"paths": {"/x": {"get": {"summary": "ok"}}}}'

    monkeypatch.setattr(dsg, "OpenAiClient", FakeClient)
    handler = ["def handler():\n"] + ["    do(work)\n"] * 3000

    dsg.get_function_definition_swagger(
        handler, _oversized_blocks(), "/users", "GET", source_file="/repo/app.py"
    )

    assert captured["prompt"].count("# helper") == 1
    assert captured["prompt"].count("# block") <= 1
    assert "... truncated" in captured["prompt"]
    assert "apimesh: context truncated for /repo/app.py" in capsys.readouterr().out


def test_incremental_all_fail_keeps_spec_and_persists_retry_state(tmp_path, monkeypatch, capsys):
    """An OpenAI outage during an incremental pass must not discard a valid
    existing spec into the slow fallback; the index persists with the failed
    keys dropped so they retry next run."""
    written = {}
    existing_swagger = {
        "openapi": "3.0.0",
        "info": {"x-commit-reference": "old"},
        "paths": {"/orders": {"post": {"summary": "keep me"}}},
    }
    existing_index = {
        "GET /users": {"files": [{"file_path": "/repo/a.py", "imports": []}]},
        "POST /orders": {"files": [{"file_path": "/repo/b.py", "imports": []}]},
    }
    monkeypatch.setattr(rsg, "_load_existing_swagger", lambda: existing_swagger)
    monkeypatch.setattr(rsg, "_load_existing_api_index", lambda: existing_index)
    monkeypatch.setattr(rsg, "_write_api_index", lambda index: written.update(index))
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda commit, *args, **kwargs: {"/repo/a.py"})
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "new")
    monkeypatch.setattr(rsg, "_generate_batch_payload", lambda directory_path, batch: None)
    monkeypatch.setattr(
        rsg, "_swagger_fragment_for_endpoint",
        lambda directory_path, method_info: None,
    )
    jobs = [
        {"route": "/users", "method": "GET", "file_path": "/repo/a.py",
         "name": "u", "start_line": 1, "end_line": 2, "type": "function"},
        {"route": "/orders", "method": "POST", "file_path": "/repo/b.py",
         "name": "o", "start_line": 1, "end_line": 2, "type": "function"},
    ]
    result = rsg._maybe_incremental_update("/repo", jobs)
    assert result is existing_swagger
    assert result["paths"] == {"/orders": {"post": {"summary": "keep me"}}}
    assert "GET /users" not in written
    assert "keeping the previous spec" in capsys.readouterr().out


def test_legacy_route_spellings_are_canonicalized_on_load(tmp_path, monkeypatch):
    """A spec and index written before routes were canonicalized must load in
    the canonical spelling, otherwise the first run after the upgrade reads
    every endpoint as removed and re-added and regenerates all of them."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    swagger_path = out_dir / "swagger.json"
    swagger_path.write_text(
        json.dumps(
            {
                "info": {"x-commit-reference": "old"},
                "paths": {
                    "/users/<int:pk>": {
                        "get": {"summary": "stale"},
                        "delete": {"summary": "only on the legacy key"},
                    },
                    "/users/{pk}": {"get": {"summary": "fresh"}},
                    "/health": {"get": {"summary": "untouched"}},
                },
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "api_index.json").write_text(
        json.dumps(
            {
                "GET /users/<int:pk>": {"files": [{"file_path": "/repo/legacy.py"}]},
                "GET /users/{pk}": {"files": [{"file_path": "/repo/app.py"}]},
                "POST /users/<int:pk>": {"files": []},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(swagger_path))

    # The canonical key wins, its legacy twin only contributes the missing verb.
    assert rsg._load_existing_swagger()["paths"] == {
        "/users/{pk}": {
            "get": {"summary": "fresh"},
            "delete": {"summary": "only on the legacy key"},
        },
        "/health": {"get": {"summary": "untouched"}},
    }

    index = rsg._load_existing_api_index()
    assert set(index) == {"GET /users/{pk}", "POST /users/{pk}"}
    assert index["GET /users/{pk}"]["files"][0]["file_path"] == "/repo/app.py"


def test_incremental_no_change_return_carries_the_new_host(monkeypatch):
    """--api-host has to reach the spec on the incremental path too, or a run
    that changes the host keeps publishing the previous server url."""
    existing_swagger = {
        "info": {"x-commit-reference": "old"},
        "servers": [{"url": "https://old.example.com"}],
        "paths": {"/users": {"get": {"summary": "old"}}},
    }
    monkeypatch.setattr(rsg, "_load_existing_swagger", lambda: existing_swagger)
    monkeypatch.setattr(rsg, "_load_existing_api_index", lambda: {"GET /users": {"files": []}})
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: set())
    monkeypatch.setattr(
        rsg,
        "_swagger_fragment_for_endpoint",
        lambda *args, **kwargs: pytest.fail("nothing changed, nothing may be generated"),
    )

    result = rsg._maybe_incremental_update(
        "/repo", [_job("/users", "GET")], "https://new.example.com"
    )
    assert result["servers"] == [{"url": "https://new.example.com"}]
    assert result["paths"] == {"/users": {"get": {"summary": "old"}}}


BATCH_REPLY = {
    "paths": {
        "/users": {"get": {"summary": "regenerated"}},
        "/orders": {"post": {"summary": "regenerated"}},
    }
}


def _incremental_fixture(monkeypatch):
    """Four endpoints, two of them in the one file git reports as changed.

    Half the endpoints is the most that can be dirty without the escalation
    gate taking the run into a full regeneration, so this is the shape every
    hash test needs.
    """
    _stub_context(monkeypatch)
    jobs = [
        _job("/users", "GET"),
        _job("/orders", "POST"),
        _job("/health", "GET", "/repo/other.py"),
        _job("/ping", "GET", "/repo/other.py"),
    ]
    index = {
        rsg._endpoint_key(job["route"], job["method"]): {
            "files": [{"file_path": job["file_path"], "imports": []}]
        }
        for job in jobs
    }
    return jobs, index


def _run_incremental(monkeypatch, jobs, index, changed_files, reply=BATCH_REPLY):
    """One incremental pass. Returns the spec, the index written, the LLM calls."""
    written = {}
    calls = []
    swagger = {
        "info": {"x-commit-reference": "old"},
        "paths": {
            "/users": {"get": {"summary": "stored"}},
            "/orders": {"post": {"summary": "stored"}},
        },
    }

    def fake_batch(entries, blocks, source_file):
        calls.append([label for label, _ in entries])
        return reply

    monkeypatch.setattr(rsg, "get_batch_definition_swagger", fake_batch)
    monkeypatch.setattr(rsg, "_load_existing_swagger", lambda: swagger)
    monkeypatch.setattr(rsg, "_load_existing_api_index", lambda: index)
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: changed_files)
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "new")
    monkeypatch.setattr(rsg, "_write_api_index", lambda entries: written.update(entries))

    result = rsg._maybe_incremental_update("/repo", jobs)
    return result, written, calls


def test_generation_stores_the_context_hash(monkeypatch):
    """The index has to carry what the endpoint was generated from, or the next
    run has nothing to compare against and documents it again."""
    jobs = [_job("/users", "GET"), _job("/orders", "POST")]
    _stub_context(monkeypatch, [["# shared helper\n"]])
    monkeypatch.setattr(
        rsg, "get_batch_definition_swagger", lambda entries, blocks, source_file: BATCH_REPLY
    )

    generated, failed = rsg._update_swagger_for_batches({"paths": {}}, "/repo", jobs)
    index = rsg._build_api_index("/repo", generated)

    assert (len(generated), len(failed)) == (2, 0)
    assert index["GET /users"]["context_hash"] == rsg._endpoint_context_hash("/repo", jobs[0])
    assert index["POST /orders"]["context_hash"] == rsg._endpoint_context_hash("/repo", jobs[1])
    # Two endpoints of one file share their context, the label keeps them apart.
    assert index["GET /users"]["context_hash"] != index["POST /orders"]["context_hash"]


def test_unchanged_context_hash_skips_the_llm(monkeypatch, capsys):
    """A file git reports as changed whose endpoints read the same as last run
    costs no LLM call at all."""
    jobs, index = _incremental_fixture(monkeypatch)
    for job in jobs[:2]:
        key = rsg._endpoint_key(job["route"], job["method"])
        index[key]["context_hash"] = rsg._endpoint_context_hash("/repo", job)

    swagger, written, calls = _run_incremental(monkeypatch, jobs, index, {"/repo/app.py"})

    assert calls == []
    assert swagger["paths"] == {
        "/users": {"get": {"summary": "stored"}},
        "/orders": {"post": {"summary": "stored"}},
    }
    # The skipped endpoints keep their entries, hash included.
    assert set(written) == set(index)
    assert written["GET /users"]["context_hash"] == index["GET /users"]["context_hash"]
    assert "apimesh: skipped 2 unchanged endpoints (context hash match)" in capsys.readouterr().out


def test_changed_handler_regenerates_past_the_hash(monkeypatch, capsys):
    """The endpoint whose context moved is the only one that reaches the model."""
    jobs, index = _incremental_fixture(monkeypatch)
    index["GET /users"]["context_hash"] = rsg._endpoint_context_hash("/repo", jobs[0])
    index["POST /orders"]["context_hash"] = "sha256 of the handler before the edit"

    swagger, written, calls = _run_incremental(monkeypatch, jobs, index, {"/repo/app.py"})

    assert calls == [["POST /orders"]]
    assert swagger["paths"]["/orders"] == {"post": {"summary": "regenerated"}}
    assert swagger["paths"]["/users"] == {"get": {"summary": "stored"}}
    assert written["POST /orders"]["context_hash"] == rsg._endpoint_context_hash("/repo", jobs[1])
    assert "apimesh: skipped 1 unchanged endpoints (context hash match)" in capsys.readouterr().out


def test_endpoint_without_a_stored_hash_always_regenerates(monkeypatch, capsys):
    """An index written before hashing existed carries no hash to match."""
    jobs, index = _incremental_fixture(monkeypatch)

    _, written, calls = _run_incremental(monkeypatch, jobs, index, {"/repo/app.py"})

    # One call for the file, carrying both of its endpoints (the dirty set is a
    # set, so the order the two land in is not part of the contract).
    assert [sorted(labels) for labels in calls] == [["GET /users", "POST /orders"]]
    assert written["GET /users"]["context_hash"] == rsg._endpoint_context_hash("/repo", jobs[0])
    assert "context hash match" not in capsys.readouterr().out


def test_a_changed_dependency_file_makes_the_endpoint_dirty(monkeypatch):
    """An edit one hop away, in a file the handler imports, still regenerates it."""
    jobs, index = _incremental_fixture(monkeypatch)
    index["GET /users"]["files"][0]["imports"] = [{"file_path": "/repo/helpers.py"}]

    # Only the imported file was touched, every handler file is untouched.
    _, _, calls = _run_incremental(monkeypatch, jobs, index, {"/repo/helpers.py"})

    assert calls == [["GET /users"]]


RELOCATION_APP = """from {module} import helper


def list_orders():
    return helper()
"""

RELOCATION_HELPER = """def helper():
    return []
"""

RELOCATION_OTHER = """def ping():
    return "pong"
"""


def _relocation_jobs(repo: Path) -> list:
    """The handler at app.py:4-5 plus an endpoint nothing in this test touches.

    Two keys is the smallest repo where one dirty endpoint stays under the
    escalation gate.
    """
    return [
        {
            "route": "/orders",
            "method": "GET",
            "file_path": str(repo / "app.py"),
            "start_line": 4,
            "end_line": 5,
        },
        {
            "route": "/ping",
            "method": "GET",
            "file_path": str(repo / "other.py"),
            "start_line": 1,
            "end_line": 2,
        },
    ]


def _stage_metadata(repo: Path) -> None:
    """The per file metadata the run writes before anything reads the index."""
    for py_file in sorted(repo.rglob("*.py")):
        info = process_file(str(py_file), str(repo))
        target = Path(rsg._metadata_file_path(str(repo), str(py_file)))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(info), encoding="utf-8")


def _relocation_pass(monkeypatch, repo: Path, out_dir: Path, changed_files, summary):
    """One incremental pass over the repo as it is on disk right now."""
    calls = []

    def fake_batch(entries, blocks, source_file):
        calls.append([label for label, _ in entries])
        return {"paths": {"/orders": {"get": {"summary": summary}}}}

    monkeypatch.setattr(rsg, "get_batch_definition_swagger", fake_batch)
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: changed_files)
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "head")
    _stage_metadata(repo)

    swagger = rsg._maybe_incremental_update(str(repo), _relocation_jobs(repo))

    # The caller persists the spec between runs, the pipeline reads it back.
    Path(os.environ["APIMESH_OUTPUT_FILEPATH"]).write_text(
        json.dumps(swagger), encoding="utf-8"
    )
    index = json.loads((out_dir / "api_index.json").read_text(encoding="utf-8"))
    return index, calls


def _imported_paths(entry) -> list:
    return [
        imp["file_path"]
        for file_entry in entry["files"]
        for imp in file_entry["imports"]
    ]


def test_a_relocated_dependency_is_reindexed_when_the_endpoint_skips(tmp_path, monkeypatch):
    """A helper that moves to a file with identical text keeps the prompt, and
    so the hash, exactly as it was, so the endpoint skips. Its index entry still
    has to follow the helper: keeping the old one points the dependency hop at
    the file the helper left, and every later edit to the file it moved to is
    invisible, so the endpoint is documented from stale source forever.
    """
    repo = tmp_path / "repo"
    _write(repo / "app.py", RELOCATION_APP.format(module="helpers_a"))
    _write(repo / "helpers_a.py", RELOCATION_HELPER)
    _write(repo / "other.py", RELOCATION_OTHER)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "swagger.json").write_text(
        json.dumps({"info": {"commit_reference": "base"}, "paths": {}}), encoding="utf-8"
    )
    (out_dir / "api_index.json").write_text(
        json.dumps({"GET /ping": {"files": [{"file_path": str(repo / "other.py"), "imports": []}]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(out_dir / "swagger.json"))

    index, calls = _relocation_pass(monkeypatch, repo, out_dir, set(), "documented")

    assert calls == [["GET /orders"]]
    assert _imported_paths(index["GET /orders"]) == [str(repo / "helpers_a.py")]
    stored_hash = index["GET /orders"]["context_hash"]

    # The helper moves to a file that reads the same, and only the import line
    # of the handler's own file changes with it.
    (repo / "helpers_a.py").unlink()
    _write(repo / "helpers_b.py", RELOCATION_HELPER)
    _write(repo / "app.py", RELOCATION_APP.format(module="helpers_b"))

    index, calls = _relocation_pass(
        monkeypatch, repo, out_dir, {os.path.abspath(str(repo / "app.py"))}, "never asked for"
    )

    assert calls == []
    assert index["GET /orders"]["context_hash"] == stored_hash
    assert _imported_paths(index["GET /orders"]) == [str(repo / "helpers_b.py")]

    # The edit the stale entry used to hide: the file the helper moved to.
    _write(repo / "helpers_b.py", RELOCATION_HELPER.replace("return []", "return [1]"))

    index, calls = _relocation_pass(
        monkeypatch, repo, out_dir, {os.path.abspath(str(repo / "helpers_b.py"))}, "redocumented"
    )

    assert calls == [["GET /orders"]]
    assert index["GET /orders"]["context_hash"] != stored_hash


def test_escalation_gate_hands_the_run_back_for_a_full_regeneration(monkeypatch, capsys):
    """Past half the endpoints the surgical path is the slow one."""
    jobs, index = _incremental_fixture(monkeypatch)
    # A third endpoint moves into the changed file, so three of four are dirty.
    jobs[2]["file_path"] = "/repo/app.py"
    index["GET /health"]["files"][0]["file_path"] = "/repo/app.py"

    result, written, calls = _run_incremental(monkeypatch, jobs, index, {"/repo/app.py"})

    assert result is None
    assert calls == []
    # The full run owns the spec and the index from here, nothing is half done.
    assert written == {}
    assert "apimesh: 3 of 4 endpoints affected, running a full regeneration" in capsys.readouterr().out


FLASK_MULTI_METHOD = '''
from flask import Flask

app = Flask(__name__)


@app.route("/users", methods=["GET", "POST"])
def users():
    return []
'''

FLASK_SAME_ROUTE_TWICE = '''
from flask import Flask

app = Flask(__name__)


@app.route("/users", methods=["GET"])
def list_users():
    return []


@app.route("/users", methods=["POST"])
def create_user():
    return {}
'''


def test_flask_methods_keyword_is_one_endpoint_per_verb(tmp_path):
    """@app.route(..., methods=[...]) was read as a route with no method."""
    target = _write(tmp_path / "app.py", FLASK_MULTI_METHOD)
    assert _routes(tmp_path, target) == {("/users", "GET"), ("/users", "POST")}


def test_two_handlers_on_one_route_keep_their_own_keys(tmp_path):
    """Both collapsed into a single UNKNOWN index key before the verbs were read."""
    target = _write(tmp_path / "app.py", FLASK_SAME_ROUTE_TWICE)
    endpoints = find_api_endpoints(target)
    keys = {rsg._endpoint_key(endpoint["route"], endpoint["method"]) for endpoint in endpoints}
    assert keys == {"GET /users", "POST /users"}


def test_both_verbs_of_one_route_reach_the_spec_and_the_index(tmp_path, monkeypatch):
    target = _write(tmp_path / "app.py", FLASK_MULTI_METHOD)
    jobs = find_api_endpoints(target)
    _stub_context(monkeypatch)
    monkeypatch.setattr(
        rsg,
        "get_batch_definition_swagger",
        lambda entries, blocks, source_file: {
            "paths": {"/users": {"get": {"summary": "list"}, "post": {"summary": "create"}}}
        },
    )
    swagger = {"paths": {}}

    generated, failed = rsg._update_swagger_for_batches(swagger, str(tmp_path), jobs)

    assert (len(generated), len(failed)) == (2, 0)
    assert swagger["paths"]["/users"] == {
        "get": {"summary": "list"},
        "post": {"summary": "create"},
    }
    assert set(rsg._build_api_index(str(tmp_path), generated)) == {"GET /users", "POST /users"}


DJANGO_PROJECT_URLS = '''
from django.urls import path, include

urlpatterns = [
    path('api/', include('myapp.urls')),
]
'''

DJANGO_APP_URLS = '''
from django.urls import path, re_path
from . import views
from .views import UserDetail

urlpatterns = [
    path('users/', views.list_users),
    path('users/<int:pk>/', UserDetail.as_view()),
    re_path(r'^legacy/(?P<slug>[-\\w]+)/$', views.legacy),
]
'''

DJANGO_VIEWS = '''
from rest_framework.decorators import api_view
from rest_framework.views import APIView


@api_view(['GET', 'POST'])
def list_users(request):
    return None


def legacy(request):
    return None


class UserDetail(APIView):
    def get(self, request, pk):
        return None

    def delete(self, request, pk):
        return None

    def get_object(self, pk):
        return None


class ProfileView(APIView):
    pass
'''


def _django_endpoints(repo_root):
    endpoints = collect_django_endpoints(find_python_files(str(repo_root)), str(repo_root))
    return {(endpoint["route"], endpoint["method"]) for endpoint in endpoints}


def test_django_urlconf_extracts_the_project_routes(tmp_path):
    """A conventional Django project has no decorator anywhere, and used to
    extract zero endpoints."""
    _write(tmp_path / "myproject" / "urls.py", DJANGO_PROJECT_URLS)
    _write(tmp_path / "myapp" / "urls.py", DJANGO_APP_URLS)
    _write(tmp_path / "myapp" / "views.py", DJANGO_VIEWS)

    assert _django_endpoints(tmp_path) == {
        ("/api/users/", "GET"),
        ("/api/users/", "POST"),
        ("/api/users/<int:pk>/", "GET"),
        ("/api/users/<int:pk>/", "DELETE"),
        ("/api/legacy/<slug>/", "GET"),
    }


def test_django_include_prefix_is_not_documented_twice(tmp_path):
    """The app urls.py is walked under the prefix the project mounts it at,
    not a second time on its own."""
    _write(tmp_path / "myproject" / "urls.py", DJANGO_PROJECT_URLS)
    _write(tmp_path / "myapp" / "urls.py", DJANGO_APP_URLS)
    _write(tmp_path / "myapp" / "views.py", DJANGO_VIEWS)

    routes = {route for route, _ in _django_endpoints(tmp_path)}
    assert all(route.startswith("/api/") for route in routes)


def test_django_class_view_helpers_are_not_endpoints(tmp_path):
    """get_object is a helper; only the HTTP verbs a class defines are routes."""
    _write(tmp_path / "myproject" / "urls.py", DJANGO_PROJECT_URLS)
    _write(tmp_path / "myapp" / "urls.py", DJANGO_APP_URLS)
    _write(tmp_path / "myapp" / "views.py", DJANGO_VIEWS)

    names = {
        endpoint["name"]
        for endpoint in collect_django_endpoints(
            find_python_files(str(tmp_path)), str(tmp_path)
        )
    }
    assert names == {"list_users", "legacy", "UserDetail.get", "UserDetail.delete"}


DRF_ROUTER_URLS = '''
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import AccountViewSet, TagViewSet

router = DefaultRouter()
router.register('accounts', AccountViewSet)
router.register('tags', TagViewSet)

urlpatterns = [
    path('api/', include(router.urls)),
]
'''

DRF_VIEWS = '''
from rest_framework.viewsets import ModelViewSet, ViewSet


class AccountViewSet(ModelViewSet):
    queryset = None


class TagViewSet(ViewSet):
    def list(self, request):
        return None
'''


def test_drf_router_registration_maps_the_standard_actions(tmp_path):
    _write(tmp_path / "myapp" / "urls.py", DRF_ROUTER_URLS)
    _write(tmp_path / "myapp" / "views.py", DRF_VIEWS)

    assert _django_endpoints(tmp_path) == {
        ("/api/accounts/", "GET"),
        ("/api/accounts/", "POST"),
        ("/api/accounts/<pk>/", "GET"),
        ("/api/accounts/<pk>/", "PUT"),
        ("/api/accounts/<pk>/", "PATCH"),
        ("/api/accounts/<pk>/", "DELETE"),
        # The bare ViewSet only serves what it defines itself.
        ("/api/tags/", "GET"),
    }


DJANGO_HELPER_URLS = '''
from django.conf import settings
from django.urls import path

from . import views

urlpatterns = []

if settings.DEBUG:
    urlpatterns += [
        path('debug/', views.legacy),
    ]


def build_fixture_urls():
    urlpatterns = [
        path('internal/', views.legacy),
    ]
    return urlpatterns
'''


def test_a_function_local_urlpatterns_is_not_a_urlconf(tmp_path):
    """A helper that builds its own list of patterns serves nothing, and
    reading one as live URLconf invented endpoints out of test code. The
    module's own urlpatterns still counts, an ``if`` block included."""
    _write(tmp_path / "myproject" / "urls.py", DJANGO_PROJECT_URLS)
    _write(tmp_path / "myapp" / "urls.py", DJANGO_APP_URLS)
    _write(tmp_path / "myapp" / "views.py", DJANGO_VIEWS)
    _write(tmp_path / "myapp" / "helpers.py", DJANGO_HELPER_URLS)

    routes = {route for route, _ in _django_endpoints(tmp_path)}
    assert "/internal/" not in routes
    assert {"/debug/", "/api/users/"} <= routes


DRF_OVERRIDE_URLS = '''
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import AccountViewSet, TagViewSet

router = DefaultRouter()
router.register('accounts', AccountViewSet)
router.register('tags', TagViewSet)

urlpatterns = [
    path('api/', include(router.urls)),
]
'''

DRF_OVERRIDE_VIEWS = '''
from rest_framework.viewsets import ModelViewSet, ReadOnlyModelViewSet


class AccountViewSet(ModelViewSet):
    def list(self, request):
        return None


class TagViewSet(ReadOnlyModelViewSet):
    def retrieve(self, request, pk=None):
        return None
'''


def test_an_overridden_viewset_action_keeps_the_inherited_ones(tmp_path):
    """Overriding list does not unregister the other five the router publishes,
    and a read-only viewset still serves exactly its two."""
    _write(tmp_path / "myapp" / "urls.py", DRF_OVERRIDE_URLS)
    views = _write(tmp_path / "myapp" / "views.py", DRF_OVERRIDE_VIEWS)

    endpoints = collect_django_endpoints(
        find_python_files(str(tmp_path)), str(tmp_path)
    )
    by_name = {endpoint["name"]: endpoint for endpoint in endpoints}

    assert set(by_name) == {
        "AccountViewSet.list",
        "AccountViewSet.create",
        "AccountViewSet.retrieve",
        "AccountViewSet.update",
        "AccountViewSet.partial_update",
        "AccountViewSet.destroy",
        "TagViewSet.list",
        "TagViewSet.retrieve",
    }
    # The override is documented from its own body, the inherited ones from the
    # class that carries them.
    lines = views.read_text(encoding="utf-8").splitlines()
    assert lines[by_name["AccountViewSet.list"]["start_line"] - 1].strip() == (
        "def list(self, request):"
    )
    assert (
        by_name["AccountViewSet.create"]["start_line"]
        != by_name["AccountViewSet.list"]["start_line"]
    )


def test_a_viewset_nobody_registers_is_skipped(tmp_path):
    """Without a router there is no route to give it, and inventing one is worse
    than leaving it out."""
    _write(tmp_path / "myapp" / "views.py", DRF_VIEWS)
    assert _django_endpoints(tmp_path) == set()


def test_drf_api_view_files_are_selected_and_extracted(tmp_path):
    """@api_view is a bare name call, which the selector used to miss entirely."""
    views = _write(tmp_path / "myapp" / "views.py", DJANGO_VIEWS)
    assert str(views) in find_api_definition_files(str(tmp_path))
    # The verbs come from the URLconf, so the decorator alone carries no route.
    assert {endpoint["route"] for endpoint in find_api_endpoints(views)} == {None}


DECORATED_CLASS = '''
from fastapi import APIRouter

router = APIRouter(prefix="/items")


@router.route("/things")
class Things:
    def __init__(self):
        self.cache = {}

    def get(self):
        return []

    def _load(self):
        return 1
'''


def _expanded_jobs(endpoints):
    """The endpoint jobs run_swagger_generation builds out of an extraction."""
    jobs = []
    for endpoint in endpoints:
        if endpoint.get("type") == "class":
            jobs.extend(endpoint.get("methods", []))
        else:
            jobs.append(endpoint)
    return jobs


def test_only_the_http_methods_of_a_decorated_class_are_endpoints(tmp_path):
    """__init__ and helpers were documented as endpoints of their own."""
    target = _write(tmp_path / "app.py", DECORATED_CLASS)
    endpoints = find_api_endpoints(target)

    assert [endpoint["name"] for endpoint in endpoints] == ["Things"]
    jobs = _expanded_jobs(endpoints)
    assert [(job["name"], job["route"], job["method"]) for job in jobs] == [
        ("get", "/items/things", "GET")
    ]


def test_a_class_method_is_not_emitted_twice(tmp_path):
    """The parented tree was thrown away, so every method was also extracted as
    a free function."""
    target = _write(tmp_path / "app.py", DECORATED_CLASS)
    endpoints = find_api_endpoints(target)
    assert [endpoint["type"] for endpoint in endpoints] == ["class"]
    assert len(_expanded_jobs(endpoints)) == 1


def test_find_api_endpoints_accepts_a_string_path(tmp_path):
    target = _write(tmp_path / "app.py", FLASK_MULTI_METHOD)
    assert find_api_endpoints(str(target)) == find_api_endpoints(target)


def test_routeless_and_duplicate_jobs_are_dropped(tmp_path):
    """A route the extractor could not read is not a generation failure, and the
    same handler found twice is one endpoint."""
    routed = _job("/users", "GET")
    jobs = rsg._routed_endpoints([
        routed,
        dict(routed),
        {"route": None, "method": "GET", "file_path": "/repo/app.py",
         "start_line": 1, "end_line": 2},
    ])
    assert jobs == [routed]


def test_metadata_filenames_do_not_collide_on_a_shared_suffix(tmp_path):
    """strip('.py') strips a character set: apply.py and appl.py shared a file."""
    apply_path = rsg._metadata_file_path(str(tmp_path), "/repo/apply.py")
    appl_path = rsg._metadata_file_path(str(tmp_path), "/repo/appl.py")

    assert apply_path != appl_path
    assert os.path.basename(apply_path) == "_q_repo_q_apply.json"
    assert os.path.basename(appl_path) == "_q_repo_q_appl.json"


HELPER_SOURCE = '''def helper(payload):
    total = payload["a"]
    total += payload["b"]
    return total


def handler():
    return helper({"a": 1, "b": 2})
'''


def test_in_file_dependency_context_carries_the_whole_helper(tmp_path):
    """The slice was [start:start], so the helper arrived as its def line alone."""
    source = _write(tmp_path / "app.py", HELPER_SOURCE)
    dependency = {
        "name": "helper",
        "function_start_line": 1,
        "function_end_line": 4,
        "start_line": 8,
        "end_line": 8,
    }

    blocks = rsg.get_code_blocks([dependency], [], str(source), str(tmp_path))

    assert len(blocks) == 1
    assert "".join(blocks[0]) == HELPER_SOURCE.split("\n\n\n")[0] + "\n"


def test_missing_metadata_skips_the_dependency(tmp_path):
    """A module imported out of an ignored directory was never processed."""
    imported = {"origin": str(tmp_path / "ignored" / "helpers.py"), "imported_name": "helper"}
    assert rsg.get_code_blocks([], [imported], str(tmp_path), str(tmp_path)) == []


def test_aliased_imports_are_matched_to_their_usages(tmp_path):
    """Without the alias, ``import helpers as h`` was never seen to be used."""
    _write(tmp_path / "helpers.py", "def helper():\n    return 1\n")
    target = _write(
        tmp_path / "app.py",
        "import helpers as h\n\n\ndef handler():\n    return h.helper()\n",
    )

    info = process_file(str(target), str(tmp_path))
    alias_import = next(item for item in info["imports"] if item["imported_name"] == "helpers")

    assert alias_import["asname"] == "h"
    assert alias_import["usage_lines"] == [5]


def test_a_file_that_is_not_utf8_does_not_kill_the_metadata_pass(tmp_path):
    latin1 = tmp_path / "legacy.py"
    latin1.write_bytes("# caf\xe9\ndef handler():\n    return 1\n".encode("latin-1"))

    info = process_file(str(latin1), str(tmp_path))

    assert [element["name"] for element in info["elements"]["functions"]] == ["handler"]


def test_one_unreadable_file_does_not_stop_the_walk(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _write(repo / "broken.py", "def add(a, b):\n    return a + b\n")
    _write(repo / "helpers.py", "def sub(a, b):\n    return a - b\n")
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(tmp_path / "out" / "swagger.json"))

    def exploding(file_path, base_directory=None):
        if file_path.endswith("broken.py"):
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "boom")
        return {"filename": file_path, "elements": {}, "imports": []}

    monkeypatch.setattr(rsg, "process_file", exploding)

    assert rsg.run_swagger_generation("http://localhost:8000") is None
    output = capsys.readouterr().out
    assert "skipping metadata for" in output
    assert "found 0 endpoints" in output


def test_a_django_repo_runs_end_to_end(tmp_path, monkeypatch):
    """The URLconf endpoints have to survive the whole run: the metadata file
    of the views module, the context read, the batch call and the index."""
    repo = tmp_path / "repo"
    _write(repo / "myproject" / "urls.py", DJANGO_PROJECT_URLS)
    _write(repo / "myapp" / "urls.py", DJANGO_APP_URLS)
    _write(repo / "myapp" / "views.py", DJANGO_VIEWS)
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(tmp_path / "out" / "swagger.json"))
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "commit")
    monkeypatch.setattr(rsg, "get_github_repo_url", lambda: "https://example.com/repo")
    def fake_batch(entries, blocks, source_file):
        """One operation per requested label, the way the model answers."""
        paths = {}
        for label, _ in entries:
            method, route = label.split(" ", 1)
            paths.setdefault(route, {})[method.lower()] = {"summary": label}
        return {"paths": paths}

    monkeypatch.setattr(rsg, "get_batch_definition_swagger", fake_batch)

    swagger = rsg.run_swagger_generation("http://localhost:8000")

    assert set(swagger["paths"]) == {
        "/api/users/",
        "/api/users/{pk}/",
        "/api/legacy/{slug}/",
    }
    assert set(swagger["paths"]["/api/users/"]) == {"get", "post"}
    index = json.loads((tmp_path / "out" / "api_index.json").read_text(encoding="utf-8"))
    assert "GET /api/users/{pk}/" in index
    assert index["GET /api/users/{pk}/"]["files"][0]["file_path"].endswith("myapp/views.py")

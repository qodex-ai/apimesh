"""Regression tests for the python pipeline audit fixes.

Covers blueprint/router prefix resolution, ignore matching that is relative to
the scanned repo, the per-endpoint error boundary, fragment re-keying, and the
guarantee that resolving an import never executes the scanned repo's code.
"""

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
from python_pipeline.find_api_definition_files import find_python_files
from python_pipeline.generate_file_information import get_module_origin
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
    assert _routes(tmp_path, target) == {("/api/v1/users", None)}


def test_flask_register_blueprint_prefix_wins(tmp_path):
    """Flask ignores the Blueprint prefix once register_blueprint supplies one."""
    target = _write(tmp_path / "users.py", FLASK_BLUEPRINT_REGISTERED)
    assert _routes(tmp_path, target) == {("/v2/users", None)}


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
    assert _routes(tmp_path, routes_file, [routes_file, app_file]) == {("/admin/users", None)}


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
        (FLASK_PLAIN, {("/ping", None)}),
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


def test_failed_endpoints_are_left_out_of_the_api_index(monkeypatch):
    """A failure must stay dirty, otherwise the next run never retries it."""
    endpoints = [
        {"route": "/users", "method": "GET", "file_path": "/repo/app.py"},
        {"route": "/orders", "method": "POST", "file_path": "/repo/app.py"},
    ]
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
        lambda: {"GET /users": {"files": [{"file_path": "/repo/stale.py", "imports": []}]}},
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
    ]
    # The swagger and the index are the state the pipeline carries between runs.
    swagger = {"info": {"commit_reference": "old"}, "paths": {}}
    index = {}
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
    assert set(index) == {"POST /orders"}

    failing.clear()
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: set())

    rsg._maybe_incremental_update("/repo", endpoints)
    assert swagger["paths"] == {
        "/orders": {"post": {"summary": "/orders"}},
        "/users": {"get": {"summary": "/users"}},
    }
    assert set(index) == {"GET /users", "POST /orders"}


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
    jobs = [_job("/users", "GET"), _job("/orders", "POST")]
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
    monkeypatch.setattr(rsg, "_load_existing_api_index", lambda: {})
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: {"/repo/app.py"})
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "new")
    monkeypatch.setattr(rsg, "_write_api_index", lambda index: written.update(index))

    swagger = rsg._maybe_incremental_update("/repo", jobs)

    assert swagger["paths"] == {"/orders": {"post": {"summary": "create"}}}
    assert set(written) == {"POST /orders"}
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

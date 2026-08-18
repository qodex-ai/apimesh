"""Regression tests for the golang pipeline audit fixes.

Covers the defects that silently produced wrong or empty specs, among them:

* routes registered on a group variable (``v1 := r.Group("/api/v1")``) lost the
  group prefix, so every gin/echo repo emitted bare paths;
* ignore matching ran over the absolute path, so a repo checked out below
  /var, /tmp or /build discovered zero .go files;
* the LLM fragment was merged under the model's own path key, so a normalized
  path (/users/:id returned as /users/{id}) diverged from the api_index key.
"""

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ["APIMESH_CONFIG_PATH"] = str(REPO_ROOT / "config.yml")

import golang_pipeline.definition_swagger_generator as dsg
import golang_pipeline.run_swagger_generation as rsg
from golang_pipeline.find_api_definition_files import find_api_definition_files
from golang_pipeline.generate_file_information import process_file
from golang_pipeline.identify_api_functions import (
    extraction_drops,
    find_api_endpoints,
    log_extraction_drops,
    reset_extraction_drops,
)
from golang_pipeline.run_swagger_generation import _normalize_swagger_fragment
from utils import num_tokens_from_string


def _endpoints(tmp_path: Path, source: str) -> list:
    go_file = tmp_path / "routes.go"
    go_file.parent.mkdir(parents=True, exist_ok=True)
    go_file.write_text(source, encoding="utf-8")
    reset_extraction_drops()
    return find_api_endpoints(go_file, str(tmp_path))


def _routes(tmp_path: Path, source: str) -> set:
    return {
        (endpoint["http_method"], endpoint["route"])
        for endpoint in _endpoints(tmp_path, source)
    }


GIN_GROUP = """package main

import "github.com/gin-gonic/gin"

func getUser(c *gin.Context) {}

func RegisterRoutes(r *gin.Engine) {
	v1 := r.Group("/api/v1")
	v1.GET("/users/:id", getUser)
	r.POST("/login", getUser)
}
"""


def test_gin_group_variable_prefix(tmp_path):
    assert _routes(tmp_path, GIN_GROUP) == {
        ("GET", "/api/v1/users/:id"),
        ("POST", "/login"),
    }


NESTED_GROUPS = """package main

import "github.com/gin-gonic/gin"

func listAdmins(c *gin.Context) {}

func RegisterRoutes(r *gin.Engine) {
	v1 := r.Group("/api/v1")
	admin := v1.Group("/admin")
	deep := admin.Group("/settings")
	admin.GET("/users", listAdmins)
	deep.PUT("/flags", listAdmins)
}
"""


def test_nested_group_composition(tmp_path):
    assert _routes(tmp_path, NESTED_GROUPS) == {
        ("GET", "/api/v1/admin/users"),
        ("PUT", "/api/v1/admin/settings/flags"),
    }


ECHO_GROUP = """package main

import "github.com/labstack/echo/v4"

func listBooks(c echo.Context) error { return nil }

func RegisterRoutes(e *echo.Echo) {
	g := e.Group("/books")
	g.GET("/:id", listBooks)
}
"""


def test_echo_group_variable_prefix(tmp_path):
    assert _routes(tmp_path, ECHO_GROUP) == {("GET", "/books/:id")}


RAW_ROUTER = """package main

import "github.com/gin-gonic/gin"

func health(c *gin.Context) {}

func RegisterRoutes(r *gin.Engine) {
	db := store.Connect()
	r.GET("/health", health)
	db.GET("/ignored", health)
}
"""


def test_routes_on_non_group_receivers_are_unchanged(tmp_path):
    """A receiver that is not a router group must not gain a prefix."""
    assert _routes(tmp_path, RAW_ROUTER) == {
        ("GET", "/health"),
        ("GET", "/ignored"),
    }


VARIABLE_GROUP_ARGUMENT = """package main

import "github.com/gin-gonic/gin"

func listUsers(c *gin.Context) {}

func RegisterRoutes(r *gin.Engine, prefix string) {
	v1 := r.Group(prefix)
	joined := r.Group("/api" + prefix)
	v1.GET("/users", listUsers)
	joined.GET("/teams", listUsers)
}
"""


def test_non_literal_group_argument_leaves_routes_unprefixed(tmp_path):
    """The prefix is unknowable at parse time, so nothing is invented."""
    assert _routes(tmp_path, VARIABLE_GROUP_ARGUMENT) == {
        ("GET", "/users"),
        ("GET", "/teams"),
    }


TRAILING_SLASH_GROUP = """package main

import "github.com/gin-gonic/gin"

func listUsers(c *gin.Context) {}

func RegisterRoutes(r *gin.Engine) {
	v1 := r.Group("/api/v1/")
	v1.GET("/users", listUsers)
}
"""


def test_prefix_join_never_doubles_slashes(tmp_path):
    assert _routes(tmp_path, TRAILING_SLASH_GROUP) == {("GET", "/api/v1/users")}


def test_ignored_dirs_are_matched_relative_to_the_repo_root(tmp_path):
    """"build" above the repo root is not the repo's own build directory."""
    repo_root = tmp_path / "build" / "myrepo"
    (repo_root / "api").mkdir(parents=True)
    (repo_root / "vendor").mkdir()
    (repo_root / "api" / "routes.go").write_text(GIN_GROUP, encoding="utf-8")
    (repo_root / "vendor" / "lib.go").write_text(GIN_GROUP, encoding="utf-8")

    found = find_api_definition_files(str(repo_root))

    assert found == [str(repo_root / "api" / "routes.go")]


def test_fragment_is_rekeyed_under_the_extractor_route():
    """The model's normalized path and method are discarded."""
    fragment = {
        "paths": {
            "/users/{id}": {
                "parameters": [],
                "get": {"summary": "Fetch a user", "responses": {}},
            }
        }
    }

    assert _normalize_swagger_fragment(fragment, "/api/v1/users/:id", "GET") == {
        "/api/v1/users/{id}": {"get": {"summary": "Fetch a user", "responses": {}}}
    }


def test_fragment_renames_legacy_operation_fields():
    """Bare custom keys are not valid OpenAPI 3.0; a value already re-keyed wins."""
    fragment = {
        "paths": {
            "/users/{id}": {
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
    }

    assert _normalize_swagger_fragment(fragment, "/users/:id", "GET") == {
        "/users/{id}": {
            "get": {
                "description": "compliant body",
                "x-authorization-tag": "Authorization Required",
                "x-module-tag": "Users",
                "x-auth-tag": "Auth API",
                "x-sensitive-information": True,
            }
        }
    }


SAME_NAME_GROUPS = """package main

import "github.com/gin-gonic/gin"

func listUsers(c *gin.Context) {}

func RegisterV1(r *gin.Engine) {
	api := r.Group("/v1")
	api.GET("/users", listUsers)
}

func RegisterV2(r *gin.Engine) {
	api := r.Group("/v2")
	api.GET("/users", listUsers)
}
"""


def test_same_group_name_in_two_functions_resolves_independently(tmp_path):
    """Group variables are function-scoped, the first declaration is not global."""
    assert _routes(tmp_path, SAME_NAME_GROUPS) == {
        ("GET", "/v1/users"),
        ("GET", "/v2/users"),
    }


PACKAGE_LEVEL_GROUP = """package main

import "github.com/gin-gonic/gin"

func listUsers(c *gin.Context) {}

var shared *gin.RouterGroup

func setup(r *gin.Engine) {
	shared = r.Group("/shared")
}

func RegisterRoutes() {
	shared.GET("/users", listUsers)
}
"""


def test_unambiguous_group_still_resolves_across_functions(tmp_path):
    """Scoping must not break a package-level group assigned in another function."""
    assert _routes(tmp_path, PACKAGE_LEVEL_GROUP) == {("GET", "/shared/users")}


def test_route_templates_are_canonical_openapi():
    """The swagger key must be the form the save step writes, or removal breaks."""
    assert rsg._normalize_route("/users/:id/posts/:postId") == "/users/{id}/posts/{postId}"
    assert rsg._normalize_route("/users/{id}") == "/users/{id}"
    assert rsg._normalize_route("/static/*filepath") == "/static/*filepath"
    assert rsg._endpoint_key("/users/:id", "get") == "GET /users/{id}"


def test_fragment_prefers_a_real_http_operation():
    """A path item may legally hold non-operation members before the verb."""
    fragment = {
        "paths": {
            "/x": {
                "x-vendor": {"note": "not an operation"},
                "post": {"summary": "create"},
            }
        }
    }

    assert _normalize_swagger_fragment(fragment, "/x", "POST") == {
        "/x": {"post": {"summary": "create"}}
    }


def test_fragment_without_an_http_verb_is_rejected():
    """A vendor extension is a dict, but it is not an operation body."""
    fragment = {"paths": {"/x": {"x-metadata": {"owner": "payments"}}}}
    assert _normalize_swagger_fragment(fragment, "/x", "GET") is None


def test_invalid_fragments_are_rejected():
    assert _normalize_swagger_fragment(None, "/users", "GET") is None
    assert _normalize_swagger_fragment({}, "/users", "GET") is None
    assert _normalize_swagger_fragment({"paths": {}}, "/users", "GET") is None
    assert _normalize_swagger_fragment({"paths": []}, "/users", "GET") is None
    assert (
        _normalize_swagger_fragment({"paths": {"/users": {"parameters": []}}}, "/users", "GET")
        is None
    )
    assert _normalize_swagger_fragment({"paths": {"/x": {"get": {}}}}, None, "GET") is None


def test_incremental_endpoint_failure_does_not_abort_the_run(monkeypatch, capsys):
    """A failing LLM call used to propagate and drop the CLI into its fallback."""
    endpoints = [
        {"route": "/users", "http_method": "GET", "name": "listUsers"},
        {"route": "/orders", "http_method": "POST", "name": "createOrder"},
    ]

    def fake_generate(directory_path, method_info):
        if method_info["route"] == "/users":
            raise RuntimeError("openai exploded")
        return {"paths": {"/model/rewrote/this": {"post": {"summary": "create"}}}}

    monkeypatch.setattr(rsg, "_generate_swagger_fragment", fake_generate)
    swagger = {"paths": {}}
    generated, failed = rsg._update_swagger_for_endpoints(swagger, "/repo", endpoints)

    assert (len(generated), len(failed)) == (1, 1)
    assert swagger["paths"] == {"/orders": {"post": {"summary": "create"}}}
    assert "openai exploded" in capsys.readouterr().out


def _clean_jobs():
    """Endpoints in an untouched file, so a two endpoint fixture stays under the
    half of all endpoints that sends the run into a full regeneration."""
    return [
        {"route": "/health", "http_method": "GET", "file_path": "/repo/other.go"},
        {"route": "/ping", "http_method": "GET", "file_path": "/repo/other.go"},
    ]


def _clean_index():
    return {
        "GET /health": {"files": [{"file_path": "/repo/other.go", "imports": []}]},
        "GET /ping": {"files": [{"file_path": "/repo/other.go", "imports": []}]},
    }


def test_failed_endpoints_are_left_out_of_the_api_index(monkeypatch):
    """A failure must stay dirty, otherwise the next run never retries it."""
    endpoints = [
        {"route": "/users", "http_method": "GET", "file_path": "/repo/app.go"},
        {"route": "/orders", "http_method": "POST", "file_path": "/repo/app.go"},
    ] + _clean_jobs()
    written = {}

    def fake_fragment(directory_path, method_info):
        if method_info["route"] == "/users":
            return None
        return {"/orders": {"post": {"summary": "create"}}}

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
            **{"GET /users": {"files": [{"file_path": "/repo/stale.go", "imports": []}]}},
        ),
    )
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: {"/repo/app.go"})
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
    assert written["POST /orders"]["files"][0]["file_path"] == "/repo/app.go"


def test_failed_endpoint_is_retried_when_no_files_changed(monkeypatch):
    """A failure has to be retried on the next run even with nothing changed in git."""
    endpoints = [
        {"route": "/users", "http_method": "GET", "file_path": "/repo/app.go"},
        {"route": "/orders", "http_method": "POST", "file_path": "/repo/app.go"},
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
        return {route: {method_info["http_method"].lower(): {"summary": route}}}

    monkeypatch.setattr(rsg, "_load_existing_swagger", lambda: swagger)
    monkeypatch.setattr(rsg, "_load_existing_api_index", lambda: index)
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: {"/repo/app.go"})
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


def _job(route, http_method, file_path="/repo/app.go"):
    return {
        "route": route,
        "http_method": http_method,
        "file_path": file_path,
        "start_line": 1,
        "end_line": 2,
    }


def _stub_context(monkeypatch, context_blocks=None):
    monkeypatch.setattr(
        rsg,
        "provide_context_codeblock",
        lambda directory_path, job: (list(context_blocks or []), ["func handler() {}\n"]),
    )


def test_batches_are_grouped_by_file_and_capped_at_ten():
    jobs = [_job(f"/a/{index}", "GET", "/repo/a.go") for index in range(12)]
    jobs.append(_job("/b", "POST", "/repo/b.go"))

    batches = rsg._batch_endpoint_jobs(jobs)

    assert [len(batch) for batch in batches] == [10, 2, 1]
    assert {job["file_path"] for job in batches[0]} == {"/repo/a.go"}
    assert batches[2][0]["file_path"] == "/repo/b.go"


def _sized_handler(name: str, target_tokens: int) -> list:
    """A handler body whose token count lands just past target_tokens."""
    lines = [f"func {name}(c *gin.Context) {{\n"]
    while num_tokens_from_string("".join(lines)) < target_tokens:
        lines.append("\tvalue := compute(input)\n")
    lines.append("}\n")
    return lines


def _write_sized_handlers(tmp_path: Path, sizes: list):
    """One .go file of handlers that big, and the jobs pointing at each."""
    source = tmp_path / "handlers.go"
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


def test_batches_are_packed_to_fit_the_context_budget(tmp_path, monkeypatch):
    """Three 2500 token sections cannot share one 6000 token prompt."""
    monkeypatch.setattr(dsg, "HANDLER_TOKEN_BUDGET", 4000)
    _, jobs = _write_sized_handlers(tmp_path, [2500, 2500, 2500])

    batches = rsg._batch_endpoint_jobs(jobs)

    assert [len(batch) for batch in batches] == [2, 1]


def test_packed_batches_keep_their_sections_inside_the_budget(tmp_path, monkeypatch):
    """The invariant the packing exists for, read off the prompt entries."""
    monkeypatch.setattr(dsg, "HANDLER_TOKEN_BUDGET", 4000)
    _prepare_run(monkeypatch, tmp_path, tmp_path / "out")
    _, jobs = _write_sized_handlers(tmp_path, [2500, 2500, 2500])
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
    spelling (/users/{id} for /users/:id) still matches the requested key."""
    jobs = [_job("/users/:id", "GET"), _job("/users", "POST")]
    calls = []
    _stub_context(monkeypatch, [["// shared helper\n"]])

    def fake_batch(entries, context_blocks, source_file):
        calls.append((entries, source_file))
        return {
            "paths": {
                "/users/{id}": {"get": {"summary": "read"}},
                "/users": {"post": {"summary": "create"}},
            }
        }

    monkeypatch.setattr(rsg, "get_batch_definition_swagger", fake_batch)
    swagger = {"paths": {}}

    generated, failed = rsg._update_swagger_for_batches(swagger, "/repo", jobs)

    assert len(calls) == 1
    assert [label for label, _ in calls[0][0]] == ["GET /users/{id}", "POST /users"]
    assert calls[0][1] == "/repo/app.go"
    assert (len(generated), len(failed)) == (2, 0)
    assert swagger["paths"] == {
        "/users/{id}": {"get": {"summary": "read"}},
        "/users": {"post": {"summary": "create"}},
    }


def test_batch_renames_legacy_operation_fields():
    payload = {
        "paths": {
            "/users/{id}": {
                "get": {"api_description": "legacy body", "module_tag": "Users"}
            }
        }
    }

    assert rsg._operation_from_batch(payload, "/users/:id", "GET") == {
        "/users/{id}": {"get": {"description": "legacy body", "x-module-tag": "Users"}}
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
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: {"/repo/app.go"})
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "new")
    monkeypatch.setattr(rsg, "_write_api_index", lambda index: written.update(index))

    swagger = rsg._maybe_incremental_update("/repo", jobs)

    assert swagger["paths"] == {"/orders": {"post": {"summary": "create"}}}
    assert set(written) == set(_clean_index()) | {"POST /orders"}
    assert "skipped GET /users" in capsys.readouterr().out


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


BIG_BLOCK_LINE = "value := compute(input)\n"


def _oversized_blocks():
    """Seven distinct blocks, each well past the whole context budget."""
    shared = ["// helper\n" + BIG_BLOCK_LINE * 500]
    return [shared, list(shared)] + [
        [f"// block {index}\n" + BIG_BLOCK_LINE * 500] for index in range(6)
    ]


def test_batch_prompt_dedupes_context_and_stays_inside_the_budget(capsys):
    """The raw context here is ~25k tokens; the prompt has to stay near 6k."""
    handler = "func handler() {\n" + "\tdo(work)\n" * 3000 + "}\n"

    prompt = dsg.build_batch_prompt([("GET /users", handler)], _oversized_blocks(), "/repo/app.go")

    assert prompt.count("// helper") == 1
    assert "... truncated" in prompt
    assert num_tokens_from_string(prompt) <= dsg.CONTEXT_TOKEN_BUDGET + 1000
    assert "apimesh: context truncated for /repo/app.go (6 blocks dropped)" in capsys.readouterr().out


def test_per_endpoint_prompt_is_deduped_and_budgeted_too(monkeypatch, capsys):
    captured = {}

    class FakeClient:
        def call_chat_completion(self, messages, temperature=0):
            captured["prompt"] = messages[-1]["content"]
            return '{"paths": {"/x": {"get": {"summary": "ok"}}}}'

    monkeypatch.setattr(dsg, "OpenAiClient", FakeClient)
    handler = ["func handler() {\n"] + ["\tdo(work)\n"] * 3000 + ["}\n"]

    dsg.get_function_definition_swagger(
        handler, _oversized_blocks(), "/users", "GET", source_file="/repo/app.go"
    )

    assert captured["prompt"].count("// helper") == 1
    assert captured["prompt"].count("// block") <= 1
    assert "... truncated" in captured["prompt"]
    assert "apimesh: context truncated for /repo/app.go" in capsys.readouterr().out


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
                    "/users/:id": {
                        "get": {"summary": "stale"},
                        "delete": {"summary": "only on the legacy key"},
                    },
                    "/users/{id}": {"get": {"summary": "fresh"}},
                    "/health": {"get": {"summary": "untouched"}},
                },
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "api_index.json").write_text(
        json.dumps(
            {
                "GET /users/:id": {"files": [{"file_path": "/repo/legacy.go"}]},
                "GET /users/{id}": {"files": [{"file_path": "/repo/app.go"}]},
                "POST /users/:id": {"files": []},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(swagger_path))

    # The canonical key wins, its legacy twin only contributes the missing verb.
    assert rsg._load_existing_swagger()["paths"] == {
        "/users/{id}": {
            "get": {"summary": "fresh"},
            "delete": {"summary": "only on the legacy key"},
        },
        "/health": {"get": {"summary": "untouched"}},
    }

    index = rsg._load_existing_api_index()
    assert set(index) == {"GET /users/{id}", "POST /users/{id}"}
    assert index["GET /users/{id}"]["files"][0]["file_path"] == "/repo/app.go"


def test_incremental_no_change_return_carries_the_new_host(monkeypatch):
    """--api-host has to reach the spec on the incremental path too, or a run
    that changes the host keeps publishing the previous server url."""
    existing_swagger = {
        "info": {"x-commit-reference": "old"},
        "servers": [{"url": "https://old.example.com"}],
        "paths": {"/users": {"get": {"summary": "old"}}},
    }

    def _boom(*args, **kwargs):
        raise AssertionError("nothing changed, nothing may be generated")

    monkeypatch.setattr(rsg, "_load_existing_swagger", lambda: existing_swagger)
    monkeypatch.setattr(rsg, "_load_existing_api_index", lambda: {"GET /users": {"files": []}})
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: set())
    monkeypatch.setattr(rsg, "_swagger_fragment_for_endpoint", _boom)

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
        _job("/health", "GET", "/repo/other.go"),
        _job("/ping", "GET", "/repo/other.go"),
    ]
    index = {
        rsg._endpoint_key(job["route"], job["http_method"]): {
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
    _stub_context(monkeypatch, [["// shared helper\n"]])
    monkeypatch.setattr(
        rsg, "get_batch_definition_swagger", lambda entries, blocks, source_file: BATCH_REPLY
    )

    generated, failed = rsg._update_swagger_for_batches({"paths": {}}, "/repo", jobs)
    index = rsg._build_api_index(generated)

    assert (len(generated), len(failed)) == (2, 0)
    assert index["GET /users"]["context_hash"] == rsg._endpoint_context_hash("/repo", jobs[0])
    assert index["POST /orders"]["context_hash"] == rsg._endpoint_context_hash("/repo", jobs[1])
    # The recipe covers the endpoint's own prompt text and not its label, so two
    # endpoints a stub hands identical bodies and context to hash alike. Real
    # endpoints are kept apart by their bodies, and a rails PUT mirroring a
    # PATCH relies on exactly this.
    assert index["GET /users"]["context_hash"] == index["POST /orders"]["context_hash"]


def test_unchanged_context_hash_skips_the_llm(monkeypatch, capsys):
    """A file git reports as changed whose endpoints read the same as last run
    costs no LLM call at all."""
    jobs, index = _incremental_fixture(monkeypatch)
    for job in jobs[:2]:
        key = rsg._endpoint_key(job["route"], job["http_method"])
        index[key]["context_hash"] = rsg._endpoint_context_hash("/repo", job)

    swagger, written, calls = _run_incremental(monkeypatch, jobs, index, {"/repo/app.go"})

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

    swagger, written, calls = _run_incremental(monkeypatch, jobs, index, {"/repo/app.go"})

    assert calls == [["POST /orders"]]
    assert swagger["paths"]["/orders"] == {"post": {"summary": "regenerated"}}
    assert swagger["paths"]["/users"] == {"get": {"summary": "stored"}}
    assert written["POST /orders"]["context_hash"] == rsg._endpoint_context_hash("/repo", jobs[1])
    assert "apimesh: skipped 1 unchanged endpoints (context hash match)" in capsys.readouterr().out


def test_endpoint_without_a_stored_hash_always_regenerates(monkeypatch, capsys):
    """An index written before hashing existed carries no hash to match."""
    jobs, index = _incremental_fixture(monkeypatch)

    _, written, calls = _run_incremental(monkeypatch, jobs, index, {"/repo/app.go"})

    # One call for the file, carrying both of its endpoints (the dirty set is a
    # set, so the order the two land in is not part of the contract).
    assert [sorted(labels) for labels in calls] == [["GET /users", "POST /orders"]]
    assert written["GET /users"]["context_hash"] == rsg._endpoint_context_hash("/repo", jobs[0])
    assert "context hash match" not in capsys.readouterr().out


def test_a_changed_dependency_file_makes_the_endpoint_dirty(monkeypatch):
    """An edit one hop away, in a file the handler imports, still regenerates it."""
    jobs, index = _incremental_fixture(monkeypatch)
    index["GET /users"]["files"][0]["imports"] = [{"file_path": "/repo/helpers.go"}]

    # Only the imported file was touched, every handler file is untouched.
    _, _, calls = _run_incremental(monkeypatch, jobs, index, {"/repo/helpers.go"})

    assert calls == [["GET /users"]]


RELOCATION_MAIN = """package main

import (
	"github.com/gin-gonic/gin"

	"example.com/app/handlers"
)

func RegisterRoutes(r *gin.Engine) {
	r.GET("/users", handlers.ListUsers)
	r.GET("/health", handlers.Health)
}
"""

RELOCATION_LIST_USERS = """package handlers

import "github.com/gin-gonic/gin"

func ListUsers(c *gin.Context) {
	c.JSON(200, []string{})
}
"""

RELOCATION_HEALTH = """package handlers

import "github.com/gin-gonic/gin"

func Health(c *gin.Context) {
	c.JSON(200, "ok")
}
"""


def _relocation_jobs(repo: Path) -> list:
    """One route registration per line, the way the extractor reports them.

    Two keys is the smallest repo where one dirty endpoint stays under the
    escalation gate.
    """
    main_go = str(repo / "main.go")
    return [
        {"route": "/users", "http_method": "GET", "file_path": main_go,
         "start_line": 10, "end_line": 10},
        {"route": "/health", "http_method": "GET", "file_path": main_go,
         "start_line": 11, "end_line": 11},
    ]


def _relocation_pass(monkeypatch, repo: Path, out_dir: Path, changed_files, summary):
    """One incremental pass over the repo as it is on disk right now."""
    calls = []

    def fake_batch(entries, blocks, source_file):
        calls.append([label for label, _ in entries])
        return {"paths": {"/users": {"get": {"summary": summary}}}}

    monkeypatch.setattr(rsg, "get_batch_definition_swagger", fake_batch)
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: changed_files)
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "head")
    # Two runs in one process are two checkouts: nothing may be read from the
    # cache the previous run filled.
    rsg._reset_caches()
    _write_metadata(repo)

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
    """A handler that moves to a file with identical text keeps the prompt, and
    so the hash, exactly as it was, so the endpoint skips. Its index entry still
    has to follow the handler: keeping the old one points the dependency hop at
    the file the handler left, and every later edit to the file it moved to is
    invisible, so the endpoint is documented from stale source forever.
    """
    repo = tmp_path / "repo"
    (repo / "handlers").mkdir(parents=True)
    (repo / "go.mod").write_text("module example.com/app\n\ngo 1.22\n", encoding="utf-8")
    (repo / "main.go").write_text(RELOCATION_MAIN, encoding="utf-8")
    (repo / "handlers" / "users.go").write_text(RELOCATION_LIST_USERS, encoding="utf-8")
    (repo / "handlers" / "health.go").write_text(RELOCATION_HEALTH, encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "swagger.json").write_text(
        json.dumps({"info": {"commit_reference": "base"}, "paths": {}}), encoding="utf-8"
    )
    (out_dir / "api_index.json").write_text(
        json.dumps({"GET /health": {"files": [{"file_path": str(repo / "main.go"), "imports": []}]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(out_dir / "swagger.json"))

    index, calls = _relocation_pass(monkeypatch, repo, out_dir, set(), "documented")

    assert calls == [["GET /users"]]
    assert _imported_paths(index["GET /users"]) == [str(repo / "handlers" / "users.go")]
    stored_hash = index["GET /users"]["context_hash"]

    # The handler moves to another file of the same package, reading exactly the
    # same, so the route registration it is reached from never changes.
    (repo / "handlers" / "users.go").unlink()
    (repo / "handlers" / "list_users.go").write_text(RELOCATION_LIST_USERS, encoding="utf-8")

    index, calls = _relocation_pass(
        monkeypatch,
        repo,
        out_dir,
        {os.path.abspath(str(repo / "handlers" / "users.go"))},
        "never asked for",
    )

    assert calls == []
    assert index["GET /users"]["context_hash"] == stored_hash
    assert _imported_paths(index["GET /users"]) == [str(repo / "handlers" / "list_users.go")]

    # The edit the stale entry used to hide: the file the handler moved to.
    (repo / "handlers" / "list_users.go").write_text(
        RELOCATION_LIST_USERS.replace('[]string{}', '[]string{"a"}'), encoding="utf-8"
    )

    index, calls = _relocation_pass(
        monkeypatch,
        repo,
        out_dir,
        {os.path.abspath(str(repo / "handlers" / "list_users.go"))},
        "redocumented",
    )

    assert calls == [["GET /users"]]
    assert index["GET /users"]["context_hash"] != stored_hash


def test_escalation_gate_hands_the_run_back_for_a_full_regeneration(monkeypatch, capsys):
    """Past half the endpoints the surgical path is the slow one."""
    jobs, index = _incremental_fixture(monkeypatch)
    # A third endpoint moves into the changed file, so three of four are dirty.
    jobs[2]["file_path"] = "/repo/app.go"
    index["GET /health"]["files"][0]["file_path"] = "/repo/app.go"

    result, written, calls = _run_incremental(monkeypatch, jobs, index, {"/repo/app.go"})

    assert result is None
    assert calls == []
    # The full run owns the spec and the index from here, nothing is half done.
    assert written == {}
    assert "apimesh: 3 of 4 endpoints affected, running a full regeneration" in capsys.readouterr().out


NON_ROUTER_VERB_CALLS = """package main

import "github.com/gin-gonic/gin"

func getUser(c *gin.Context) {}

func RegisterRoutes(r *gin.Engine) {
	var user User
	db.Get(&user, "SELECT * FROM users WHERE id = ?", id)
	cache.Get(ctx, "user:42")
	traceID := req.Header.Get("X-Trace-Id")
	r.GET("/users/:id", getUser)
	v1 := r.Group("/api/v1")
	v1.POST("/users", getUser)
}
"""


def test_verb_calls_that_register_nothing_are_rejected(tmp_path):
    """db.Get(&user, "SELECT ...") is not an endpoint. The path literal has to
    be the first argument and has to look like a path."""
    assert _routes(tmp_path, NON_ROUTER_VERB_CALLS) == {
        ("GET", "/users/:id"),
        ("POST", "/api/v1/users"),
    }
    assert extraction_drops() == {"rejected": 3, "no_handler": 0}


MISSING_HANDLER = """package main

import "github.com/gin-gonic/gin"

func RegisterRoutes(r *gin.Engine) {
	r.GET("/lonely")
	r.POST("/typo", "notAHandler")
}
"""


def test_a_route_with_no_handler_argument_is_dropped(tmp_path):
    assert _routes(tmp_path, MISSING_HANDLER) == set()
    assert extraction_drops() == {"rejected": 0, "no_handler": 2}


def test_extraction_drops_are_reported_in_one_line(tmp_path, capsys):
    _routes(tmp_path, NON_ROUTER_VERB_CALLS)
    log_extraction_drops()

    output = capsys.readouterr().out
    assert (
        "apimesh: golang extraction skipped 3 calls that register no route "
        "and 0 routes with no usable handler" in output
    )
    assert output.count("golang extraction skipped") == 1


def test_nothing_is_reported_when_nothing_was_dropped(tmp_path, capsys):
    _routes(tmp_path, GIN_GROUP)
    log_extraction_drops()

    assert capsys.readouterr().out == ""


CHI_ROUTE_CLOSURES = """package main

import "github.com/go-chi/chi/v5"

func listUsers(w http.ResponseWriter, r *http.Request) {}

func RegisterRoutes(r chi.Router) {
	r.Route("/v2", func(r chi.Router) {
		r.Get("/users", listUsers)
		r.Route("/admin", func(r chi.Router) {
			r.Post("/users", listUsers)
		})
	})
}
"""


def test_chi_route_closures_compose_their_prefixes(tmp_path):
    """A chi Route closure pushes its prefix onto everything registered inside
    it, and a nested Route composes on top of the outer one."""
    assert _routes(tmp_path, CHI_ROUTE_CLOSURES) == {
        ("GET", "/v2/users"),
        ("POST", "/v2/admin/users"),
    }


CHI_MOUNT = """package main

import "github.com/go-chi/chi/v5"

func listUsers(w http.ResponseWriter, r *http.Request) {}

func adminRouter() chi.Router {
	r := chi.NewRouter()
	r.Get("/users", listUsers)
	return r
}

func RegisterRoutes(root chi.Router) {
	root.Mount("/admin", adminRouter())
}
"""


def test_chi_mount_prefixes_an_in_file_router(tmp_path):
    assert _routes(tmp_path, CHI_MOUNT) == {("GET", "/admin/users")}


CHI_MOUNT_ELSEWHERE = """package main

import "github.com/go-chi/chi/v5"

func listUsers(w http.ResponseWriter, r *http.Request) {}

func RegisterRoutes(root chi.Router) {
	root.Mount("/admin", external.Router())
}
"""


def test_an_unresolvable_mount_invents_nothing(tmp_path):
    """The mounted router is not defined here, so there is nothing to prefix."""
    assert _routes(tmp_path, CHI_MOUNT_ELSEWHERE) == set()


SHADOWED_GROUP = """package main

import "github.com/gin-gonic/gin"

func listUsers(c *gin.Context) {}

func RegisterRoutes(r *gin.Engine) {
	a := r.Group("/outer")
	a.GET("/users", listUsers)
	{
		a := r.Group("/inner")
		a.GET("/teams", listUsers)
	}
}
"""


def test_an_inner_block_group_shadows_the_outer_one(tmp_path):
    """Blocks are scopes in Go, so the inner a is a different router."""
    assert _routes(tmp_path, SHADOWED_GROUP) == {
        ("GET", "/outer/users"),
        ("GET", "/inner/teams"),
    }


CLOSURE_PARAMETER_SHADOWS = """package main

import "github.com/go-chi/chi/v5"

func listUsers(w http.ResponseWriter, r *http.Request) {}

func RegisterRoutes(r chi.Router) {
	api := r.Group("/api")
	api.Route("/v1", func(api chi.Router) {
		api.Get("/users", listUsers)
	})
}
"""


def test_a_closure_parameter_shadows_a_group_of_the_same_name(tmp_path):
    """The inner api is the closure's own router, so its prefix is the
    closure's, not the outer group's on top of it."""
    assert _routes(tmp_path, CLOSURE_PARAMETER_SHADOWS) == {
        ("GET", "/api/v1/users")
    }


STRUCT_METHOD_HANDLERS = """package main

import "github.com/gin-gonic/gin"

type Server struct{}

type Admin struct{}

func (s *Server) CreateUser(c *gin.Context) {
	_ = s
}

func (a *Admin) CreateUser(c *gin.Context) {
	_ = a
}

func RegisterRoutes(r *gin.Engine, s *Server) {
	admin := &Admin{}
	r.POST("/users", s.CreateUser)
	r.POST("/admin/users", admin.CreateUser)
}
"""


def test_struct_method_handlers_resolve_by_receiver(tmp_path):
    """Two methods share a name, so the receiver's type is what tells them
    apart, and the wrong one would document the wrong handler."""
    by_route = {
        endpoint["route"]: endpoint
        for endpoint in _endpoints(tmp_path, STRUCT_METHOD_HANDLERS)
    }

    assert set(by_route) == {"/users", "/admin/users"}
    assert by_route["/users"]["handler_name"] == "CreateUser"
    assert by_route["/users"]["start_line"] == 9
    assert by_route["/admin/users"]["start_line"] == 13
    assert by_route["/users"]["file_path"] == str(tmp_path / "routes.go")


GORILLA_CHAIN = """package main

import "github.com/gorilla/mux"

func createItem(w http.ResponseWriter, r *http.Request) {}

func RegisterRoutes(m *mux.Router) {
	m.HandleFunc("/items", createItem).Queries("draft", "{draft}").Methods("POST")
	m.HandleFunc("/items/{id}", createItem).Methods("PUT", "PATCH")
	m.HandleFunc("/health", createItem)
}
"""


def test_a_gorilla_chain_keeps_its_verb_past_other_calls(tmp_path):
    """A Queries call between HandleFunc and Methods used to swallow the verb
    and leave the route documented as a GET."""
    assert _routes(tmp_path, GORILLA_CHAIN) == {
        ("POST", "/items"),
        ("PUT", "/items/{id}"),
        ("PATCH", "/items/{id}"),
        ("GET", "/health"),
    }


GO_122_PATTERNS = """package main

import "net/http"

func listItems(w http.ResponseWriter, r *http.Request) {}

func RegisterRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /items", listItems)
	mux.Handle("POST /items/{id}", listItems)
	mux.HandleFunc("/legacy", listItems)
}
"""


def test_go_122_method_prefixed_patterns_are_split(tmp_path):
    """net/http serves every verb for a bare pattern, so that one stays GET."""
    assert _routes(tmp_path, GO_122_PATTERNS) == {
        ("GET", "/items"),
        ("POST", "/items/{id}"),
        ("GET", "/legacy"),
    }


ANY_ROUTE = """package main

import "github.com/gin-gonic/gin"

func proxy(c *gin.Context) {}

func RegisterRoutes(r *gin.Engine) {
	r.Any("/proxy", proxy)
}
"""


def test_any_expands_to_the_standard_verbs(tmp_path):
    """OpenAPI has no "any" operation, so one route per verb is emitted."""
    assert _routes(tmp_path, ANY_ROUTE) == {
        (verb, "/proxy")
        for verb in ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD")
    }


INLINE_HANDLER = """package main

import "github.com/gin-gonic/gin"

func RegisterRoutes(r *gin.Engine) {
	r.GET("/ping", func(c *gin.Context) {
		c.JSON(200, gin.H{"pong": true})
	})
}
"""


def test_an_inline_handler_is_documented_from_its_own_body(tmp_path):
    endpoints = _endpoints(tmp_path, INLINE_HANDLER)

    assert [(e["http_method"], e["route"]) for e in endpoints] == [("GET", "/ping")]
    assert endpoints[0]["file_path"] == str(tmp_path / "routes.go")
    assert (endpoints[0]["start_line"], endpoints[0]["end_line"]) == (6, 8)


CROSS_PACKAGE_MAIN = """package main

import (
	"github.com/gin-gonic/gin"

	"example.com/app/handlers"
)

func RegisterRoutes(r *gin.Engine) {
	r.GET("/users", handlers.ListUsers)
}
"""

CROSS_PACKAGE_HANDLERS = """package handlers

import "github.com/gin-gonic/gin"

type UserPayload struct {
	Name string `json:"name"`
}

func ListUsers(c *gin.Context) {
	c.JSON(200, []UserPayload{})
}
"""


def _cross_package_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "handlers").mkdir(parents=True)
    (repo / "go.mod").write_text("module example.com/app\n\ngo 1.22\n", encoding="utf-8")
    (repo / "main.go").write_text(CROSS_PACKAGE_MAIN, encoding="utf-8")
    (repo / "handlers" / "users.go").write_text(
        CROSS_PACKAGE_HANDLERS, encoding="utf-8"
    )
    return repo


def _prepare_run(monkeypatch, repo: Path, output_dir: Path) -> Path:
    """The two paths every run reads: the repo scanned and where output lands."""
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    output_filepath = output_dir / "swagger.json"
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(output_filepath))
    return output_filepath


def _write_metadata(repo: Path) -> None:
    """The per file metadata the run writes before anything reads the index."""
    rsg._CONTENT_HASHES.clear()
    rsg._METADATA_ENTRIES.clear()
    rsg._build_metadata_cache(str(repo))


def test_a_cross_package_handler_reaches_the_prompt(tmp_path, monkeypatch):
    """The import names the package, the call site names the function. Without
    the call site the pipeline compared "handlers" against function names and
    every cross-package handler was documented from its route line alone."""
    repo = _cross_package_repo(tmp_path)
    _prepare_run(monkeypatch, repo, tmp_path / "out")
    _write_metadata(repo)

    blocks, definition = rsg.provide_context_codeblock(
        str(repo),
        {"file_path": str(repo / "main.go"), "start_line": 9, "end_line": 11},
    )
    context = "\n".join("".join(block) for block in blocks)

    assert "r.GET(\"/users\", handlers.ListUsers)" in "".join(definition)
    assert "func ListUsers(c *gin.Context)" in context
    assert "type UserPayload struct" in context


def test_a_cross_package_handler_is_indexed_as_a_dependency(tmp_path, monkeypatch):
    """The same resolution feeds the api_index, so an edit in the handler's own
    package marks the endpoint stale on the next run."""
    repo = _cross_package_repo(tmp_path)
    _prepare_run(monkeypatch, repo, tmp_path / "out")
    _write_metadata(repo)

    metadata = rsg._load_file_metadata(str(repo / "main.go"))
    _, imported = rsg.get_dependencies(metadata, 9, 11, str(repo / "main.go"))

    assert [item["used_names"] for item in imported] == [["ListUsers"]]
    resolved = rsg._resolve_imported_definitions(imported[0], "/users")
    assert [(item["name"], item["file_path"]) for item in resolved] == [
        ("ListUsers", str(repo / "handlers" / "users.go"))
    ]


def test_grouped_imports_are_collected(tmp_path):
    """Go's grouped import block nests its specs one level deeper, and missing
    them left every idiomatic file with no imports at all."""
    repo = _cross_package_repo(tmp_path)

    info = process_file(str(repo / "main.go"), str(repo))

    assert {item["imported_name"] for item in info["imports"]} == {"gin", "handlers"}


def test_metadata_filenames_fit_the_filesystem_limit():
    """Expanding every separator of a deep path blew past NAME_MAX and the
    metadata write failed for exactly the deepest files."""
    deep = "/" + "a-rather-long-directory-name/" * 30 + "handlers.go"

    name = rsg._metadata_cache_filename(deep, "0123456789abcdef")

    assert len(name.encode("utf-8")) <= 255
    assert name.startswith("handlers_")


def test_duplicate_registrations_cost_one_job(tmp_path):
    """The same route registered twice, in either spelling, is one LLM call."""
    source = """package main

import "github.com/gin-gonic/gin"

func getUser(c *gin.Context) {}

func RegisterRoutes(r *gin.Engine) {
	r.GET("/users/:id", getUser)
	r.GET("/users/{id}", getUser)
}
"""
    endpoints = _endpoints(tmp_path, source)
    assert len(endpoints) == 2

    jobs = rsg._dedupe_endpoint_jobs(endpoints)

    assert len(jobs) == 1
    assert len(rsg._batch_endpoint_jobs(jobs)) == 1


def test_a_second_run_starts_from_empty_caches(tmp_path, monkeypatch):
    """Two runs in one process see two checkouts; a cached file body from the
    first would be read as the second's."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    _prepare_run(monkeypatch, repo, tmp_path / "out")
    monkeypatch.setattr(rsg, "get_repo_path", lambda: str(repo))
    monkeypatch.setattr(rsg, "get_repo_name", lambda: "repo")
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "abc123")
    monkeypatch.setattr(rsg, "get_github_repo_url", lambda: "https://example.com/repo")
    rsg._FILE_CONTENT_CACHE["/stale/app.go"] = ["stale\n"]
    rsg._FUNCTION_INDEX_CACHE["staleHandler"] = [{"file_path": "/stale/app.go"}]
    monkeypatch.setattr(rsg, "_FUNCTION_INDEX_CACHE_ROOT", "/stale")

    # No endpoints, so the run stops before any LLM call.
    assert rsg.run_swagger_generation("https://api.example.com") is None

    assert rsg._FILE_CONTENT_CACHE == {}
    assert rsg._FUNCTION_INDEX_CACHE == {}
    assert rsg._FUNCTION_INDEX_CACHE_ROOT is None


PACKAGE_SELECTOR_HANDLER = """package main

import (
	"github.com/gin-gonic/gin"

	"example.com/app/handlers"
)

type Server struct{}

func (s *Server) ListUsers(c *gin.Context) {}

func RegisterRoutes(r *gin.Engine) {
	r.GET("/users", handlers.ListUsers)
}
"""


def test_a_package_selector_handler_is_left_for_repo_wide_resolution(tmp_path):
    """handlers.ListUsers is another package's function, so the local method of
    the same name must not be documented in its place."""
    endpoints = _endpoints(tmp_path, PACKAGE_SELECTOR_HANDLER)

    assert len(endpoints) == 1
    assert endpoints[0]["handler_name"] == "ListUsers"
    assert endpoints[0]["file_path"] is None


CHI_MOUNT_VARIABLE = """package main

import "github.com/go-chi/chi/v5"

func listUsers(w http.ResponseWriter, r *http.Request) {}

func RegisterRoutes(root chi.Router) {
	admin := chi.NewRouter()
	admin.Get("/users", listUsers)
	root.Mount("/admin", admin)
}
"""


def test_chi_mount_prefixes_a_router_built_in_a_variable(tmp_path):
    """The router is assembled first and mounted after, so the prefix has to
    reach routes registered above the Mount call."""
    assert _routes(tmp_path, CHI_MOUNT_VARIABLE) == {("GET", "/admin/users")}


def test_metadata_filenames_survive_a_long_basename():
    """A single path component can fill NAME_MAX on its own."""
    long_name = "/repo/" + "a" * 235 + ".go"

    name = rsg._metadata_cache_filename(long_name, "0123456789abcdef")

    assert len(name.encode("utf-8")) <= 255


TWO_PACKAGE_MAIN = """package main

import (
	"github.com/gin-gonic/gin"

	"example.com/app/billing"
)

func RegisterRoutes(r *gin.Engine) {
	r.GET("/invoices", billing.List)
}
"""


def test_a_cross_package_handler_resolves_inside_its_own_package(tmp_path, monkeypatch):
    """Two packages export List. The import alias says which one this route
    registered, so the other one's body must not be documented."""
    repo = tmp_path / "repo"
    (repo / "billing").mkdir(parents=True)
    (repo / "accounts").mkdir()
    (repo / "go.mod").write_text("module example.com/app\n\ngo 1.22\n", encoding="utf-8")
    (repo / "main.go").write_text(TWO_PACKAGE_MAIN, encoding="utf-8")
    (repo / "accounts" / "list.go").write_text(
        "package accounts\n\nfunc List() {}\n", encoding="utf-8"
    )
    (repo / "billing" / "list.go").write_text(
        "package billing\n\nfunc List() {}\n", encoding="utf-8"
    )
    _prepare_run(monkeypatch, repo, tmp_path / "out")
    monkeypatch.setattr(rsg, "_FUNCTION_INDEX_CACHE", {})
    monkeypatch.setattr(rsg, "_FUNCTION_INDEX_CACHE_ROOT", None)
    _write_metadata(repo)

    endpoints = find_api_endpoints(repo / "main.go", str(repo))
    hydrated = rsg._hydrate_method_info(str(repo), endpoints[0])

    assert hydrated["file_path"] == str(repo / "billing" / "list.go")


TYPED_RECEIVER_MAIN = """package main

import "github.com/gin-gonic/gin"

func RegisterRoutes(r *gin.Engine) {
	var users UserService
	orders := OrderService{}
	r.GET("/users", users.List)
	r.GET("/orders", orders.List)
}
"""

USER_SERVICE = """package main

import "github.com/gin-gonic/gin"

type UserService struct{}

func (s UserService) List(c *gin.Context) {}
"""

ORDER_SERVICE = """package main

import "github.com/gin-gonic/gin"

type OrderService struct{}

func (s *OrderService) List(c *gin.Context) {}
"""


def test_a_method_handler_hydrates_from_its_receiver_type(tmp_path, monkeypatch):
    """Two types both have a List, and both live outside the route file. The
    call site says which one the route registered, so the repo wide index must
    not hand back whichever it reached first."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/app\n\ngo 1.22\n", encoding="utf-8")
    (repo / "main.go").write_text(TYPED_RECEIVER_MAIN, encoding="utf-8")
    (repo / "users.go").write_text(USER_SERVICE, encoding="utf-8")
    (repo / "orders.go").write_text(ORDER_SERVICE, encoding="utf-8")
    _prepare_run(monkeypatch, repo, tmp_path / "out")
    monkeypatch.setattr(rsg, "_FUNCTION_INDEX_CACHE", {})
    monkeypatch.setattr(rsg, "_FUNCTION_INDEX_CACHE_ROOT", None)
    _write_metadata(repo)

    endpoints = find_api_endpoints(repo / "main.go", str(repo))
    hydrated = {
        endpoint["route"]: rsg._hydrate_method_info(str(repo), endpoint)
        for endpoint in endpoints
    }

    assert hydrated["/users"]["file_path"] == str(repo / "users.go")
    assert hydrated["/orders"]["file_path"] == str(repo / "orders.go")


CONSTRUCTOR_RECEIVER = """package main

import "github.com/gin-gonic/gin"

func RegisterRoutes(r *gin.Engine) {
	svc := NewUserService()
	r.GET("/users", svc.List)
}
"""


def test_a_receiver_of_unknown_type_claims_nothing(tmp_path):
    """svc := NewUserService() does not name a type in this file, so the
    selector text stays the only hint hydration has to go on."""
    endpoints = _endpoints(tmp_path, CONSTRUCTOR_RECEIVER)

    assert endpoints[0]["handler_receiver_type"] is None
    assert endpoints[0]["handler_selector"] == "svc.List"


def test_the_module_name_cache_is_reset_between_runs(tmp_path, monkeypatch):
    """The same path can hold a different go.mod on the second run."""
    import golang_pipeline.generate_file_information as gfi

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    _prepare_run(monkeypatch, repo, tmp_path / "out")
    monkeypatch.setattr(rsg, "get_repo_path", lambda: str(repo))
    monkeypatch.setattr(rsg, "get_repo_name", lambda: "repo")
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "abc123")
    monkeypatch.setattr(rsg, "get_github_repo_url", lambda: "https://example.com/repo")
    gfi._MODULE_NAME_CACHE[str(repo)] = "example.com/stale"

    assert rsg.run_swagger_generation("https://api.example.com") is None

    assert str(repo) not in gfi._MODULE_NAME_CACHE


CACHE_MAIN = """package main

import "github.com/gin-gonic/gin"

func listUsers(c *gin.Context) {}

func RegisterRoutes(r *gin.Engine) {
	r.GET("/users", listUsers)
}
"""


def _cache_dir(out_dir: Path) -> Path:
    return out_dir / "metadata_cache" / "golang"


def _cache_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "go.mod").write_text("module example.com/app\n\ngo 1.22\n", encoding="utf-8")
    (repo / "main.go").write_text(CACHE_MAIN, encoding="utf-8")
    return repo


def _cache_run(monkeypatch, repo: Path, out_dir: Path):
    """One full run over the repo, with every LLM call answered locally."""
    _prepare_run(monkeypatch, repo, out_dir)
    # The incremental pass would skip the work these tests are about.
    monkeypatch.setattr(rsg, "_maybe_incremental_update", lambda *args: None)
    fragment = {"paths": {"/users": {"get": {"summary": "listed"}}}}
    monkeypatch.setattr(
        rsg, "get_batch_definition_swagger", lambda *args, **kwargs: fragment
    )
    monkeypatch.setattr(
        rsg, "get_function_definition_swagger", lambda *args, **kwargs: fragment
    )
    return rsg.run_swagger_generation("https://api.example.com")


def _count_parses(monkeypatch) -> list:
    """Every file the run actually hands to the parser."""
    parsed = []
    real_process_file = rsg.process_file

    def _counting(file_path, directory_path):
        parsed.append(file_path)
        return real_process_file(file_path, directory_path)

    monkeypatch.setattr(rsg, "process_file", _counting)
    return parsed


def test_metadata_is_cached_outside_the_scanned_repo(tmp_path, monkeypatch):
    """The run built its metadata in a temp directory it then threw away."""
    repo = _cache_repo(tmp_path)
    out_dir = tmp_path / "out"
    before = sorted(path.name for path in repo.iterdir())

    _cache_run(monkeypatch, repo, out_dir)

    entries = sorted(path.name for path in _cache_dir(out_dir).glob("*.json"))
    assert len(entries) == 1 and entries[0].startswith("main_")
    assert sorted(path.name for path in repo.iterdir()) == before


def test_a_second_run_over_unchanged_files_parses_nothing(tmp_path, monkeypatch):
    """The cache is content addressed, so untouched files are never reparsed."""
    repo = _cache_repo(tmp_path)
    out_dir = tmp_path / "out"
    _cache_run(monkeypatch, repo, out_dir)

    parsed = _count_parses(monkeypatch)
    _cache_run(monkeypatch, repo, out_dir)

    assert parsed == []


def test_an_edited_file_is_reparsed_and_its_stale_entry_dropped(tmp_path, monkeypatch):
    """A new content hash means a new entry, and the old one has to go."""
    repo = _cache_repo(tmp_path)
    out_dir = tmp_path / "out"
    _cache_run(monkeypatch, repo, out_dir)
    stale = sorted(_cache_dir(out_dir).glob("*.json"))

    (repo / "main.go").write_text(
        CACHE_MAIN.replace("func listUsers(c *gin.Context) {}",
                           "func listUsers(c *gin.Context) { _ = c }"),
        encoding="utf-8",
    )
    parsed = _count_parses(monkeypatch)
    _cache_run(monkeypatch, repo, out_dir)

    assert parsed == [str(repo / "main.go")]
    assert not stale[0].exists()
    assert len(list(_cache_dir(out_dir).glob("*.json"))) == 1


def test_an_existing_qodex_directory_in_the_repo_is_left_alone(tmp_path, monkeypatch):
    """Nothing inside the scanned repo is created, read back or removed."""
    repo = _cache_repo(tmp_path)
    kept = repo / "qodex_file_information" / "notes.txt"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text("mine\n", encoding="utf-8")
    out_dir = tmp_path / "out"

    _cache_run(monkeypatch, repo, out_dir)

    assert kept.read_text(encoding="utf-8") == "mine\n"


def test_a_stale_version_marker_wipes_the_cache(tmp_path, monkeypatch):
    """Entries written by an older metadata format must not be read back."""
    repo = _cache_repo(tmp_path)
    out_dir = tmp_path / "out"
    _cache_run(monkeypatch, repo, out_dir)
    cache_dir = _cache_dir(out_dir)
    (cache_dir / "cache_version").write_text("0", encoding="utf-8")

    parsed = _count_parses(monkeypatch)
    _cache_run(monkeypatch, repo, out_dir)

    assert parsed == [str(repo / "main.go")]
    assert (cache_dir / "cache_version").read_text(
        encoding="utf-8"
    ) == rsg.METADATA_CACHE_VERSION

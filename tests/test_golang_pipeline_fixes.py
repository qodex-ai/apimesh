"""Regression tests for the golang pipeline audit fixes.

Covers three defects that silently produced wrong or empty specs:

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
from golang_pipeline.identify_api_functions import find_api_endpoints
from golang_pipeline.run_swagger_generation import _normalize_swagger_fragment
from utils import num_tokens_from_string


def _routes(tmp_path: Path, source: str) -> set:
    go_file = tmp_path / "routes.go"
    go_file.parent.mkdir(parents=True, exist_ok=True)
    go_file.write_text(source, encoding="utf-8")
    endpoints = find_api_endpoints(go_file, str(tmp_path))
    return {(endpoint["http_method"], endpoint["route"]) for endpoint in endpoints}


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


def test_failed_endpoints_are_left_out_of_the_api_index(monkeypatch):
    """A failure must stay dirty, otherwise the next run never retries it."""
    endpoints = [
        {"route": "/users", "http_method": "GET", "file_path": "/repo/app.go"},
        {"route": "/orders", "http_method": "POST", "file_path": "/repo/app.go"},
    ]
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
        lambda: {"GET /users": {"files": [{"file_path": "/repo/stale.go", "imports": []}]}},
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
    ]
    # The swagger and the index are the state the pipeline carries between runs.
    swagger = {"info": {"commit_reference": "old"}, "paths": {}}
    index = {}
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
    assert set(index) == {"POST /orders"}

    failing.clear()
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: set())

    rsg._maybe_incremental_update("/repo", endpoints)
    assert swagger["paths"] == {
        "/orders": {"post": {"summary": "/orders"}},
        "/users": {"get": {"summary": "/users"}},
    }
    assert set(index) == {"GET /users", "POST /orders"}


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
    monkeypatch.setattr(rsg, "get_changed_files_since", lambda *args, **kwargs: {"/repo/app.go"})
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "new")
    monkeypatch.setattr(rsg, "_write_api_index", lambda index: written.update(index))

    swagger = rsg._maybe_incremental_update("/repo", jobs)

    assert swagger["paths"] == {"/orders": {"post": {"summary": "create"}}}
    assert set(written) == {"POST /orders"}
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

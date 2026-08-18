"""Regression tests for the nodejs pipeline audit fixes.

Covers express router mount prefixes, ignore-dir matching that used to test the
absolute path, and the fragment re-keying that keeps swagger paths, api index
keys and incremental removals on the same route string. No network is used:
the LLM boundary is never crossed.
"""

import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ["APIMESH_CONFIG_PATH"] = str(REPO_ROOT / "config.yml")

from nodejs_pipeline import run_swagger_generation as run_module
from nodejs_pipeline.find_api_definition_files import find_api_definition_files
from nodejs_pipeline.identify_api_functions import find_api_endpoints_js, join_mount_prefix
from nodejs_pipeline.run_swagger_generation import (
    CONTEXT_TOKEN_BUDGET,
    _batch_endpoint_jobs,
    _build_mount_prefix_map,
    _collect_batch_context,
    _endpoint_key,
    _maybe_incremental_update,
    _merge_paths,
    _remove_endpoint_from_swagger,
    run_swagger_generation,
    should_process_directory,
)
from utils import num_tokens_from_string


@pytest.fixture(autouse=True)
def _stub_batch_llm(monkeypatch):
    """
    No test may reach the network. The batch call is stubbed to an unusable
    reply by default, which is what makes a test that only stubs the per
    endpoint call exercise the fallback path. Tests override it as needed.
    """
    monkeypatch.setattr(run_module, "get_batch_definition_swagger", lambda *args, **kwargs: None)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _routes(path: Path) -> set:
    return {(endpoint["method"], endpoint["route"]) for endpoint in find_api_endpoints_js(path)}


SAME_FILE_APP = """const express = require('express');
const app = express();
const router = express.Router();

router.get('/users', (req, res) => res.send([]));
router.post('/users', (req, res) => res.send({}));

app.get('/health', (req, res) => res.send('ok'));

app.use(express.json());
app.use('/api/v1', router);

module.exports = app;
"""


def test_same_file_mount_prefix(tmp_path):
    """A router created and mounted in one file carries the mount prefix."""
    app_file = _write(tmp_path / "app.js", SAME_FILE_APP)
    assert _routes(app_file) == {
        ("GET", "/api/v1/users"),
        ("POST", "/api/v1/users"),
        ("GET", "/health"),
    }


UNMOUNTED_APP = """const express = require('express');
const app = express();
const router = express.Router();

router.get('/users', (req, res) => res.send([]));
app.get('/health', (req, res) => res.send('ok'));

app.use(express.json());
app.use(router);

module.exports = app;
"""


def test_unmounted_routes_unchanged(tmp_path):
    """A router mounted without a path prefix leaves its routes alone."""
    app_file = _write(tmp_path / "app.js", UNMOUNTED_APP)
    assert _routes(app_file) == {("GET", "/users"), ("GET", "/health")}


CROSS_FILE_APP = """const express = require('express');
const usersRouter = require('./routes/users');
const app = express();

app.use('/api/v1', usersRouter);

module.exports = app;
"""

CROSS_FILE_ROUTER = """const express = require('express');
const router = express.Router();

router.get('/', (req, res) => res.send([]));
router.get('/:id', (req, res) => res.send({}));

module.exports = router;
"""


def test_cross_file_mount_prefix(tmp_path):
    """A router required and mounted from another file carries that mount prefix."""
    repo = tmp_path / "repo"
    _write(repo / "app.js", CROSS_FILE_APP)
    router_file = _write(repo / "routes" / "users.js", CROSS_FILE_ROUTER)

    prefixes = _build_mount_prefix_map(str(repo))
    prefix = prefixes.get(str(router_file.resolve()))
    assert prefix == "/api/v1"

    mounted = {
        (endpoint["method"], join_mount_prefix(prefix, endpoint["route"]))
        for endpoint in find_api_endpoints_js(router_file)
    }
    assert mounted == {("GET", "/api/v1"), ("GET", "/api/v1/:id")}


INLINE_REQUIRE_APP = """const express = require('express');
const app = express();

app.use('/api/v1', require('./routes/users'));

module.exports = app;
"""


def test_inline_require_mount_prefix(tmp_path):
    """app.use('/api/v1', require('./routes/users')) resolves the mounted module."""
    repo = tmp_path / "repo"
    _write(repo / "app.js", INLINE_REQUIRE_APP)
    router_file = _write(repo / "routes" / "users.js", CROSS_FILE_ROUTER)

    prefixes = _build_mount_prefix_map(str(repo))
    assert prefixes.get(str(router_file.resolve())) == "/api/v1"


def test_cross_file_mount_map_empty_without_mounts(tmp_path):
    """No X.use('<prefix>', ident) anywhere means no file gets a prefix."""
    repo = tmp_path / "repo"
    _write(repo / "app.js", UNMOUNTED_APP)
    assert _build_mount_prefix_map(str(repo)) == {}


SERVER_JS = """const express = require('express');
const app = express();
app.get('/health', (req, res) => res.send('ok'));
module.exports = app;
"""


def test_ignored_dirs_match_relative_path_only(tmp_path):
    """A repo whose absolute path contains an ignored component still gets scanned."""
    repo = tmp_path / "build" / "my-repo"
    server = _write(repo / "src" / "server.js", SERVER_JS)
    _write(repo / "node_modules" / "pkg" / "index.js", SERVER_JS)

    found = find_api_definition_files(str(repo))
    assert [Path(path).resolve() for path in found] == [server.resolve()]


def test_should_process_directory_matches_relative_path_only():
    root = "/home/ci/build/my-repo"
    assert should_process_directory(f"{root}/src/server.js", root)
    assert not should_process_directory(f"{root}/node_modules/pkg/index.js", root)


def test_merge_paths_rekeys_fragment_to_extracted_route():
    """The model's own path and method keys are discarded."""
    target = {"paths": {}}
    fragment = {"paths": {"/api/v1/users/{userId}": {"post": {"summary": "List users"}}}}
    method_info = {"route": "/users/:id", "method": "GET"}

    assert _merge_paths(target, fragment, method_info) is True
    assert target["paths"] == {"/users/{id}": {"get": {"summary": "List users"}}}


def test_merge_paths_renames_legacy_operation_fields():
    """Bare custom keys are not valid OpenAPI 3.0; a value already re-keyed wins."""
    target = {"paths": {}}
    fragment = {
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
    }

    assert _merge_paths(target, fragment, {"route": "/users", "method": "GET"}) is True
    assert target["paths"] == {
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


def test_merge_paths_prefers_the_http_verb_over_a_non_operation_key():
    """A path item may hold `parameters`; the verb body must win over that list."""
    target = {"paths": {}}
    fragment = {
        "paths": {
            "/users": {
                "parameters": [{"name": "id", "in": "path"}],
                "get": {"summary": "List users"},
            }
        }
    }
    assert _merge_paths(target, fragment, {"route": "/users", "method": "GET"}) is True
    assert target["paths"] == {"/users": {"get": {"summary": "List users"}}}


def test_merge_paths_rejects_a_fragment_with_no_http_verb():
    """A vendor extension is a dict, but it is not an operation body."""
    target = {"paths": {}}
    fragment = {"paths": {"/x": {"x-metadata": {"owner": "payments"}}}}

    assert _merge_paths(target, fragment, {"route": "/x", "method": "GET"}) is False
    assert target["paths"] == {}


def test_merge_paths_rejects_unusable_fragments():
    target = {"paths": {}}
    method_info = {"route": "/users", "method": "GET"}
    for fragment in (
        None,
        {},
        {"paths": {}},
        {"paths": None},
        {"paths": {"/users": {}}},
        {"paths": {"/users": {"parameters": [{"name": "id"}]}}},
    ):
        assert _merge_paths(target, fragment, method_info) is False
    assert target["paths"] == {}


def test_merge_paths_requires_route_and_method():
    fragment = {"paths": {"/users": {"get": {"summary": "x"}}}}
    assert _merge_paths({"paths": {}}, fragment, {"route": None, "method": "GET"}) is False
    assert _merge_paths({"paths": {}}, fragment, {"route": "/users", "method": None}) is False


def test_endpoint_key_and_removal_use_the_merged_route():
    """The api index key and the removal lookup must hit the path merge wrote."""
    swagger = {"paths": {}}
    method_info = {"route": "/users/:id", "method": "DELETE"}
    assert _merge_paths(swagger, {"paths": {"/x": {"get": {}}}}, method_info) is True

    key = _endpoint_key(method_info["route"], method_info["method"])
    assert key == "DELETE /users/{id}"

    _remove_endpoint_from_swagger(swagger, key)
    assert swagger["paths"] == {}


ROUTER_JS = """const express = require('express');
const router = express.Router();
router.get('/orders', (req, res) => res.send([]));
module.exports = router;
"""


DEFAULT_JOB_SPECS = [("GET", "/orders", 3, 3)]


def _incremental_fixture(
    tmp_path,
    monkeypatch,
    existing_index,
    existing_paths=None,
    source_text=ROUTER_JS,
    job_specs=DEFAULT_JOB_SPECS,
):
    """Stage an existing swagger plus api index so the incremental path runs."""
    repo = tmp_path / "repo"
    source = _write(repo / "routes.js", source_text)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    swagger_path = out_dir / "swagger.json"
    swagger_path.write_text(
        json.dumps({"info": {"commit_reference": "base"}, "paths": existing_paths or {}}),
        encoding="utf-8",
    )
    index_path = out_dir / "api_index.json"
    index_path.write_text(json.dumps(existing_index), encoding="utf-8")

    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(swagger_path))
    monkeypatch.setattr(
        run_module, "get_changed_files_since", lambda *args, **kwargs: {os.path.abspath(str(source))}
    )
    monkeypatch.setattr(run_module, "get_git_commit_hash", lambda: "head")
    jobs = [
        {
            "type": "function",
            "method": method,
            "route": route,
            "file_path": str(source),
            "start_line": start_line,
            "end_line": end_line,
        }
        for method, route, start_line, end_line in job_specs
    ]
    return repo, source, index_path, jobs


def test_failed_endpoint_keeps_its_index_entry_dirty(tmp_path, monkeypatch):
    """A failed generation must not refresh the index, or it is never retried."""
    stale_index = {"GET /orders": {"files": []}}
    repo, _, index_path, jobs = _incremental_fixture(tmp_path, monkeypatch, stale_index)

    def _boom(*args, **kwargs):
        raise RuntimeError("llm call failed")

    monkeypatch.setattr(run_module, "get_function_definition_swagger", _boom)

    swagger = _maybe_incremental_update(str(repo), jobs)
    assert swagger is not None
    assert swagger["paths"] == {}
    # The failed key is dropped from the index entirely: kept stale entries
    # stopped retries once the commit reference advanced, an absent key reads
    # as newly added next run.
    assert json.loads(index_path.read_text(encoding="utf-8")) == {}


def test_successful_endpoint_refreshes_index_and_removals_still_apply(tmp_path, monkeypatch):
    existing_index = {"GET /orders": {"files": []}, "DELETE /gone": {"files": []}}
    repo, source, index_path, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        existing_index,
        existing_paths={"/gone": {"delete": {"summary": "old"}}},
    )
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: {"paths": {"/whatever": {"post": {"summary": "Orders"}}}},
    )

    swagger = _maybe_incremental_update(str(repo), jobs)
    assert swagger["paths"] == {"/orders": {"get": {"summary": "Orders"}}}
    assert json.loads(index_path.read_text(encoding="utf-8")) == {
        "GET /orders": {"files": [{"file_path": os.path.abspath(str(source)), "imports": []}]}
    }


def test_failed_endpoint_is_retried_when_no_files_changed(tmp_path, monkeypatch):
    """A failure has to be retried on the next run even with nothing changed in git."""
    repo, source, index_path, jobs = _incremental_fixture(tmp_path, monkeypatch, {})

    def _boom(*args, **kwargs):
        raise RuntimeError("llm call failed")

    monkeypatch.setattr(run_module, "get_function_definition_swagger", _boom)
    first = _maybe_incremental_update(str(repo), jobs)
    assert first["paths"] == {}
    assert json.loads(index_path.read_text(encoding="utf-8")) == {}

    monkeypatch.setattr(run_module, "get_changed_files_since", lambda *args, **kwargs: set())
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: {"paths": {"/whatever": {"post": {"summary": "Orders"}}}},
    )

    second = _maybe_incremental_update(str(repo), jobs)
    assert second["paths"] == {"/orders": {"get": {"summary": "Orders"}}}
    assert json.loads(index_path.read_text(encoding="utf-8")) == {
        "GET /orders": {"files": [{"file_path": os.path.abspath(str(source)), "imports": []}]}
    }


def test_unchanged_repo_returns_the_existing_swagger_untouched(tmp_path, monkeypatch):
    """Extraction and index agreeing plus no changed files still means no work."""
    existing_index = {"GET /orders": {"files": []}}
    repo, _, index_path, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        existing_index,
        existing_paths={"/orders": {"get": {"summary": "old"}}},
    )
    monkeypatch.setattr(run_module, "get_changed_files_since", lambda *args, **kwargs: set())

    def _boom(*args, **kwargs):
        raise AssertionError("no endpoint should be generated")

    monkeypatch.setattr(run_module, "get_function_definition_swagger", _boom)

    swagger = _maybe_incremental_update(str(repo), jobs)
    assert swagger["paths"] == {"/orders": {"get": {"summary": "old"}}}
    assert json.loads(index_path.read_text(encoding="utf-8")) == existing_index


def test_legacy_route_spellings_are_canonicalized_on_load(tmp_path, monkeypatch):
    """A spec and index written before routes were canonicalized must load in
    the canonical spelling, otherwise the first run after the upgrade reads
    every endpoint as removed and re-added and regenerates all of them."""
    _incremental_fixture(
        tmp_path,
        monkeypatch,
        {
            "GET /users/:id": {"files": [{"file_path": "/repo/legacy.js"}]},
            "GET /users/{id}": {"files": [{"file_path": "/repo/routes.js"}]},
            "POST /users/:id": {"files": []},
        },
        existing_paths={
            "/users/:id": {
                "get": {"summary": "stale"},
                "delete": {"summary": "only on the legacy key"},
            },
            "/users/{id}": {"get": {"summary": "fresh"}},
            "/health": {"get": {"summary": "untouched"}},
        },
    )

    # The canonical key wins, its legacy twin only contributes the missing verb.
    assert run_module._load_existing_swagger()["paths"] == {
        "/users/{id}": {
            "get": {"summary": "fresh"},
            "delete": {"summary": "only on the legacy key"},
        },
        "/health": {"get": {"summary": "untouched"}},
    }

    index = run_module._load_existing_api_index()
    assert set(index) == {"GET /users/{id}", "POST /users/{id}"}
    assert index["GET /users/{id}"]["files"][0]["file_path"] == "/repo/routes.js"


def test_incremental_no_change_return_carries_the_new_host(tmp_path, monkeypatch):
    """--api-host has to reach the spec on the incremental path too, or a run
    that changes the host keeps publishing the previous server url."""
    repo, _, _, jobs = _incremental_fixture(tmp_path, monkeypatch, {"GET /orders": {"files": []}})
    Path(os.environ["APIMESH_OUTPUT_FILEPATH"]).write_text(
        json.dumps(
            {
                "info": {"commit_reference": "base"},
                "servers": [{"url": "https://old.example.com"}],
                "paths": {"/orders": {"get": {"summary": "old"}}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(run_module, "get_changed_files_since", lambda *args, **kwargs: set())
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: pytest.fail("nothing changed, nothing may be generated"),
    )

    swagger = _maybe_incremental_update(str(repo), jobs, "https://new.example.com")
    assert swagger["servers"] == [{"url": "https://new.example.com"}]
    assert swagger["paths"] == {"/orders": {"get": {"summary": "old"}}}


TWO_ROUTE_JS = """const express = require('express');
const router = express.Router();
router.get('/orders', (req, res) => res.send([]));
router.post('/orders', (req, res) => res.send({}));
module.exports = router;
"""

TWO_DIRTY_JOB_SPECS = [("GET", "/orders", 3, 3), ("POST", "/orders", 4, 4)]


def test_incremental_dirty_endpoints_of_one_file_share_a_batch(tmp_path, monkeypatch):
    """Two dirty endpoints of one file cost one call, not one call each."""
    repo, _, index_path, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        {},
        source_text=TWO_ROUTE_JS,
        job_specs=TWO_DIRTY_JOB_SPECS,
    )
    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {
            "paths": {
                "/orders": {"get": {"summary": "List"}, "post": {"summary": "Create"}}
            }
        }

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the per endpoint path must not be used"),
    )

    swagger = _maybe_incremental_update(str(repo), jobs)

    assert len(batch_calls) == 1
    assert set(batch_calls[0].splitlines()) == {"GET /orders", "POST /orders"}
    assert swagger["paths"] == {
        "/orders": {"get": {"summary": "List"}, "post": {"summary": "Create"}}
    }
    assert set(json.loads(index_path.read_text(encoding="utf-8"))) == {
        "GET /orders",
        "POST /orders",
    }


def test_incremental_batch_indexes_only_the_keys_that_generated(tmp_path, monkeypatch):
    """Batching the dirty endpoints together keeps the per key accounting."""
    repo, _, index_path, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        {},
        source_text=TWO_ROUTE_JS,
        job_specs=TWO_DIRTY_JOB_SPECS,
    )
    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {"paths": {"/orders": {"get": {"summary": "List"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    swagger = _maybe_incremental_update(str(repo), jobs)

    assert len(batch_calls) == 1
    assert swagger["paths"] == {"/orders": {"get": {"summary": "List"}}}
    assert set(json.loads(index_path.read_text(encoding="utf-8"))) == {"GET /orders"}


FULL_RUN_APP = """const express = require('express');
const app = express();

app.get('/users', (req, res) => res.send([]));
app.post('/orders', (req, res) => res.send({}));

module.exports = app;
"""


def test_full_run_indexes_only_the_endpoints_that_generated(tmp_path, monkeypatch):
    """A failed endpoint must stay out of a fresh index, or it is never retried."""
    repo = tmp_path / "repo"
    _write(repo / "app.js", FULL_RUN_APP)
    out_dir = tmp_path / "out"
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(out_dir / "swagger.json"))

    def _fake_generation(definition, context, route, http_method=None):
        if route == "/users":
            raise RuntimeError("llm call failed")
        return {"paths": {"/whatever": {"post": {"summary": "Orders"}}}}

    monkeypatch.setattr(run_module, "get_function_definition_swagger", _fake_generation)

    swagger = run_swagger_generation("http://localhost:3000")

    assert swagger["paths"] == {"/orders": {"post": {"summary": "Orders"}}}
    index = json.loads((out_dir / "api_index.json").read_text(encoding="utf-8"))
    assert set(index) == {"POST /orders"}


def test_batches_are_grouped_per_file_and_capped_at_ten(tmp_path):
    """One batch per file, and a file with more than ten endpoints is chunked."""
    jobs = [
        {"file_path": "/repo/routes.js", "route": f"/r{index}", "method": "GET"}
        for index in range(12)
    ]
    jobs.append({"file_path": "/repo/app.js", "route": "/health", "method": "GET"})

    batches = _batch_endpoint_jobs(jobs)

    assert [len(batch) for batch in batches] == [10, 2, 1]
    assert {job["file_path"] for job in batches[0] + batches[1]} == {"/repo/routes.js"}
    assert batches[2][0]["file_path"] == "/repo/app.js"
    assert sum(len(batch) for batch in batches) == len(jobs)


def _sized_handler(name: str, target_tokens: int) -> list:
    """A handler body whose token count lands just past target_tokens."""
    lines = [f"router.get('/{name}', (req, res) => {{\n"]
    while num_tokens_from_string("".join(lines)) < target_tokens:
        lines.append("  const value = compute(payload);\n")
    lines.append("});\n")
    return lines


def _write_sized_handlers(tmp_path, sizes):
    """One .js file of handlers that big, and the jobs pointing at each."""
    source = tmp_path / "handlers.js"
    lines = []
    jobs = []
    for index, target_tokens in enumerate(sizes):
        body = _sized_handler(f"handler{index}", target_tokens)
        start_line = len(lines) + 1
        lines.extend(body)
        jobs.append(
            {
                "file_path": str(source),
                "route": f"/r{index}",
                "method": "GET",
                "start_line": start_line,
                "end_line": len(lines),
            }
        )
    _write(source, "".join(lines))
    return source, jobs


def test_batches_are_packed_to_fit_the_context_budget(tmp_path, monkeypatch):
    """Three 2500 token sections cannot share one 6000 token prompt."""
    monkeypatch.setattr(run_module, "MAX_HANDLER_TOKENS", 4000)
    _, jobs = _write_sized_handlers(tmp_path, [2500, 2500, 2500])

    batches = _batch_endpoint_jobs(jobs)

    assert [len(batch) for batch in batches] == [2, 1]


def test_packed_batches_keep_their_sections_inside_the_budget(tmp_path, monkeypatch):
    """The invariant the packing exists for, read off the prompt sections."""
    monkeypatch.setattr(run_module, "MAX_HANDLER_TOKENS", 4000)
    _, jobs = _write_sized_handlers(tmp_path, [2500, 2500, 2500])
    sections_sent = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        sections_sent.append(per_endpoint_sections)
        return {"paths": {}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    run_module._update_swagger_for_endpoints({"paths": {}}, str(tmp_path), jobs)

    assert len(sections_sent) == 2
    for sections in sections_sent:
        assert num_tokens_from_string(sections) <= CONTEXT_TOKEN_BUDGET


def test_a_single_oversized_endpoint_gets_a_batch_of_its_own(tmp_path, monkeypatch):
    """One section can fill the budget by itself, and still has to be sent."""
    monkeypatch.setattr(run_module, "MAX_HANDLER_TOKENS", 8000)
    _, jobs = _write_sized_handlers(tmp_path, [7000, 100])

    batches = _batch_endpoint_jobs(jobs)

    assert [len(batch) for batch in batches] == [1, 1]
    assert batches[0][0]["route"] == "/r0"


def _batch_run_fixture(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write(repo / "app.js", FULL_RUN_APP)
    out_dir = tmp_path / "out"
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(out_dir / "swagger.json"))
    return out_dir


BATCH_RESPONSE = {
    "paths": {
        "/users": {"get": {"summary": "List users"}},
        "/orders": {"post": {"summary": "Create order"}},
    }
}


def test_one_batch_call_documents_every_endpoint_of_the_file(tmp_path, monkeypatch):
    """Two endpoints in one file cost one LLM call, not two."""
    out_dir = _batch_run_fixture(tmp_path, monkeypatch)
    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return BATCH_RESPONSE

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the per endpoint path must not be used"),
    )

    swagger = run_swagger_generation("http://localhost:3000")

    assert len(batch_calls) == 1
    assert set(batch_calls[0].splitlines()) == {"GET /users", "POST /orders"}
    assert swagger["paths"] == {
        "/users": {"get": {"summary": "List users"}},
        "/orders": {"post": {"summary": "Create order"}},
    }
    index = json.loads((out_dir / "api_index.json").read_text(encoding="utf-8"))
    assert set(index) == {"GET /users", "POST /orders"}


def test_endpoint_missing_from_the_batch_response_fails_alone(tmp_path, monkeypatch):
    """An endpoint the model skipped stays out of the index so it is retried."""
    out_dir = _batch_run_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        run_module,
        "get_batch_definition_swagger",
        lambda *args, **kwargs: {"paths": {"/users": {"get": {"summary": "List users"}}}},
    )
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the per endpoint path must not be used"),
    )

    swagger = run_swagger_generation("http://localhost:3000")

    assert swagger["paths"] == {"/users": {"get": {"summary": "List users"}}}
    index = json.loads((out_dir / "api_index.json").read_text(encoding="utf-8"))
    assert set(index) == {"GET /users"}


def test_extra_endpoints_in_the_batch_response_are_ignored(tmp_path, monkeypatch):
    _batch_run_fixture(tmp_path, monkeypatch)
    response = {
        "paths": dict(BATCH_RESPONSE["paths"], **{"/invented": {"get": {"summary": "nope"}}})
    }
    monkeypatch.setattr(
        run_module, "get_batch_definition_swagger", lambda *args, **kwargs: response
    )

    swagger = run_swagger_generation("http://localhost:3000")

    assert set(swagger["paths"]) == {"/users", "/orders"}


def test_unusable_batch_response_falls_back_to_the_per_endpoint_calls(tmp_path, monkeypatch):
    """One retry of the batch, then the endpoints are generated one by one."""
    out_dir = _batch_run_fixture(tmp_path, monkeypatch)
    batch_calls = []
    per_endpoint_calls = []

    def _unusable_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {"not_paths": {}}

    def _fake_endpoint(definition, context, route, http_method=None):
        per_endpoint_calls.append(route)
        return {"paths": {"/whatever": {"post": {"summary": f"one {route}"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _unusable_batch)
    monkeypatch.setattr(run_module, "get_function_definition_swagger", _fake_endpoint)

    swagger = run_swagger_generation("http://localhost:3000")

    assert len(batch_calls) == 2
    assert sorted(per_endpoint_calls) == ["/orders", "/users"]
    assert swagger["paths"] == {
        "/users": {"get": {"summary": "one /users"}},
        "/orders": {"post": {"summary": "one /orders"}},
    }
    index = json.loads((out_dir / "api_index.json").read_text(encoding="utf-8"))
    assert set(index) == {"GET /users", "POST /orders"}


def _context_jobs():
    return [
        {"file_path": "/repo/app.js", "route": "/users", "method": "GET", "start_line": 1, "end_line": 2},
        {"file_path": "/repo/app.js", "route": "/orders", "method": "POST", "start_line": 4, "end_line": 5},
    ]


def test_shared_context_blocks_are_deduped(monkeypatch, capsys):
    """The same helper pulled in by two endpoints is sent once."""
    shared_block = ["const auth = require('./auth');\n"]
    monkeypatch.setattr(
        run_module,
        "provide_context_codeblock",
        lambda directory_path, method_info: ([shared_block], ["handler body\n"]),
    )

    _, _, shared_context, sections, failures = _collect_batch_context("/repo", _context_jobs())

    assert failures == []
    assert shared_context.count("const auth") == 1
    assert sections.count("handler body") == 2
    assert "context truncated" not in capsys.readouterr().out


def test_oversized_context_is_truncated_to_the_budget(monkeypatch, capsys):
    """Handler bodies are capped first, then the shared blocks are dropped."""
    huge_block = ["# helper\n" + ("shared_filler_token = 1\n" * 4000)]
    huge_handler = ["function handler() {\n" + ("  const filler = 1;\n" * 6000) + "}\n"]
    monkeypatch.setattr(
        run_module,
        "provide_context_codeblock",
        lambda directory_path, method_info: ([huge_block], huge_handler),
    )

    _, _, shared_context, sections, _ = _collect_batch_context("/repo", _context_jobs())

    assert num_tokens_from_string(shared_context + sections) <= CONTEXT_TOKEN_BUDGET
    assert sections.count("... truncated") == 2
    assert shared_context == ""
    assert "apimesh: context truncated for /repo/app.js (1 blocks dropped)" in capsys.readouterr().out


NO_ENDPOINTS_JS = """const helper = (value) => value + 1;
module.exports = helper;
"""


def test_zero_endpoints_returns_none(tmp_path, monkeypatch, capsys):
    """Zero extracted endpoints must fall back instead of shipping an empty spec."""
    repo = tmp_path / "repo"
    _write(repo / "src" / "helper.js", NO_ENDPOINTS_JS)
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(tmp_path / "out" / "swagger.json"))

    assert run_swagger_generation("http://localhost:3000") is None
    assert "found 0 endpoints" in capsys.readouterr().out


NEST_CONTROLLER = """import { Controller, Get, Post, Delete } from '@nestjs/common';

@Controller('cats')
export class CatsController {
  @Get()
  findAll() { return []; }

  @Get(':id')
  findOne() { return {}; }

  @Post()
  create() { return {}; }

  helper() { return 1; }
}

@Controller('dogs')
class DogsController {
  @Delete(':id')
  remove() {}
}
"""


def test_nestjs_controller_methods_are_extracted(tmp_path):
    """Exported and non-exported controllers must both yield their decorated routes.

    Before the fix this returned an empty set: class decorators were looked up
    via a Python-grammar node type, methods were read from class_declaration
    instead of class_body, and method decorators (siblings of the method inside
    class_body) were never collected.
    """
    controller = _write(tmp_path / "cats.controller.ts", NEST_CONTROLLER)
    assert _routes(controller) == {
        ("GET", "/cats"),
        ("GET", "/cats/:id"),
        ("POST", "/cats"),
        ("DELETE", "/dogs/:id"),
    }


def test_nestjs_undecorated_methods_are_not_endpoints(tmp_path):
    controller = _write(tmp_path / "cats.controller.ts", NEST_CONTROLLER)
    routes = {route for _, route in _routes(controller)}
    assert not any("helper" in route for route in routes)

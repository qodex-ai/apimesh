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
from nodejs_pipeline.generate_file_information import process_file
from nodejs_pipeline.identify_api_functions import find_api_endpoints_js, join_mount_prefix
from nodejs_pipeline.run_swagger_generation import (
    CONTEXT_TOKEN_BUDGET,
    _batch_endpoint_jobs,
    _build_mount_prefix_map,
    _collect_batch_context,
    _endpoint_key,
    _maybe_incremental_update,
    _merge_paths,
    _normalize_route,
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
    assert prefixes.get(str(router_file.resolve())) == ["/api/v1"]

    mounted = {
        (endpoint["method"], join_mount_prefix("/api/v1", endpoint["route"]))
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
    assert prefixes.get(str(router_file.resolve())) == ["/api/v1"]


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

UNTOUCHED_JS = """const express = require('express');
const router = express.Router();
router.get('/health', (req, res) => res.send('ok'));
router.get('/ping', (req, res) => res.send('ok'));
module.exports = router;
"""

UNTOUCHED_JOB_SPECS = [("GET", "/health", 3, 3), ("GET", "/ping", 4, 4)]

# Endpoints of a file git never reports as changed, staged by every incremental
# fixture. The escalation gate hands the run back to the full path once more
# than half the extracted endpoints are dirty, and one dirty endpoint out of one
# is already past that.
UNTOUCHED_INDEX = {"GET /health": {"files": []}, "GET /ping": {"files": []}}


def _jobs(source, job_specs):
    return [
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


def _handler_hash(source_text, start_line, end_line, context_blocks=()):
    """The context hash of one handler, taken off the source the fixture writes."""
    lines = source_text.splitlines(keepends=True)
    return run_module._context_hash(list(context_blocks), lines[start_line - 1 : end_line])


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
    untouched = _write(repo / "untouched.js", UNTOUCHED_JS)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    swagger_path = out_dir / "swagger.json"
    swagger_path.write_text(
        json.dumps({"info": {"commit_reference": "base"}, "paths": existing_paths or {}}),
        encoding="utf-8",
    )
    index_path = out_dir / "api_index.json"
    index_path.write_text(
        json.dumps({**UNTOUCHED_INDEX, **existing_index}), encoding="utf-8"
    )

    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(swagger_path))
    monkeypatch.setattr(
        run_module, "get_changed_files_since", lambda *args, **kwargs: {os.path.abspath(str(source))}
    )
    monkeypatch.setattr(run_module, "get_git_commit_hash", lambda: "head")
    jobs = _jobs(source, job_specs) + _jobs(untouched, UNTOUCHED_JOB_SPECS)
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
    assert json.loads(index_path.read_text(encoding="utf-8")) == UNTOUCHED_INDEX


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
        **UNTOUCHED_INDEX,
        "GET /orders": {
            "files": [{"file_path": os.path.abspath(str(source)), "imports": []}],
            "context_hash": _handler_hash(ROUTER_JS, 3, 3),
        },
    }


def test_failed_endpoint_is_retried_when_no_files_changed(tmp_path, monkeypatch):
    """A failure has to be retried on the next run even with nothing changed in git."""
    repo, source, index_path, jobs = _incremental_fixture(tmp_path, monkeypatch, {})

    def _boom(*args, **kwargs):
        raise RuntimeError("llm call failed")

    monkeypatch.setattr(run_module, "get_function_definition_swagger", _boom)
    first = _maybe_incremental_update(str(repo), jobs)
    assert first["paths"] == {}
    assert json.loads(index_path.read_text(encoding="utf-8")) == UNTOUCHED_INDEX

    monkeypatch.setattr(run_module, "get_changed_files_since", lambda *args, **kwargs: set())
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: {"paths": {"/whatever": {"post": {"summary": "Orders"}}}},
    )

    second = _maybe_incremental_update(str(repo), jobs)
    assert second["paths"] == {"/orders": {"get": {"summary": "Orders"}}}
    assert json.loads(index_path.read_text(encoding="utf-8")) == {
        **UNTOUCHED_INDEX,
        "GET /orders": {
            "files": [{"file_path": os.path.abspath(str(source)), "imports": []}],
            "context_hash": _handler_hash(ROUTER_JS, 3, 3),
        },
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
    assert json.loads(index_path.read_text(encoding="utf-8")) == {
        **UNTOUCHED_INDEX,
        **existing_index,
    }


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
    assert set(index) == {"GET /users/{id}", "POST /users/{id}", *UNTOUCHED_INDEX}
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
        *UNTOUCHED_INDEX,
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
    assert set(json.loads(index_path.read_text(encoding="utf-8"))) == {
        "GET /orders",
        *UNTOUCHED_INDEX,
    }


def test_generation_stores_the_context_hash(tmp_path, monkeypatch):
    """The index carries the fingerprint of the prompt the endpoint came from."""
    repo, _, index_path, jobs = _incremental_fixture(tmp_path, monkeypatch, {})
    monkeypatch.setattr(
        run_module,
        "get_batch_definition_swagger",
        lambda *args, **kwargs: {"paths": {"/orders": {"get": {"summary": "Orders"}}}},
    )

    _maybe_incremental_update(str(repo), jobs)

    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["GET /orders"]["context_hash"] == _handler_hash(ROUTER_JS, 3, 3)


def test_unchanged_context_hash_skips_the_endpoint(tmp_path, monkeypatch, capsys):
    """A file can change without changing what the prompt for its endpoint says."""
    existing_index = {
        "GET /orders": {"files": [], "context_hash": _handler_hash(ROUTER_JS, 3, 3)}
    }
    repo, source, index_path, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        existing_index,
        existing_paths={"/orders": {"get": {"summary": "old"}}},
    )
    monkeypatch.setattr(
        run_module,
        "get_batch_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the context hash matched, nothing may be generated"),
    )
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the context hash matched, nothing may be generated"),
    )

    swagger = _maybe_incremental_update(str(repo), jobs)

    assert swagger["paths"] == {"/orders": {"get": {"summary": "old"}}}
    # The skipped endpoint keeps its hash, and its entry is rebuilt from the
    # current extraction instead of being carried over as it was.
    assert json.loads(index_path.read_text(encoding="utf-8")) == {
        **UNTOUCHED_INDEX,
        "GET /orders": {
            "files": [{"file_path": os.path.abspath(str(source)), "imports": []}],
            "context_hash": _handler_hash(ROUTER_JS, 3, 3),
        },
    }
    assert "apimesh: skipped 1 unchanged endpoints (context hash match)" in capsys.readouterr().out


RELOCATION_ROUTER = """const helper = require('./{module}');
const express = require('express');
const router = express.Router();
router.get('/orders', (req, res) => res.send(helper()));
module.exports = router;
"""

RELOCATION_HELPER = """function helper() {
  return [];
}
module.exports = helper;
"""


def _relocation_jobs(repo: Path) -> list:
    """The handler at routes.js:4 plus the endpoints nothing in this test touches."""
    return _jobs(repo / "routes.js", [("GET", "/orders", 4, 4)]) + _jobs(
        repo / "untouched.js", UNTOUCHED_JOB_SPECS
    )


def _stage_metadata(repo: Path) -> None:
    """The per file metadata the run writes before anything reads the index."""
    json_dir = Path(run_module._metadata_dir_path(str(repo)))
    json_dir.mkdir(parents=True, exist_ok=True)
    for js_file in sorted(repo.rglob("*.js")):
        info = process_file(str(js_file), str(repo))
        name = run_module._metadata_file_name(str(js_file))
        (json_dir / name).write_text(json.dumps(info), encoding="utf-8")


def _relocation_pass(monkeypatch, repo: Path, out_dir: Path, changed_files, summary):
    """One incremental pass over the repo as it is on disk right now."""
    calls = []

    def fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        calls.append(endpoints_list)
        return {"paths": {"/orders": {"get": {"summary": summary}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", fake_batch)
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the batch path documents every endpoint"),
    )
    monkeypatch.setattr(run_module, "get_changed_files_since", lambda *args, **kwargs: changed_files)
    monkeypatch.setattr(run_module, "get_git_commit_hash", lambda: "head")
    _stage_metadata(repo)

    swagger = _maybe_incremental_update(str(repo), _relocation_jobs(repo))

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
    _write(repo / "routes.js", RELOCATION_ROUTER.format(module="helpers_a"))
    _write(repo / "helpers_a.js", RELOCATION_HELPER)
    _write(repo / "untouched.js", UNTOUCHED_JS)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "swagger.json").write_text(
        json.dumps({"info": {"commit_reference": "base"}, "paths": {}}), encoding="utf-8"
    )
    (out_dir / "api_index.json").write_text(json.dumps(UNTOUCHED_INDEX), encoding="utf-8")
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(out_dir / "swagger.json"))

    index, calls = _relocation_pass(monkeypatch, repo, out_dir, set(), "documented")

    assert calls == ["GET /orders"]
    assert _imported_paths(index["GET /orders"]) == [str(repo / "helpers_a.js")]
    stored_hash = index["GET /orders"]["context_hash"]

    # The helper moves to a file that reads the same, and only the require line
    # of the handler's own file changes with it.
    (repo / "helpers_a.js").unlink()
    _write(repo / "helpers_b.js", RELOCATION_HELPER)
    _write(repo / "routes.js", RELOCATION_ROUTER.format(module="helpers_b"))

    index, calls = _relocation_pass(
        monkeypatch, repo, out_dir, {os.path.abspath(str(repo / "routes.js"))}, "never asked for"
    )

    assert calls == []
    assert index["GET /orders"]["context_hash"] == stored_hash
    assert _imported_paths(index["GET /orders"]) == [str(repo / "helpers_b.js")]

    # The edit the stale entry used to hide: the file the helper moved to.
    _write(repo / "helpers_b.js", RELOCATION_HELPER.replace("return [];", "return [1];"))

    index, calls = _relocation_pass(
        monkeypatch, repo, out_dir, {os.path.abspath(str(repo / "helpers_b.js"))}, "redocumented"
    )

    assert calls == ["GET /orders"]
    assert index["GET /orders"]["context_hash"] != stored_hash


def test_a_changed_handler_regenerates_over_its_stored_hash(tmp_path, monkeypatch):
    """The other half of the skip: a hash that no longer matches costs a call."""
    repo, _, index_path, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        {"GET /orders": {"files": [], "context_hash": "hash of the handler as it was"}},
        existing_paths={"/orders": {"get": {"summary": "old"}}},
    )
    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {"paths": {"/orders": {"get": {"summary": "Orders"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    swagger = _maybe_incremental_update(str(repo), jobs)

    assert batch_calls == ["GET /orders"]
    assert swagger["paths"] == {"/orders": {"get": {"summary": "Orders"}}}
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["GET /orders"]["context_hash"] == _handler_hash(ROUTER_JS, 3, 3)


def test_an_endpoint_without_a_stored_hash_regenerates(tmp_path, monkeypatch):
    """An index written before context hashes existed may not skip anything."""
    repo, _, _, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        {"GET /orders": {"files": []}},
        existing_paths={"/orders": {"get": {"summary": "old"}}},
    )
    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {"paths": {"/orders": {"get": {"summary": "Orders"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    swagger = _maybe_incremental_update(str(repo), jobs)

    assert batch_calls == ["GET /orders"]
    assert swagger["paths"] == {"/orders": {"get": {"summary": "Orders"}}}


def test_a_changed_dependency_marks_the_endpoint_dirty(tmp_path, monkeypatch):
    """One hop out: the handler's own file is untouched, a file it imports changed."""
    helper = tmp_path / "repo" / "helper.js"
    existing_index = {
        "GET /orders": {
            "files": [
                {
                    "file_path": str(tmp_path / "repo" / "routes.js"),
                    "imports": [{"file_path": str(helper), "name": "compute"}],
                }
            ]
        }
    }
    repo, _, _, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        existing_index,
        existing_paths={"/orders": {"get": {"summary": "old"}}},
    )
    _write(helper, "module.exports = (value) => value + 1;\n")
    monkeypatch.setattr(
        run_module,
        "get_changed_files_since",
        lambda *args, **kwargs: {os.path.abspath(str(helper))},
    )
    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {"paths": {"/orders": {"get": {"summary": "Orders"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    swagger = _maybe_incremental_update(str(repo), jobs)

    assert batch_calls == ["GET /orders"]
    assert swagger["paths"] == {"/orders": {"get": {"summary": "Orders"}}}


THREE_ROUTE_JS = """const express = require('express');
const router = express.Router();
router.get('/orders', (req, res) => res.send([]));
router.post('/orders', (req, res) => res.send({}));
router.delete('/orders', (req, res) => res.send({}));
module.exports = router;
"""

THREE_DIRTY_JOB_SPECS = [
    ("GET", "/orders", 3, 3),
    ("POST", "/orders", 4, 4),
    ("DELETE", "/orders", 5, 5),
]


def test_more_than_half_the_endpoints_dirty_hands_back_a_full_run(tmp_path, monkeypatch, capsys):
    """Patching a spec endpoint by endpoint stops paying off past the halfway mark."""
    repo, _, index_path, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        {},
        source_text=THREE_ROUTE_JS,
        job_specs=THREE_DIRTY_JOB_SPECS,
    )
    monkeypatch.setattr(
        run_module,
        "get_batch_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the full run generates, not the incremental pass"),
    )

    assert _maybe_incremental_update(str(repo), jobs) is None
    assert (
        "apimesh: 3 of 5 endpoints affected, running a full regeneration"
        in capsys.readouterr().out
    )
    # Nothing is written on the way out: the full run replaces the index.
    assert json.loads(index_path.read_text(encoding="utf-8")) == UNTOUCHED_INDEX


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
    # A full run has to leave the hashes behind too, or the next run skips nothing.
    assert all(entry["context_hash"] for entry in index.values())


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


SPANNED_JS = """function compute(a, b) {
  const total = a + b;
  const scaled = total * 2;
  return scaled;
}

class Ledger {
  add(entry) {
    return entry;
  }
}

const handler = async (req, res) => {
  res.json(compute(1, 2));
};
"""


def _by_name(entries):
    return {entry["name"]: entry for entry in entries}


def test_metadata_records_the_full_definition_span(tmp_path):
    """Line ranges came off the name identifier, so every symbol was one line.

    A single-line range makes the context builder hand the model the signature
    of a helper and none of its body.
    """
    source = _write(tmp_path / "app.js", SPANNED_JS)
    elements = process_file(str(source), str(tmp_path))["elements"]

    compute = _by_name(elements["functions"])["compute"]
    assert (compute["start_line"], compute["end_line"]) == (1, 5)
    assert compute["line"] == 1

    ledger = _by_name(elements["classes"])["Ledger"]
    assert (ledger["start_line"], ledger["end_line"]) == (7, 11)

    handler = _by_name(elements["variables"])["handler"]
    assert (handler["start_line"], handler["end_line"]) == (13, 15)


PAIRED_IMPORTS_JS = """const express = require('express');
const PORT = 3000;
const db = require('./db');
const TIMEOUT = 5000;
const helper = require('./helper');
"""


def test_requires_pair_with_their_own_declarator(tmp_path):
    """Sources were zipped against every declarator, not just the requiring ones.

    The plain `const PORT` in between shifted the alignment, so ./db was
    recorded as imported under the name PORT and the last require lost its name.
    """
    repo = tmp_path / "repo"
    _write(repo / "db.js", "module.exports = {};\n")
    _write(repo / "helper.js", "module.exports = {};\n")
    source = _write(repo / "app.js", PAIRED_IMPORTS_JS)

    imports = process_file(str(source), str(repo))["elements"]["imports"]

    assert [(item["imported_name"], item["from_module"]) for item in imports] == [
        ("express", "express"),
        ("db", "./db"),
        ("helper", "./helper"),
    ]


def test_relative_require_resolves_against_the_importing_file(tmp_path):
    """'./helper' means a sibling of the requiring file, not of the repo root."""
    repo = tmp_path / "repo"
    sibling = _write(repo / "src" / "services" / "helper.js", "module.exports = {};\n")
    source = _write(
        repo / "src" / "services" / "user.js",
        "const helper = require('./helper');\nmodule.exports = helper;\n",
    )

    imports = process_file(str(source), str(repo))["elements"]["imports"]

    assert len(imports) == 1
    assert imports[0]["origin"] == str(sibling.resolve())
    assert imports[0]["path_exists"] is True


MODERN_JS = """const express = require('express');
const axios = require('axios');
const cache = require('./cache');
const apiClient = require('./apiClient');
const app = express();

class Settings {
  timeout = 5000;
}

app.get('/users/:id', async (req, res) => {
  const cached = cache.get(req.params.id);
  const upstream = await axios.get('/remote');
  const extra = await apiClient.get('/extra');
  const name = req.user?.profile?.name ?? 'anonymous';
  res.json({ name, cached, upstream, extra });
});

app.post('/users', (req, res) => res.status(201).json({}));

module.exports = app;
"""


def test_modern_javascript_extracts_with_full_line_spans(tmp_path):
    """Optional chaining, nullish coalescing and class fields used to break the
    JS parser, which dropped the file to the regex extractor and its one-line,
    route-object-guessing output."""
    source = _write(tmp_path / "app.js", MODERN_JS)
    endpoints = find_api_endpoints_js(source)
    spans = {
        (endpoint["method"], endpoint["route"]): (endpoint["start_line"], endpoint["end_line"])
        for endpoint in endpoints
    }

    assert spans == {("GET", "/users/:id"): (11, 17), ("POST", "/users"): (19, 19)}


def test_http_clients_and_caches_are_not_endpoints(tmp_path):
    """axios.get, cache.get and apiClient.get are calls out, not registrations."""
    source = _write(tmp_path / "app.js", MODERN_JS)
    routes = {route for _, route in _routes(source)}

    assert routes == {"/users/:id", "/users"}


SHORT_ROUTER_JS = """const express = require('express');
const r = express.Router();

r.get('/widgets', (req, res) => res.send([]));

module.exports = r;
"""


def test_a_router_with_an_unroutey_name_still_extracts(tmp_path):
    """The name filter must not cost the routers the file declares itself."""
    source = _write(tmp_path / "widgets.js", SHORT_ROUTER_JS)
    assert _routes(source) == {("GET", "/widgets")}


def test_normalize_route_handles_optional_hyphenated_and_constrained_params():
    assert _normalize_route("/a/:id?") == "/a/{id}"
    assert _normalize_route("/:from-:to") == "/{from}-{to}"
    assert _normalize_route("/u/:id(\\d+)") == "/u/{id}"
    # Idempotent: a route already in the OpenAPI spelling is left alone.
    assert _normalize_route("/u/{id}") == "/u/{id}"


def test_normalize_route_consumes_a_nested_constraint_whole():
    """A constraint with its own parentheses used to leave a stray ')' behind."""
    assert _normalize_route("/:id(\\d{2}(?:-\\d{2})?)") == "/{id}"
    assert _normalize_route("/x/:id(\\d+)?/y") == "/x/{id}/y"
    # An escaped paren inside the constraint does not close it.
    assert _normalize_route("/:id(a\\)b)") == "/{id}"


def test_normalize_route_leaves_an_unbalanced_constraint_alone():
    """Better an express spelling than a path with half a constraint in it."""
    assert _normalize_route("/:id(\\d{2}") == "/:id(\\d{2}"
    assert _normalize_route("/a/:id((\\d+)/b") == "/a/:id((\\d+)/b"


TWICE_MOUNTED_APP = """const express = require('express');
const app = express();
const router = express.Router();

router.get('/users', (req, res) => res.send([]));

app.use('/api/v1', router);
app.use('/api/v2', router);

module.exports = app;
"""


def test_router_mounted_twice_yields_both_prefixes(tmp_path):
    """The mount map kept one prefix per router, so /api/v2 was never emitted."""
    app_file = _write(tmp_path / "app.js", TWICE_MOUNTED_APP)
    assert _routes(app_file) == {
        ("GET", "/api/v1/users"),
        ("GET", "/api/v2/users"),
    }


TWICE_MOUNTED_CROSS_FILE_APP = """const express = require('express');
const usersRouter = require('./routes/users');
const app = express();

app.use('/v1', usersRouter);
app.use('/v2', usersRouter);

module.exports = app;
"""


def test_cross_file_router_mounted_twice_documents_both_path_sets(tmp_path, monkeypatch):
    """A router required elsewhere and mounted twice reaches the spec twice."""
    repo = tmp_path / "repo"
    _write(repo / "app.js", TWICE_MOUNTED_CROSS_FILE_APP)
    router_file = _write(repo / "routes" / "users.js", CROSS_FILE_ROUTER)
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(tmp_path / "out" / "swagger.json"))

    assert _build_mount_prefix_map(str(repo)).get(str(router_file.resolve())) == ["/v1", "/v2"]

    monkeypatch.setattr(
        run_module,
        "get_batch_definition_swagger",
        lambda *args, **kwargs: {
            "paths": {
                "/v1": {"get": {"summary": "List v1"}},
                "/v1/{id}": {"get": {"summary": "One v1"}},
                "/v2": {"get": {"summary": "List v2"}},
                "/v2/{id}": {"get": {"summary": "One v2"}},
            }
        },
    )

    swagger = run_swagger_generation("http://localhost:3000")

    assert set(swagger["paths"]) == {"/v1", "/v1/{id}", "/v2", "/v2/{id}"}


JSDOC_MODULE_ONLY_JS = """/**
 * @module utils/logger
 */
const format = (value) => String(value);

module.exports = { format };
"""


def test_a_jsdoc_module_tag_alone_does_not_select_a_file(tmp_path):
    """'module' and 'api' are ordinary JSDoc tags, not route decorators.

    Keeping them in the decorator set handed the LLM files that define no
    endpoint at all.
    """
    repo = tmp_path / "repo"
    _write(repo / "utils" / "logger.js", JSDOC_MODULE_ONLY_JS)
    router = _write(repo / "routes" / "users.js", CROSS_FILE_ROUTER)

    found = find_api_definition_files(str(repo))
    assert [Path(path).resolve() for path in found] == [router.resolve()]

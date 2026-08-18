"""Regression tests for the rails pipeline audit fixes.

Covers the positional `scope` prefix, ignore matching that is relative to the
scanned repo root, the LLM fragment re-keying, and the empty-extraction
contract. Nothing here touches the network: no endpoint is ever generated.
"""

import copy
import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("APIMESH_CONFIG_PATH", str(REPO_ROOT / "config.yml"))

from rails_pipeline import definition_swagger_generator as generator_module
from rails_pipeline import run_swagger_generation as run_module
from rails_pipeline.find_api_definition_files import find_api_definition_files
from rails_pipeline.generate_file_information import process_file
from rails_pipeline.identify_api_functions import find_api_endpoints
from rails_pipeline.run_swagger_generation import (
    CONTEXT_TOKEN_BUDGET,
    _batch_endpoint_jobs,
    _collect_batch_context,
    _endpoint_key,
    _maybe_incremental_update,
    _merge_paths,
    _normalize_route,
    _rekey_fragment,
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


def _route_map(tmp_path: Path, routes_source: str) -> dict:
    routes_file = _write(tmp_path / "config" / "routes.rb", routes_source)
    route_map: dict = {}
    find_api_endpoints(routes_file, str(tmp_path), route_map)
    return {
        controller: {(route["verb"], route["path"], route["action"]) for route in routes}
        for controller, routes in route_map.items()
    }


def test_positional_scope_prefixes_verb_route(tmp_path):
    """scope '/public' is a path prefix even though it is not a hash option."""
    routes = _route_map(
        tmp_path,
        """Rails.application.routes.draw do
  scope '/public' do
    get 'status', to: 'health#status'
  end
end
""",
    )
    assert routes == {"health": {("GET", "/public/status", "status")}}


def test_positional_scope_without_leading_slash_prefixes_resources(tmp_path):
    routes = _route_map(
        tmp_path,
        """Rails.application.routes.draw do
  scope 'v2' do
    resources :widgets, only: [:index, :show]
  end
end
""",
    )
    assert routes == {
        "widgets": {
            ("GET", "/v2/widgets", "index"),
            ("GET", "/v2/widgets/:id", "show"),
        }
    }


def test_hash_option_scope_still_works(tmp_path):
    """scope path:/module: must behave exactly as it did before the fix."""
    routes = _route_map(
        tmp_path,
        """Rails.application.routes.draw do
  scope path: '/admin', module: 'admin' do
    get 'stats', to: 'reports#stats'
  end
end
""",
    )
    assert routes == {"admin/reports": {("GET", "/admin/stats", "stats")}}


def test_unscoped_route_is_unchanged(tmp_path):
    routes = _route_map(
        tmp_path,
        """Rails.application.routes.draw do
  get '/ping', to: 'health#ping'
end
""",
    )
    assert routes == {"health": {("GET", "/ping", "ping")}}


def _build_repo_under_ignored_name(tmp_path: Path) -> Path:
    """A repo whose absolute path contains 'build', which is an ignored dir."""
    repo_root = tmp_path / "build" / "sample_app"
    _write(
        repo_root / "config" / "routes.rb",
        """Rails.application.routes.draw do
  get '/ping', to: 'health#ping'
end
""",
    )
    _write(
        repo_root / "app" / "controllers" / "health_controller.rb",
        """class HealthController < ApplicationController
  def ping
    render json: {}
  end
end
""",
    )
    _write(
        repo_root / "vendor" / "app" / "controllers" / "vendored_controller.rb",
        "class VendoredController; end\n",
    )
    return repo_root


def test_ignored_dirs_are_matched_relative_to_repo_root(tmp_path):
    repo_root = _build_repo_under_ignored_name(tmp_path)
    found = {Path(path).relative_to(repo_root).as_posix() for path in find_api_definition_files(str(repo_root))}
    assert found == {
        "config/routes.rb",
        "app/controllers/health_controller.rb",
    }


def test_should_process_directory_is_relative_to_repo_root(tmp_path):
    repo_root = _build_repo_under_ignored_name(tmp_path)
    assert should_process_directory(
        str(repo_root / "app" / "controllers" / "health_controller.rb"), str(repo_root)
    )
    assert not should_process_directory(
        str(repo_root / "vendor" / "app" / "controllers" / "vendored_controller.rb"),
        str(repo_root),
    )


def test_normalize_route_uses_openapi_params():
    assert _normalize_route("/users/:user_id/posts/:id") == "/users/{user_id}/posts/{id}"
    assert _normalize_route("users") == "/users"
    assert _normalize_route(None) == ""


def test_rekey_fragment_uses_extractor_route_and_verb():
    fragment = {
        "paths": {
            "/users/{id}": {
                "get": {"summary": "Fetch a user", "responses": {"200": {}}}
            }
        }
    }
    rekeyed = _rekey_fragment(fragment, "/users/:user_id", "GET")
    assert rekeyed == {
        "paths": {
            "/users/{user_id}": {
                "get": {"summary": "Fetch a user", "responses": {"200": {}}}
            }
        }
    }


def test_rekey_fragment_renames_legacy_operation_fields():
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

    assert _rekey_fragment(fragment, "/users/:id", "GET") == {
        "paths": {
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
    }


def test_rekey_fragment_keeps_first_operation_only():
    fragment = {
        "paths": {
            "/wrong": {"post": {"summary": "first"}},
            "/also-wrong": {"put": {"summary": "second"}},
        }
    }
    rekeyed = _rekey_fragment(fragment, "/users", "POST")
    assert rekeyed == {"paths": {"/users": {"post": {"summary": "first"}}}}


def test_rekey_fragment_prefers_the_http_verb_over_a_non_operation_key():
    """A path item may hold `parameters`; the verb body must win over that list."""
    fragment = {
        "paths": {
            "/users/{id}": {
                "parameters": [{"name": "id", "in": "path"}],
                "get": {"summary": "Fetch a user"},
            }
        }
    }
    assert _rekey_fragment(fragment, "/users/:id", "GET") == {
        "paths": {"/users/{id}": {"get": {"summary": "Fetch a user"}}}
    }


def test_rekey_fragment_rejects_a_path_item_with_no_http_verb():
    """A vendor extension is a dict, but it is not an operation body."""
    fragment = {"paths": {"/x": {"x-metadata": {"owner": "payments"}}}}
    assert _rekey_fragment(fragment, "/x", "GET") is None


def test_rekey_fragment_rejects_unusable_fragments():
    assert _rekey_fragment(None, "/users", "GET") is None
    assert _rekey_fragment({}, "/users", "GET") is None
    assert _rekey_fragment({"paths": {}}, "/users", "GET") is None
    assert _rekey_fragment({"paths": "nope"}, "/users", "GET") is None
    assert _rekey_fragment({"paths": {"/users": {}}}, "/users", "GET") is None
    assert _rekey_fragment({"paths": {"/users": {"parameters": [{"name": "id"}]}}}, "/users", "GET") is None
    assert _rekey_fragment({"paths": {"/users": {"get": {}}}}, "", "GET") is None


def test_removal_lookup_matches_the_generated_swagger_key():
    """The incremental delete path must find the key the generator wrote."""
    swagger = {"paths": {}}
    fragment = _rekey_fragment(
        {"paths": {"/users/{id}": {"get": {"summary": "Fetch a user"}}}},
        "/users/:user_id",
        "GET",
    )
    _merge_paths(swagger, fragment)
    assert list(swagger["paths"]) == ["/users/{user_id}"]

    _remove_endpoint_from_swagger(swagger, _endpoint_key("/users/:user_id", "GET"))
    assert swagger["paths"] == {}


def _prepare_run(monkeypatch, repo_root: Path, output_dir: Path) -> Path:
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo_root))
    output_filepath = output_dir / "swagger.json"
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(output_filepath))
    return output_filepath


ORDERS_CONTROLLER = """class OrdersController < ApplicationController
  def users
    render json: []
  end

  def orders
    render json: []
  end
end
"""


DEFAULT_JOB_SPECS = [("GET", "/users", 2, 4), ("GET", "/orders", 6, 8)]

UNTOUCHED_CONTROLLER = """class HealthController < ApplicationController
  def status
    render json: {}
  end

  def ping
    render json: {}
  end
end
"""

UNTOUCHED_JOB_SPECS = [("GET", "/status", 2, 4), ("GET", "/ping", 6, 8)]

# Endpoints of a controller git never reports as changed, staged by every
# incremental fixture. The escalation gate hands the run back to the full path
# once more than half the extracted endpoints are dirty, and two dirty endpoints
# out of two are already past that.
UNTOUCHED_INDEX = {"GET /status": {"files": []}, "GET /ping": {"files": []}}


def _jobs(controller, job_specs):
    return [
        {
            "type": "function",
            "http_method": http_method,
            "route": route,
            "file_path": str(controller),
            "start_line": start_line,
            "end_line": end_line,
        }
        for http_method, route, start_line, end_line in job_specs
    ]


def _action_hash(controller_source, start_line, end_line, context_blocks=()):
    """The context hash of one action, taken off the source the fixture writes."""
    lines = controller_source.splitlines(keepends=True)
    return run_module._context_hash(list(context_blocks), lines[start_line - 1 : end_line])


def _incremental_fixture(
    tmp_path,
    monkeypatch,
    existing_index,
    existing_paths=None,
    controller_name="orders_controller.rb",
    controller_source=ORDERS_CONTROLLER,
    job_specs=DEFAULT_JOB_SPECS,
):
    """Stage an existing swagger plus api index so the incremental path runs."""
    repo_root = tmp_path / "app_repo"
    controller = _write(
        repo_root / "app" / "controllers" / controller_name, controller_source
    )
    untouched = _write(
        repo_root / "app" / "controllers" / "health_controller.rb", UNTOUCHED_CONTROLLER
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    output_filepath = _prepare_run(monkeypatch, repo_root, output_dir)
    output_filepath.write_text(
        json.dumps({"info": {"commit_reference": "base"}, "paths": existing_paths or {}}),
        encoding="utf-8",
    )
    api_index_path = output_dir / "api_index.json"
    api_index_path.write_text(
        json.dumps({**UNTOUCHED_INDEX, **existing_index}), encoding="utf-8"
    )

    monkeypatch.setattr(
        run_module,
        "get_changed_files_since",
        lambda *args, **kwargs: {os.path.abspath(str(controller))},
    )
    monkeypatch.setattr(run_module, "get_git_commit_hash", lambda: "head")
    jobs = _jobs(controller, job_specs) + _jobs(untouched, UNTOUCHED_JOB_SPECS)
    return repo_root, controller, api_index_path, jobs


def test_incremental_failure_is_isolated_and_leaves_the_entry_dirty(tmp_path, monkeypatch):
    """
    One failing endpoint must not abort the incremental pass, and its index entry
    must stay stale so the next run retries it. Removals still apply.
    """
    existing_index = {
        "GET /users": {"files": []},
        "GET /orders": {"files": []},
        "DELETE /gone": {"files": []},
    }
    repo_root, controller, api_index_path, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        existing_index,
        existing_paths={"/gone": {"delete": {"summary": "old"}}},
    )

    def _fake_generation(function_definition, context, route, http_method=None):
        if route == "/users":
            raise RuntimeError("llm call failed")
        return {"paths": {"/whatever": {"post": {"summary": "Orders"}}}}

    monkeypatch.setattr(run_module, "get_function_definition_swagger", _fake_generation)

    swagger = _maybe_incremental_update(str(repo_root), jobs)
    assert swagger is not None
    assert swagger["paths"] == {"/orders": {"get": {"summary": "Orders"}}}
    # The failed key is dropped so the next run retries it as newly added.
    assert json.loads(api_index_path.read_text(encoding="utf-8")) == {
        **UNTOUCHED_INDEX,
        "GET /orders": {
            "files": [{"file_path": os.path.abspath(str(controller)), "imports": []}],
            "context_hash": _action_hash(ORDERS_CONTROLLER, 6, 8),
        },
    }


def test_failed_endpoint_is_retried_when_no_files_changed(tmp_path, monkeypatch):
    """A failure has to be retried on the next run even with nothing changed in git."""
    repo_root, _, api_index_path, jobs = _incremental_fixture(tmp_path, monkeypatch, {})

    def _fake_generation(function_definition, context, route, http_method=None):
        if route == "/users":
            raise RuntimeError("llm call failed")
        return {"paths": {"/whatever": {"post": {"summary": "Orders"}}}}

    monkeypatch.setattr(run_module, "get_function_definition_swagger", _fake_generation)
    first = _maybe_incremental_update(str(repo_root), jobs)
    assert first["paths"] == {"/orders": {"get": {"summary": "Orders"}}}
    assert set(json.loads(api_index_path.read_text(encoding="utf-8"))) == {
        "GET /orders",
        *UNTOUCHED_INDEX,
    }
    # The caller persists the spec between runs, the pipeline reads it back.
    Path(os.environ["APIMESH_OUTPUT_FILEPATH"]).write_text(json.dumps(first), encoding="utf-8")

    monkeypatch.setattr(run_module, "get_changed_files_since", lambda *args, **kwargs: set())
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: {"paths": {"/whatever": {"post": {"summary": "Users"}}}},
    )

    second = _maybe_incremental_update(str(repo_root), jobs)
    assert second["paths"] == {
        "/orders": {"get": {"summary": "Orders"}},
        "/users": {"get": {"summary": "Users"}},
    }
    assert set(json.loads(api_index_path.read_text(encoding="utf-8"))) == {
        "GET /users",
        "GET /orders",
        *UNTOUCHED_INDEX,
    }


def test_legacy_route_spellings_are_canonicalized_on_load(tmp_path, monkeypatch):
    """A spec and index written before routes were canonicalized must load in
    the canonical spelling, otherwise the first run after the upgrade reads
    every endpoint as removed and re-added and regenerates all of them."""
    _incremental_fixture(
        tmp_path,
        monkeypatch,
        {
            "GET /users/:id": {"files": [{"file_path": "/repo/legacy.rb"}]},
            "GET /users/{id}": {"files": [{"file_path": "/repo/users_controller.rb"}]},
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
    assert index["GET /users/{id}"]["files"][0]["file_path"] == "/repo/users_controller.rb"


def test_incremental_no_change_return_carries_the_new_host(tmp_path, monkeypatch):
    """--api-host has to reach the spec on the incremental path too, or a run
    that changes the host keeps publishing the previous server url."""
    existing_index = {"GET /users": {"files": []}, "GET /orders": {"files": []}}
    repo_root, _, _, jobs = _incremental_fixture(tmp_path, monkeypatch, existing_index)
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

    swagger = _maybe_incremental_update(str(repo_root), jobs, "https://new.example.com")
    assert swagger["servers"] == [{"url": "https://new.example.com"}]
    assert swagger["paths"] == {"/orders": {"get": {"summary": "old"}}}


def test_incremental_dirty_endpoints_of_one_controller_share_a_batch(tmp_path, monkeypatch):
    """Two dirty endpoints of one controller cost one call, not one call each."""
    repo_root, _, api_index_path, jobs = _incremental_fixture(tmp_path, monkeypatch, {})
    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {
            "paths": {
                "/users": {"get": {"summary": "List users"}},
                "/orders": {"get": {"summary": "List orders"}},
            }
        }

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the per endpoint path must not be used"),
    )

    swagger = _maybe_incremental_update(str(repo_root), jobs)

    assert len(batch_calls) == 1
    assert set(batch_calls[0].splitlines()) == {"GET /users", "GET /orders"}
    assert swagger["paths"] == {
        "/users": {"get": {"summary": "List users"}},
        "/orders": {"get": {"summary": "List orders"}},
    }
    assert set(json.loads(api_index_path.read_text(encoding="utf-8"))) == {
        "GET /users",
        "GET /orders",
        *UNTOUCHED_INDEX,
    }


def test_incremental_batch_indexes_only_the_keys_that_generated(tmp_path, monkeypatch):
    """Batching the dirty endpoints together keeps the per key accounting."""
    repo_root, _, api_index_path, jobs = _incremental_fixture(tmp_path, monkeypatch, {})
    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {"paths": {"/orders": {"get": {"summary": "List orders"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    swagger = _maybe_incremental_update(str(repo_root), jobs)

    assert len(batch_calls) == 1
    assert swagger["paths"] == {"/orders": {"get": {"summary": "List orders"}}}
    assert set(json.loads(api_index_path.read_text(encoding="utf-8"))) == {
        "GET /orders",
        *UNTOUCHED_INDEX,
    }


def test_generation_stores_the_context_hash(tmp_path, monkeypatch):
    """The index carries the fingerprint of the prompt the endpoint came from."""
    repo_root, _, api_index_path, jobs = _incremental_fixture(tmp_path, monkeypatch, {})
    monkeypatch.setattr(
        run_module,
        "get_batch_definition_swagger",
        lambda *args, **kwargs: BATCH_RESPONSE,
    )

    _maybe_incremental_update(str(repo_root), jobs)

    index = json.loads(api_index_path.read_text(encoding="utf-8"))
    assert index["GET /users"]["context_hash"] == _action_hash(ORDERS_CONTROLLER, 2, 4)
    assert index["GET /orders"]["context_hash"] == _action_hash(ORDERS_CONTROLLER, 6, 8)


def test_unchanged_context_hashes_skip_every_endpoint(tmp_path, monkeypatch, capsys):
    """A file can change without changing what the prompts for its actions say."""
    existing_index = {
        "GET /users": {"files": [], "context_hash": _action_hash(ORDERS_CONTROLLER, 2, 4)},
        "GET /orders": {"files": [], "context_hash": _action_hash(ORDERS_CONTROLLER, 6, 8)},
    }
    existing_paths = {
        "/users": {"get": {"summary": "old users"}},
        "/orders": {"get": {"summary": "old orders"}},
    }
    repo_root, controller, api_index_path, jobs = _incremental_fixture(
        tmp_path, monkeypatch, existing_index, existing_paths=existing_paths
    )
    monkeypatch.setattr(
        run_module,
        "get_batch_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the context hashes matched, nothing may be generated"),
    )
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the context hashes matched, nothing may be generated"),
    )

    swagger = _maybe_incremental_update(str(repo_root), jobs)

    assert swagger["paths"] == existing_paths
    # The skipped actions keep their hashes, and their entries are rebuilt from
    # the current extraction instead of being carried over as they were.
    controller_files = [{"file_path": os.path.abspath(str(controller)), "imports": []}]
    assert json.loads(api_index_path.read_text(encoding="utf-8")) == {
        **UNTOUCHED_INDEX,
        "GET /users": {
            "files": controller_files,
            "context_hash": _action_hash(ORDERS_CONTROLLER, 2, 4),
        },
        "GET /orders": {
            "files": controller_files,
            "context_hash": _action_hash(ORDERS_CONTROLLER, 6, 8),
        },
    }
    assert "apimesh: skipped 2 unchanged endpoints (context hash match)" in capsys.readouterr().out


def test_only_the_action_whose_context_moved_is_regenerated(tmp_path, monkeypatch, capsys):
    """Both halves at once: a matching hash skips, a stale one costs a call."""
    existing_index = {
        "GET /users": {"files": [], "context_hash": _action_hash(ORDERS_CONTROLLER, 2, 4)},
        "GET /orders": {"files": [], "context_hash": "hash of the action as it was"},
    }
    repo_root, _, _, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        existing_index,
        existing_paths={"/users": {"get": {"summary": "old users"}}},
    )
    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {"paths": {"/orders": {"get": {"summary": "List orders"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    swagger = _maybe_incremental_update(str(repo_root), jobs)

    assert batch_calls == ["GET /orders"]
    assert swagger["paths"] == {
        "/users": {"get": {"summary": "old users"}},
        "/orders": {"get": {"summary": "List orders"}},
    }
    assert "apimesh: skipped 1 unchanged endpoints (context hash match)" in capsys.readouterr().out


def test_an_endpoint_without_a_stored_hash_regenerates(tmp_path, monkeypatch):
    """An index written before context hashes existed may not skip anything."""
    existing_index = {
        "GET /users": {"files": [], "context_hash": _action_hash(ORDERS_CONTROLLER, 2, 4)},
        "GET /orders": {"files": []},
    }
    repo_root, _, _, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        existing_index,
        existing_paths={"/users": {"get": {"summary": "old users"}}},
    )
    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {"paths": {"/orders": {"get": {"summary": "List orders"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    swagger = _maybe_incremental_update(str(repo_root), jobs)

    assert batch_calls == ["GET /orders"]
    assert swagger["paths"]["/orders"] == {"get": {"summary": "List orders"}}


def test_a_changed_dependency_marks_the_endpoint_dirty(tmp_path, monkeypatch):
    """One hop out: the action's own controller is untouched, a file it uses changed."""
    concern = tmp_path / "app_repo" / "app" / "controllers" / "concerns" / "billing.rb"
    existing_index = {
        "GET /users": {"files": [], "context_hash": _action_hash(ORDERS_CONTROLLER, 2, 4)},
        "GET /orders": {
            "files": [
                {
                    "file_path": str(
                        tmp_path / "app_repo" / "app" / "controllers" / "orders_controller.rb"
                    ),
                    "imports": [{"file_path": str(concern), "name": "Billing"}],
                }
            ]
        },
    }
    repo_root, _, _, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        existing_index,
        existing_paths={"/users": {"get": {"summary": "old users"}}},
    )
    _write(concern, "module Billing\nend\n")
    monkeypatch.setattr(
        run_module,
        "get_changed_files_since",
        lambda *args, **kwargs: {os.path.abspath(str(concern))},
    )
    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {"paths": {"/orders": {"get": {"summary": "List orders"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    swagger = _maybe_incremental_update(str(repo_root), jobs)

    assert batch_calls == ["GET /orders"]
    assert swagger["paths"]["/orders"] == {"get": {"summary": "List orders"}}


REPORTS_CONTROLLER = """class ReportsController < ApplicationController
  def index
    render json: []
  end

  def show
    render json: {}
  end

  def create
    render json: {}
  end
end
"""

THREE_DIRTY_JOB_SPECS = [
    ("GET", "/reports", 2, 4),
    ("GET", "/reports/:id", 6, 8),
    ("POST", "/reports", 10, 12),
]


def test_more_than_half_the_endpoints_dirty_hands_back_a_full_run(tmp_path, monkeypatch, capsys):
    """Patching a spec endpoint by endpoint stops paying off past the halfway mark."""
    repo_root, _, api_index_path, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        {},
        controller_name="reports_controller.rb",
        controller_source=REPORTS_CONTROLLER,
        job_specs=THREE_DIRTY_JOB_SPECS,
    )
    monkeypatch.setattr(
        run_module,
        "get_batch_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the full run generates, not the incremental pass"),
    )

    assert _maybe_incremental_update(str(repo_root), jobs) is None
    assert (
        "apimesh: 3 of 5 endpoints affected, running a full regeneration"
        in capsys.readouterr().out
    )
    # Nothing is written on the way out: the full run replaces the index.
    assert json.loads(api_index_path.read_text(encoding="utf-8")) == UNTOUCHED_INDEX


FULL_RUN_ROUTES = """Rails.application.routes.draw do
  get '/users', to: 'orders#users'
  get '/orders', to: 'orders#orders'
end
"""


def test_full_run_indexes_only_the_endpoints_that_generated(tmp_path, monkeypatch):
    """A failed endpoint must stay out of a fresh index, or it is never retried."""
    repo_root = tmp_path / "full_app"
    _write(repo_root / "config" / "routes.rb", FULL_RUN_ROUTES)
    _write(
        repo_root / "app" / "controllers" / "orders_controller.rb", ORDERS_CONTROLLER
    )
    output_dir = tmp_path / "out"
    _prepare_run(monkeypatch, repo_root, output_dir)

    def _fake_generation(function_definition, context, route, http_method=None):
        if route == "/users":
            raise RuntimeError("llm call failed")
        return {"paths": {"/whatever": {"post": {"summary": "Orders"}}}}

    monkeypatch.setattr(run_module, "get_function_definition_swagger", _fake_generation)

    swagger = run_swagger_generation("http://localhost:3000")

    assert swagger["paths"] == {"/orders": {"get": {"summary": "Orders"}}}
    index = json.loads((output_dir / "api_index.json").read_text(encoding="utf-8"))
    assert set(index) == {"GET /orders"}


APPLICATION_CONTROLLER = """class ApplicationController < ActionController::Base
  before_action :authenticate_user!
end
"""


def _batch_run_fixture(tmp_path, monkeypatch):
    """A two endpoint controller whose parent carries the shared auth context."""
    repo_root = tmp_path / "batch_app"
    _write(repo_root / "config" / "routes.rb", FULL_RUN_ROUTES)
    _write(
        repo_root / "app" / "controllers" / "application_controller.rb",
        APPLICATION_CONTROLLER,
    )
    _write(repo_root / "app" / "controllers" / "orders_controller.rb", ORDERS_CONTROLLER)
    output_dir = tmp_path / "out"
    _prepare_run(monkeypatch, repo_root, output_dir)
    return output_dir


BATCH_RESPONSE = {
    "paths": {
        "/users": {"get": {"summary": "List users"}},
        "/orders": {"get": {"summary": "List orders"}},
    }
}


def test_batches_are_grouped_per_controller_and_capped_at_ten():
    jobs = [
        {"file_path": "/repo/a_controller.rb", "route": f"/a{index}", "http_method": "GET"}
        for index in range(12)
    ]
    jobs.append({"file_path": "/repo/b_controller.rb", "route": "/b", "http_method": "GET"})

    batches = _batch_endpoint_jobs(jobs)

    assert [len(batch) for batch in batches] == [10, 2, 1]
    assert {job["file_path"] for job, _ in batches[0] + batches[1]} == {
        "/repo/a_controller.rb"
    }
    assert batches[2][0][0]["file_path"] == "/repo/b_controller.rb"


def _sized_action(name: str, target_tokens: int) -> list:
    """A controller action whose token count lands just past target_tokens."""
    lines = [f"  def {name}\n"]
    while num_tokens_from_string("".join(lines)) < target_tokens:
        lines.append("    value = compute(payload)\n")
    lines.append("  end\n")
    return lines


def _write_sized_actions(tmp_path, sizes):
    """One controller of actions that big, and the jobs pointing at each."""
    source = tmp_path / "app" / "controllers" / "sized_controller.rb"
    lines = ["class SizedController < ApplicationController\n"]
    jobs = []
    for index, target_tokens in enumerate(sizes):
        body = _sized_action(f"action{index}", target_tokens)
        start_line = len(lines) + 1
        lines.extend(body)
        jobs.append(
            {
                "file_path": str(source),
                "route": f"/r{index}",
                "http_method": "GET",
                "start_line": start_line,
                "end_line": len(lines),
            }
        )
    lines.append("end\n")
    _write(source, "".join(lines))
    return source, jobs


def test_batches_are_packed_to_fit_the_context_budget(tmp_path, monkeypatch):
    """Three 2500 token sections cannot share one 6000 token prompt."""
    monkeypatch.setattr(run_module, "MAX_HANDLER_TOKENS", 4000)
    _, jobs = _write_sized_actions(tmp_path, [2500, 2500, 2500])

    batches = _batch_endpoint_jobs(jobs)

    assert [len(batch) for batch in batches] == [2, 1]


def test_packed_batches_keep_their_sections_inside_the_budget(tmp_path, monkeypatch):
    """The invariant the packing exists for, read off the prompt sections."""
    monkeypatch.setattr(run_module, "MAX_HANDLER_TOKENS", 4000)
    _prepare_run(monkeypatch, tmp_path, tmp_path / "out")
    _, jobs = _write_sized_actions(tmp_path, [2500, 2500, 2500])
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
    _, jobs = _write_sized_actions(tmp_path, [7000, 100])

    batches = _batch_endpoint_jobs(jobs)

    assert [len(batch) for batch in batches] == [1, 1]
    assert batches[0][0][0]["route"] == "/r0"


def test_update_action_requests_patch_only_and_mirrors_put():
    jobs = [
        {"file_path": "/repo/widgets_controller.rb", "route": "/widgets/:id", "http_method": "PATCH"},
        {"file_path": "/repo/widgets_controller.rb", "route": "/widgets/:id", "http_method": "PUT"},
        {"file_path": "/repo/widgets_controller.rb", "route": "/widgets", "http_method": "GET"},
    ]

    batches = _batch_endpoint_jobs(jobs)

    assert len(batches) == 1
    requested = [(job["http_method"], [mirror["http_method"] for mirror in mirrors]) for job, mirrors in batches[0]]
    assert requested == [("PATCH", ["PUT"]), ("GET", [])]


UPDATE_ROUTES = """Rails.application.routes.draw do
  resources :widgets, only: [:update]
end
"""

WIDGETS_CONTROLLER = """class WidgetsController < ApplicationController
  def update
    render json: {}
  end
end
"""


def test_patch_is_generated_once_and_copied_to_put(tmp_path, monkeypatch):
    """One call documents PATCH; PUT reuses a deep copy of the same operation."""
    repo_root = tmp_path / "update_app"
    _write(repo_root / "config" / "routes.rb", UPDATE_ROUTES)
    _write(repo_root / "app" / "controllers" / "widgets_controller.rb", WIDGETS_CONTROLLER)
    output_dir = tmp_path / "out"
    _prepare_run(monkeypatch, repo_root, output_dir)

    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {"paths": {"/widgets/{id}": {"patch": {"summary": "Update widget"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    swagger = run_swagger_generation("http://localhost:3000")

    assert batch_calls == ["PATCH /widgets/{id}"]
    path_item = swagger["paths"]["/widgets/{id}"]
    assert path_item["put"] == path_item["patch"] == {"summary": "Update widget"}
    assert path_item["put"] is not path_item["patch"]
    index = json.loads((output_dir / "api_index.json").read_text(encoding="utf-8"))
    assert set(index) == {"PATCH /widgets/{id}", "PUT /widgets/{id}"}
    # A full run has to leave the hashes behind too, the mirror on its PATCH's.
    assert (
        index["PUT /widgets/{id}"]["context_hash"]
        == index["PATCH /widgets/{id}"]["context_hash"]
    )


def test_incremental_patch_and_put_share_one_generated_operation(tmp_path, monkeypatch):
    """A dirty PATCH and PUT on the same route cost one call between them."""
    repo_root, _, api_index_path, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        {},
        controller_name="widgets_controller.rb",
        controller_source=WIDGETS_CONTROLLER,
        job_specs=[("PATCH", "/widgets/:id", 2, 4), ("PUT", "/widgets/:id", 2, 4)],
    )
    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {"paths": {"/widgets/{id}": {"patch": {"summary": "Update widget"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    swagger = _maybe_incremental_update(str(repo_root), jobs)

    assert batch_calls == ["PATCH /widgets/{id}"]
    path_item = swagger["paths"]["/widgets/{id}"]
    assert path_item["put"] == path_item["patch"] == {"summary": "Update widget"}
    assert path_item["put"] is not path_item["patch"]
    assert set(json.loads(api_index_path.read_text(encoding="utf-8"))) == {
        "PATCH /widgets/{id}",
        "PUT /widgets/{id}",
        *UNTOUCHED_INDEX,
    }


PATCH_PUT_JOB_SPECS = [("PATCH", "/widgets/:id", 2, 4), ("PUT", "/widgets/:id", 2, 4)]


def test_patch_and_put_store_the_same_context_hash(tmp_path, monkeypatch):
    """The mirrored PUT was answered from the PATCH's prompt, so it stores its hash."""
    repo_root, _, api_index_path, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        {},
        controller_name="widgets_controller.rb",
        controller_source=WIDGETS_CONTROLLER,
        job_specs=PATCH_PUT_JOB_SPECS,
    )
    monkeypatch.setattr(
        run_module,
        "get_batch_definition_swagger",
        lambda *args, **kwargs: {
            "paths": {"/widgets/{id}": {"patch": {"summary": "Update widget"}}}
        },
    )

    _maybe_incremental_update(str(repo_root), jobs)

    index = json.loads(api_index_path.read_text(encoding="utf-8"))
    expected = _action_hash(WIDGETS_CONTROLLER, 2, 4)
    assert index["PATCH /widgets/{id}"]["context_hash"] == expected
    assert index["PUT /widgets/{id}"]["context_hash"] == expected


def test_a_mirrored_put_skips_on_the_patch_hash(tmp_path, monkeypatch, capsys):
    """The PUT recomputes the PATCH's value, so an untouched action skips both."""
    stored_hash = _action_hash(WIDGETS_CONTROLLER, 2, 4)
    existing_index = {
        "PATCH /widgets/{id}": {"files": [], "context_hash": stored_hash},
        "PUT /widgets/{id}": {"files": [], "context_hash": stored_hash},
    }
    existing_paths = {
        "/widgets/{id}": {
            "patch": {"summary": "Update widget"},
            "put": {"summary": "Update widget"},
        }
    }
    repo_root, _, _, jobs = _incremental_fixture(
        tmp_path,
        monkeypatch,
        existing_index,
        existing_paths=existing_paths,
        controller_name="widgets_controller.rb",
        controller_source=WIDGETS_CONTROLLER,
        job_specs=PATCH_PUT_JOB_SPECS,
    )
    monkeypatch.setattr(
        run_module,
        "get_batch_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the context hashes matched, nothing may be generated"),
    )

    swagger = _maybe_incremental_update(str(repo_root), jobs)

    assert swagger["paths"] == existing_paths
    assert "apimesh: skipped 2 unchanged endpoints (context hash match)" in capsys.readouterr().out


MOVED_ACTION = """  def update
    render json: {}
  end
"""

CHILD_WITH_ACTION = "class WidgetsController < ApplicationController\n" + MOVED_ACTION + "end\n"

CHILD_WITHOUT_ACTION = "class WidgetsController < BaseController\nend\n"

PARENT_WITH_ACTION = "class BaseController < ApplicationController\n" + MOVED_ACTION + "end\n"

PARENT_WITHOUT_ACTION = "class BaseController < ApplicationController\nend\n"


def _moved_action_jobs(controller: Path, untouched: Path) -> list:
    """The PATCH and its mirrored PUT, plus the endpoints nothing here touches."""
    return _jobs(controller, PATCH_PUT_JOB_SPECS) + _jobs(untouched, UNTOUCHED_JOB_SPECS)


def _moved_action_pass(monkeypatch, repo_root: Path, out_dir: Path, jobs, changed_files):
    """One incremental pass over the repo as it is on disk right now."""
    calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        calls.append(endpoints_list)
        return {"paths": {"/widgets/{id}": {"patch": {"summary": "Update widget"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the batch path documents every endpoint"),
    )
    monkeypatch.setattr(run_module, "get_changed_files_since", lambda *args, **kwargs: changed_files)
    monkeypatch.setattr(run_module, "get_git_commit_hash", lambda: "head")
    # Two runs in one process are two checkouts: nothing may be read from the
    # caches the previous run filled.
    monkeypatch.setattr(run_module, "_CLASS_INDEX_CACHE", {})
    monkeypatch.setattr(run_module, "_CLASS_INDEX_CACHE_ROOT", None)
    monkeypatch.setattr(run_module, "_CLASS_CODE_BLOCK_CACHE", {})
    monkeypatch.setattr(run_module, "_FILE_CONTENT_CACHE", {})
    _stage_file_information(repo_root)

    swagger = _maybe_incremental_update(str(repo_root), jobs)

    # The caller persists the spec between runs, the pipeline reads it back.
    Path(os.environ["APIMESH_OUTPUT_FILEPATH"]).write_text(
        json.dumps(swagger), encoding="utf-8"
    )
    index = json.loads((out_dir / "api_index.json").read_text(encoding="utf-8"))
    return index, calls


def _indexed_files(entry) -> list:
    return [file_entry["file_path"] for file_entry in entry["files"]]


def test_a_relocated_action_is_reindexed_when_the_endpoint_skips(tmp_path, monkeypatch):
    """An action extracted into a parent controller reads exactly the same, so
    the prompt, and with it the hash, never moves and the endpoint skips. Its
    index entry still has to follow the action: keeping the old one leaves the
    endpoint tied to a file it no longer lives in, so every edit to that file
    documents it again while the file it moved to is the one that matters. The
    mirrored PUT is rebuilt on the same terms as the PATCH it rides on.
    """
    repo_root = tmp_path / "moved_app"
    child = _write(
        repo_root / "app" / "controllers" / "widgets_controller.rb", CHILD_WITH_ACTION
    )
    parent = _write(
        repo_root / "app" / "controllers" / "base_controller.rb", PARENT_WITHOUT_ACTION
    )
    untouched = _write(
        repo_root / "app" / "controllers" / "health_controller.rb", UNTOUCHED_CONTROLLER
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _prepare_run(monkeypatch, repo_root, out_dir).write_text(
        json.dumps({"info": {"commit_reference": "base"}, "paths": {}}), encoding="utf-8"
    )
    (out_dir / "api_index.json").write_text(json.dumps(UNTOUCHED_INDEX), encoding="utf-8")

    index, calls = _moved_action_pass(
        monkeypatch, repo_root, out_dir, _moved_action_jobs(child, untouched), set()
    )

    assert calls == ["PATCH /widgets/{id}"]
    assert _indexed_files(index["PATCH /widgets/{id}"]) == [os.path.abspath(str(child))]
    assert _indexed_files(index["PUT /widgets/{id}"]) == [os.path.abspath(str(child))]
    stored_hash = index["PATCH /widgets/{id}"]["context_hash"]

    # The action is extracted into the parent controller, reading exactly the same.
    _write(child, CHILD_WITHOUT_ACTION)
    _write(parent, PARENT_WITH_ACTION)
    moved_jobs = _moved_action_jobs(parent, untouched)

    index, calls = _moved_action_pass(
        monkeypatch,
        repo_root,
        out_dir,
        moved_jobs,
        {os.path.abspath(str(child)), os.path.abspath(str(parent))},
    )

    assert calls == []
    assert index["PATCH /widgets/{id}"]["context_hash"] == stored_hash
    assert index["PUT /widgets/{id}"]["context_hash"] == stored_hash
    assert _indexed_files(index["PATCH /widgets/{id}"]) == [os.path.abspath(str(parent))]
    assert _indexed_files(index["PUT /widgets/{id}"]) == [os.path.abspath(str(parent))]

    # The controller the action left no longer has a claim on it.
    index, calls = _moved_action_pass(
        monkeypatch, repo_root, out_dir, moved_jobs, {os.path.abspath(str(child))}
    )

    assert calls == []

    # The file it moved to still does.
    _write(parent, PARENT_WITH_ACTION.replace("render json: {}", "render json: []"))

    index, calls = _moved_action_pass(
        monkeypatch, repo_root, out_dir, moved_jobs, {os.path.abspath(str(parent))}
    )

    assert calls == ["PATCH /widgets/{id}"]
    assert index["PATCH /widgets/{id}"]["context_hash"] != stored_hash


def test_one_batch_call_documents_every_endpoint_of_the_controller(tmp_path, monkeypatch):
    output_dir = _batch_run_fixture(tmp_path, monkeypatch)
    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append((endpoints_list, shared_context, per_endpoint_sections))
        return BATCH_RESPONSE

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the per endpoint path must not be used"),
    )

    swagger = run_swagger_generation("http://localhost:3000")

    assert len(batch_calls) == 1
    endpoints_list, shared_context, sections = batch_calls[0]
    assert set(endpoints_list.splitlines()) == {"GET /users", "GET /orders"}
    # The parent controller carries the auth context for both endpoints and must
    # be sent once, not once per endpoint and not twice per endpoint.
    assert (shared_context + sections).count("before_action :authenticate_user!") == 1
    assert swagger["paths"] == BATCH_RESPONSE["paths"]
    index = json.loads((output_dir / "api_index.json").read_text(encoding="utf-8"))
    assert set(index) == {"GET /users", "GET /orders"}


def test_endpoint_missing_from_the_batch_response_fails_alone(tmp_path, monkeypatch):
    output_dir = _batch_run_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        run_module,
        "get_batch_definition_swagger",
        lambda *args, **kwargs: {"paths": {"/orders": {"get": {"summary": "List orders"}}}},
    )
    monkeypatch.setattr(
        run_module,
        "get_function_definition_swagger",
        lambda *args, **kwargs: pytest.fail("the per endpoint path must not be used"),
    )

    swagger = run_swagger_generation("http://localhost:3000")

    assert swagger["paths"] == {"/orders": {"get": {"summary": "List orders"}}}
    index = json.loads((output_dir / "api_index.json").read_text(encoding="utf-8"))
    assert set(index) == {"GET /orders"}


def test_unusable_batch_response_falls_back_to_the_per_endpoint_calls(tmp_path, monkeypatch):
    output_dir = _batch_run_fixture(tmp_path, monkeypatch)
    batch_calls = []
    per_endpoint_calls = []

    def _unusable_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {"paths": "not a dict"}

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
        "/orders": {"get": {"summary": "one /orders"}},
    }
    index = json.loads((output_dir / "api_index.json").read_text(encoding="utf-8"))
    assert set(index) == {"GET /users", "GET /orders"}


def _context_jobs():
    return [
        {"file_path": "/repo/orders_controller.rb", "route": "/users", "http_method": "GET"},
        {"file_path": "/repo/orders_controller.rb", "route": "/orders", "http_method": "GET"},
    ]


def test_shared_context_blocks_are_deduped(monkeypatch, capsys):
    auth_block = ["before_action :authenticate_user!\n"]
    monkeypatch.setattr(
        run_module,
        "provide_context_codeblock",
        lambda directory_path, method_info: ([auth_block], ["def action\n"]),
    )

    batch = [(job, []) for job in _context_jobs()]
    _, _, shared_context, sections, failures = _collect_batch_context("/repo", batch)

    assert failures == []
    assert shared_context.count("authenticate_user!") == 1
    assert sections.count("def action") == 2
    assert "context truncated" not in capsys.readouterr().out


def test_oversized_context_is_truncated_to_the_budget(monkeypatch, capsys):
    huge_block = ["# concern\n" + ("shared_filler = 1\n" * 4000)]
    huge_action = ["def action\n" + ("  filler = 1\n" * 6000) + "end\n"]
    monkeypatch.setattr(
        run_module,
        "provide_context_codeblock",
        lambda directory_path, method_info: ([huge_block], huge_action),
    )

    batch = [(job, []) for job in _context_jobs()]
    _, _, shared_context, sections, _ = _collect_batch_context("/repo", batch)

    assert num_tokens_from_string(shared_context + sections) <= CONTEXT_TOKEN_BUDGET
    assert sections.count("... truncated") == 2
    assert shared_context == ""
    assert (
        "apimesh: context truncated for /repo/orders_controller.rb (1 blocks dropped)"
        in capsys.readouterr().out
    )


def test_per_endpoint_prompt_sends_the_context_once(monkeypatch):
    """The fallback path used to paste the auth context into the prompt twice."""
    captured = {}

    class _FakeClient:
        def call_chat_completion(self, messages, temperature=0):
            captured["prompt"] = messages[-1]["content"]
            return json.dumps({"paths": {"/users": {"get": {"summary": "x"}}}})

    monkeypatch.setattr(generator_module, "OpenAiClient", _FakeClient)

    generator_module.get_function_definition_swagger(
        ["def show\n"], [["AUTH_CONTEXT_MARKER\n"]], "/users/:id", "GET"
    )

    assert captured["prompt"].count("AUTH_CONTEXT_MARKER") == 1


def test_empty_extraction_returns_none(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "empty_app"
    repo_root.mkdir()
    _prepare_run(monkeypatch, repo_root, tmp_path / "out")

    assert run_swagger_generation("http://localhost:3000") is None
    assert "found 0 endpoints" in capsys.readouterr().out


def test_empty_extraction_does_not_wipe_an_existing_api_index(tmp_path, monkeypatch):
    repo_root = tmp_path / "empty_app"
    repo_root.mkdir()
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    output_filepath = _prepare_run(monkeypatch, repo_root, output_dir)

    existing_index = {"GET /users": {"files": []}}
    api_index_path = output_dir / "api_index.json"
    api_index_path.write_text(json.dumps(existing_index), encoding="utf-8")
    output_filepath.write_text(
        json.dumps({"info": {"commit_reference": "abc123"}, "paths": {"/users": {}}}),
        encoding="utf-8",
    )

    assert run_swagger_generation("http://localhost:3000") is None
    assert json.loads(api_index_path.read_text(encoding="utf-8")) == existing_index


def test_resources_controller_option_keys_the_route_map(tmp_path):
    """resources :photos, controller: 'images' is served by images_controller.rb."""
    routes = _route_map(
        tmp_path,
        """Rails.application.routes.draw do
  resources :photos, controller: 'images', only: [:index, :show]
end
""",
    )
    assert routes == {
        "images": {
            ("GET", "/photos", "index"),
            ("GET", "/photos/:id", "show"),
        }
    }


def test_a_concern_is_replayed_inside_every_resource_that_asks_for_it(tmp_path):
    """The concern emits nothing where it is defined, once per resource that uses it."""
    routes = _route_map(
        tmp_path,
        """Rails.application.routes.draw do
  concern :commentable do
    resources :comments, only: [:index]
    member do
      get :preview
    end
  end

  resources :posts, concerns: :commentable, only: []
  resources :articles, concerns: [:commentable], only: []
end
""",
    )
    assert routes == {
        "comments": {
            ("GET", "/posts/:post_id/comments", "index"),
            ("GET", "/articles/:article_id/comments", "index"),
        },
        "posts": {("GET", "/posts/:id/preview", "preview")},
        "articles": {("GET", "/articles/:id/preview", "preview")},
    }


def test_the_concerns_call_inside_a_resource_block_replays_the_concern(tmp_path):
    """The DSL call form, not the hash option, and it was silently ignored."""
    routes = _route_map(
        tmp_path,
        """Rails.application.routes.draw do
  concern :commentable do
    resources :comments, only: [:index]
    member do
      get :preview
    end
  end

  concern :taggable do
    collection do
      get :tags
    end
  end

  resources :posts, only: [] do
    concerns :commentable
  end

  resources :articles, only: [] do
    concerns [:commentable, :taggable]
  end
end
""",
    )
    assert routes == {
        "comments": {
            ("GET", "/posts/:post_id/comments", "index"),
            ("GET", "/articles/:article_id/comments", "index"),
        },
        "posts": {("GET", "/posts/:id/preview", "preview")},
        "articles": {
            ("GET", "/articles/:id/preview", "preview"),
            ("GET", "/articles/tags", "tags"),
        },
    }


def test_a_namespaced_root_uses_the_namespace_path_and_module(tmp_path):
    routes = _route_map(
        tmp_path,
        """Rails.application.routes.draw do
  root 'home#index'
  namespace :admin do
    root to: 'dashboard#index'
  end
end
""",
    )
    assert routes == {
        "home": {("GET", "/", "index")},
        "admin/dashboard": {("GET", "/admin", "index")},
    }


def test_shallow_is_inherited_by_the_nested_resource(tmp_path):
    """Rails answers a shallow member on /members/:id, not under the parent."""
    routes = _route_map(
        tmp_path,
        """Rails.application.routes.draw do
  resources :teams, shallow: true, only: [:show] do
    resources :members, only: [:index, :show]
  end
end
""",
    )
    assert routes == {
        "teams": {("GET", "/teams/:id", "show")},
        "members": {
            ("GET", "/teams/:team_id/members", "index"),
            ("GET", "/members/:id", "show"),
        },
    }


def test_hashrocket_route_without_a_leading_slash_extracts(tmp_path):
    routes = _route_map(
        tmp_path,
        """Rails.application.routes.draw do
  get 'legacy/:id' => 'system#legacy'
  get '/old/:id' => 'system#old'
end
""",
    )
    assert routes == {
        "system": {
            ("GET", "/legacy/:id", "legacy"),
            ("GET", "/old/:id", "old"),
        }
    }


def test_bare_symbol_verbs_inside_a_resource_block(tmp_path):
    """A member verb hangs off :id, one written straight in the block off :user_id."""
    routes = _route_map(
        tmp_path,
        """Rails.application.routes.draw do
  resources :users, only: [] do
    get :avatar
    member do
      get :profile
    end
    collection do
      get :search
    end
  end
end
""",
    )
    assert routes == {
        "users": {
            ("GET", "/users/:user_id/avatar", "avatar"),
            ("GET", "/users/:id/profile", "profile"),
            ("GET", "/users/search", "search"),
        }
    }


def test_only_the_ancestor_resource_carries_a_named_id(tmp_path):
    """`rails routes` names the innermost member :id and its ancestors :<name>_id."""
    routes = _route_map(
        tmp_path,
        """Rails.application.routes.draw do
  resources :users, only: [:show] do
    resources :posts, only: [:index, :show]
  end
end
""",
    )
    assert routes == {
        "users": {("GET", "/users/:id", "show")},
        "posts": {
            ("GET", "/users/:user_id/posts", "index"),
            ("GET", "/users/:user_id/posts/:id", "show"),
        },
    }


SPLIT_MAIN_ROUTES = """Rails.application.routes.draw do
  get '/ping', to: 'health#ping'
  draw(:admin)
end
"""

SPLIT_ADMIN_ROUTES = """namespace :admin do
  resources :reports, only: [:index]
end
"""


def test_split_route_files_compose_into_one_route_map(tmp_path):
    """Rails' draw(:admin) loads config/routes/admin.rb, so it is parsed too."""
    _write(tmp_path / "config" / "routes.rb", SPLIT_MAIN_ROUTES)
    _write(tmp_path / "config" / "routes" / "admin.rb", SPLIT_ADMIN_ROUTES)

    files = find_api_definition_files(str(tmp_path))
    assert {Path(path).relative_to(tmp_path).as_posix() for path in files} == {
        "config/routes.rb",
        "config/routes/admin.rb",
    }

    route_map: dict = {}
    for path in files:
        find_api_endpoints(Path(path), str(tmp_path), route_map)
    assert {
        controller: {(route["verb"], route["path"]) for route in routes}
        for controller, routes in route_map.items()
    } == {
        "health": {("GET", "/ping")},
        "admin/reports": {("GET", "/admin/reports")},
    }


ENGINE_ROUTES = """Rails.application.routes.draw do
  resources :invoices, only: [:index]
end
"""

INVOICES_CONTROLLER = """class InvoicesController < ApplicationController
  def index
    render json: []
  end
end
"""


def test_an_engine_controller_keys_off_its_own_app_controllers(tmp_path):
    """A mounted engine has its own app/controllers below the repo root."""
    _write(tmp_path / "config" / "routes.rb", ENGINE_ROUTES)
    controller = _write(
        tmp_path / "engines" / "billing" / "app" / "controllers" / "invoices_controller.rb",
        INVOICES_CONTROLLER,
    )

    assert str(controller) in find_api_definition_files(str(tmp_path))

    route_map: dict = {}
    find_api_endpoints(tmp_path / "config" / "routes.rb", str(tmp_path), route_map)
    endpoints = find_api_endpoints(controller, str(tmp_path), route_map)

    assert [
        (method["http_method"], method["route"], method["name"])
        for method in endpoints[0]["methods"]
    ] == [("GET", "/invoices", "index")]


def test_a_latin_1_controller_keeps_its_metadata(tmp_path):
    """Decoding strict utf-8 first dropped the whole file's metadata."""
    source = "class CafeController < ApplicationController\n  # caf\xe9\n  def index\n    render json: []\n  end\nend\n"
    controller = tmp_path / "app" / "controllers" / "cafe_controller.rb"
    controller.parent.mkdir(parents=True, exist_ok=True)
    controller.write_bytes(source.encode("latin-1"))

    file_info = process_file(str(controller), str(tmp_path))

    assert "CafeController" in {item["name"] for item in file_info["elements"]["classes"]}
    assert [item["name"] for item in file_info["elements"]["functions"]] == ["index"]


LATIN_1_ROUTES = """Rails.application.routes.draw do
  resources :cafes, only: [:index]
end
"""

LATIN_1_CONTROLLER = (
    "class CafesController < ApplicationController\n"
    "  def index\n"
    "    # caf\xe9\n"
    "    render json: []\n"
    "  end\n"
    "end\n"
)


def test_a_latin_1_controller_still_generates_its_endpoint(tmp_path, monkeypatch):
    """The parser reads bytes, so the context reads must not raise on them."""
    repo_root = tmp_path / "latin_app"
    _write(repo_root / "config" / "routes.rb", LATIN_1_ROUTES)
    controller = repo_root / "app" / "controllers" / "cafes_controller.rb"
    controller.parent.mkdir(parents=True, exist_ok=True)
    controller.write_bytes(LATIN_1_CONTROLLER.encode("latin-1"))
    _prepare_run(monkeypatch, repo_root, tmp_path / "out")
    monkeypatch.setattr(
        run_module,
        "get_batch_definition_swagger",
        lambda *args, **kwargs: {"paths": {"/cafes": {"get": {"summary": "List cafes"}}}},
    )

    swagger = run_swagger_generation("http://localhost:3000")

    assert swagger["paths"]["/cafes"]["get"]["summary"] == "List cafes"


DROPPED_ROUTES = """Rails.application.routes.draw do
  get '/users', to: 'orders#users'
  get '/ghost', to: 'orders#ghost'
end
"""


def test_a_routed_action_with_no_method_is_dropped_not_fabricated(tmp_path, monkeypatch, capsys):
    """Documenting /ghost from the body of another action invents an endpoint."""
    repo_root = tmp_path / "drop_app"
    _write(repo_root / "config" / "routes.rb", DROPPED_ROUTES)
    _write(repo_root / "app" / "controllers" / "orders_controller.rb", ORDERS_CONTROLLER)
    output_dir = tmp_path / "out"
    _prepare_run(monkeypatch, repo_root, output_dir)

    batch_calls = []

    def _fake_batch(endpoints_list, shared_context, per_endpoint_sections):
        batch_calls.append(endpoints_list)
        return {"paths": {"/users": {"get": {"summary": "List users"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    swagger = run_swagger_generation("http://localhost:3000")

    assert batch_calls == ["GET /users"]
    assert swagger["paths"] == {"/users": {"get": {"summary": "List users"}}}
    assert (
        "apimesh: dropped 1 routed actions with no controller method: "
        "GET /ghost (orders#ghost)" in capsys.readouterr().out
    )


BASE_CONTROLLER = """class BaseController < ApplicationController
  def show
    render json: { inherited: true }
  end
end
"""

INHERITING_CONTROLLER = """class WidgetsController < BaseController
  def index
    render json: []
  end
end
"""

INHERITED_ROUTES = """Rails.application.routes.draw do
  resources :widgets, only: [:index, :show]
end
"""


def _stage_file_information(repo_root: Path) -> None:
    """The per file metadata the run writes before anything reads the index."""
    run_module._CONTENT_HASHES.clear()
    run_module._METADATA_ENTRIES.clear()
    run_module._build_metadata_cache(str(repo_root))


def _build_class_index(monkeypatch, repo_root: Path, output_dir: Path) -> dict:
    _prepare_run(monkeypatch, repo_root, output_dir)
    _stage_file_information(repo_root)
    run_module._CLASS_INDEX_CACHE = {}
    run_module._CLASS_INDEX_CACHE_ROOT = None
    return run_module._ensure_class_index(str(repo_root))


def test_an_inherited_action_documents_the_parent_method(tmp_path, monkeypatch):
    """show lives in the parent controller, so the parent's body is the endpoint."""
    repo_root = tmp_path / "inherited_app"
    _write(repo_root / "config" / "routes.rb", INHERITED_ROUTES)
    base = _write(
        repo_root / "app" / "controllers" / "base_controller.rb", BASE_CONTROLLER
    )
    controller = _write(
        repo_root / "app" / "controllers" / "widgets_controller.rb",
        INHERITING_CONTROLLER,
    )
    class_index = _build_class_index(monkeypatch, repo_root, tmp_path / "out")

    route_map: dict = {}
    find_api_endpoints(repo_root / "config" / "routes.rb", str(repo_root), route_map)
    dropped: list = []
    endpoints = find_api_endpoints(
        controller, str(repo_root), route_map, class_index, dropped
    )

    methods = {method["route"]: method for method in endpoints[0]["methods"]}
    assert dropped == []
    assert set(methods) == {"/widgets", "/widgets/:id"}
    show = methods["/widgets/:id"]
    assert show["file_path"] == str(base)
    assert show["inherited_from"] == "BaseController"
    assert show["start_line"] == 2 and show["end_line"] == 4


HELPER_CONTROLLER = """class HelperWidgetsController < ApplicationController
  def show
    set_widget
    render json: @widget
  end

  private

  def set_widget
    @widget = Widget.find(params[:widget_key])
  end
end
"""


def test_a_bare_helper_call_reaches_the_context_blocks(tmp_path, monkeypatch):
    """`set_widget` parses as an identifier, so the call pass never saw it."""
    repo_root = tmp_path / "helper_app"
    controller = _write(
        repo_root / "app" / "controllers" / "helper_widgets_controller.rb",
        HELPER_CONTROLLER,
    )
    _prepare_run(monkeypatch, repo_root, tmp_path / "out")
    _stage_file_information(repo_root)
    run_module._CLASS_INDEX_CACHE = {}
    run_module._CLASS_INDEX_CACHE_ROOT = None

    context_blocks, method_definition = run_module.provide_context_codeblock(
        str(repo_root),
        {
            "file_path": str(controller),
            "start_line": 2,
            "end_line": 5,
            "class_name": "HelperWidgetsController",
        },
    )

    assert "set_widget\n" in "".join(method_definition)
    context_text = "".join("".join(block) for block in context_blocks)
    assert "def set_widget" in context_text
    assert "params[:widget_key]" in context_text


def test_the_class_index_is_complete_before_the_workers_start(tmp_path, monkeypatch):
    """Built lazily under five workers, the first of them read a half built index."""
    repo_root = tmp_path / "index_app"
    _write(repo_root / "config" / "routes.rb", FULL_RUN_ROUTES)
    _write(
        repo_root / "app" / "controllers" / "application_controller.rb",
        APPLICATION_CONTROLLER,
    )
    _write(repo_root / "app" / "controllers" / "orders_controller.rb", ORDERS_CONTROLLER)
    expected = copy.deepcopy(_build_class_index(monkeypatch, repo_root, tmp_path / "out"))
    run_module._CLASS_INDEX_CACHE = {}
    run_module._CLASS_INDEX_CACHE_ROOT = None

    seen = []
    real_context = run_module.provide_context_codeblock

    def _recording(directory_path, method_info):
        seen.append(copy.deepcopy(run_module._CLASS_INDEX_CACHE))
        return real_context(directory_path, method_info)

    monkeypatch.setattr(run_module, "provide_context_codeblock", _recording)
    monkeypatch.setattr(
        run_module, "get_batch_definition_swagger", lambda *args, **kwargs: BATCH_RESPONSE
    )

    run_swagger_generation("http://localhost:3000")

    assert expected
    assert seen and all(view == expected for view in seen)


MOVED_BASE_BEFORE = """class BaseController < ApplicationController
  def show
    render json: { marker: 'first' }
  end
end
"""

MOVED_BASE_AFTER = """class BaseController < ApplicationController
  before_action :noop

  def show
    render json: { marker: 'second' }
  end
end
"""


def test_a_second_run_reads_the_moved_line_ranges(tmp_path, monkeypatch):
    """The caches are keyed by repo path, and the path is the same both runs."""
    repo_root = tmp_path / "two_run_app"
    _write(repo_root / "config" / "routes.rb", INHERITED_ROUTES)
    base = repo_root / "app" / "controllers" / "base_controller.rb"
    _write(base, MOVED_BASE_BEFORE)
    _write(
        repo_root / "app" / "controllers" / "widgets_controller.rb",
        INHERITING_CONTROLLER,
    )
    _prepare_run(monkeypatch, repo_root, tmp_path / "out")
    # Both runs go the full path: the incremental one would send no prompt.
    monkeypatch.setattr(run_module, "_maybe_incremental_update", lambda *args: None)

    prompts = []

    def _fake_batch(endpoints_list, shared_context, sections):
        prompts.append(f"{shared_context}\n{sections}")
        return {"paths": {"/widgets": {"get": {}}, "/widgets/{id}": {"get": {}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    run_swagger_generation("http://localhost:3000")
    _write(base, MOVED_BASE_AFTER)
    prompts.clear()
    run_swagger_generation("http://localhost:3000")

    second_run = "\n".join(prompts)
    assert "marker: 'second'" in second_run
    assert "marker: 'first'" not in second_run


UPDATE_ONLY_CONTROLLER = """class WidgetsController < ApplicationController
  def update
    render json: {}
  end
end
"""


def test_the_extractor_leaves_one_llm_job_for_an_update_action(tmp_path):
    """PATCH and PUT are both real routes, but only one of them is ever sent."""
    _write(tmp_path / "config" / "routes.rb", UPDATE_ROUTES)
    controller = _write(
        tmp_path / "app" / "controllers" / "widgets_controller.rb",
        UPDATE_ONLY_CONTROLLER,
    )
    route_map: dict = {}
    find_api_endpoints(tmp_path / "config" / "routes.rb", str(tmp_path), route_map)
    jobs = find_api_endpoints(controller, str(tmp_path), route_map)[0]["methods"]

    assert [(job["http_method"], job["route"]) for job in jobs] == [
        ("PATCH", "/widgets/:id"),
        ("PUT", "/widgets/:id"),
    ]

    batches = _batch_endpoint_jobs(jobs)

    assert len(batches) == 1 and len(batches[0]) == 1
    requested, mirrors = batches[0][0]
    assert requested["http_method"] == "PATCH"
    assert [mirror["http_method"] for mirror in mirrors] == ["PUT"]


CACHE_ROUTES = """Rails.application.routes.draw do
  get '/widgets', to: 'widgets#index'
end
"""

CACHE_CONTROLLER = """class WidgetsController < ApplicationController
  def index
    render json: []
  end
end
"""


def _cache_dir(out_dir: Path) -> Path:
    return out_dir / "metadata_cache" / "rails"


def _cache_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "cache_app"
    _write(repo_root / "config" / "routes.rb", CACHE_ROUTES)
    _write(repo_root / "app" / "controllers" / "widgets_controller.rb", CACHE_CONTROLLER)
    return repo_root


def _cache_run(monkeypatch, repo_root: Path, out_dir: Path):
    """One full run over the repo, with every LLM call answered locally."""
    _prepare_run(monkeypatch, repo_root, out_dir)
    # The incremental pass would skip the work these tests are about.
    monkeypatch.setattr(run_module, "_maybe_incremental_update", lambda *args: None)
    fragment = {"paths": {"/widgets": {"get": {"summary": "listed"}}}}
    monkeypatch.setattr(
        run_module, "get_batch_definition_swagger", lambda *args, **kwargs: fragment
    )
    monkeypatch.setattr(
        run_module, "get_function_definition_swagger", lambda *args, **kwargs: fragment
    )
    return run_swagger_generation("http://localhost:3000")


def _count_parses(monkeypatch) -> list:
    """Every file the run actually hands to the parser."""
    parsed = []
    real_process_file = run_module.process_file

    def _counting(file_path, directory_path):
        parsed.append(file_path)
        return real_process_file(file_path, directory_path)

    monkeypatch.setattr(run_module, "process_file", _counting)
    return parsed


def test_metadata_is_cached_outside_the_scanned_repo(tmp_path, monkeypatch):
    """The run built its metadata directory inside the repo it was reading."""
    repo_root = _cache_repo(tmp_path)
    out_dir = tmp_path / "out"
    before = sorted(path.name for path in repo_root.iterdir())

    _cache_run(monkeypatch, repo_root, out_dir)

    entries = sorted(path.name for path in _cache_dir(out_dir).glob("*.json"))
    assert len(entries) == 2
    assert any(name.startswith("widgets_controller_") for name in entries)
    assert sorted(path.name for path in repo_root.iterdir()) == before


def test_a_second_run_over_unchanged_files_parses_nothing(tmp_path, monkeypatch):
    """The cache is content addressed, so untouched files are never reparsed."""
    repo_root = _cache_repo(tmp_path)
    out_dir = tmp_path / "out"
    _cache_run(monkeypatch, repo_root, out_dir)

    parsed = _count_parses(monkeypatch)
    _cache_run(monkeypatch, repo_root, out_dir)

    assert parsed == []


def test_an_edited_file_is_reparsed_and_its_stale_entry_dropped(tmp_path, monkeypatch):
    """A new content hash means a new entry, and the old one has to go."""
    repo_root = _cache_repo(tmp_path)
    out_dir = tmp_path / "out"
    _cache_run(monkeypatch, repo_root, out_dir)
    controller = repo_root / "app" / "controllers" / "widgets_controller.rb"
    stale = _cache_dir(out_dir) / run_module._metadata_cache_filename(
        str(controller), run_module._content_hash(str(controller))
    )
    assert stale.exists()

    _write(controller, CACHE_CONTROLLER.replace("render json: []", "render json: [1]"))
    parsed = _count_parses(monkeypatch)
    _cache_run(monkeypatch, repo_root, out_dir)

    assert parsed == [str(controller)]
    assert not stale.exists()
    assert len(list(_cache_dir(out_dir).glob("*.json"))) == 2


def test_an_existing_qodex_directory_in_the_repo_is_left_alone(tmp_path, monkeypatch):
    """That directory used to be rebuilt and then deleted, user files and all."""
    repo_root = _cache_repo(tmp_path)
    kept = _write(repo_root / "qodex_file_information" / "notes.txt", "mine\n")
    out_dir = tmp_path / "out"

    _cache_run(monkeypatch, repo_root, out_dir)

    assert kept.read_text(encoding="utf-8") == "mine\n"


def test_a_stale_version_marker_wipes_the_cache(tmp_path, monkeypatch):
    """Entries written by an older metadata format must not be read back."""
    repo_root = _cache_repo(tmp_path)
    out_dir = tmp_path / "out"
    _cache_run(monkeypatch, repo_root, out_dir)
    cache_dir = _cache_dir(out_dir)
    (cache_dir / "cache_version").write_text("0", encoding="utf-8")

    parsed = _count_parses(monkeypatch)
    _cache_run(monkeypatch, repo_root, out_dir)

    assert len(parsed) == 2
    assert (cache_dir / "cache_version").read_text(
        encoding="utf-8"
    ) == run_module.METADATA_CACHE_VERSION

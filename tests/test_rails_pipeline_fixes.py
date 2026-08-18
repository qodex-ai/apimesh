"""Regression tests for the rails pipeline audit fixes.

Covers the positional `scope` prefix, ignore matching that is relative to the
scanned repo root, the LLM fragment re-keying, and the empty-extraction
contract. Nothing here touches the network: no endpoint is ever generated.
"""

import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("APIMESH_CONFIG_PATH", str(REPO_ROOT / "config.yml"))

from rails_pipeline import definition_swagger_generator as generator_module
from rails_pipeline import run_swagger_generation as run_module
from rails_pipeline.find_api_definition_files import find_api_definition_files
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
            ("GET", "/v2/widgets/:widget_id", "show"),
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
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    output_filepath = _prepare_run(monkeypatch, repo_root, output_dir)
    output_filepath.write_text(
        json.dumps({"info": {"commit_reference": "base"}, "paths": existing_paths or {}}),
        encoding="utf-8",
    )
    api_index_path = output_dir / "api_index.json"
    api_index_path.write_text(json.dumps(existing_index), encoding="utf-8")

    monkeypatch.setattr(
        run_module,
        "get_changed_files_since",
        lambda *args, **kwargs: {os.path.abspath(str(controller))},
    )
    monkeypatch.setattr(run_module, "get_git_commit_hash", lambda: "head")
    jobs = [
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
        "GET /orders": {
            "files": [{"file_path": os.path.abspath(str(controller)), "imports": []}]
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
    assert set(json.loads(api_index_path.read_text(encoding="utf-8"))) == {"GET /orders"}
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
    assert set(index) == {"GET /users/{id}", "POST /users/{id}"}
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
    assert set(json.loads(api_index_path.read_text(encoding="utf-8"))) == {"GET /orders"}


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
        return {"paths": {"/widgets/{widget_id}": {"patch": {"summary": "Update widget"}}}}

    monkeypatch.setattr(run_module, "get_batch_definition_swagger", _fake_batch)

    swagger = run_swagger_generation("http://localhost:3000")

    assert batch_calls == ["PATCH /widgets/{widget_id}"]
    path_item = swagger["paths"]["/widgets/{widget_id}"]
    assert path_item["put"] == path_item["patch"] == {"summary": "Update widget"}
    assert path_item["put"] is not path_item["patch"]
    index = json.loads((output_dir / "api_index.json").read_text(encoding="utf-8"))
    assert set(index) == {"PATCH /widgets/{widget_id}", "PUT /widgets/{widget_id}"}


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
    }


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

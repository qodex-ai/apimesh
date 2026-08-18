"""Direct coverage of the layer the four language pipelines share.

Each pipeline exercises this code through its own wrappers, which makes a
regression here read as four unrelated failures. These tests pin the shared
contracts on their own, with a stand-in route normalizer where a pipeline would
pass its language's one.
"""

import json
import os

import pipeline_common
from utils import num_tokens_from_string


def _normalize_route(route):
    """A stand-in for a pipeline's normalizer: :id becomes {id}, always rooted."""
    if not route or not isinstance(route, str):
        return ""
    normalized = route.strip()
    while ":" in normalized:
        head, _, tail = normalized.partition(":")
        name, slash, rest = tail.partition("/")
        normalized = f"{head}{{{name}}}{slash}{rest}"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def _endpoint_key(route, method):
    return pipeline_common.endpoint_key(route, method, _normalize_route)


# --------------------------------------------------------------------------- #
# Endpoint keys and operation handling
# --------------------------------------------------------------------------- #


def test_endpoint_key_uses_the_normalizer_it_is_handed():
    assert _endpoint_key("/users/:id", "get") == "GET /users/{id}"
    assert _endpoint_key("users", "post") == "POST /users"


def test_endpoint_key_without_a_method_is_marked_unknown():
    """A route the extractor found no verb for still needs a stable key."""
    assert _endpoint_key("/users", None) == "UNKNOWN /users"
    assert _endpoint_key(None, "GET") == "GET"


def test_split_endpoint_key_round_trips_a_key():
    assert pipeline_common.split_endpoint_key("GET /users/{id}") == ("GET", "/users/{id}")
    assert pipeline_common.split_endpoint_key("GET") == ("GET", "")
    assert pipeline_common.split_endpoint_key("") == ("UNKNOWN", "")


def test_select_operation_ignores_everything_that_is_not_a_verb():
    """A path item legally carries `parameters` and vendor extensions."""
    path_item = {"parameters": [], "x-owner": {"team": "payments"}, "GET": {"summary": "s"}}
    assert pipeline_common.select_operation(path_item) == ("get", {"summary": "s"})
    assert pipeline_common.select_operation({"x-owner": {"team": "p"}}) == (None, None)
    assert pipeline_common.select_operation({"get": "not a dict"}) == (None, None)
    assert pipeline_common.select_operation("not a path item") == (None, None)


def test_first_operation_walks_the_paths_of_a_fragment():
    fragment = {
        "paths": {
            "/skipped": {"x-metadata": {"owner": "payments"}},
            "/users": {"post": {"summary": "create"}},
        }
    }
    assert pipeline_common.first_operation(fragment) == ("post", {"summary": "create"})
    assert pipeline_common.first_operation({"paths": {}}) == (None, None)
    assert pipeline_common.first_operation(None) == (None, None)


def test_normalize_operation_fields_renames_legacy_keys_and_keeps_the_new_one():
    operation = {
        "api_description": "old",
        "description": "new",
        "authorization_tag": "Authorization Required",
    }
    pipeline_common.normalize_operation_fields(operation)
    assert operation == {
        "description": "new",
        "x-authorization-tag": "Authorization Required",
    }


def test_merge_paths_overwrites_only_the_verb_it_carries():
    target = {"paths": {"/users": {"get": {"summary": "kept"}}}}
    pipeline_common.merge_paths(target, {"paths": {"/users": {"post": {"summary": "new"}}}})
    assert target["paths"]["/users"] == {
        "get": {"summary": "kept"},
        "post": {"summary": "new"},
    }


# --------------------------------------------------------------------------- #
# Spec migration
# --------------------------------------------------------------------------- #


LEGACY_SPEC = {
    "info": {
        "generated_at": "2025-01-01T00:00:00Z",
        "commit_reference": "abc123",
        "github_repo_url": "https://github.com/o/r",
    },
    "paths": {"/users/:id": {"get": {"api_description": "old", "summary": "s"}}},
}


def test_migrate_legacy_spec_upgrades_info_operations_and_path_keys():
    migrated = pipeline_common.migrate_legacy_spec(
        json.loads(json.dumps(LEGACY_SPEC)), _normalize_route
    )
    assert migrated["info"] == {
        "x-generated-at": "2025-01-01T00:00:00Z",
        "x-commit-reference": "abc123",
        "x-github-repo-url": "https://github.com/o/r",
    }
    operation = migrated["paths"]["/users/{id}"]["get"]
    assert operation == {"description": "old", "summary": "s"}


def test_canonicalize_path_keys_lets_the_canonical_spelling_win():
    """A spec holding both spellings must not lose the canonical operation."""
    paths = {
        "/users/{id}": {"get": {"summary": "canonical"}},
        "/users/:id": {"get": {"summary": "legacy"}, "post": {"summary": "only legacy"}},
    }
    canonical = pipeline_common.canonicalize_path_keys(paths, _normalize_route)
    assert set(canonical) == {"/users/{id}"}
    assert canonical["/users/{id}"] == {
        "get": {"summary": "canonical"},
        "post": {"summary": "only legacy"},
    }


def test_canonicalize_index_keys_lets_the_canonical_key_win():
    index = {
        "GET /users/{id}": {"files": [{"file_path": "/repo/canonical.go"}]},
        "GET /users/:id": {"files": [{"file_path": "/repo/legacy.go"}]},
        "POST /orders": {"files": []},
    }
    canonical = pipeline_common.canonicalize_index_keys(index, _endpoint_key)
    assert set(canonical) == {"GET /users/{id}", "POST /orders"}
    assert canonical["GET /users/{id}"]["files"][0]["file_path"] == "/repo/canonical.go"


def test_load_existing_swagger_migrates_what_it_reads(tmp_path):
    spec_path = tmp_path / "swagger.json"
    spec_path.write_text(json.dumps(LEGACY_SPEC), encoding="utf-8")
    loaded = pipeline_common.load_existing_swagger(str(spec_path), _normalize_route)
    assert loaded["info"]["x-commit-reference"] == "abc123"
    assert "/users/{id}" in loaded["paths"]


def test_load_existing_swagger_survives_a_missing_or_broken_file(tmp_path):
    assert pipeline_common.load_existing_swagger(str(tmp_path / "gone.json"), _normalize_route) is None
    broken = tmp_path / "swagger.json"
    broken.write_text("{not json", encoding="utf-8")
    assert pipeline_common.load_existing_swagger(str(broken), _normalize_route) is None


def test_remove_endpoint_normalizes_a_key_written_before_canonicalization():
    swagger = {"paths": {"/users/{id}": {"get": {}, "post": {}}}}
    pipeline_common.remove_endpoint_from_swagger(swagger, "GET /users/:id", _normalize_route)
    assert swagger["paths"]["/users/{id}"] == {"post": {}}


def test_removing_the_last_verb_drops_the_whole_path():
    swagger = {"paths": {"/users": {"get": {}}}}
    pipeline_common.remove_endpoint_from_swagger(swagger, "GET /users", _normalize_route)
    assert swagger["paths"] == {}


def test_an_unknown_method_removes_every_verb_of_the_path():
    swagger = {"paths": {"/users": {"get": {}, "post": {}}}}
    pipeline_common.remove_endpoint_from_swagger(swagger, "UNKNOWN /users", _normalize_route)
    assert swagger["paths"] == {}


def test_apply_host_overrides_the_stored_server():
    swagger = {"servers": [{"url": "https://old.example.com"}]}
    assert pipeline_common.apply_host(swagger, "https://new.example.com")["servers"] == [
        {"url": "https://new.example.com"}
    ]
    # No host given leaves whatever the stored spec carries.
    assert pipeline_common.apply_host(swagger, None)["servers"] == [
        {"url": "https://new.example.com"}
    ]
    assert pipeline_common.apply_host(None, "https://new.example.com") is None


def test_record_coverage_writes_the_completeness_block():
    swagger = pipeline_common.record_coverage({}, 10, 6, 3, 1, dropped=2)
    assert swagger["info"]["x-apimesh-coverage"] == {
        "endpoints_extracted": 10,
        "generated": 6,
        "skipped_unchanged": 3,
        "failed": 1,
        "dropped_routes": 2,
    }
    assert "dropped_routes" not in pipeline_common.record_coverage({}, 1, 1, 0, 0)["info"][
        "x-apimesh-coverage"
    ]


def test_base_commit_reads_the_legacy_key_too():
    assert pipeline_common.base_commit_of({"info": {"x-commit-reference": "new"}}) == "new"
    assert pipeline_common.base_commit_of({"info": {"commit_reference": "old"}}) == "old"
    assert pipeline_common.base_commit_of({"info": {}}) is None


def test_stamp_commit_reference_drops_the_legacy_spelling():
    swagger = {"info": {"commit_reference": "old"}}
    pipeline_common.stamp_commit_reference(swagger, "head")
    assert swagger["info"] == {"x-commit-reference": "head"}


# --------------------------------------------------------------------------- #
# api_index
# --------------------------------------------------------------------------- #


def _endpoint(route, method, file_path="/repo/app.go", **extra):
    entry = {"route": route, "http_method": method, "file_path": file_path}
    entry.update(extra)
    return entry


def _job_method(endpoint):
    return endpoint.get("http_method") or endpoint.get("method")


def _build_index(endpoints, collect_imports=lambda endpoint, path, route: []):
    return pipeline_common.build_api_index(
        endpoints, _endpoint_key, _job_method, collect_imports
    )


def test_build_api_index_keys_entries_and_carries_the_context_hash():
    index = _build_index([_endpoint("/users/:id", "GET", context_hash="deadbeef")])
    assert set(index) == {"GET /users/{id}"}
    entry = index["GET /users/{id}"]
    assert entry["context_hash"] == "deadbeef"
    assert entry["files"] == [{"file_path": os.path.abspath("/repo/app.go"), "imports": []}]


def test_build_api_index_skips_an_endpoint_with_no_file():
    assert _build_index([_endpoint("/users", "GET", file_path=None)]) == {}


def test_build_api_index_merges_and_dedupes_the_files_of_one_key():
    """The same route reached from two files is one entry with both files."""
    imports = [{"file_path": "/repo/helpers.go", "name": "h", "start_line": 1, "end_line": 2}]
    index = _build_index(
        [_endpoint("/users", "GET"), _endpoint("/users", "GET", file_path="/repo/other.go")],
        collect_imports=lambda endpoint, path, route: list(imports) + list(imports),
    )
    entry = index["GET /users"]
    assert [file_entry["file_path"] for file_entry in entry["files"]] == [
        os.path.abspath("/repo/app.go"),
        os.path.abspath("/repo/other.go"),
    ]
    # The duplicate import is collapsed rather than stored twice.
    assert entry["files"][0]["imports"] == imports


def test_endpoint_imports_records_both_hops_of_a_handler():
    metadata = {"elements": {}}
    in_file = [{"name": "helper", "function_start_line": 5, "function_end_line": 9}]
    imported = [{"origin": "/repo/models.go"}]
    resolved = [{"file_path": "/repo/models.go", "name": "User", "start_line": 1, "end_line": 3}]

    imports = pipeline_common.endpoint_imports(
        _endpoint("/users", "GET", start_line=1, end_line=4),
        "/repo/app.go",
        "/users",
        lambda path: metadata,
        lambda data, start, end, path: (in_file, imported),
        lambda item, route: resolved,
    )
    assert [entry["name"] for entry in imports] == ["helper", "User"]
    assert imports[0]["file_path"] == "/repo/app.go"


def test_endpoint_imports_is_empty_without_line_numbers_or_metadata():
    """An endpoint the extractor could not place records no dependency edges."""

    def _unreachable(*args):
        raise AssertionError("metadata must not be read")

    assert pipeline_common.endpoint_imports(
        _endpoint("/users", "GET"), "/repo/app.go", "/users",
        _unreachable, _unreachable, _unreachable,
    ) == []
    assert pipeline_common.endpoint_imports(
        _endpoint("/users", "GET", start_line=1, end_line=4), "/repo/app.go", "/users",
        lambda path: None, _unreachable, _unreachable,
    ) == []


def test_apply_generated_index_entries_drops_every_failed_key():
    """A failed key must leave the index, or the next run never retries it."""
    updated = {"GET /users": {"files": ["stale"]}, "POST /orders": {"files": []}}
    generated = {"GET /users": {"files": ["fresh"]}, "PUT /items": {"files": []}}
    pipeline_common.apply_generated_index_entries(updated, generated, {"GET /users"})
    assert set(updated) == {"POST /orders", "PUT /items"}


def test_write_and_load_api_index_round_trip(tmp_path):
    output_path = pipeline_common.api_index_output_path(str(tmp_path / "out" / "swagger.json"))
    pipeline_common.write_api_index({"GET /users/:id": {"files": []}}, output_path)
    loaded = pipeline_common.load_existing_api_index(output_path, _endpoint_key)
    # The load canonicalizes, so a legacy-spelled stored key comes back matched.
    assert set(loaded) == {"GET /users/{id}"}


def test_load_existing_api_index_returns_none_when_there_is_none(tmp_path):
    assert pipeline_common.load_existing_api_index(str(tmp_path / "gone.json"), _endpoint_key) is None


# --------------------------------------------------------------------------- #
# Metadata cache
# --------------------------------------------------------------------------- #


def test_content_hash_is_read_once_and_keyed_by_path(tmp_path):
    source = tmp_path / "app.go"
    source.write_text("package main\n", encoding="utf-8")
    twin = tmp_path / "nested"
    twin.mkdir()
    (twin / "app.go").write_text("package main\n", encoding="utf-8")

    cache = {}
    first = pipeline_common.content_hash(str(source), cache)
    assert first and len(first) == 16
    # Identical content in another directory is a different entry: an entry
    # records the file it came from.
    assert pipeline_common.content_hash(str(twin / "app.go"), cache) != first
    # The cached value is served even after the file changes underneath.
    source.write_text("package other\n", encoding="utf-8")
    assert pipeline_common.content_hash(str(source), cache) == first


def test_content_hash_of_an_unreadable_file_is_none(tmp_path):
    assert pipeline_common.content_hash(str(tmp_path / "gone.go"), {}) is None


def test_cache_filename_caps_the_readable_half(tmp_path):
    name = pipeline_common.metadata_cache_filename("a" * 500 + ".go", "0123456789abcdef")
    assert name.endswith("_0123456789abcdef.json")
    assert len(name.encode("utf-8")) < 255


def test_cache_path_is_none_without_a_hash():
    def _boom():
        raise AssertionError("the cache directory must not be resolved")

    assert pipeline_common.metadata_cache_path("/repo/app.go", _boom, None) is None


def test_preparing_the_cache_wipes_it_when_the_version_moved(tmp_path):
    cache_dir = str(tmp_path / "metadata_cache" / "golang")
    pipeline_common.prepare_metadata_cache(cache_dir, "1")
    stale = os.path.join(cache_dir, "app_0123456789abcdef.json")
    with open(stale, "w", encoding="utf-8") as f:
        f.write("{}")

    pipeline_common.prepare_metadata_cache(cache_dir, "1")
    assert os.path.exists(stale)

    pipeline_common.prepare_metadata_cache(cache_dir, "2")
    assert not os.path.exists(stale)
    marker = os.path.join(cache_dir, pipeline_common.METADATA_CACHE_VERSION_FILE)
    with open(marker, encoding="utf-8") as f:
        assert f.read() == "2"


def test_cache_file_metadata_writes_once_and_reuses_the_entry(tmp_path):
    cache_dir = pipeline_common.prepare_metadata_cache(str(tmp_path / "cache"), "1")
    source = tmp_path / "app.go"
    source.write_text("package main\n", encoding="utf-8")
    hashes, entries, parsed = {}, {}, []

    def _cache_path(path):
        return pipeline_common.metadata_cache_path(
            path, lambda: cache_dir, pipeline_common.content_hash(path, hashes)
        )

    def _process(path, directory_path):
        parsed.append(path)
        return {"filename": path, "elements": {}}

    for _ in range(2):
        pipeline_common.cache_file_metadata(
            str(source), str(tmp_path), _cache_path, _process, entries
        )
    assert parsed == [str(source)]
    assert pipeline_common.load_file_metadata(_cache_path(str(source)))["filename"] == str(source)


def test_a_file_that_will_not_parse_is_reported_and_skipped(tmp_path):
    cache_dir = pipeline_common.prepare_metadata_cache(str(tmp_path / "cache"), "1")
    source = tmp_path / "broken.go"
    source.write_text("package main\n", encoding="utf-8")
    entries, reported = {}, []

    def _cache_path(path):
        return pipeline_common.metadata_cache_path(
            path, lambda: cache_dir, pipeline_common.content_hash(path, {})
        )

    def _explode(path, directory_path):
        raise UnicodeDecodeError("utf-8", b"", 0, 1, "boom")

    pipeline_common.cache_file_metadata(
        str(source), str(tmp_path), _cache_path, _explode, entries,
        on_error=lambda path, exc: reported.append(path),
    )
    assert entries == {} and reported == [str(source)]


def test_build_metadata_cache_walks_only_supported_files(tmp_path):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "app.go").write_text("package main\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("hi\n", encoding="utf-8")
    (tmp_path / "vendor" / "dep.go").write_text("package dep\n", encoding="utf-8")
    seen = []

    pipeline_common.build_metadata_cache(
        str(tmp_path),
        lambda: None,
        lambda path, root: "vendor" not in path,
        lambda path: path.endswith(".go"),
        lambda path, root: seen.append(os.path.basename(path)),
    )
    assert seen == ["app.go"]


def test_prune_drops_only_the_entries_this_run_did_not_touch(tmp_path):
    cache_dir = pipeline_common.prepare_metadata_cache(str(tmp_path / "cache"), "1")
    live = os.path.join(cache_dir, "live_0000000000000000.json")
    dead = os.path.join(cache_dir, "dead_1111111111111111.json")
    for path in (live, dead):
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")

    pipeline_common.prune_metadata_cache(cache_dir, {"/repo/live.go": live})
    assert os.path.exists(live) and not os.path.exists(dead)
    # The version marker is not a cache entry and must survive.
    assert os.path.exists(os.path.join(cache_dir, pipeline_common.METADATA_CACHE_VERSION_FILE))


def test_load_file_metadata_tolerates_a_broken_entry(tmp_path):
    broken = tmp_path / "entry.json"
    broken.write_text("{not json", encoding="utf-8")
    assert pipeline_common.load_file_metadata(str(broken)) is None
    assert pipeline_common.load_file_metadata(None) is None


# --------------------------------------------------------------------------- #
# Prompt budget
# --------------------------------------------------------------------------- #


def test_block_text_accepts_lines_or_text():
    assert pipeline_common.block_text(["a\n", "b\n"]) == "a\nb\n"
    assert pipeline_common.block_text("already text") == "already text"
    assert pipeline_common.block_text(None) == ""


def test_truncate_marks_what_it_cut():
    text = "word " * 500
    cut, was_truncated = pipeline_common.truncate_to_tokens(text, 50)
    assert was_truncated and cut.endswith(pipeline_common.TRUNCATION_MARKER)
    assert num_tokens_from_string(cut) <= 50 + num_tokens_from_string(
        pipeline_common.TRUNCATION_MARKER
    )
    assert pipeline_common.truncate_to_tokens("short", 50) == ("short", False)


def test_dedupe_blocks_drops_blanks_and_repeats():
    assert pipeline_common.dedupe_blocks([["a\n"], "   ", ["a\n"], ["b\n"]]) == ["a\n", "b\n"]


def test_fit_context_reports_what_did_not_fit():
    blocks = [["word " * 100], ["other " * 100], ["third " * 100]]
    kept, dropped = pipeline_common.fit_context(blocks, 150)
    assert kept and dropped == len(blocks) - len(kept)
    assert pipeline_common.fit_context(blocks, 0) == ([], 3)


def test_handler_section_without_a_header_is_the_body_alone():
    assert pipeline_common.handler_section("", "body", 100) == ("body", False)
    assert pipeline_common.handler_section("GET /users:", "body", 100) == (
        "GET /users:\nbody",
        False,
    )


def test_pack_batches_closes_on_the_budget_and_on_the_count():
    items = list(range(25))
    by_count = pipeline_common.pack_batches(items, lambda item: 1, 10, 1000)
    assert [len(batch) for batch in by_count] == [10, 10, 5]

    by_budget = pipeline_common.pack_batches(items, lambda item: 40, 10, 100)
    assert [len(batch) for batch in by_budget] == [2] * 12 + [1]


def test_a_single_oversized_item_still_gets_a_batch():
    """One section can fill the budget by itself, and still has to be sent."""
    batches = pipeline_common.pack_batches([1, 2], lambda item: 5000, 10, 100)
    assert [len(batch) for batch in batches] == [1, 1]


def test_group_jobs_by_file_buckets_on_the_source():
    jobs = [
        {"file_path": "/repo/a.go", "route": "/a"},
        {"file_path": "/repo/b.go", "route": "/b"},
        {"file_path": "/repo/a.go", "route": "/c"},
    ]
    grouped = pipeline_common.group_jobs_by_file(jobs)
    assert [len(bucket) for bucket in grouped.values()] == [2, 1]


def test_apply_context_budget_prices_handlers_first(capsys):
    """The handler bodies are what the model cannot document without."""
    handler_sections = [("GET /users:", "handler " * 50)]
    shared_blocks = [["helper " * 2000], ["also dropped " * 2000]]
    kept, sections = pipeline_common.apply_context_budget(
        handler_sections, shared_blocks, "/repo/app.go", 2000, 200
    )
    assert len(sections) == 1 and sections[0].startswith("GET /users:\n")
    assert kept == []
    assert "context truncated for /repo/app.go (2 blocks dropped)" in capsys.readouterr().out


def test_retry_batch_call_gives_up_after_two_unusable_replies():
    calls = []

    def _unusable():
        calls.append(1)
        return {"no paths": True}

    assert pipeline_common.retry_batch_call(_unusable) is None
    assert len(calls) == 2


def test_retry_batch_call_survives_one_raising_attempt():
    """A transient failure costs the retry, not the whole batch."""
    calls = []

    def _second_time_lucky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return {"paths": {"/users": {}}}

    assert pipeline_common.retry_batch_call(_second_time_lucky) == {"paths": {"/users": {}}}
    assert len(calls) == 2


def test_retry_batch_call_stops_at_the_first_usable_reply():
    calls = []

    def _usable():
        calls.append(1)
        return {"paths": {"/users": {}}}

    assert pipeline_common.retry_batch_call(_usable) == {"paths": {"/users": {}}}
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Context hashing and the incremental skeleton
# --------------------------------------------------------------------------- #


def test_context_hash_covers_the_body_and_its_blocks():
    body = ["func handler() {}\n"]
    base = pipeline_common.context_hash([["// helper\n"]], body)
    assert base != pipeline_common.context_hash([["// helper\n"]], ["func other() {}\n"])
    assert base != pipeline_common.context_hash([["// different\n"]], body)
    assert base == pipeline_common.context_hash([["// helper\n"]], body)


def test_context_hash_ignores_the_verb_and_the_route():
    """Unified on the node/rails recipe: two endpoints answered from the same
    prompt text share a hash, which is what lets a rails PUT ride on its PATCH."""
    body = ["def update\n  head :ok\nend\n"]
    blocks = [["# helper\n"]]
    assert pipeline_common.context_hash(blocks, body) == pipeline_common.context_hash(
        list(blocks), list(body)
    )


def test_context_hash_is_unmoved_by_a_repeated_block():
    """The blocks are deduped exactly as the prompt dedupes them."""
    body = ["func handler() {}\n"]
    once = pipeline_common.context_hash([["// helper\n"]], body)
    assert pipeline_common.context_hash([["// helper\n"], ["// helper\n"]], body) == once
    assert pipeline_common.context_hash([["// helper\n"], ["  \n"]], body) == once


def test_context_hash_follows_the_order_the_prompt_sends_blocks_in():
    body = ["func handler() {}\n"]
    assert pipeline_common.context_hash([["a\n"], ["b\n"]], body) != pipeline_common.context_hash(
        [["b\n"], ["a\n"]], body
    )


def test_stored_context_hash_rejects_anything_that_is_not_a_hash():
    assert pipeline_common.stored_context_hash({"context_hash": "abc"}) == "abc"
    assert pipeline_common.stored_context_hash({"context_hash": ""}) is None
    assert pipeline_common.stored_context_hash({"context_hash": 7}) is None
    assert pipeline_common.stored_context_hash({}) is None
    assert pipeline_common.stored_context_hash(None) is None


def test_group_endpoints_buckets_the_jobs_behind_one_key():
    jobs = [_endpoint("/users", "GET"), _endpoint("/users", "GET", file_path="/repo/b.go")]
    grouped = pipeline_common.group_endpoints(jobs, _endpoint_key, _job_method)
    assert list(grouped) == ["GET /users"] and len(grouped["GET /users"]) == 2


def test_endpoint_has_changed_follows_one_dependency_hop():
    entry = {
        "files": [
            {"file_path": "/repo/app.go", "imports": [{"file_path": "/repo/helpers.go"}]}
        ]
    }
    # The handler's own file.
    assert pipeline_common.endpoint_has_changed(entry, [], {"/repo/app.go"})
    # One hop away, in a file it imports.
    assert pipeline_common.endpoint_has_changed(entry, [], {"/repo/helpers.go"})
    # Two hops away is not the hop's job: the context hash decides.
    assert not pipeline_common.endpoint_has_changed(entry, [], {"/repo/far.go"})


def test_a_new_endpoint_is_dirty_from_its_own_file_alone():
    """A key with no index entry yet still has to notice its file changed."""
    assert pipeline_common.endpoint_has_changed(
        None, [{"file_path": "/repo/new.go"}], {"/repo/new.go"}
    )


def test_the_escalation_gate_trips_just_past_half(capsys):
    new_keys = {"a", "b", "c", "d"}
    assert not pipeline_common.should_regenerate_fully({"a", "b"}, new_keys)
    assert pipeline_common.should_regenerate_fully({"a", "b", "c"}, new_keys)
    assert "3 of 4 endpoints affected, running a full regeneration" in capsys.readouterr().out


def test_the_gate_holds_when_nothing_was_extracted():
    assert not pipeline_common.should_regenerate_fully(set(), set())


def test_rebuild_unchanged_entries_carries_the_matched_hash_over():
    """The entry is rebuilt from the current extraction so a moved helper is
    tracked, but the hash stays: nothing was regenerated."""
    endpoint_map = {"GET /users": [_endpoint("/users", "GET", file_path="/repo/moved.go")]}
    existing_index = {"GET /users": {"files": [{"file_path": "/repo/old.go"}], "context_hash": "kept"}}
    updated_index = {}

    pipeline_common.rebuild_unchanged_index_entries(
        ["GET /users"], endpoint_map, existing_index, updated_index, _build_index
    )
    entry = updated_index["GET /users"]
    assert entry["context_hash"] == "kept"
    assert entry["files"][0]["file_path"] == os.path.abspath("/repo/moved.go")


def test_rebuild_leaves_a_key_the_extraction_no_longer_carries():
    updated_index = {}
    pipeline_common.rebuild_unchanged_index_entries(
        ["GET /gone"], {}, {}, updated_index, _build_index
    )
    assert updated_index == {}

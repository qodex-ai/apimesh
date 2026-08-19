"""Regression tests for the java (Spring) pipeline.

Covers the parts of extraction and of the incremental run that would silently
produce a wrong or empty spec, among them:

* the class level @RequestMapping prefix, without which every controller emits
  bare paths;
* the verb of a @RequestMapping, which lives in an attribute rather than in the
  annotation's name, and which fans out when several are listed;
* the mappings an interface declares for the controller that implements it,
  which is how an API first codebase writes every one of its routes;
* ignore matching over the relative path, so a repo checked out below /build
  still discovers its .java files;
* the context hash, which is what keeps an untouched endpoint off the model.
"""

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ["APIMESH_CONFIG_PATH"] = str(REPO_ROOT / "config.yml")

import java_pipeline.definition_swagger_generator as dsg
import java_pipeline.run_swagger_generation as rsg
from java_pipeline.find_api_definition_files import find_api_definition_files
from java_pipeline.generate_file_information import process_file
from java_pipeline.identify_api_functions import (
    extraction_drops,
    find_api_endpoints,
    log_extraction_drops,
    reset_extraction_drops,
    reset_type_index,
)
from java_pipeline.run_swagger_generation import _normalize_swagger_fragment


def _write(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _endpoints(tmp_path: Path, source: str) -> list:
    java_file = _write(tmp_path / "UserController.java", source)
    reset_extraction_drops()
    reset_type_index()
    return find_api_endpoints(java_file, str(tmp_path))


def _scan_set_endpoints(tmp_path: Path, files: dict) -> list:
    """Extraction over a whole repo, which is what resolves an implements clause."""
    repo = tmp_path / "repo"
    for relative, source in files.items():
        _write(repo / relative, source)
    reset_extraction_drops()
    reset_type_index()
    endpoints = []
    for path in find_api_definition_files(str(repo)):
        endpoints.extend(find_api_endpoints(Path(path), str(repo)))
    return endpoints


def _routes(tmp_path: Path, source: str) -> set:
    return {
        (endpoint["method"], endpoint["route"])
        for endpoint in _endpoints(tmp_path, source)
    }


ALL_VERBS = """package com.acme.web;

@RestController
@RequestMapping("/api/v1")
public class UserController {

    @GetMapping("/users/{id}")
    public User get(@PathVariable String id) {
        return null;
    }

    @PostMapping("/users")
    public User create(@RequestBody User user) {
        return user;
    }

    @PutMapping("/users/{id}")
    public void replace() {}

    @DeleteMapping("/users/{id}")
    public void remove() {}

    @PatchMapping("/users/{id}")
    public void patch() {}
}
"""


def test_every_verb_annotation_lands_under_the_class_prefix(tmp_path):
    assert _routes(tmp_path, ALL_VERBS) == {
        ("GET", "/api/v1/users/{id}"),
        ("POST", "/api/v1/users"),
        ("PUT", "/api/v1/users/{id}"),
        ("DELETE", "/api/v1/users/{id}"),
        ("PATCH", "/api/v1/users/{id}"),
    }


REQUEST_MAPPING_VERB = """package com.acme.web;

@Controller
@RequestMapping(path = "/admin")
public class AdminController {

    @RequestMapping(value = "/users/{id}", method = RequestMethod.PUT)
    public void replace() {}

    @RequestMapping(method = RequestMethod.POST)
    public void createOnThePrefix() {}
}
"""


def test_request_mapping_reads_its_verb_from_the_method_attribute(tmp_path):
    """The verb is an attribute here, not part of the annotation's name, and a
    mapping with no path of its own sits on the class prefix alone."""
    assert _routes(tmp_path, REQUEST_MAPPING_VERB) == {
        ("PUT", "/admin/users/{id}"),
        ("POST", "/admin"),
    }


FAN_OUT = """package com.acme.web;

@RestController
@RequestMapping({"/api", "/api/v2"})
public class ItemController {

    @RequestMapping(value = {"/items", "/things"}, method = {RequestMethod.PUT, RequestMethod.PATCH})
    public void upsert() {}
}
"""


def test_multi_path_and_multi_method_mappings_fan_out(tmp_path):
    """Two class prefixes, two paths and two verbs are eight endpoints, and
    every one of them is a route the service really serves."""
    assert _routes(tmp_path, FAN_OUT) == {
        (verb, f"{prefix}{path}")
        for verb in ("PUT", "PATCH")
        for prefix in ("/api", "/api/v2")
        for path in ("/items", "/things")
    }


NO_METHOD_ATTRIBUTE = """package com.acme.web;

@RestController
public class LegacyController {

    @RequestMapping("/legacy")
    public void legacy() {}
}
"""


def test_a_request_mapping_without_a_method_is_documented_as_get_only(tmp_path, capsys):
    """Spring serves every verb here. Documenting seven operations from one
    handler invents six of them, so only GET is emitted and the assumption is
    reported rather than hidden."""
    assert _routes(tmp_path, NO_METHOD_ATTRIBUTE) == {("GET", "/legacy")}
    assert extraction_drops() == {"assumed_get": 1, "unresolved": 0}

    log_extraction_drops()
    assert "documented 1 @RequestMapping methods" in capsys.readouterr().out


UNREADABLE_MAPPING = """package com.acme.web;

@RestController
public class ConstantController {

    @GetMapping(Paths.USERS)
    public void users() {}

    @GetMapping("/ok")
    public void ok() {}
}
"""


def test_a_path_the_parser_cannot_read_is_dropped_not_invented(tmp_path, capsys):
    """The constant's value is unknowable at parse time; documenting the route
    as the class prefix alone would publish a path nobody serves."""
    assert _routes(tmp_path, UNREADABLE_MAPPING) == {("GET", "/ok")}
    assert extraction_drops() == {"assumed_get": 0, "unresolved": 1}

    log_extraction_drops()
    assert "skipped 1 mappings" in capsys.readouterr().out


NOT_A_CONTROLLER = """package com.acme.service;

@Service
public class UserService {

    @GetMapping("/users")
    public User find(String id) {
        return null;
    }
}
"""


def test_a_service_class_defines_no_endpoints(tmp_path):
    """Nothing routes to a @Service, whatever its methods are annotated with."""
    assert _routes(tmp_path, NOT_A_CONTROLLER) == set()


NO_CLASS_PREFIX = """package com.acme.web;

@RestController
public class RootController {

    @GetMapping("/health")
    public String health() {
        return "ok";
    }

    @GetMapping
    public String index() {
        return "index";
    }
}
"""


def test_a_controller_without_a_class_prefix_keeps_the_method_paths(tmp_path):
    """With no prefix and no path there is nothing left but the root."""
    assert _routes(tmp_path, NO_CLASS_PREFIX) == {("GET", "/health"), ("GET", "/")}


TRAILING_SLASH_PREFIX = """package com.acme.web;

@RestController
@RequestMapping("/api/v1/")
public class UserController {

    @GetMapping("/users")
    public void list() {}
}
"""


def test_prefix_join_never_doubles_slashes(tmp_path):
    assert _routes(tmp_path, TRAILING_SLASH_PREFIX) == {("GET", "/api/v1/users")}


SPAN_SOURCE = """package com.acme.web;

@RestController
public class UserController {

    @GetMapping("/users")
    @ResponseStatus(HttpStatus.OK)
    public List<User> list(@RequestParam int page) {
        return List.of();
    }
}
"""


def test_the_endpoint_span_covers_the_whole_annotated_method(tmp_path):
    """The prompt is read off these lines, so the mapping annotation and the
    parameter annotations have to be inside them."""
    endpoints = _endpoints(tmp_path, SPAN_SOURCE)

    assert len(endpoints) == 1
    endpoint = endpoints[0]
    assert (endpoint["start_line"], endpoint["end_line"]) == (6, 10)
    assert endpoint["name"] == "list"
    assert endpoint["file_path"] == str(tmp_path / "UserController.java")

    lines = SPAN_SOURCE.splitlines()[endpoint["start_line"] - 1 : endpoint["end_line"]]
    assert lines[0].strip() == '@GetMapping("/users")'
    assert lines[-1].strip() == "}"


def test_a_non_ascii_comment_does_not_shift_the_offsets(tmp_path):
    """tree-sitter reports byte offsets. One three-byte character above the
    mappings used to push every later str index out of alignment, and the paths
    were read as garbage."""
    source = ALL_VERBS.replace(
        "@RestController",
        "// Users API, maintained by Renée. See docs/routing.md.\n@RestController",
    )

    assert _routes(tmp_path, source) == _routes(tmp_path, ALL_VERBS)


NESTED_CONTROLLER = """package com.acme.web;

public class Outer {

    @GetMapping("/not-routed")
    public void ignored() {}

    @RestController
    public static class Inner {

        @GetMapping("/routed")
        public void routed() {}
    }
}
"""


def test_only_the_annotated_class_contributes_endpoints(tmp_path):
    """A nested controller is a controller; its enclosing class is not."""
    assert _routes(tmp_path, NESTED_CONTROLLER) == {("GET", "/routed")}


USER_API_INTERFACE = """package com.acme.api;

@RequestMapping("/api/v1")
public interface UserApi {

    @GetMapping("/users/{id}")
    User get(String id);

    @PostMapping("/users")
    User create(User user);
}
"""

UNANNOTATED_IMPLEMENTATION = """package com.acme.web;

import com.acme.api.UserApi;

@RestController
public class UserController implements UserApi {

    @Override
    public User get(String id) {
        return null;
    }

    @Override
    public User create(User user) {
        return user;
    }
}
"""


def test_an_interface_declares_the_mappings_its_implementation_serves(tmp_path):
    """The API first pattern: the contract carries the annotations, the
    controller carries the body. The endpoints belong to the body."""
    endpoints = _scan_set_endpoints(
        tmp_path,
        {
            "api/UserApi.java": USER_API_INTERFACE,
            "web/UserController.java": UNANNOTATED_IMPLEMENTATION,
        },
    )

    assert {(item["method"], item["route"]) for item in endpoints} == {
        ("GET", "/api/v1/users/{id}"),
        ("POST", "/api/v1/users"),
    }
    # Once each, and owned by the implementation: its file, its line span.
    assert len(endpoints) == 2
    assert {item["file_path"] for item in endpoints} == {
        str(tmp_path / "repo" / "web" / "UserController.java")
    }
    get = next(item for item in endpoints if item["method"] == "GET")
    lines = UNANNOTATED_IMPLEMENTATION.splitlines()[
        get["start_line"] - 1 : get["end_line"]
    ]
    assert lines[0].strip() == "@Override"
    assert lines[-1].strip() == "}"


DUPLICATING_IMPLEMENTATION = """package com.acme.web;

import com.acme.api.UserApi;

@RestController
@RequestMapping("/api/v1")
public class UserController implements UserApi {

    @Override
    @GetMapping("/users/{id}")
    public User get(String id) {
        return null;
    }

    @Override
    @PostMapping("/users")
    public User create(User user) {
        return user;
    }
}
"""


def test_annotations_repeated_on_the_implementation_are_not_a_second_endpoint(tmp_path):
    """The interface and the class say the same thing, and the route is served
    once, so it is documented once."""
    endpoints = _scan_set_endpoints(
        tmp_path,
        {
            "api/UserApi.java": USER_API_INTERFACE,
            "web/UserController.java": DUPLICATING_IMPLEMENTATION,
        },
    )

    assert len(endpoints) == 2
    assert {(item["method"], item["route"]) for item in endpoints} == {
        ("GET", "/api/v1/users/{id}"),
        ("POST", "/api/v1/users"),
    }


OVERRIDING_IMPLEMENTATION = """package com.acme.web;

import com.acme.api.UserApi;

@RestController
public class UserController implements UserApi {

    @Override
    @GetMapping("/people/{id}")
    public User get(String id) {
        return null;
    }

    @Override
    public User create(User user) {
        return user;
    }
}
"""


def test_the_implementation_mapping_wins_over_the_interface_one(tmp_path):
    """Spring routes what the bean class declares, and the class prefix still
    comes from the interface because the class declares none."""
    endpoints = _scan_set_endpoints(
        tmp_path,
        {
            "api/UserApi.java": USER_API_INTERFACE,
            "web/UserController.java": OVERRIDING_IMPLEMENTATION,
        },
    )

    assert {(item["method"], item["route"]) for item in endpoints} == {
        ("GET", "/api/v1/people/{id}"),
        ("POST", "/api/v1/users"),
    }


V1_API_INTERFACE = """package com.acme.api;

@RequestMapping("/api/v1")
public interface V1Api {

    @GetMapping("/users/{id}")
    User get(String id);

    @PostMapping("/users")
    User create(User user);
}
"""

V2_API_INTERFACE = """package com.acme.api;

public interface V2Api extends V1Api {
}
"""

V2_IMPLEMENTATION = """package com.acme.web;

import com.acme.api.V2Api;

@RestController
public class UserController implements V2Api {

    @Override
    public User get(String id) {
        return null;
    }

    @Override
    public User create(User user) {
        return user;
    }
}
"""


def test_an_interface_inherits_the_mappings_of_the_one_it_extends(tmp_path):
    """Spring searches the parent interfaces too, so a controller claiming only
    V2Api still serves everything V1Api declares."""
    endpoints = _scan_set_endpoints(
        tmp_path,
        {
            "api/V1Api.java": V1_API_INTERFACE,
            "api/V2Api.java": V2_API_INTERFACE,
            "web/UserController.java": V2_IMPLEMENTATION,
        },
    )

    assert {(item["method"], item["route"]) for item in endpoints} == {
        ("GET", "/api/v1/users/{id}"),
        ("POST", "/api/v1/users"),
    }
    assert len(endpoints) == 2
    assert {item["file_path"] for item in endpoints} == {
        str(tmp_path / "repo" / "web" / "UserController.java")
    }
    # Reached through the chain, so V1Api counts as implemented and nothing is
    # reported as a mapping that never landed on a body.
    assert extraction_drops() == {"assumed_get": 0, "unresolved": 0}


NESTED_CONTRACTS = """package com.acme.api;

public interface Contracts {

    @RequestMapping("/api/v1")
    interface UserApi {

        @GetMapping("/users/{id}")
        User get(String id);
    }

    @RequestMapping("/api/v1")
    interface OrderApi {

        @GetMapping("/orders")
        String list();
    }
}
"""

IMPORTING_NESTED_IMPLEMENTATION = """package com.acme.web;

import com.acme.api.Contracts;

@RestController
public class UserController implements Contracts.UserApi {

    @Override
    public User get(String id) {
        return null;
    }
}
"""

SAME_PACKAGE_NESTED_IMPLEMENTATION = """package com.acme.api;

@RestController
public class OrderController implements Contracts.OrderApi {

    @Override
    public String list() {
        return "[]";
    }
}
"""


def test_a_nested_contract_interface_resolves_through_its_outer_type(tmp_path):
    """`Contracts.UserApi` is com.acme.api.Contracts.UserApi. Indexed as though
    it sat at the top level, it matched no implements clause and every mapping
    it declares was thrown away."""
    endpoints = _scan_set_endpoints(
        tmp_path,
        {
            "api/Contracts.java": NESTED_CONTRACTS,
            "web/UserController.java": IMPORTING_NESTED_IMPLEMENTATION,
            "api/OrderController.java": SAME_PACKAGE_NESTED_IMPLEMENTATION,
        },
    )

    # Resolved through the import in one file, through the package in the other.
    assert {
        (item["method"], item["route"], Path(item["file_path"]).name)
        for item in endpoints
    } == {
        ("GET", "/api/v1/users/{id}", "UserController.java"),
        ("GET", "/api/v1/orders", "OrderController.java"),
    }
    assert extraction_drops() == {"assumed_get": 0, "unresolved": 0}


def test_a_mapped_interface_nobody_implements_emits_nothing(tmp_path, capsys):
    """There is no body to document, and inventing one from the signature would
    publish routes no bean serves."""
    endpoints = _scan_set_endpoints(tmp_path, {"api/UserApi.java": USER_API_INTERFACE})

    assert endpoints == []
    assert extraction_drops() == {"assumed_get": 0, "unresolved": 2}

    log_extraction_drops()
    assert "skipped 2 mappings" in capsys.readouterr().out


NEGOTIATED_CONTROLLER = """package com.acme.web;

@RestController
@RequestMapping("/api/v1")
public class OrderController {

    @PostMapping(value = "/orders", consumes = "application/json")
    public void createFromJson(@RequestBody Order order) {}

    @PostMapping(value = "/orders", consumes = "application/xml")
    public void createFromXml(@RequestBody OrderXml order) {}
}
"""


def test_content_negotiation_variants_become_one_endpoint_carrying_both_bodies(
    tmp_path, monkeypatch
):
    """Two handlers, one operation. Dropping the second used to lose the xml
    request body entirely, so both bodies are documented together, each under
    the condition that picks it."""
    repo = tmp_path / "repo"
    controller = _write(repo / "web" / "OrderController.java", NEGOTIATED_CONTROLLER)
    _prepare_run(monkeypatch, repo, tmp_path / "out")
    reset_extraction_drops()
    reset_type_index()

    jobs = rsg._dedupe_endpoint_jobs(find_api_endpoints(controller, str(repo)))

    assert len(jobs) == 1
    _, body = rsg.provide_context_codeblock(str(repo), jobs[0])
    prompt_body = "".join(body)
    assert "// Mapping conditions: consumes=application/json" in prompt_body
    assert "// Mapping conditions: consumes=application/xml" in prompt_body
    assert "createFromJson" in prompt_body
    assert "createFromXml" in prompt_body


def test_two_handlers_that_differ_by_nothing_stay_one_job():
    """Nothing tells them apart, so the second is the same endpoint registered
    twice and costs no second call."""
    jobs = rsg._dedupe_endpoint_jobs([_job("/orders", "POST"), _job("/orders", "POST")])

    assert len(jobs) == 1
    assert "variants" not in jobs[0]


CLASS_LEVEL_PRODUCES = """package com.acme.web;

@RestController
@RequestMapping(value = "/api/v1", produces = "application/json")
public class ReportController {

    @GetMapping("/reports")
    public String reports() {
        return "[]";
    }
}
"""


def test_class_level_conditions_reach_the_endpoint_and_the_prompt(tmp_path, monkeypatch):
    """The media type the whole controller answers in is part of the operation,
    and it is only written once, on the class."""
    repo = tmp_path / "repo"
    controller = _write(repo / "web" / "ReportController.java", CLASS_LEVEL_PRODUCES)
    _prepare_run(monkeypatch, repo, tmp_path / "out")
    reset_extraction_drops()
    reset_type_index()

    endpoints = find_api_endpoints(controller, str(repo))

    assert [item.get("conditions") for item in endpoints] == [
        {"produces": ["application/json"]}
    ]
    _, body = rsg.provide_context_codeblock(str(repo), endpoints[0])
    assert "// Mapping conditions: produces=application/json" in "".join(body)


NESTED_DTO = """package com.acme;

public class Dto {

    public static class UserRequest {
        public String id;
    }
}
"""

NESTED_IMPORT_CONTROLLER = """package com.acme.web;

import com.acme.Dto.UserRequest;

@RestController
public class NestedController {

    @PostMapping("/users")
    public void create(@RequestBody UserRequest request) {}
}
"""


def test_a_nested_type_import_resolves_to_the_file_of_its_outer_type(tmp_path):
    """com.acme.Dto.UserRequest is declared inside com/acme/Dto.java; there is
    no Dto/UserRequest.java, and looking for one recorded no edge at all."""
    repo = tmp_path / "repo"
    dto = _write(repo / "src/main/java/com/acme/Dto.java", NESTED_DTO)
    controller = _write(
        repo / "src/main/java/com/acme/web/NestedController.java",
        NESTED_IMPORT_CONTROLLER,
    )

    info = process_file(str(controller), str(repo))

    nested = {item["imported_name"]: item for item in info["imports"]}["UserRequest"]
    assert nested["origin"] == str(dto)
    assert nested["path_exists"] is True


CONTROLLER_WITH_IMPORTS = """package com.acme.web;

import com.acme.model.User;
import com.acme.service.UserService;

@RestController
@RequestMapping("/api/v1")
public class UserController {

    private final UserService userService;

    public UserController(UserService userService) {
        this.userService = userService;
    }

    @GetMapping("/users/{id}")
    public User get(@PathVariable String id) {
        return userService.find(id);
    }
}
"""

USER_SERVICE = """package com.acme.service;

import com.acme.model.User;

public class UserService {

    public User find(String id) {
        return new User(id);
    }
}
"""

USER_MODEL = """package com.acme.model;

public class User {

    private final String id;

    public User(String id) {
        this.id = id;
    }
}
"""


def _spring_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    source_root = repo / "src" / "main" / "java" / "com" / "acme"
    _write(source_root / "web" / "UserController.java", CONTROLLER_WITH_IMPORTS)
    _write(source_root / "service" / "UserService.java", USER_SERVICE)
    _write(source_root / "model" / "User.java", USER_MODEL)
    return repo


def test_an_import_resolves_to_the_file_of_its_package(tmp_path):
    """com.acme.service.UserService is the file sitting under com/acme/service,
    and the simple name is what every usage site writes."""
    repo = _spring_repo(tmp_path)
    controller = repo / "src/main/java/com/acme/web/UserController.java"

    info = process_file(str(controller), str(repo))

    by_name = {item["imported_name"]: item for item in info["imports"]}
    assert set(by_name) == {"User", "UserService"}
    assert by_name["UserService"]["origin"] == str(
        repo / "src/main/java/com/acme/service/UserService.java"
    )
    assert by_name["UserService"]["path_exists"] is True
    # The type is named on the field and in the constructor, never inside the
    # handler, which is why imports are scoped to the file.
    assert by_name["UserService"]["usage_lines"] == [10, 12]


def test_wildcard_imports_record_the_package_they_name(tmp_path):
    repo = _spring_repo(tmp_path)
    controller = _write(
        repo / "src/main/java/com/acme/web/WildcardController.java",
        """package com.acme.web;

import com.acme.service.*;

@RestController
public class WildcardController {

    private final UserService userService;

    @GetMapping("/users")
    public void list() {}
}
""",
    )

    info = process_file(str(controller), str(repo))

    wildcard = info["imports"][0]
    assert wildcard["imported_name"] == "*"
    assert wildcard["origin"] == str(repo / "src/main/java/com/acme/service")
    # The package binds every one of its types, so the usage says which one.
    assert [usage["name"] for usage in wildcard["usages"]] == ["UserService"]


def test_ignored_dirs_are_matched_relative_to_the_repo_root(tmp_path):
    """"build" above the repo root is not the repo's own build directory."""
    repo_root = tmp_path / "build" / "myrepo"
    _write(repo_root / "web" / "UserController.java", ALL_VERBS)
    _write(repo_root / "target" / "Generated.java", ALL_VERBS)
    _write(repo_root / "web" / "Plain.java", "package com.acme.web;\n\nclass Plain {}\n")

    found = find_api_definition_files(str(repo_root))

    assert found == [str(repo_root / "web" / "UserController.java")]


def test_fragment_is_rekeyed_under_the_extractor_route():
    """The model's own path and method are discarded."""
    fragment = {
        "paths": {
            "/rewritten/{id}": {
                "parameters": [],
                "get": {"summary": "Fetch a user", "responses": {}},
            }
        }
    }

    assert _normalize_swagger_fragment(fragment, "/api/v1/users/{id}", "GET") == {
        "/api/v1/users/{id}": {"get": {"summary": "Fetch a user", "responses": {}}}
    }


def test_invalid_fragments_are_rejected():
    assert _normalize_swagger_fragment(None, "/users", "GET") is None
    assert _normalize_swagger_fragment({}, "/users", "GET") is None
    assert _normalize_swagger_fragment({"paths": {}}, "/users", "GET") is None
    assert _normalize_swagger_fragment({"paths": {"/x": {"get": {}}}}, None, "GET") is None


def test_batch_selection_keeps_only_the_requested_verb():
    """One path carries several operations; an endpoint documents exactly one."""
    payload = {
        "paths": {
            "/api/v1/users/{id}": {
                "get": {"summary": "read"},
                "delete": {"summary": "remove"},
            }
        }
    }

    assert rsg._operation_from_batch(payload, "/api/v1/users/{id}", "DELETE") == {
        "/api/v1/users/{id}": {"delete": {"summary": "remove"}}
    }
    assert rsg._operation_from_batch(payload, "/api/v1/users/{id}", "PUT") is None


def test_batch_renames_legacy_operation_fields():
    payload = {
        "paths": {"/users": {"get": {"api_description": "legacy", "module_tag": "Users"}}}
    }

    assert rsg._operation_from_batch(payload, "/users", "GET") == {
        "/users": {"get": {"description": "legacy", "x-module-tag": "Users"}}
    }


def test_routes_are_canonical_openapi():
    """Spring already writes {id}, so only the separators need any work."""
    assert rsg._normalize_route("/api//v1/users/{id}") == "/api/v1/users/{id}"
    assert rsg._normalize_route("api/v1") == "/api/v1"
    assert rsg._normalize_route("") == ""
    assert rsg._endpoint_key("users/{id}", "get") == "GET /users/{id}"


def _job(route, method, file_path="/repo/UserController.java"):
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
        lambda directory_path, job: (
            list(context_blocks or []),
            ["    public void handler() {}\n"],
        ),
    )


def test_batches_are_grouped_by_file_and_capped_at_ten():
    jobs = [_job(f"/a/{index}", "GET", "/repo/A.java") for index in range(12)]
    jobs.append(_job("/b", "POST", "/repo/B.java"))

    batches = rsg._batch_endpoint_jobs(jobs)

    assert [len(batch) for batch in batches] == [10, 2, 1]
    assert {job["file_path"] for job in batches[0]} == {"/repo/A.java"}
    assert batches[2][0]["file_path"] == "/repo/B.java"


def test_one_batch_call_documents_every_endpoint_of_a_file(monkeypatch):
    jobs = [_job("/api/v1/users/{id}", "GET"), _job("/api/v1/users", "POST")]
    calls = []
    _stub_context(monkeypatch, [["// shared helper\n"]])

    def fake_batch(entries, context_blocks, source_file):
        calls.append((entries, source_file))
        return {
            "paths": {
                "/api/v1/users/{id}": {"get": {"summary": "read"}},
                "/api/v1/users": {"post": {"summary": "create"}},
            }
        }

    monkeypatch.setattr(rsg, "get_batch_definition_swagger", fake_batch)
    swagger = {"paths": {}}

    generated, failed = rsg._update_swagger_for_batches(swagger, "/repo", jobs)

    assert len(calls) == 1
    assert [label for label, _ in calls[0][0]] == [
        "GET /api/v1/users/{id}",
        "POST /api/v1/users",
    ]
    assert (len(generated), len(failed)) == (2, 0)
    assert swagger["paths"] == {
        "/api/v1/users/{id}": {"get": {"summary": "read"}},
        "/api/v1/users": {"post": {"summary": "create"}},
    }


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


def test_an_endpoint_missing_from_the_batch_reply_fails_alone(monkeypatch, capsys):
    jobs = [_job("/users", "GET"), _job("/orders", "POST")]
    _stub_context(monkeypatch)
    monkeypatch.setattr(
        rsg,
        "get_batch_definition_swagger",
        lambda entries, blocks, source_file: {
            "paths": {"/orders": {"post": {"summary": "create"}}}
        },
    )
    swagger = {"paths": {}}

    generated, failed = rsg._update_swagger_for_batches(swagger, "/repo", jobs)

    assert [job["route"] for job in failed] == ["/users"]
    assert [job["route"] for job in generated] == ["/orders"]
    assert "skipped GET /users" in capsys.readouterr().out


def test_the_batch_prompt_names_the_framework():
    prompt = dsg.build_batch_prompt(
        [("GET /users", "public void list() {}\n")], [], "/repo/UserController.java"
    )

    assert "Java Spring source file" in prompt
    assert "Paths already use OpenAPI {param} templating." in prompt


def _prepare_run(monkeypatch, repo: Path, output_dir: Path) -> Path:
    """The two paths every run reads: the repo scanned and where output lands."""
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo))
    output_filepath = output_dir / "swagger.json"
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(output_filepath))
    return output_filepath


def _echo_batch(entries, blocks, source_file):
    """One operation per requested label, the way the model answers."""
    paths = {}
    for label, _ in entries:
        method, route = label.split(" ", 1)
        paths.setdefault(route, {})[method.lower()] = {"summary": label}
    return {"paths": paths}


def test_a_spring_repo_runs_end_to_end(tmp_path, monkeypatch):
    """The controller has to survive the whole run: file selection, extraction,
    the metadata of its imports, the batch call, the spec and the index."""
    repo = _spring_repo(tmp_path)
    _write(
        repo / "src/main/java/com/acme/web/UserController.java",
        CONTROLLER_WITH_IMPORTS.replace(
            "    @GetMapping",
            """    @PostMapping("/users")
    public User create(@RequestBody User user) {
        return user;
    }

    @GetMapping""",
        ),
    )
    output_filepath = _prepare_run(monkeypatch, repo, tmp_path / "out")
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "commit")
    monkeypatch.setattr(rsg, "get_github_repo_url", lambda: "https://example.com/repo")
    monkeypatch.setattr(rsg, "get_batch_definition_swagger", _echo_batch)

    swagger = rsg.run_swagger_generation("http://localhost:8080")

    assert set(swagger["paths"]) == {"/api/v1/users", "/api/v1/users/{id}"}
    assert set(swagger["paths"]["/api/v1/users"]) == {"post"}
    assert set(swagger["paths"]["/api/v1/users/{id}"]) == {"get"}
    assert swagger["servers"] == [{"url": "http://localhost:8080"}]

    index = json.loads(
        (output_filepath.parent / "api_index.json").read_text(encoding="utf-8")
    )
    assert set(index) == {"GET /api/v1/users/{id}", "POST /api/v1/users"}
    entry = index["GET /api/v1/users/{id}"]
    assert entry["files"][0]["file_path"].endswith("web/UserController.java")
    imported = {item["file_path"] for item in entry["files"][0]["imports"]}
    assert str(repo / "src/main/java/com/acme/service/UserService.java") in imported
    assert entry["context_hash"]


PARTIALLY_READABLE_CONTROLLER = """package com.acme.web;

@RestController
@RequestMapping("/api/v1")
public class MixedController {

    @GetMapping("/health")
    public String health() {
        return "ok";
    }

    @GetMapping("/users")
    public String users() {
        return "users";
    }

    @GetMapping(Paths.LEGACY)
    public String legacy() {
        return "legacy";
    }
}
"""


def test_the_coverage_block_sums_the_run(tmp_path, monkeypatch):
    """Two endpoints extracted, one documented, one the model left out, and one
    mapping extraction could not read at all."""
    repo = tmp_path / "repo"
    _write(repo / "web" / "MixedController.java", PARTIALLY_READABLE_CONTROLLER)
    _prepare_run(monkeypatch, repo, tmp_path / "out")
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "commit")
    monkeypatch.setattr(rsg, "get_github_repo_url", lambda: "https://example.com/repo")
    monkeypatch.setattr(
        rsg,
        "get_batch_definition_swagger",
        lambda *args, **kwargs: {"paths": {"/api/v1/health": {"get": {"summary": "ok"}}}},
    )

    swagger = rsg.run_swagger_generation("http://localhost:8080")

    assert swagger["info"]["x-apimesh-coverage"] == {
        "endpoints_extracted": 2,
        "generated": 1,
        "skipped_unchanged": 0,
        "failed": 1,
        "dropped_routes": 1,
    }


def test_a_repo_without_endpoints_falls_back(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _write(repo / "Main.java", "package com.acme;\n\npublic class Main {}\n")
    _prepare_run(monkeypatch, repo, tmp_path / "out")

    assert rsg.run_swagger_generation("http://localhost:8080") is None
    assert (
        "apimesh: java parser found 0 endpoints, falling back to generic extraction"
        in capsys.readouterr().out
    )


def _incremental_pass(monkeypatch, repo: Path, out_dir: Path, changed_files, entries_seen=None):
    """One incremental pass over the repo as it is on disk right now."""
    calls = []

    def fake_batch(entries, blocks, source_file):
        calls.append(sorted(label for label, _ in entries))
        if entries_seen is not None:
            entries_seen.extend(entries)
        return _echo_batch(entries, blocks, source_file)

    monkeypatch.setattr(rsg, "get_batch_definition_swagger", fake_batch)
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "head")
    monkeypatch.setattr(rsg, "get_github_repo_url", lambda: "https://example.com/repo")
    monkeypatch.setattr(
        rsg, "get_changed_files_since", lambda *args, **kwargs: changed_files
    )

    swagger = rsg.run_swagger_generation("http://localhost:8080")

    # The caller persists the spec between runs, the pipeline reads it back.
    (out_dir / "swagger.json").write_text(json.dumps(swagger), encoding="utf-8")
    index = json.loads((out_dir / "api_index.json").read_text(encoding="utf-8"))
    return swagger, index, calls


USERS_CONTROLLER = """package com.acme.web;

import com.acme.service.UserService;

@RestController
@RequestMapping("/api/v1")
public class UserController {

    private final UserService userService;

    public UserController(UserService userService) {
        this.userService = userService;
    }

    @GetMapping("/users/{id}")
    public String get(@PathVariable String id) {
        return userService.find(id);
    }

    @DeleteMapping("/users/{id}")
    public void remove(@PathVariable String id) {}
}
"""

STATUS_CONTROLLER = """package com.acme.web;

@RestController
public class StatusController {

    @GetMapping("/health")
    public String health() {
        return "ok";
    }

    @GetMapping("/ready")
    public String ready() {
        return "ready";
    }
}
"""


def test_an_unchanged_endpoint_costs_no_llm_call_and_a_dependency_edit_does(
    tmp_path, monkeypatch, capsys
):
    """Three runs over one checkout. The second regenerates nothing at all, and
    the third only regenerates the controller that imports the edited service.

    Two endpoints of four is the most that can be dirty without the escalation
    gate handing the run back for a full regeneration, so the fixture keeps the
    untouched controller's endpoints out of the dirty set.
    """
    repo = tmp_path / "repo"
    controller = _write(
        repo / "src/main/java/com/acme/web/UserController.java", USERS_CONTROLLER
    )
    _write(repo / "src/main/java/com/acme/web/StatusController.java", STATUS_CONTROLLER)
    service = _write(
        repo / "src/main/java/com/acme/service/UserService.java", USER_SERVICE
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _prepare_run(monkeypatch, repo, out_dir)

    swagger, index, calls = _incremental_pass(monkeypatch, repo, out_dir, set())

    assert set(swagger["paths"]) == {"/api/v1/users/{id}", "/health", "/ready"}
    # One call per file, so both controllers are documented in two calls.
    assert len(calls) == 2
    assert set(index) == {
        "GET /api/v1/users/{id}",
        "DELETE /api/v1/users/{id}",
        "GET /health",
        "GET /ready",
    }
    stored_hash = index["GET /api/v1/users/{id}"]["context_hash"]

    # git reports the controller as touched, but its endpoints read exactly as
    # they did last run, so neither of them reaches the model.
    _, index, calls = _incremental_pass(monkeypatch, repo, out_dir, {str(controller)})

    assert calls == []
    assert index["GET /api/v1/users/{id}"]["context_hash"] == stored_hash
    assert "skipped 2 unchanged endpoints (context hash match)" in capsys.readouterr().out

    # The imported service changes. Only the controller that imports it is one
    # hop away, so the status endpoints are never looked at again.
    service.write_text(
        USER_SERVICE.replace("return new User(id);", "return new User(id.trim());"),
        encoding="utf-8",
    )

    swagger, index, calls = _incremental_pass(monkeypatch, repo, out_dir, {str(service)})

    assert calls == [["DELETE /api/v1/users/{id}", "GET /api/v1/users/{id}"]]
    assert index["GET /api/v1/users/{id}"]["context_hash"] != stored_hash
    assert swagger["info"]["x-apimesh-coverage"] == {
        "endpoints_extracted": 4,
        "generated": 2,
        "skipped_unchanged": 2,
        "failed": 0,
    }


JSON_ORDERS_CONTROLLER = """package com.acme.web;

@RestController
@RequestMapping("/api/v1")
public class OrderController {

    @PostMapping(value = "/orders", consumes = "application/json")
    public void createFromJson(@RequestBody Order order) {}

    @GetMapping("/orders")
    public String list() {
        return "[]";
    }
}
"""

XML_ORDERS_CONTROLLER = """package com.acme.web;

@RestController
@RequestMapping("/api/v1")
public class XmlOrderController {

    @PostMapping(value = "/orders", consumes = "application/xml")
    public void createFromXml(@RequestBody OrderXml order) {}
}
"""


def test_a_variant_arriving_from_another_file_dirties_the_endpoint(
    tmp_path, monkeypatch
):
    """The xml handler of POST /orders is added in a file the endpoint never
    mentioned. Only the json handler's file was watched, so the endpoint read as
    clean and its operation stayed missing the whole xml request body."""
    repo = tmp_path / "repo"
    _write(repo / "src/main/java/com/acme/web/OrderController.java", JSON_ORDERS_CONTROLLER)
    _write(repo / "src/main/java/com/acme/web/StatusController.java", STATUS_CONTROLLER)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _prepare_run(monkeypatch, repo, out_dir)

    _, index, calls = _incremental_pass(monkeypatch, repo, out_dir, set())

    assert len(calls) == 2
    assert set(index) == {
        "POST /api/v1/orders",
        "GET /api/v1/orders",
        "GET /health",
        "GET /ready",
    }
    stored_hash = index["POST /api/v1/orders"]["context_hash"]

    xml = _write(
        repo / "src/main/java/com/acme/web/XmlOrderController.java",
        XML_ORDERS_CONTROLLER,
    )

    entries: list = []
    _, index, calls = _incremental_pass(
        monkeypatch, repo, out_dir, {str(xml)}, entries_seen=entries
    )

    assert calls == [["POST /api/v1/orders"]]
    assert index["POST /api/v1/orders"]["context_hash"] != stored_hash
    # Both bodies reach the prompt, each under the condition that picks it.
    body = dict(entries)["POST /api/v1/orders"]
    assert "// Mapping conditions: consumes=application/json" in body
    assert "// Mapping conditions: consumes=application/xml" in body
    assert "createFromJson" in body and "createFromXml" in body
    # And the xml file is now an edge of its own, so the next edit to it lands.
    assert {item["file_path"] for item in index["POST /api/v1/orders"]["files"]} == {
        str(repo / "src/main/java/com/acme/web/OrderController.java"),
        str(xml),
    }


def _cache_dir(out_dir: Path) -> Path:
    return out_dir / "metadata_cache" / "java"


def test_metadata_is_cached_outside_the_scanned_repo(tmp_path, monkeypatch):
    repo = _spring_repo(tmp_path)
    out_dir = tmp_path / "out"
    _prepare_run(monkeypatch, repo, out_dir)
    monkeypatch.setattr(rsg, "get_git_commit_hash", lambda: "commit")
    monkeypatch.setattr(rsg, "get_github_repo_url", lambda: "https://example.com/repo")
    monkeypatch.setattr(rsg, "get_batch_definition_swagger", _echo_batch)
    before = sorted(path.name for path in repo.iterdir())

    rsg.run_swagger_generation("http://localhost:8080")

    entries = {
        path.name.rsplit("_", 1)[0] for path in _cache_dir(out_dir).glob("*.json")
    }
    assert entries == {"User", "UserController", "UserService"}
    assert sorted(path.name for path in repo.iterdir()) == before

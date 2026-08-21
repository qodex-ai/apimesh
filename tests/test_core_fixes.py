"""Tests for the core audit fixes.

Covers five things that used to fail silently:
  1. git remotes with an embedded token were copied straight into swagger.json
  2. a repo checked out under /var, /tmp or /build matched the ignore list and scanned nothing
  3. one bad LLM response aborted swagger generation for every endpoint
  4. the HTML viewer was always written, with no way to ask for the spec alone
  5. the api host and the model could only be changed by hand-editing config.json
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Configurations() runs at import time in config.py, so both paths must exist first.
os.environ.setdefault("APIMESH_CONFIG_PATH", str(REPO_ROOT / "config.yml"))
os.environ.setdefault("APIMESH_USER_REPO_PATH", str(REPO_ROOT))
# user_config.py reads this at import time and creates the directory, so keep it out of the repo.
os.environ.setdefault(
    "APIMESH_USER_CONFIG_PATH",
    str(Path(tempfile.mkdtemp(prefix="apimesh-user-config-")) / "config.json"),
)

import pytest

import swagger_generation_cli
import user_config
from file_scanner import FileScanner
from swagger_generator import SwaggerGeneration
from user_config import UserConfigurations
from utils import _sanitize_remote_url


@pytest.mark.parametrize(
    "remote_url,expected",
    [
        ("https://ghp_secrettoken@github.com/owner/repo.git", "https://github.com/owner/repo"),
        ("https://user:pass@gitlab.com/group/sub/repo.git", "https://gitlab.com/group/sub/repo"),
        ("https://github.com/owner/repo.git", "https://github.com/owner/repo"),
        ("https://github.com/owner/repo", "https://github.com/owner/repo"),
        ("https://github.com/owner/repo.git?token=abc#frag", "https://github.com/owner/repo"),
        ("git@github.com:owner/repo.git", "https://github.com/owner/repo"),
        ("git@github.com:owner/repo", "https://github.com/owner/repo"),
        ("ssh://git@example.com/owner/repo.git", ""),
        ("/Users/someone/code/repo", ""),
        ("not a url at all", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_sanitize_remote_url(remote_url, expected):
    assert _sanitize_remote_url(remote_url) == expected


def test_sanitize_remote_url_never_leaks_credentials():
    sanitized = _sanitize_remote_url("https://ghp_secrettoken@github.com/owner/repo.git")
    assert "ghp_secrettoken" not in sanitized
    assert "@" not in sanitized


def test_should_process_directory_ignores_only_repo_relative_parts(monkeypatch, tmp_path):
    """A repo living under a directory named "build" must still be scanned."""
    repo_path = tmp_path / "build" / "my-api"
    repo_path.mkdir(parents=True)
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo_path))

    assert FileScanner.should_process_directory(str(repo_path)) is True
    assert FileScanner.should_process_directory(str(repo_path / "src")) is True
    assert FileScanner.should_process_directory(str(repo_path / "src" / "controllers")) is True


def test_should_process_directory_still_ignores_listed_dirs(monkeypatch, tmp_path):
    repo_path = tmp_path / "build" / "my-api"
    repo_path.mkdir(parents=True)
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(repo_path))

    assert FileScanner.should_process_directory(str(repo_path / "node_modules")) is False
    assert FileScanner.should_process_directory(str(repo_path / "src" / "tests")) is False


def _generator():
    """Build a SwaggerGeneration without __init__, which would need a live OpenAI client."""
    return SwaggerGeneration.__new__(SwaggerGeneration)


def _endpoint(path, method):
    return {"path": path, "method": method, "info": "handler source"}


def test_create_swagger_json_rekeys_operation_under_our_path(monkeypatch, tmp_path):
    """The model's own path and method keys are ignored in favour of the endpoint's."""
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(tmp_path))
    generator = _generator()
    generator.generate_endpoint_swagger = lambda endpoint, auth, framework: {
        "paths": {"/hallucinated": {"post": {"summary": "wrong keys, right body"}}}
    }

    swagger = generator.create_swagger_json(
        [_endpoint("/users/{id}", "GET")], "", "flask", "https://api.example.com"
    )

    assert swagger["paths"] == {"/users/{id}": {"get": {"summary": "wrong keys, right body"}}}


def test_create_swagger_json_renames_legacy_operation_fields(monkeypatch, tmp_path):
    """Bare custom keys are not valid OpenAPI 3.0, so an older reply gets re-keyed."""
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(tmp_path))
    generator = _generator()
    generator.generate_endpoint_swagger = lambda endpoint, auth, framework: {
        "paths": {
            "/users": {
                "get": {
                    "api_description": "legacy body",
                    "authorization_tag": "Authorization Required",
                    "module_tag": "Users",
                    "auth_tag": "Auth API",
                    "sensitive_information": True,
                }
            }
        }
    }

    swagger = generator.create_swagger_json(
        [_endpoint("/users", "GET")], "", "flask", "https://api.example.com"
    )

    assert swagger["paths"]["/users"]["get"] == {
        "description": "legacy body",
        "x-authorization-tag": "Authorization Required",
        "x-module-tag": "Users",
        "x-auth-tag": "Auth API",
        "x-sensitive-information": True,
    }


def test_create_swagger_json_keeps_the_new_field_when_both_are_present(monkeypatch, tmp_path):
    """A reply carrying both spellings must not end up with duplicated content."""
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(tmp_path))
    generator = _generator()
    generator.generate_endpoint_swagger = lambda endpoint, auth, framework: {
        "paths": {
            "/users": {
                "get": {
                    "description": "compliant body",
                    "api_description": "legacy body",
                    "x-module-tag": "Users",
                    "module_tag": "LegacyUsers",
                }
            }
        }
    }

    swagger = generator.create_swagger_json(
        [_endpoint("/users", "GET")], "", "flask", "https://api.example.com"
    )

    assert swagger["paths"]["/users"]["get"] == {
        "description": "compliant body",
        "x-module-tag": "Users",
    }


def test_create_swagger_json_info_uses_openapi_extension_keys(monkeypatch, tmp_path):
    """Custom info members need the x- prefix or strict validators reject the spec."""
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(tmp_path))
    monkeypatch.setattr("swagger_generator.get_git_commit_hash", lambda: "abc123")
    monkeypatch.setattr(
        "swagger_generator.get_github_repo_url", lambda: "https://github.com/acme/repo"
    )
    generator = _generator()

    info = generator.create_swagger_json([], "", "flask", "https://api.example.com")["info"]

    assert info["x-commit-reference"] == "abc123"
    assert info["x-github-repo-url"] == "https://github.com/acme/repo"
    assert info["x-generated-at"].endswith("Z")
    assert not {"generated_at", "commit_reference", "github_repo_url"} & set(info)


def test_create_swagger_json_survives_one_bad_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(tmp_path))
    generator = _generator()

    def generate(endpoint, auth, framework):
        if endpoint["path"] == "/broken":
            raise RuntimeError("model exploded")
        return {"paths": {endpoint["path"]: {"get": {"summary": "ok"}}}}

    generator.generate_endpoint_swagger = generate

    swagger = generator.create_swagger_json(
        [_endpoint("/broken", "GET"), _endpoint("/healthy", "GET")],
        "",
        "flask",
        "https://api.example.com",
    )

    assert list(swagger["paths"].keys()) == ["/healthy"]


@pytest.mark.parametrize(
    "fragment",
    [None, {}, "not a dict", {"paths": {}}, {"paths": "nope"}, {"paths": {"/x": {}}}],
)
def test_create_swagger_json_rejects_unusable_fragments(monkeypatch, tmp_path, fragment):
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(tmp_path))
    generator = _generator()
    generator.generate_endpoint_swagger = lambda endpoint, auth, framework: fragment

    with pytest.raises(RuntimeError):
        generator.create_swagger_json(
            [_endpoint("/users", "GET")], "", "flask", "https://api.example.com"
        )


def test_create_swagger_json_with_no_endpoints_returns_empty_paths(monkeypatch, tmp_path):
    """Zero endpoints in is not a generation failure, the CLI handles that case."""
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(tmp_path))
    generator = _generator()

    swagger = generator.create_swagger_json([], "", "flask", "https://api.example.com")

    assert swagger["paths"] == {}


def test_call_chat_completion_retries_transient_failures(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    generator = _generator()
    attempts = []

    class FlakyClient:
        def call_chat_completion(self, messages):
            attempts.append(messages)
            if len(attempts) < 3:
                raise RuntimeError("rate limited")
            return "swagger body"

    generator.openai_client = FlakyClient()

    assert generator._call_chat_completion_with_retry([]) == "swagger body"
    assert len(attempts) == 3


def test_call_chat_completion_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    generator = _generator()
    attempts = []

    class DeadClient:
        def call_chat_completion(self, messages):
            attempts.append(messages)
            raise RuntimeError("still down")

    generator.openai_client = DeadClient()

    with pytest.raises(RuntimeError):
        generator._call_chat_completion_with_retry([])
    assert len(attempts) == 3


SAMPLE_SWAGGER = {"openapi": "3.0.0", "paths": {"/users": {"get": {"summary": "list users"}}}}


def test_save_swagger_json_writes_html_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(tmp_path))
    monkeypatch.delenv("APIMESH_SKIP_HTML", raising=False)
    output_path = tmp_path / "apimesh" / "swagger.json"

    SwaggerGeneration.save_swagger_json(dict(SAMPLE_SWAGGER), str(output_path))

    assert json.loads(output_path.read_text())["paths"] == SAMPLE_SWAGGER["paths"]
    assert (output_path.parent / "apimesh-docs.html").exists()


@pytest.mark.parametrize("flag", ["1", "true", "TRUE"])
def test_save_swagger_json_skips_html_when_opted_out(monkeypatch, tmp_path, flag):
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("APIMESH_SKIP_HTML", flag)
    output_path = tmp_path / "apimesh" / "swagger.json"

    SwaggerGeneration.save_swagger_json(dict(SAMPLE_SWAGGER), str(output_path))

    assert output_path.exists()
    assert not (output_path.parent / "apimesh-docs.html").exists()


def test_all_routing_patterns_are_valid_regexes_and_match_their_framework():
    """Guards config.yml: every pattern must compile, and the nestjs set must
    actually match real NestJS code (the old patterns were double-escaped and
    raised re.error at match time)."""
    import re

    import yaml

    config_data = yaml.safe_load((REPO_ROOT / "config.yml").read_text(encoding="utf-8"))
    nest_sample = "@Controller('users')\nexport class UsersController {\n  @Get(':id')\n  findOne() {}\n}\nNestFactory.create(AppModule);\n"
    for patterns in config_data["routing_patterns_map"].values():
        for pattern in patterns:
            re.compile(pattern)
    nest_patterns = config_data["routing_patterns_map"]["nestjs"]
    assert any(re.search(p, nest_sample) for p in nest_patterns)


def test_unknown_framework_uses_generic_extractor(monkeypatch):
    """spring/laravel used to die with UnboundLocalError before any LLM call."""
    import endpoints_extractor as ee

    captured = {}

    class FakeClient:
        def call_chat_completion(self, messages, temperature=0.5):
            captured["messages"] = messages
            return '[{"method": "GET", "path": "/springy"}]'

    monkeypatch.setattr(ee, "OpenAiClient", lambda: FakeClient())
    extractor = ee.EndpointsExtractor()
    import tempfile, os as _os
    with tempfile.NamedTemporaryFile("w", suffix=".java", delete=False) as f:
        f.write('@GetMapping("/springy") public String get() {}')
        tmp_sample = f.name
    try:
        endpoints = extractor.extract_endpoints_with_gpt(tmp_sample, "laravel")
    finally:
        _os.unlink(tmp_sample)
    assert endpoints == [{"method": "GET", "path": "/springy"}]
    assert "routing expert" in captured["messages"][1]["content"]


PLACEHOLDER_HOST = UserConfigurations.PLACEHOLDER_API_HOST


class _FakeStdin:
    """Stand-in for sys.stdin so a test can say whether a human is watching."""

    def __init__(self, is_a_tty):
        self._is_a_tty = is_a_tty

    def isatty(self):
        return self._is_a_tty


@pytest.fixture
def user_config_file(monkeypatch, tmp_path):
    """user_config.py resolves APIMESH_USER_CONFIG_PATH into module globals at import
    time, so a test swaps the globals rather than the environment variable."""
    config_path = tmp_path / "apimesh" / "config.json"
    config_path.parent.mkdir(parents=True)
    monkeypatch.setattr(user_config, "config_file", str(config_path))
    monkeypatch.setattr(user_config, "config_dir", str(config_path.parent))
    monkeypatch.delenv("APIMESH_API_HOST", raising=False)
    monkeypatch.delenv("APIMESH_OPENAI_MODEL", raising=False)
    monkeypatch.setattr(sys, "stdin", _FakeStdin(False))
    return config_path


def _store(config_path, **values):
    config_path.write_text(json.dumps(values))


def _configure(config_path, is_mcp="true", **overrides):
    """Capture the config the way the CLI does, then return what landed on disk."""
    UserConfigurations("project-key", "sk-test", "chat-1", is_mcp, **overrides)
    return json.loads(config_path.read_text())


def test_cli_keeps_the_positional_interface():
    args = swagger_generation_cli.parse_args(["sk-test", "project-key", "chat-1", "true"])

    assert args.openai_api_key == "sk-test"
    assert args.project_api_key == "project-key"
    assert args.ai_chat_id == "chat-1"
    assert args.is_mcp == "true"
    assert args.api_host is None
    assert args.openai_model is None
    assert args.redetect_framework is False


def test_cli_defaults_every_positional_to_empty():
    args = swagger_generation_cli.parse_args([])

    assert [args.openai_api_key, args.project_api_key, args.ai_chat_id, args.is_mcp] == ["", "", "", ""]


def test_cli_parses_the_new_flags():
    args = swagger_generation_cli.parse_args(
        [
            "sk-test",
            "project-key",
            "chat-1",
            "true",
            "--api-host",
            "https://api.acme.dev",
            "--model",
            "gpt-4o",
            "--redetect-framework",
        ]
    )

    assert args.is_mcp == "true"
    assert args.api_host == "https://api.acme.dev"
    assert args.openai_model == "gpt-4o"
    assert args.redetect_framework is True


def test_cli_parses_flags_with_no_positionals_at_all():
    args = swagger_generation_cli.parse_args(["--api-host", "https://api.acme.dev"])

    assert args.openai_api_key == ""
    assert args.api_host == "https://api.acme.dev"


def test_api_host_flag_beats_env_and_stored_value(user_config_file, monkeypatch):
    _store(user_config_file, api_host="https://stored.example.org")
    monkeypatch.setenv("APIMESH_API_HOST", "https://env.example.org")

    assert _configure(user_config_file, api_host="https://flag.example.org")["api_host"] == "https://flag.example.org"


def test_api_host_env_beats_stored_value(user_config_file, monkeypatch):
    _store(user_config_file, api_host="https://stored.example.org")
    monkeypatch.setenv("APIMESH_API_HOST", "https://env.example.org")

    assert _configure(user_config_file)["api_host"] == "https://env.example.org"


def test_api_host_falls_back_to_the_stored_value(user_config_file):
    _store(user_config_file, api_host="https://stored.example.org")

    assert _configure(user_config_file)["api_host"] == "https://stored.example.org"


def test_placeholder_api_host_warns_and_keeps_going(user_config_file, capsys):
    """Non-interactive runs must still produce a spec, just a loudly flagged one."""
    stored = _configure(user_config_file, is_mcp="")

    assert stored["api_host"] == PLACEHOLDER_HOST
    output = capsys.readouterr().out
    assert "--api-host" in output
    assert "APIMESH_API_HOST" in output


def test_api_host_is_prompted_on_a_tty(user_config_file, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "https://typed.example.org")

    assert _configure(user_config_file, is_mcp="")["api_host"] == "https://typed.example.org"


def test_api_host_prompt_that_cannot_be_read_falls_back(user_config_file, monkeypatch):
    """A tty that answers nothing (a piped-in run under `script`, a closed stdin) must
    not take the whole run down with an EOFError."""
    monkeypatch.setattr(sys, "stdin", _FakeStdin(True))

    def _eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)

    assert _configure(user_config_file, is_mcp="")["api_host"] == PLACEHOLDER_HOST


def test_api_host_is_never_prompted_under_mcp(user_config_file, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(True))

    def _refuse(prompt=""):
        raise AssertionError("an MCP run must never block on input()")

    monkeypatch.setattr("builtins.input", _refuse)

    assert _configure(user_config_file, is_mcp="true")["api_host"] == PLACEHOLDER_HOST


def test_a_real_api_host_is_not_prompted_for(user_config_file, monkeypatch):
    _store(user_config_file, api_host="https://stored.example.org")
    monkeypatch.setattr(sys, "stdin", _FakeStdin(True))

    def _refuse(prompt=""):
        raise AssertionError("nothing to ask, the host is already known")

    monkeypatch.setattr("builtins.input", _refuse)

    assert _configure(user_config_file)["api_host"] == "https://stored.example.org"


def test_model_flag_beats_env_and_stored_value(user_config_file, monkeypatch):
    _store(user_config_file, openai_model="gpt-stored")
    monkeypatch.setenv("APIMESH_OPENAI_MODEL", "gpt-env")

    assert _configure(user_config_file, openai_model="gpt-flag")["openai_model"] == "gpt-flag"


def test_model_env_beats_stored_value(user_config_file, monkeypatch):
    _store(user_config_file, openai_model="gpt-stored")
    monkeypatch.setenv("APIMESH_OPENAI_MODEL", "gpt-env")

    assert _configure(user_config_file)["openai_model"] == "gpt-env"


def test_model_falls_back_to_the_default(user_config_file):
    stored = _configure(user_config_file)

    assert stored["openai_model"] == "gpt-5.6-terra"
    assert stored["openai_api_key"] == "sk-test"


def test_clear_cached_framework_drops_only_that_key(user_config_file):
    _store(user_config_file, framework="flask", api_host="https://stored.example.org")

    assert UserConfigurations.clear_cached_framework() is True

    stored = json.loads(user_config_file.read_text())
    assert "framework" not in stored
    assert stored["api_host"] == "https://stored.example.org"


def test_clear_cached_framework_is_a_noop_when_nothing_is_cached(user_config_file):
    assert UserConfigurations.clear_cached_framework() is False
    assert not user_config_file.exists()


def test_missing_key_fails_early_with_a_clear_message(user_config_file, monkeypatch, capsys):
    """A non-interactive run with no key anywhere must exit 1 up front instead of
    dying later inside the OpenAI client (or hanging on an unreadable prompt)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        UserConfigurations("project-key", "", "chat-1", "true")
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "No OpenAI API key provided" in out
    assert "OPENAI_API_KEY" in out


def test_env_key_still_satisfies_non_interactive_runs(user_config_file, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    UserConfigurations("project-key", "", "chat-1", "true")
    config = json.loads(user_config_file.read_text())
    assert config["openai_api_key"] == "sk-from-env"


def test_null_env_key_does_not_pass_as_a_real_key(user_config_file, monkeypatch):
    """MCP wrappers export placeholder values like 'null'; those must not bypass
    the early failure or overwrite a valid stored key."""
    _store(user_config_file, openai_api_key="sk-stored")
    monkeypatch.setenv("OPENAI_API_KEY", "null")
    UserConfigurations("project-key", "", "chat-1", "true")
    assert json.loads(user_config_file.read_text())["openai_api_key"] == "sk-stored"

    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    _store(user_config_file)
    with pytest.raises(SystemExit):
        UserConfigurations("project-key", "", "chat-1", "true")


def test_mcp_tool_raises_on_child_failure(monkeypatch, tmp_path):
    """A failed generation must be a tool error, not a success payload (agents
    read tool success/failure, not an exit_code field inside a result)."""
    mcp_module = pytest.importorskip("mcp")
    import swagger_mcp

    class FakeProc:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdout = "some output"
            self.stderr = "boom"

    def fake_run(cmd, *args, **kwargs):
        # curl and chmod succeed; the generation child fails
        if cmd[0] in ("curl", "chmod"):
            (tmp_path / "bootstrap_mcp_runner.sh").write_text("#!/bin/bash\n")
            return FakeProc(0)
        return FakeProc(3)

    monkeypatch.setattr(swagger_mcp, "DEFAULT_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(swagger_mcp.subprocess, "run", fake_run)
    monkeypatch.setattr(swagger_mcp.shutil, "which", lambda cmd: "/usr/bin/" + cmd)

    with pytest.raises(RuntimeError, match="exit code 3"):
        swagger_mcp.run_swagger_generation(
            openai_api_key="sk-test", repo_path=str(tmp_path)
        )


def test_mcp_file_declares_inline_uv_dependencies():
    """The README tells users to launch via `uv run swagger_mcp.py`; without PEP 723
    metadata that command cannot install the mcp package."""
    header = (REPO_ROOT / "swagger_mcp.py").read_text(encoding="utf-8")[:500]
    assert "# /// script" in header
    assert '"mcp>=1.2,<2"' in header


def test_mcp_cleanup_failure_does_not_mask_the_real_error(monkeypatch, tmp_path):
    pytest.importorskip("mcp")
    import subprocess as _subprocess

    import swagger_mcp

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] in ("curl", "chmod"):
            (tmp_path / "bootstrap_mcp_runner.sh").write_text("#!/bin/bash\n")
            class Ok:
                returncode = 0
                stdout = ""
                stderr = ""
            return Ok()
        raise _subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(swagger_mcp, "DEFAULT_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(swagger_mcp.subprocess, "run", fake_run)
    monkeypatch.setattr(swagger_mcp.shutil, "which", lambda cmd: "/usr/bin/" + cmd)
    monkeypatch.setattr(
        swagger_mcp.os, "remove", lambda path: (_ for _ in ()).throw(OSError("locked"))
    )

    with pytest.raises(_subprocess.TimeoutExpired):
        swagger_mcp.run_swagger_generation(openai_api_key="sk-test", repo_path=str(tmp_path))


def test_loading_a_legacy_spec_migrates_it_in_every_pipeline(monkeypatch, tmp_path):
    """A pre-x-extension spec loaded for an incremental run must come back with
    migrated info keys and operation fields, or the early returns write the old
    spellings straight back to disk."""
    import golang_pipeline.run_swagger_generation as go_rsg
    import nodejs_pipeline.run_swagger_generation as node_rsg
    import python_pipeline.run_swagger_generation as py_rsg
    import rails_pipeline.run_swagger_generation as rails_rsg

    legacy = {
        "openapi": "3.0.0",
        "info": {
            "title": "t",
            "generated_at": "2025-01-01T00:00:00Z",
            "commit_reference": "abc123",
            "github_repo_url": "https://github.com/o/r",
        },
        "paths": {
            "/users": {
                "get": {
                    "summary": "s",
                    "api_description": "old description",
                    "authorization_tag": "Authorization Required",
                }
            }
        },
    }
    spec_path = tmp_path / "swagger.json"
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(spec_path))

    for module in (node_rsg, py_rsg, go_rsg, rails_rsg):
        spec_path.write_text(json.dumps(legacy))
        loaded = module._load_existing_swagger()
        info = loaded["info"]
        assert info["x-commit-reference"] == "abc123"
        assert info["x-generated-at"] == "2025-01-01T00:00:00Z"
        assert info["x-github-repo-url"] == "https://github.com/o/r"
        assert "commit_reference" not in info and "generated_at" not in info
        operation = loaded["paths"]["/users"]["get"]
        assert operation["description"] == "old description"
        assert operation["x-authorization-tag"] == "Authorization Required"
        assert "api_description" not in operation and "authorization_tag" not in operation


def test_legacy_extractor_rejects_extension_only_fragments():
    fragment = {"paths": {"/x": {"x-metadata": {"owner": "payments"}}}}
    assert SwaggerGeneration._extract_first_operation(fragment) is None
    ok = {"paths": {"/x": {"parameters": [], "get": {"summary": "s"}}}}
    assert SwaggerGeneration._extract_first_operation(ok) == {"summary": "s"}


def test_sanitize_swagger_normalizes_angle_bracket_params():
    swagger = {"paths": {"/users/<int:pk>": {"get": {}}, "/files/<path>": {"get": {}}}}
    sanitized = SwaggerGeneration._sanitize_swagger(swagger)
    assert set(sanitized["paths"]) == {"/users/{pk}", "/files/{path}"}


def test_cli_parses_no_html_flag():
    args = swagger_generation_cli.parse_args(["sk", "", "", "", "--no-html"])
    assert args.no_html is True
    assert swagger_generation_cli.parse_args(["sk"]).no_html is False


def test_extractor_filters_malformed_endpoint_entries(monkeypatch):
    import endpoints_extractor as ee

    class FakeClient:
        def call_chat_completion(self, messages, temperature=0.5):
            return '[null, {"method": "GET", "path": "/health"}, {"method": 5, "path": "/x"}, "junk"]'

    monkeypatch.setattr(ee, "OpenAiClient", lambda: FakeClient())
    extractor = ee.EndpointsExtractor()
    import tempfile, os as _os
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write("@app.route('/health')\ndef h(): pass\n")
        name = f.name
    try:
        endpoints = extractor.extract_endpoints_with_gpt(name, "flask")
    finally:
        _os.unlink(name)
    assert endpoints == [{"method": "GET", "path": "/health"}]


def test_config_json_is_written_owner_only(user_config_file):
    _configure(user_config_file)
    mode = user_config_file.stat().st_mode & 0o777
    assert mode == 0o600


def test_faiss_index_returns_merged_batches_and_skips_unreadable(monkeypatch, tmp_path):
    """The batched indices are the result; re-embedding the corpus doubled the
    spend and reintroduced the per-request token limit."""
    import faiss_index_generator as fig

    calls = []

    class FakeIndex:
        def __init__(self, texts):
            self.texts = list(texts)
        def merge_from(self, other):
            self.texts.extend(other.texts)

    monkeypatch.setattr(
        fig.FAISS, "from_texts",
        lambda texts, embeddings, metadatas=None: (calls.append(list(texts)), FakeIndex(texts))[1],
    )
    good = tmp_path / "a.py"
    good.write_text("def handler(): pass\n" * 5)
    unreadable = tmp_path / "b.py"
    unreadable.write_bytes(b"\xff\xfe garbage \xff")

    generator = fig.GenerateFaissIndex.__new__(fig.GenerateFaissIndex)
    class C: embeddings = None
    generator.openai_client = C()
    index = generator.create_faiss_index([str(good), str(unreadable)], "flask")

    assert isinstance(index, FakeIndex)
    total_embedded = sum(len(batch) for batch in calls)
    assert total_embedded == len(index.texts)  # embedded exactly once, no duplicate pass

    with pytest.raises(ValueError):
        generator.create_faiss_index([str(unreadable)], "flask")


def test_save_swagger_json_reports_html_outcome(monkeypatch, tmp_path):
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(tmp_path))
    out = tmp_path / "apimesh" / "swagger.json"

    monkeypatch.setenv("APIMESH_SKIP_HTML", "1")
    assert SwaggerGeneration.save_swagger_json(SAMPLE_SWAGGER, str(out)) is True

    monkeypatch.delenv("APIMESH_SKIP_HTML", raising=False)
    monkeypatch.setattr(SwaggerGeneration, "generate_html_viewer", staticmethod(lambda path: None))
    assert SwaggerGeneration.save_swagger_json(SAMPLE_SWAGGER, str(out)) is False
    monkeypatch.setattr(SwaggerGeneration, "generate_html_viewer", staticmethod(lambda path: "ok.html"))
    assert SwaggerGeneration.save_swagger_json(SAMPLE_SWAGGER, str(out)) is True


def test_framework_prompt_path_list_is_capped(monkeypatch, tmp_path):
    from framework_identifier import FrameworkIdentifier, MAX_PATHS_IN_PROMPT

    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(tmp_path))
    paths = [str(tmp_path / f"src/module_{i}.py") for i in range(MAX_PATHS_IN_PROMPT + 50)]
    listing = FrameworkIdentifier._paths_for_prompt(paths)
    assert listing.count("\n") == MAX_PATHS_IN_PROMPT  # capped lines + truncation note
    assert "and 50 more files not listed" in listing
    assert str(tmp_path) not in listing  # relative, not absolute


def test_framework_detection_raises_clearly_on_junk(monkeypatch):
    import framework_identifier as fi

    class FakeClient:
        def call_chat_completion(self, messages, temperature=0.5):
            return "I could not determine the framework, sorry!"

    monkeypatch.setattr(fi, "OpenAiClient", lambda: FakeClient())
    identifier = fi.FrameworkIdentifier()
    with pytest.raises(ValueError, match="no JSON object"):
        identifier.get_framework(["a.py"])


def test_changed_files_include_untracked(tmp_path, monkeypatch):
    """A brand-new uncommitted route file must count as changed, or incremental
    runs never see newly added endpoints until they are committed."""
    import subprocess

    from utils import get_changed_files_since

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"],
        check=True,
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (repo / "new_routes.py").write_text("@app.route('/new')\ndef new(): pass\n")

    changed = get_changed_files_since(base, repo_path=str(repo))
    assert changed is not None
    assert str(repo / "new_routes.py") in {str(Path(p)) for p in changed}


def test_legacy_generation_reports_coverage(monkeypatch, tmp_path):
    monkeypatch.setenv("APIMESH_USER_REPO_PATH", str(tmp_path))
    generator = _generator()

    def generate(endpoint, auth, framework):
        if endpoint["path"] == "/broken":
            raise RuntimeError("boom")
        return {"paths": {endpoint["path"]: {"get": {"summary": "ok"}}}}

    generator.generate_endpoint_swagger = generate
    swagger = generator.create_swagger_json(
        [_endpoint("/broken", "GET"), _endpoint("/healthy", "GET")],
        "", "flask", "https://api.example.com",
    )
    assert swagger["info"]["x-apimesh-coverage"] == {
        "endpoints_extracted": 2,
        "generated": 1,
        "skipped_unchanged": 0,
        "failed": 1,
    }


def test_no_endpoints_short_circuits_before_embedding(monkeypatch, capsys):
    """A frontend or marketing folder with zero endpoints must exit cleanly
    before any embedding spend, not after indexing the whole tree."""
    runner = swagger_generation_cli.RunSwagger.__new__(swagger_generation_cli.RunSwagger)
    runner.ai_chat_id = ""
    runner.user_config = {"framework": "express", "api_host": "https://x.example"}
    runner.user_configurations = None

    class Scanner:
        def get_all_file_paths(self):
            return ["/repo/src/App.js"]
        def find_api_files(self, file_paths, framework):
            return []

    class Faiss:
        def create_faiss_index(self, *a, **k):
            raise AssertionError("embedding must not run for zero endpoints")

    class Telemetry:
        def new_run_id(self):
            return "r"
        def capture(self, *a, **k):
            pass
        def stage(self, *a, **k):
            import contextlib
            return contextlib.nullcontext()

    runner.file_scanner = Scanner()
    runner.faiss_index = Faiss()
    runner.telemetry = Telemetry()
    runner.framework_identifier = None
    runner.endpoints_extractor = None
    runner.swagger_generator = None

    def fake_pipeline(self, framework):
        return None

    monkeypatch.setattr(
        swagger_generation_cli.RunSwagger, "run_python_nodejs_ruby", fake_pipeline
    )
    with pytest.raises(swagger_generation_cli.NoEndpointsFound):
        runner.run("")
    out = capsys.readouterr().out
    assert "No API endpoints were found" in out
    assert "Started creating faiss index" not in out


# ---------------------------------------------------------------------------
# Pipeline frameworks never fall back to LLM route guessing
# ---------------------------------------------------------------------------

class _NullTelemetry:
    def new_run_id(self):
        return "test-run"

    def capture(self, *args, **kwargs):
        pass

    def stage(self, *args, **kwargs):
        import contextlib
        return contextlib.nullcontext()


def _cli_with_stubs(framework):
    """A RunSwagger wired for run() without touching config, network or disk."""
    import swagger_generation_cli as cli_mod

    cli = cli_mod.RunSwagger.__new__(cli_mod.RunSwagger)
    cli.user_config = {"framework": framework, "api_host": "http://api.example.test"}
    cli.telemetry = _NullTelemetry()

    class _Scanner:
        def get_all_file_paths(self):
            return ["/repo/App.java"]

        def find_api_files(self, paths, fw):
            return list(paths)

    cli.file_scanner = _Scanner()
    cli.framework_identifier = None
    cli.swagger_generator = None
    return cli


_PIPELINE_GENERATOR_NAMES = (
    "python_swagger_generator",
    "nodejs_swagger_generator",
    "ruby_on_rails_swagger_generator",
    "golang_swagger_generator",
    "java_swagger_generator",
)


def _stub_all_pipelines(monkeypatch, result=None, raises=None):
    import swagger_generation_cli as cli_mod

    def _generator(host):
        if raises is not None:
            raise raises
        return result

    for name in _PIPELINE_GENERATOR_NAMES:
        monkeypatch.setattr(cli_mod, name, _generator)


@pytest.mark.parametrize(
    "framework",
    sorted(swagger_generation_cli.PIPELINE_FRAMEWORKS),
)
def test_pipeline_framework_zero_result_is_an_honest_zero(
    monkeypatch, capsys, framework
):
    """A supported framework whose parser proves nothing gets an honest zero.

    The old behavior handed the repo to a per-file LLM extractor, which
    published Feign clients and guessed paths as endpoints.
    """
    from swagger_generation_cli import NoEndpointsFound

    _stub_all_pipelines(monkeypatch, result=None)
    cli = _cli_with_stubs(framework)

    with pytest.raises(NoEndpointsFound):
        cli.run()

    assert "routes are never LLM-guessed" in capsys.readouterr().out


def test_pipeline_crash_is_fatal_not_rescued_by_llm_guessing(monkeypatch):
    """A parser crash aborts the run rather than switching to route invention."""
    _stub_all_pipelines(monkeypatch, raises=RuntimeError("parser exploded"))
    cli = _cli_with_stubs("spring")

    with pytest.raises(RuntimeError, match="parser exploded"):
        cli.run()


def test_unsupported_framework_fails_closed_with_no_llm_extraction(monkeypatch, capsys):
    """No deterministic parser means zero endpoints, never LLM guessing."""
    from swagger_generation_cli import NoEndpointsFound

    _stub_all_pipelines(monkeypatch, result=None)
    cli = _cli_with_stubs("laravel")

    with pytest.raises(NoEndpointsFound):
        cli.run()

    out = capsys.readouterr().out
    assert "no deterministic parser exists for 'laravel'" in out


@pytest.mark.parametrize("stored", ["Spring", "SPRING", " spring ", "spring_boot", "Spring Boot"])
def test_framework_spelling_cannot_dodge_the_pipeline(monkeypatch, stored):
    """A cached 'Spring' or detected 'spring_boot' still runs the java parser.

    Before normalization, any non-canonical spelling skipped the parser AND
    the fail-closed set, landing in the LLM extraction path.
    """
    import swagger_generation_cli as cli_mod
    from swagger_generation_cli import NoEndpointsFound

    dispatched = []

    def _generator(host):
        dispatched.append(host)
        return None

    for name in _PIPELINE_GENERATOR_NAMES:
        monkeypatch.setattr(cli_mod, name, _generator)
    cli = _cli_with_stubs(stored)

    with pytest.raises(NoEndpointsFound):
        cli.run()

    assert dispatched, f"{stored!r} must dispatch a pipeline parser"


def test_canonical_framework_aliases():
    from swagger_generation_cli import canonical_framework

    assert canonical_framework("Spring") == "spring"
    assert canonical_framework("spring_boot") == "spring"
    assert canonical_framework("Ruby on Rails") == "ruby_on_rails"
    assert canonical_framework("Go") == "golang"
    assert canonical_framework("laravel") == "laravel"
    assert canonical_framework(None) == ""


def test_zero_endpoint_run_warns_about_stale_output(monkeypatch, capsys, tmp_path):
    """An old swagger.json left on disk is flagged, not silently kept current."""
    from swagger_generation_cli import NoEndpointsFound

    stale = tmp_path / "swagger.json"
    stale.write_text("{\"openapi\": \"3.0.0\", \"paths\": {\"/old\": {}}}")
    monkeypatch.setenv("APIMESH_OUTPUT_FILEPATH", str(stale))
    _stub_all_pipelines(monkeypatch, result=None)
    cli = _cli_with_stubs("spring")

    with pytest.raises(NoEndpointsFound):
        cli.run()

    out = capsys.readouterr().out
    assert "previous run's result" in out

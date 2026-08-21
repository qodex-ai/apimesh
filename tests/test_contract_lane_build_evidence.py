"""Build-evidence extraction: the primary serving proof for the contract lane.

Maven, Gradle and Bazel invocations of OpenAPI code generators are parsed
statically, classified server or client by generator name, and tied to the
spec file they take as input. Nothing executes; anything unresolvable is
reported as such.
"""

import os
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("APIMESH_CONFIG_PATH", str(REPO_ROOT / "config.yml"))
os.environ.setdefault("APIMESH_USER_REPO_PATH", str(REPO_ROOT))
os.environ.setdefault(
    "APIMESH_USER_CONFIG_PATH",
    str(Path(tempfile.mkdtemp(prefix="apimesh-user-config-")) / "config.json"),
)

from contract_lane.build_evidence import collect_build_evidence, evidence_by_spec

FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "contract_lane"


def test_maven_spring_delegate_is_server_evidence():
    invocations = collect_build_evidence(str(FIXTURES_ROOT / "maven_delegate_pattern"))

    assert len(invocations) == 1
    entry = invocations[0]
    assert entry["tool"] == "maven"
    assert entry["generator"] == "spring"
    assert entry["kind"] == "server"
    assert entry["spec_path"] == "src/main/resources/orders.yaml"
    assert entry["options"]["delegatePattern"] == "true"
    assert entry["api_package"] == "com.acme.generated.api"


def test_maven_java_client_is_client_evidence():
    invocations = collect_build_evidence(str(FIXTURES_ROOT / "client_spec_colliding_ids"))

    assert len(invocations) == 1
    entry = invocations[0]
    assert entry["generator"] == "java"
    assert entry["kind"] == "client"
    assert entry["spec_path"] == "vendor/crowdstrike/alerts.yaml"


def test_maven_interface_only_options_are_captured():
    invocations = collect_build_evidence(str(FIXTURES_ROOT / "no_operation_ids"))

    assert len(invocations) == 1
    assert invocations[0]["kind"] == "server"
    assert invocations[0]["options"]["interfaceOnly"] == "true"
    assert invocations[0]["spec_path"] == "src/main/resources/health.yaml"


def test_bazel_macro_and_call_site_resolve_to_server_evidence():
    invocations = collect_build_evidence(str(FIXTURES_ROOT / "openapi_first_spring"))

    assert len(invocations) == 1
    entry = invocations[0]
    assert entry["tool"] == "bazel"
    assert entry["build_file"] == "app/BUILD.bazel"
    assert entry["generator"] == "spring"
    assert entry["kind"] == "server"
    assert entry["spec_path"] == "app/src/main/resources/api/pets.yaml"
    assert entry["api_package"] == "com.acme.generated.api"
    assert entry["options"]["interfaceOnly"] == "true"


def test_gradle_block_with_root_dir_input(tmp_path):
    (tmp_path / "settings.gradle").write_text("rootProject.name = 'demo'\n")
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "build.gradle").write_text(
        "plugins { id 'org.openapi.generator' }\n"
        "openApiGenerate {\n"
        "    generatorName = \"spring\"\n"
        "    inputSpec = \"$rootDir/specs/api.yaml\"\n"
        "    apiPackage = \"com.demo.api\"\n"
        "    configOptions = [interfaceOnly: 'true']\n"
        "}\n"
    )

    invocations = collect_build_evidence(str(tmp_path))

    assert len(invocations) == 1
    entry = invocations[0]
    assert entry["tool"] == "gradle"
    assert entry["kind"] == "server"
    assert entry["spec_path"] == "specs/api.yaml"
    assert entry["api_package"] == "com.demo.api"
    assert entry["options"]["interfaceOnly"] == "true"


def test_gradle_kotlin_dsl_client(tmp_path):
    (tmp_path / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "build.gradle.kts").write_text(
        "tasks.register<GenerateTask>(\"genClient\") {\n"
        "    generatorName.set(\"typescript-axios\")\n"
        "    inputSpec.set(\"${projectDir}/api.yaml\")\n"
        "}\n"
    )

    invocations = collect_build_evidence(str(tmp_path))

    assert len(invocations) == 1
    assert invocations[0]["kind"] == "client"
    assert invocations[0]["spec_path"] == "api.yaml"


def test_maven_property_substitution(tmp_path):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "pom.xml").write_text(
        "<project>\n"
        "  <properties><spec.dir>${project.basedir}/specs</spec.dir></properties>\n"
        "  <build><plugins><plugin>\n"
        "    <artifactId>openapi-generator-maven-plugin</artifactId>\n"
        "    <configuration>\n"
        "      <inputSpec>${spec.dir}/api.yaml</inputSpec>\n"
        "      <generatorName>spring</generatorName>\n"
        "    </configuration>\n"
        "  </plugin></plugins></build>\n"
        "</project>\n"
    )

    invocations = collect_build_evidence(str(tmp_path))

    assert len(invocations) == 1
    assert invocations[0]["spec_path"] == "specs/api.yaml"


def test_maven_unresolvable_property_is_reported_not_guessed(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><build><plugins><plugin>\n"
        "  <artifactId>openapi-generator-maven-plugin</artifactId>\n"
        "  <configuration>\n"
        "    <inputSpec>${undefined.property}/api.yaml</inputSpec>\n"
        "    <generatorName>spring</generatorName>\n"
        "  </configuration>\n"
        "</plugin></plugins></build></project>\n"
    )

    invocations = collect_build_evidence(str(tmp_path))

    assert len(invocations) == 1
    assert invocations[0]["spec_path"] is None
    assert invocations[0]["unresolved_input"] == "${undefined.property}/api.yaml"


def test_pom_with_dtd_is_refused(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<?xml version=\"1.0\"?>\n"
        "<!DOCTYPE project [<!ENTITY x \"y\">]>\n"
        "<project><build><plugins><plugin>\n"
        "  <artifactId>openapi-generator-maven-plugin</artifactId>\n"
        "  <configuration><generatorName>spring</generatorName></configuration>\n"
        "</plugin></plugins></build></project>\n"
    )

    assert collect_build_evidence(str(tmp_path)) == []


def test_unknown_generator_is_not_server_evidence(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><build><plugins><plugin>\n"
        "  <artifactId>openapi-generator-maven-plugin</artifactId>\n"
        "  <configuration><generatorName>go-server</generatorName></configuration>\n"
        "</plugin></plugins></build></project>\n"
    )
    invocations = collect_build_evidence(str(tmp_path))
    assert invocations[0]["kind"] == "server"

    (tmp_path / "pom.xml").write_text(
        "<project><build><plugins><plugin>\n"
        "  <artifactId>openapi-generator-maven-plugin</artifactId>\n"
        "  <configuration><generatorName>go</generatorName></configuration>\n"
        "</plugin></plugins></build></project>\n"
    )
    invocations = collect_build_evidence(str(tmp_path))
    assert invocations[0]["kind"] == "client"


def test_fixtures_without_build_files_produce_no_evidence():
    for name in ("feign_client", "gateway_passthrough", "stale_docs_spec"):
        assert collect_build_evidence(str(FIXTURES_ROOT / name)) == []


def test_evidence_by_spec_groups_only_resolved_inputs():
    invocations = [
        {"spec_path": "a.yaml", "kind": "server"},
        {"spec_path": "a.yaml", "kind": "client"},
        {"spec_path": None, "kind": "server"},
    ]
    grouped = evidence_by_spec(invocations)
    assert set(grouped) == {"a.yaml"}
    assert len(grouped["a.yaml"]) == 2


def test_bazel_group_macro_with_nested_dicts(tmp_path):
    """A specs list entry carrying a nested import_mappings dict still parses."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "defs.bzl").write_text(
        "def _gen_cmd(spec):\n"
        "    return \"java -jar gen.jar generate -g spring -i \" + spec\n"
        "\n"
        "def openapi_spring_group(name, specs):\n"
        "    for s in specs:\n"
        "        _gen_cmd(s[\"spec_file\"])\n"
    )
    (tmp_path / "src" / "main" / "resources" / "core").mkdir(parents=True)
    (tmp_path / "src" / "main" / "resources" / "core" / "requests.yml").write_text(
        "openapi: 3.0.0\npaths: {}\n"
    )
    (tmp_path / "BUILD.bazel").write_text(
        "load(\"//tools:defs.bzl\", \"openapi_spring_group\")\n"
        "openapi_spring_group(\n"
        "    name = \"grp\",\n"
        "    specs = [\n"
        "        {\n"
        "            \"spec_file\": \"core/requests.yml\",\n"
        "            \"api_package\": \"com.acme.requests.api\",\n"
        "            \"import_mappings\": {\n"
        "                \"Dto\": \"com.acme.other.Dto\",\n"
        "            },\n"
        "        },\n"
        "    ],\n"
        ")\n"
    )

    invocations = collect_build_evidence(str(tmp_path))

    assert len(invocations) == 1
    entry = invocations[0]
    assert entry["kind"] == "server"
    assert entry["spec_path"] == "src/main/resources/core/requests.yml"
    assert entry["api_package"] == "com.acme.requests.api"


def test_bazel_inline_genrule_client_commands(tmp_path):
    """generate -g java -i $(location x.yml) inside a genrule cmd is client evidence."""
    (tmp_path / "src" / "main" / "resources").mkdir(parents=True)
    (tmp_path / "src" / "main" / "resources" / "workflow.yml").write_text(
        "openapi: 3.0.0\npaths: {}\n"
    )
    (tmp_path / "tools.bzl").write_text(
        "def _unused():\n    return \"generate -g spring\"\n"
    )
    (tmp_path / "BUILD.bazel").write_text(
        "genrule(\n"
        "    name = \"clients_gen\",\n"
        "    cmd = \"\"\"\n"
        "\"$$JAVA\" -jar \"$$GEN\" generate -g java -i \"$(location src/main/resources/workflow.yml)\" -o out --library jersey3 --api-package com.flow.generated.api\n"
        "\"\"\",\n"
        ")\n"
    )

    invocations = collect_build_evidence(str(tmp_path))

    inline = [e for e in invocations if e["generator"] == "java"]
    assert len(inline) == 1
    assert inline[0]["kind"] == "client"
    assert inline[0]["spec_path"] == "src/main/resources/workflow.yml"
    assert inline[0]["api_package"] == "com.flow.generated.api"


# ---------------------------------------------------------------------------
# Review round 1 regressions
# ---------------------------------------------------------------------------

def test_plugin_management_is_configuration_not_evidence(tmp_path):
    (tmp_path / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "pom.xml").write_text(
        "<project><build><pluginManagement><plugins><plugin>\n"
        "  <artifactId>openapi-generator-maven-plugin</artifactId>\n"
        "  <configuration>\n"
        "    <inputSpec>${project.basedir}/api.yaml</inputSpec>\n"
        "    <generatorName>spring</generatorName>\n"
        "  </configuration>\n"
        "</plugin></plugins></pluginManagement></build></project>\n"
    )
    assert collect_build_evidence(str(tmp_path)) == []


def test_skipped_execution_is_not_evidence(tmp_path):
    (tmp_path / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "pom.xml").write_text(
        "<project><build><plugins><plugin>\n"
        "  <artifactId>openapi-generator-maven-plugin</artifactId>\n"
        "  <configuration>\n"
        "    <skip>true</skip>\n"
        "    <inputSpec>${project.basedir}/api.yaml</inputSpec>\n"
        "    <generatorName>spring</generatorName>\n"
        "  </configuration>\n"
        "</plugin></plugins></build></project>\n"
    )
    assert collect_build_evidence(str(tmp_path)) == []


def test_commented_gradle_configuration_is_not_evidence(tmp_path):
    (tmp_path / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "build.gradle").write_text(
        "// openApiGenerate {\n"
        "//     generatorName = \"spring\"\n"
        "//     inputSpec = \"$projectDir/api.yaml\"\n"
        "// }\n"
        "/*\n"
        "openApiGenerate { generatorName = \"spring\"\n"
        "inputSpec = \"$projectDir/api.yaml\" }\n"
        "*/\n"
    )
    assert collect_build_evidence(str(tmp_path)) == []


def test_bazel_label_resolves_to_its_package_not_a_same_named_file(tmp_path):
    """//shared:api.yaml must never bind to the calling package's api.yaml."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "defs.bzl").write_text(
        "def openapi_spring_spec(name, spec_file):\n"
        "    native.genrule(name = name, cmd = \"generate -g spring -i \" + spec_file)\n"
    )
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "app" / "BUILD.bazel").write_text(
        "load(\"//tools:defs.bzl\", \"openapi_spring_spec\")\n"
        "openapi_spring_spec(\n"
        "    name = \"gen\",\n"
        "    spec_file = \"//shared:api.yaml\",\n"
        ")\n"
    )

    invocations = collect_build_evidence(str(tmp_path))

    assert len(invocations) == 1
    assert invocations[0]["spec_path"] == "shared/api.yaml"


def test_macro_calls_inside_bzl_files_are_templates_not_invocations(tmp_path):
    (tmp_path / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "defs.bzl").write_text(
        "def _gen(spec):\n"
        "    return \"generate -g spring -i \" + spec\n"
        "\n"
        "def wrapper(name):\n"
        "    _gen(spec = \"api.yaml\")\n"
        "\n"
        "def helper():\n"
        "    wrapper(name = \"dead\")\n"
    )

    assert collect_build_evidence(str(tmp_path)) == []


def test_cyclic_maven_properties_terminate_as_unresolved(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project>\n"
        "  <properties><a>${b}</a><b>${a}</b></properties>\n"
        "  <build><plugins><plugin>\n"
        "    <artifactId>openapi-generator-maven-plugin</artifactId>\n"
        "    <configuration>\n"
        "      <inputSpec>${a}/api.yaml</inputSpec>\n"
        "      <generatorName>spring</generatorName>\n"
        "    </configuration>\n"
        "  </plugin></plugins></build>\n"
        "</project>\n"
    )
    invocations = collect_build_evidence(str(tmp_path))
    assert len(invocations) == 1
    assert invocations[0]["spec_path"] is None
    assert "unresolved_input" in invocations[0]


def test_symlinked_build_files_are_ignored(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    real = outside / "build.gradle"
    real.write_text(
        "openApiGenerate { generatorName = \"spring\"\ninputSpec = \"$projectDir/api.yaml\" }\n"
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (repo / "build.gradle").symlink_to(real)

    assert collect_build_evidence(str(repo)) == []


def test_unrecognized_generator_names_are_unknown_not_guessed(tmp_path):
    for name in ("vendor-client-server", "acme-custom", "jaxrs-madeup"):
        (tmp_path / "pom.xml").write_text(
            "<project><build><plugins><plugin>\n"
            "  <artifactId>openapi-generator-maven-plugin</artifactId>\n"
            f"  <configuration><generatorName>{name}</generatorName></configuration>\n"
            "</plugin></plugins></build></project>\n"
        )
        invocations = collect_build_evidence(str(tmp_path))
        assert invocations[0]["kind"] == "unknown", name


# ---------------------------------------------------------------------------
# Go (oapi-codegen) and Python (connexion)
# ---------------------------------------------------------------------------

def test_go_generate_server_flavor_is_server_evidence():
    invocations = collect_build_evidence(str(FIXTURES_ROOT / "go_oapi_codegen"))

    assert len(invocations) == 1
    entry = invocations[0]
    assert entry["tool"] == "go-generate"
    assert entry["kind"] == "server"
    assert entry["spec_path"] == "api/openapi.yaml"
    assert "chi-server" in entry["options"]["generate"]


def test_go_generate_client_flavor_is_client_evidence():
    invocations = collect_build_evidence(str(FIXTURES_ROOT / "go_oapi_client"))

    assert len(invocations) == 1
    assert invocations[0]["kind"] == "client"
    assert invocations[0]["spec_path"] == "vendorapi/partner.yaml"


def test_go_generate_config_file_supplies_the_flavors():
    invocations = collect_build_evidence(str(FIXTURES_ROOT / "go_oapi_config"))

    assert len(invocations) == 1
    entry = invocations[0]
    assert entry["kind"] == "server"
    assert entry["spec_path"] == "api.yaml"
    assert "std-http-server" in entry["options"]["generate"]


def test_connexion_add_api_is_server_evidence_with_base_path():
    invocations = collect_build_evidence(str(FIXTURES_ROOT / "connexion_app"))

    assert len(invocations) == 1
    entry = invocations[0]
    assert entry["tool"] == "connexion"
    assert entry["kind"] == "server"
    assert entry["spec_path"] == "specs/openapi.yaml"
    assert entry["options"]["base_path"] == "/v1"


def test_connexion_variable_spec_is_not_statically_knowable(tmp_path):
    (tmp_path / "app.py").write_text(
        "import connexion\n"
        "spec = pick_spec()\n"
        "app = connexion.FlaskApp(__name__)\n"
        "app.add_api(spec)\n"
    )
    assert collect_build_evidence(str(tmp_path)) == []


def test_go_generate_in_vendored_code_is_ignored(tmp_path):
    (tmp_path / "vendor" / "dep").mkdir(parents=True)
    (tmp_path / "vendor" / "dep" / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "vendor" / "dep" / "gen.go").write_text(
        "package dep\n//go:generate oapi-codegen -generate chi-server api.yaml\n"
    )
    assert collect_build_evidence(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# Review round 2 regressions
# ---------------------------------------------------------------------------

def test_profile_scoped_plugin_is_provisional(tmp_path):
    (tmp_path / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "pom.xml").write_text(
        "<project><profiles><profile><id>gen</id><build><plugins><plugin>\n"
        "  <artifactId>openapi-generator-maven-plugin</artifactId>\n"
        "  <configuration>\n"
        "    <inputSpec>${project.basedir}/api.yaml</inputSpec>\n"
        "    <generatorName>spring</generatorName>\n"
        "  </configuration>\n"
        "</plugin></plugins></build></profile></profiles></project>\n"
    )
    invocations = collect_build_evidence(str(tmp_path))
    assert len(invocations) == 1
    assert invocations[0]["provisional"] is True


def test_property_driven_skip_is_respected(tmp_path):
    (tmp_path / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "pom.xml").write_text(
        "<project>\n"
        "  <properties><openapi.skip>true</openapi.skip></properties>\n"
        "  <build><plugins><plugin>\n"
        "    <artifactId>openapi-generator-maven-plugin</artifactId>\n"
        "    <configuration>\n"
        "      <skip>${openapi.skip}</skip>\n"
        "      <inputSpec>${project.basedir}/api.yaml</inputSpec>\n"
        "      <generatorName>spring</generatorName>\n"
        "    </configuration>\n"
        "  </plugin></plugins></build>\n"
        "</project>\n"
    )
    assert collect_build_evidence(str(tmp_path)) == []


def test_commented_bazel_call_site_is_not_an_invocation(tmp_path):
    (tmp_path / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "defs.bzl").write_text(
        "def openapi_spring_spec(name, spec_file):\n"
        "    native.genrule(name = name, cmd = \"generate -g spring -i \" + spec_file)\n"
    )
    (tmp_path / "BUILD.bazel").write_text(
        "load(\"//:defs.bzl\", \"openapi_spring_spec\")\n"
        "# openapi_spring_spec(name = \"dead\", spec_file = \"api.yaml\")\n"
    )
    assert collect_build_evidence(str(tmp_path)) == []


def test_commented_or_foreign_add_api_is_not_evidence(tmp_path):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "openapi.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "app.py").write_text(
        "import connexion\n"
        "app = connexion.FlaskApp(__name__, specification_dir=\"specs\")\n"
        "# app.add_api(\"openapi.yaml\", base_path=\"/evil\")\n"
        "partner_client.add_api(\"specs/openapi.yaml\")\n"
    )
    assert collect_build_evidence(str(tmp_path)) == []


def test_go_generate_without_committed_output_is_provisional(tmp_path):
    (tmp_path / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "gen.go").write_text(
        "package api\n//go:generate oapi-codegen -generate chi-server -o api_gen.go api.yaml\n"
    )
    invocations = collect_build_evidence(str(tmp_path))
    assert len(invocations) == 1
    assert invocations[0]["provisional"] is True

    (tmp_path / "api_gen.go").write_text("package api\n")
    invocations = collect_build_evidence(str(tmp_path))
    assert invocations[0]["provisional"] is False


def test_add_api_in_test_files_proves_nothing(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "openapi.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "tests" / "test_app.py").write_text(
        "import connexion\n"
        "app = connexion.FlaskApp(__name__)\n"
        "app.add_api(\"../openapi.yaml\")\n"
    )
    assert collect_build_evidence(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# Review round 3 regressions
# ---------------------------------------------------------------------------

def test_untracked_go_output_stays_provisional(tmp_path):
    """In a git repo, only committed generated output corroborates."""
    import subprocess

    from contract_lane.build_evidence import _TRACKED_CACHE

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (repo / "gen.go").write_text(
        "package api\n//go:generate oapi-codegen -generate chi-server -o api_gen.go api.yaml\n"
    )
    (repo / "api_gen.go").write_text("package api\n")
    subprocess.run(["git", "-C", str(repo), "add", "api.yaml", "gen.go"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"],
        check=True,
    )

    _TRACKED_CACHE.clear()
    invocations = collect_build_evidence(str(repo))
    assert invocations[0]["provisional"] is True  # output exists but untracked

    subprocess.run(["git", "-C", str(repo), "add", "api_gen.go"], check=True)
    _TRACKED_CACHE.clear()
    invocations = collect_build_evidence(str(repo))
    assert invocations[0]["provisional"] is False


def test_connexion_spec_dir_is_per_app(tmp_path):
    """Two apps in one module must not borrow each other's spec directory."""
    (tmp_path / "a_specs").mkdir()
    (tmp_path / "b_specs").mkdir()
    (tmp_path / "a_specs" / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "b_specs" / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "app.py").write_text(
        "import connexion\n"
        "first = connexion.FlaskApp(__name__, specification_dir=\"a_specs\")\n"
        "second = connexion.FlaskApp(__name__, specification_dir=\"b_specs\")\n"
        "second.add_api(\"api.yaml\")\n"
    )

    invocations = collect_build_evidence(str(tmp_path))

    assert len(invocations) == 1
    assert invocations[0]["spec_path"] == "b_specs/api.yaml"


def test_bazel_helper_chain_files_are_all_proof_files(tmp_path):
    (tmp_path / "api.yaml").write_text("openapi: 3.0.0\npaths: {}\n")
    (tmp_path / "inner.bzl").write_text(
        "def _gen_cmd(spec):\n"
        "    return \"generate -g spring -i \" + spec\n"
    )
    (tmp_path / "outer.bzl").write_text(
        "load(\"//:inner.bzl\", \"_gen_cmd\")\n"
        "def openapi_spring_spec(name, spec_file):\n"
        "    native.genrule(name = name, cmd = _gen_cmd(spec_file))\n"
    )
    (tmp_path / "BUILD.bazel").write_text(
        "load(\"//:outer.bzl\", \"openapi_spring_spec\")\n"
        "openapi_spring_spec(name = \"gen\", spec_file = \"api.yaml\")\n"
    )

    invocations = collect_build_evidence(str(tmp_path))

    assert len(invocations) == 1
    assert set(invocations[0]["config_files"]) == {"inner.bzl", "outer.bzl"}

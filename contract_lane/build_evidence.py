"""Find code-generator invocations in build files and classify them.

This is the primary serving proof for the contract lane: a build file naming a
spec as input to a server-mode generator is stronger evidence than any amount
of name matching. Everything here is static text and XML parsing; no build is
ever executed. An invocation that cannot be fully resolved is reported with
what is known, never guessed into shape.
"""

import os
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Dict, List, Optional

# Generator names that produce serving code, per openapi-generator's catalog.
# Bare language names (java, go, python, typescript-axios...) are clients.
_SERVER_GENERATOR_NAMES = {
    "spring",
    "kotlin-spring",
    "micronaut",
    "java-camel",
    "java-msf4j",
    "java-vertx-web",
    "python-flask",
    "python-fastapi",
    "python-aiohttp",
    "python-blueplanet",
    "aspnetcore",
    "cpp-pistache-server",
    "cpp-restbed-server",
    "cpp-qt-qhttpengine-server",
    "haskell-servant",
    "haskell-yesod",
    "php-laravel",
    "php-lumen",
    "php-slim4",
    "php-symfony",
    "ruby-on-rails",
    "ruby-sinatra",
    "rust-axum",
    "scala-akka-http-server",
    "scala-cask",
    "scala-finch",
    "scala-lagom-server",
    "scala-play-server",
    "scalatra",
}


def _generator_kind(name: str) -> str:
    lowered = (name or "").strip().lower()
    if not lowered:
        return "unknown"
    if lowered.endswith("-server") or lowered in _SERVER_GENERATOR_NAMES:
        return "server"
    if lowered.startswith("jaxrs"):
        return "server"
    return "client"


def _contain(repo_root: Path, base_dir: Path, raw_path: str) -> Optional[str]:
    """Repo-relative form of a build-file path, or None when it escapes."""
    candidate = os.path.realpath(
        raw_path if os.path.isabs(raw_path) else str(base_dir / raw_path)
    )
    root = str(repo_root)
    if os.path.commonpath([candidate, root]) != root:
        return None
    return str(Path(candidate).relative_to(root))


_OPTION_KEYS = ("interfaceOnly", "delegatePattern", "useTags", "requestMappingMode")


def _options_from_text(text: str) -> Dict[str, str]:
    options: Dict[str, str] = {}
    for key in _OPTION_KEYS:
        match = re.search(
            rf"{key}\s*[=>:]+\s*['\"]?([A-Za-z0-9_-]+)", text
        )
        if match:
            options[key] = match.group(1)
    return options


# ---------------------------------------------------------------------------
# Maven
# ---------------------------------------------------------------------------

_MAVEN_GENERATOR_PLUGINS = {
    "openapi-generator-maven-plugin",
    "swagger-codegen-maven-plugin",
}


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _pom_properties(root_element) -> Dict[str, str]:
    properties: Dict[str, str] = {}
    for element in root_element.iter():
        if _strip_ns(element.tag) == "properties":
            for child in element:
                if child.text:
                    properties[_strip_ns(child.tag)] = child.text.strip()
    return properties


def _substitute(value: str, properties: Dict[str, str], pom_dir: str) -> str:
    def _one(match):
        name = match.group(1)
        if name in ("project.basedir", "basedir", "project.parent.basedir"):
            return pom_dir
        return properties.get(name, match.group(0))

    previous = None
    while previous != value:
        previous = value
        value = re.sub(r"\$\{([^}]+)\}", _one, value)
    return value


def _child_text(element, name: str) -> Optional[str]:
    for child in element.iter():
        if _strip_ns(child.tag) == name and child.text:
            return child.text.strip()
    return None


def _maven_invocations(pom_path: Path, repo_root: Path) -> List[dict]:
    try:
        text = pom_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # A pom carrying a DTD is not a pom: entity expansion is the classic XML
    # denial-of-service against stdlib parsers, so such a file is refused.
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return []
    try:
        root_element = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return []
    properties = _pom_properties(root_element)
    pom_dir = str(pom_path.parent)
    build_file = str(pom_path.relative_to(repo_root))
    invocations = []

    for plugin in root_element.iter():
        if _strip_ns(plugin.tag) != "plugin":
            continue
        artifact = _child_text(plugin, "artifactId")
        if artifact not in _MAVEN_GENERATOR_PLUGINS:
            continue
        for configuration in plugin.iter():
            if _strip_ns(configuration.tag) != "configuration":
                continue
            generator = _child_text(configuration, "generatorName") or _child_text(
                configuration, "language"
            )
            raw_input = _child_text(configuration, "inputSpec")
            if not generator and not raw_input:
                continue
            entry = {
                "build_file": build_file,
                "tool": "maven",
                "generator": (generator or "").strip(),
                "kind": _generator_kind(generator or ""),
                "spec_path": None,
                "options": {},
                "api_package": _child_text(configuration, "apiPackage"),
            }
            for key in _OPTION_KEYS:
                value = _child_text(configuration, key)
                if value:
                    entry["options"][key] = value
            if raw_input:
                substituted = _substitute(raw_input, properties, pom_dir)
                if "${" in substituted:
                    entry["unresolved_input"] = raw_input
                else:
                    contained = _contain(repo_root, pom_path.parent, substituted)
                    if contained is None:
                        entry["unresolved_input"] = raw_input
                    else:
                        entry["spec_path"] = contained
            invocations.append(entry)
    return invocations


# ---------------------------------------------------------------------------
# Gradle
# ---------------------------------------------------------------------------

_GRADLE_ASSIGNMENT = re.compile(
    r"\b(generatorName|inputSpec|apiPackage)\s*(?:=|\.set\()\s*[\"']([^\"']+)[\"']"
)


def _gradle_blocks(text: str) -> List[str]:
    """Top-level brace-balanced chunks, so paired assignments stay together."""
    blocks = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth = max(depth - 1, 0)
            if depth == 0:
                blocks.append(text[start : index + 1])
    return blocks or [text]


def _gradle_root_dir(build_path: Path, repo_root: Path) -> Path:
    current = build_path.parent
    while True:
        if any((current / name).exists() for name in ("settings.gradle", "settings.gradle.kts")):
            return current
        if current == repo_root or current.parent == current:
            return repo_root
        current = current.parent


def _gradle_invocations(build_path: Path, repo_root: Path) -> List[dict]:
    try:
        text = build_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "generatorName" not in text:
        return []
    build_file = str(build_path.relative_to(repo_root))
    root_dir = _gradle_root_dir(build_path, repo_root)
    invocations = []
    for block in _gradle_blocks(text):
        assignments = dict()
        for key, value in _GRADLE_ASSIGNMENT.findall(block):
            assignments.setdefault(key, value)
        generator = assignments.get("generatorName")
        if not generator:
            continue
        entry = {
            "build_file": build_file,
            "tool": "gradle",
            "generator": generator,
            "kind": _generator_kind(generator),
            "spec_path": None,
            "options": _options_from_text(block),
            "api_package": assignments.get("apiPackage"),
        }
        raw_input = assignments.get("inputSpec")
        if raw_input:
            substituted = re.sub(
                r"\$\{?(rootDir|project\.rootDir)\}?", str(root_dir), raw_input
            )
            substituted = re.sub(
                r"\$\{?(projectDir|project\.projectDir)\}?",
                str(build_path.parent),
                substituted,
            )
            if "$" in substituted:
                entry["unresolved_input"] = raw_input
            else:
                contained = _contain(repo_root, build_path.parent, substituted)
                if contained is None:
                    entry["unresolved_input"] = raw_input
                else:
                    entry["spec_path"] = contained
        invocations.append(entry)
    return invocations


# ---------------------------------------------------------------------------
# Bazel
# ---------------------------------------------------------------------------

_BZL_MACRO_DEF = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)
_GENERATOR_MARKER = re.compile(
    r"(?:generate\s+-g|[\"']-g[\"']\s*,)\s*[\"']?([a-z0-9-]+)"
)
_CALL_ATTR = re.compile(r"(\w+)\s*=\s*\"([^\"]+)\"")
_DICT_PAIR = re.compile(r"\"(\w+)\"\s*:\s*\"([^\"]+)\"")


def _dict_chunks(text: str) -> List[str]:
    """Top-level {...} chunks, nested dicts (import_mappings) kept inside."""
    chunks = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth = max(depth - 1, 0)
            if depth == 0:
                chunks.append(text[start : index + 1])
    return chunks
_SPEC_ATTR_NAMES = ("spec_file", "spec", "input_spec", "inputSpec", "src", "openapi_spec")
# A genrule can call the generator inline: generate -g java -i "$(location x.yml)".
_INLINE_GENERATE = re.compile(
    r"generate\s+-g\s+([a-z0-9-]+)[^\n]*?-i\s+\"?\$\(location\s+([^)\"]+)\)"
)
_INLINE_API_PACKAGE = re.compile(r"--api-package\s+\"?([\w.]+)")


def _resolve_spec_path(repo_root: Path, package_dir: Path, raw: str) -> Optional[str]:
    """The existing repo file a build-file spec reference names, or None.

    Bazel macros resolve inputs against different roots (the package, a
    resources root the generator cds into), so the candidates are tried most
    specific first and only a file that exists counts.
    """
    if raw.startswith("//"):
        raw = raw.lstrip("/").split(":")[-1]
    for base in (package_dir, package_dir / "src" / "main" / "resources", repo_root):
        contained = _contain(repo_root, base, raw)
        if contained and (repo_root / contained).is_file():
            return contained
    return None


def _bzl_macro_generators(repo_root: Path, bzl_files: List[Path]) -> Dict[str, dict]:
    """Macro name -> generator info, for macros wrapping openapi-generator.

    The generator flag often sits in a private helper (`def _gen_cmd(): ...
    generate -g spring`) that the public macro calls, and shared options in a
    module-level constant. So markers propagate through intra-file calls to a
    fixpoint, and a def with no options of its own inherits the file's.
    """
    definitions: Dict[str, dict] = {}
    for bzl_path in bzl_files:
        try:
            text = bzl_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_options = _options_from_text(text)
        relative = str(bzl_path.relative_to(repo_root))
        matches = list(_BZL_MACRO_DEF.finditer(text))
        for index, match in enumerate(matches):
            body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[match.start() : body_end]
            definitions[match.group(1)] = {
                "body": body,
                "file_options": file_options,
                "defined_in": relative,
            }

    macros: Dict[str, dict] = {}
    for name, definition in definitions.items():
        generator_match = _GENERATOR_MARKER.search(definition["body"])
        if generator_match is None:
            continue
        generator = generator_match.group(1)
        macros[name] = {
            "generator": generator,
            "kind": _generator_kind(generator),
            "options": {
                **definition["file_options"],
                **_options_from_text(definition["body"]),
            },
            "defined_in": definition["defined_in"],
        }

    changed = True
    while changed:
        changed = False
        for name, definition in definitions.items():
            if name in macros:
                continue
            for known, info in list(macros.items()):
                if re.search(rf"\b{re.escape(known)}\s*\(", definition["body"]):
                    macros[name] = {
                        "generator": info["generator"],
                        "kind": info["kind"],
                        "options": {
                            **info["options"],
                            **definition["file_options"],
                            **_options_from_text(definition["body"]),
                        },
                        "defined_in": definition["defined_in"],
                    }
                    changed = True
                    break
    return macros


def _call_sites(text: str, macro_name: str):
    """(attrs_text) for each `macro_name(...)` call, balanced-paren scanned."""
    for match in re.finditer(rf"\b{re.escape(macro_name)}\s*\(", text):
        depth = 1
        index = match.end()
        while index < len(text) and depth:
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
            index += 1
        yield text[match.end() : index - 1]


def _bazel_entry(macro: dict, build_file: str, extra_options: str) -> dict:
    return {
        "build_file": build_file,
        "tool": "bazel",
        "generator": macro["generator"],
        "kind": macro["kind"],
        "spec_path": None,
        "options": dict(macro["options"], **_options_from_text(extra_options)),
        "api_package": None,
    }


def _bazel_invocations(repo_root: Path, build_files: List[Path], macros: Dict[str, dict]) -> List[dict]:
    invocations = []
    for build_path in build_files:
        try:
            text = build_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        build_file = str(build_path.relative_to(repo_root))
        package_dir = build_path.parent

        # Inline generator calls in genrule cmds.
        for match in _INLINE_GENERATE.finditer(text):
            generator, raw_spec = match.group(1), match.group(2).strip()
            line = text[match.start() : text.find("\n", match.start())]
            package_match = _INLINE_API_PACKAGE.search(line)
            entry = {
                "build_file": build_file,
                "tool": "bazel",
                "generator": generator,
                "kind": _generator_kind(generator),
                "spec_path": None,
                "options": _options_from_text(line),
                "api_package": package_match.group(1) if package_match else None,
            }
            resolved = _resolve_spec_path(repo_root, package_dir, raw_spec)
            if resolved is None:
                entry["unresolved_input"] = raw_spec
            else:
                entry["spec_path"] = resolved
            invocations.append(entry)

        for macro_name, macro in macros.items():
            if macro_name not in text:
                continue
            for attrs_text in _call_sites(text, macro_name):
                attrs = dict(_CALL_ATTR.findall(attrs_text))
                dict_entries = [
                    dict(_DICT_PAIR.findall(chunk))
                    for chunk in _dict_chunks(attrs_text)
                ]
                spec_dicts = [d for d in dict_entries if d.get("spec_file") or d.get("spec")]
                if spec_dicts:
                    # Group form: specs = [{"spec_file": ..., "api_package": ...}, ...]
                    for spec_dict in spec_dicts:
                        entry = _bazel_entry(macro, build_file, attrs_text)
                        entry["api_package"] = spec_dict.get("api_package")
                        raw_spec = spec_dict.get("spec_file") or spec_dict.get("spec")
                        resolved = _resolve_spec_path(repo_root, package_dir, raw_spec)
                        if resolved is None:
                            entry["unresolved_input"] = raw_spec
                        else:
                            entry["spec_path"] = resolved
                        invocations.append(entry)
                    continue
                if not attrs:
                    # The macro's own `def` line matches the call pattern but
                    # carries no string attributes; it is not an invocation.
                    continue
                entry = _bazel_entry(macro, build_file, attrs_text)
                entry["api_package"] = attrs.get("api_package") or attrs.get("apiPackage")
                raw_spec = next(
                    (attrs[name] for name in _SPEC_ATTR_NAMES if attrs.get(name)), None
                )
                if raw_spec:
                    resolved = _resolve_spec_path(repo_root, package_dir, raw_spec)
                    if resolved is None:
                        entry["unresolved_input"] = raw_spec
                    else:
                        entry["spec_path"] = resolved
                invocations.append(entry)
    return invocations


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_SKIPPED_DIRS = {".git", "node_modules", "target", "build", "dist", ".gradle", ".idea"}


def collect_build_evidence(repo_root: str) -> List[dict]:
    """Every generator invocation the repo's build files declare."""
    root = Path(os.path.realpath(repo_root))
    poms: List[Path] = []
    gradles: List[Path] = []
    bazel_build_files: List[Path] = []
    bzl_files: List[Path] = []
    for current_dir, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIPPED_DIRS)
        for filename in filenames:
            path = Path(current_dir) / filename
            if filename == "pom.xml":
                poms.append(path)
            elif filename in ("build.gradle", "build.gradle.kts"):
                gradles.append(path)
            elif filename in ("BUILD", "BUILD.bazel"):
                bazel_build_files.append(path)
            elif filename.endswith(".bzl"):
                bzl_files.append(path)

    invocations: List[dict] = []
    for pom in sorted(poms):
        invocations.extend(_maven_invocations(pom, root))
    for gradle in sorted(gradles):
        invocations.extend(_gradle_invocations(gradle, root))
    macros = _bzl_macro_generators(root, sorted(bzl_files))
    if macros:
        invocations.extend(
            _bazel_invocations(root, sorted(bazel_build_files + bzl_files), macros)
        )
    return invocations


def evidence_by_spec(invocations: List[dict]) -> Dict[str, List[dict]]:
    """spec repo-relative path -> the invocations that name it as input."""
    by_spec: Dict[str, List[dict]] = {}
    for invocation in invocations:
        spec_path = invocation.get("spec_path")
        if spec_path:
            by_spec.setdefault(spec_path, []).append(invocation)
    return by_spec

from mcp.server.fastmcp import FastMCP
from typing import Optional
from pathlib import Path
import os, subprocess, shutil, sys, json

APP_NAME = "SwaggerGenerator MCP"
DEFAULT_WORK_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SCRIPT_URL = "https://raw.githubusercontent.com/qodex-ai/apimesh/main/bootstrap_mcp_runner.sh"

mcp = FastMCP(APP_NAME)

def _require(name: str, val: Optional[str]):
    if not val or str(val).strip().lower() == "null":
        raise ValueError(f"Missing required parameter: {name}")

def _need(cmd: str):
    if shutil.which(cmd) is None:
        raise RuntimeError(f"Missing dependency: {cmd} is not on PATH")

def _ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def _collect_artifacts(base_dir: str, repo_path: str) -> dict:
    """
    Gather the generated swagger.json path and a link to the local apimesh docs file.
    """
    artifacts = {}

    config_path = Path(base_dir) / "apimesh" / "config.json"
    config_data = {}
    swagger_path = os.path.join(repo_path, "apimesh", "swagger.json")
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as config_file:
                config_data = json.load(config_file)
            swagger_candidate = config_data.get("output_filepath")
            if swagger_candidate:
                swagger_path = swagger_candidate
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[mcp] warning: unable to read swagger path from {config_path}: {exc}", file=sys.stderr)
            config_data = {}

    swagger_path = os.path.expanduser(swagger_path)
    if swagger_path:
        if not os.path.isabs(swagger_path):
            repo_from_config = config_data.get("repo_path") or repo_path
            swagger_path = os.path.join(repo_from_config, swagger_path)
        swagger_path = os.path.abspath(swagger_path)
        artifacts["swagger_path"] = swagger_path
        try:
            artifacts["swagger_file_link"] = Path(swagger_path).resolve(strict=False).as_uri()
        except ValueError:
            pass

    docs_path = Path(base_dir) / "apimesh-docs.html"
    resolved_docs_path = docs_path.resolve(strict=False)
    artifacts["apimesh_docs_path"] = str(resolved_docs_path)
    try:
        artifacts["apimesh_docs_link"] = resolved_docs_path.as_uri()
    except ValueError:
        pass

    return artifacts

@mcp.tool()
def run_swagger_generation(
    openai_api_key: str,
    repo_path: str,
    timeout_seconds: int = 900
) -> dict:
    """
    This tool takes the path of the repository, openai_api_key and timeout to generate a openapi spec swagger json for that repo.
    """
    _require("openai_api_key", openai_api_key)
    _require("repo_path", repo_path)

    for dep in ("bash", "curl", "git", "python3", "pip3"):
        _need(dep)

    base_dir = DEFAULT_WORK_DIR
    _ensure_dir(base_dir)

    repo_path = os.path.abspath(os.path.expanduser(repo_path))
    if not os.path.isdir(repo_path):
        raise ValueError(f"repo_path is not a directory: {repo_path}")

    # --- fetch script (be sure it's a STRING, not a tuple) ---
    script_url = DEFAULT_SCRIPT_URL  # <-- no trailing comma
    script_path = os.path.join(base_dir, "bootstrap_mcp_runner.sh")  # <-- no trailing comma

    # debug types to Claude's log
    print(f"[mcp] base_dir={base_dir!r} ({type(base_dir)})", file=sys.stderr)
    print(f"[mcp] repo_path={repo_path!r} ({type(repo_path)})", file=sys.stderr)
    print(f"[mcp] script_url={script_url!r} ({type(script_url)})", file=sys.stderr)
    print(f"[mcp] script_path={script_path!r} ({type(script_path)})", file=sys.stderr)

    curl = subprocess.run(
        ["curl", "-sSL", script_url, "-o", script_path],
        capture_output=True, text=True
    )
    if curl.returncode != 0:
        raise RuntimeError(f"curl failed ({curl.returncode}): {curl.stderr or curl.stdout}")

    chmod = subprocess.run(["chmod", "+x", script_path], capture_output=True, text=True)
    if chmod.returncode != 0:
        raise RuntimeError(f"chmod failed ({chmod.returncode}): {chmod.stderr or chmod.stdout}")

    # --- env for the script ---
    env = os.environ.copy()
    env.update({
        "OPENAI_API_KEY": openai_api_key,
        "SWAGGER_BOT_REPO_PATH": repo_path,
        "WORK_DIR": base_dir,
    })

    # --- command (ALL ARGS AS STRINGS) ---
    cmd = [
        "bash", script_path,
        "--repo-path", repo_path,
        "--openai-api-key", openai_api_key,
        "--project-api-key", "null",
        "--ai-chat-id", "null",
        "--is-mcp", "true",
    ]
    print(f"[mcp] running: {cmd} (cwd={base_dir})", file=sys.stderr)

    proc = subprocess.run(
        cmd,
        cwd=base_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )

    result = {
        "exit_code": proc.returncode,
        "work_dir": base_dir,
        "stdout": proc.stdout[-200_000:],
        "stderr": proc.stderr[-200_000:],
    }
    result.update(_collect_artifacts(base_dir, repo_path))
    return result

if __name__ == "__main__":
    print("[mcp] server booted; waiting on stdio", file=sys.stderr)
    mcp.run()

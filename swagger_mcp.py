from mcp.server.fastmcp import FastMCP
from typing import Optional
import os, subprocess, shutil, sys

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

    return {
        "exit_code": proc.returncode,
        "work_dir": base_dir,
        "stdout": proc.stdout[-200_000:],
        "stderr": proc.stderr[-200_000:],
    }

if __name__ == "__main__":
    print("[mcp] server booted; waiting on stdio", file=sys.stderr)
    mcp.run()

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VENV_DIR="qodexai-virtual-env"
REPO_URL="${REPO_URL:-https://github.com/qodex-ai/apimesh.git}"
REPO_NAME="apimesh"
BRANCH_NAME="${BRANCH_NAME:-main}"
REPO_DIR=""

PROJECT_API_KEY="null"
OPENAI_API_KEY="null"
AI_CHAT_ID="null"

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing dependency: $1" >&2; exit 2; }; }
need bash; need git; need curl; need python3; need pip3

cleanup() {
  local exit_code=$?
  trap - EXIT
  cd "$SCRIPT_DIR"

  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    deactivate >/dev/null 2>&1 || true
  fi

  if [[ -n "$REPO_DIR" && -d "$REPO_DIR" ]]; then
    echo "Removing cloned repository at '$REPO_DIR'"
    rm -rf "$REPO_DIR"
  fi

  if [[ -d "$VENV_DIR" ]]; then
    echo "Removing virtual environment at '$VENV_DIR'"
    rm -rf "$VENV_DIR"
  fi

  exit "$exit_code"
}

trap cleanup EXIT

if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating Python venv at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

pip3 install --upgrade pip
pip3 install \
  "langchain==0.3.16" \
  "langchain-community==0.3.16" \
  "langchain-openai==0.3.5" \
  "openai==1.76.0" \
  "tiktoken==0.8.0" \
  "faiss-cpu==1.9.0.post1" \
  "langchain-text-splitters==0.3.4" \
  "pyyaml==6.0.2" \
  "tree-sitter==0.25.1" \
  "tree-sitter-python==0.23.6" \
  "tree-sitter-javascript==0.23.1" \
  "esprima==4.0.1"

# --- repo setup (clone/update specific branch) ---
if [[ -d "$REPO_NAME/.git" ]]; then
  echo "Repo exists, switching to branch '$BRANCH_NAME' and pulling latest..."
  git -C "$REPO_NAME" fetch --prune origin
  git -C "$REPO_NAME" checkout -B "$BRANCH_NAME" "origin/$BRANCH_NAME"
  git -C "$REPO_NAME" pull --ff-only origin "$BRANCH_NAME"
else
  echo "Cloning repo branch '$BRANCH_NAME'..."
  git clone --branch "$BRANCH_NAME" --single-branch "$REPO_URL" "$REPO_NAME"
fi
# --- end repo setup ---

REPO_DIR="$(cd "$REPO_NAME" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-api-key) PROJECT_API_KEY="${2:-null}"; shift 2 ;;
    --openai-api-key)  OPENAI_API_KEY="${2:-null}";  shift 2 ;;
    --ai-chat-id)      AI_CHAT_ID="${2:-null}";      shift 2 ;;
    --repo-path)       REPO_PATH="${2:-null}";       shift 2 ;;
    *) echo "Ignoring unknown arg: $1"; shift ;;
  esac
done

export APIMESH_CONFIG_PATH="${APIMESH_CONFIG_PATH:-$REPO_DIR/config.yml}"

cd "$REPO_DIR"
python3 -m swagger_generation_cli "$REPO_PATH" "$OPENAI_API_KEY" "$PROJECT_API_KEY" "$AI_CHAT_ID" true

exit 0

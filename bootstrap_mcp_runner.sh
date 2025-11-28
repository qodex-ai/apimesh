#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO_URL="${REPO_URL:-https://github.com/qodex-ai/apimesh.git}"
BRANCH_NAME="${BRANCH_NAME:-main}"
REPO_DIR=""

PROJECT_API_KEY="null"
OPENAI_API_KEY="null"
AI_CHAT_ID="null"
REPO_PATH="$SCRIPT_DIR"
APIMESH_DIR=""
VENV_DIR=""
CLONE_DIR=""

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing dependency: $1" >&2; exit 2; }; }
need bash; need git; need curl; need python3; need pip3

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-api-key) PROJECT_API_KEY="${2:-null}"; shift 2 ;;
    --openai-api-key)  OPENAI_API_KEY="${2:-null}";  shift 2 ;;
    --ai-chat-id)      AI_CHAT_ID="${2:-null}";      shift 2 ;;
    --repo-path)       REPO_PATH="${2:-$REPO_PATH}"; shift 2 ;;
    *) echo "Ignoring unknown arg: $1"; shift ;;
  esac
done

if [[ ! -d "$REPO_PATH" ]]; then
  echo "Provided --repo-path '$REPO_PATH' is not a directory" >&2
  exit 3
fi

REPO_PATH="$(cd "$REPO_PATH" && pwd)"
APIMESH_DIR="$REPO_PATH/apimesh"
VENV_DIR="$APIMESH_DIR/qodexai-virtual-env"
CLONE_DIR="$APIMESH_DIR/apimesh"

cleanup() {
  local exit_code=$?
  trap - EXIT
  cd "$SCRIPT_DIR"

  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    deactivate >/dev/null 2>&1 || true
  fi

  if [[ -d "$CLONE_DIR" ]]; then
    echo "Removing cloned repository at '$CLONE_DIR'"
    rm -rf "$CLONE_DIR"
  fi

  if [[ -d "$VENV_DIR" ]]; then
    echo "Removing virtual environment at '$VENV_DIR'"
    rm -rf "$VENV_DIR"
  fi

  exit "$exit_code"
}

trap cleanup EXIT

mkdir -p "$APIMESH_DIR"

if [[ -d "$VENV_DIR" ]]; then
  echo "Virtual environment already exists at '$VENV_DIR'. Removing it."
  rm -rf "$VENV_DIR"
fi

echo "Creating Python venv at $VENV_DIR"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

pip3 install --upgrade pip
pip3 install \
  "langchain==0.3.16" \
  "langchain-community==0.3.16" \
  "langchain-core==0.3.63" \
  "langchain-openai==0.3.5" \
  "langsmith==0.1.139" \
  "openai==1.76.0" \
  "numpy<2" \
  "tiktoken==0.8.0" \
  "faiss-cpu==1.9.0.post1" \
  "langchain-text-splitters==0.3.4" \
  "pyyaml==6.0.2" \
  "tree-sitter==0.25.1" \
  "tree-sitter-python==0.23.6" \
  "tree-sitter-javascript==0.23.1" \
  "tree-sitter-ruby==0.23.1" \
  "tree-sitter-go==0.25.0" \
  "esprima==4.0.1"

# --- repo setup (clone/update specific branch) ---
if [[ -d "$CLONE_DIR/.git" ]]; then
  echo "Repo exists, switching to branch '$BRANCH_NAME' and pulling latest..."
  git -C "$CLONE_DIR" fetch --prune origin
  git -C "$CLONE_DIR" checkout -B "$BRANCH_NAME" "origin/$BRANCH_NAME"
  git -C "$CLONE_DIR" pull --ff-only origin "$BRANCH_NAME"
else
  echo "Cloning repo branch '$BRANCH_NAME'..."
  if [[ -d "$CLONE_DIR" ]]; then
    rm -rf "$CLONE_DIR"
  fi
  git clone --branch "$BRANCH_NAME" --single-branch "$REPO_URL" "$CLONE_DIR"
fi
# --- end repo setup ---

REPO_DIR="$(cd "$CLONE_DIR" && pwd)"

export PYTHONPATH="$REPO_PATH:$REPO_DIR:${PYTHONPATH:-}"
export APIMESH_CONFIG_PATH="$REPO_DIR/config.yml"
export APIMESH_USER_CONFIG_PATH="$APIMESH_DIR/config.json"
export APIMESH_USER_REPO_PATH="$REPO_PATH"
export APIMESH_OUTPUT_FILEPATH="$APIMESH_DIR/swagger.json"


cd "$REPO_DIR"
python3 -m swagger_generation_cli "$OPENAI_API_KEY" "$PROJECT_API_KEY" "$AI_CHAT_ID" true

exit 0

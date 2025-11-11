#!/usr/bin/env bash
set -euo pipefail

VENV_DIR="qodexai-virtual-env"
REPO_URL="${REPO_URL:-https://github.com/qodex-ai/swagger-generator.git}"
REPO_NAME="swagger-generator"
BRANCH_NAME="${BRANCH_NAME:-main}"

PROJECT_API_KEY="null"
OPENAI_API_KEY="null"
AI_CHAT_ID="null"

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing dependency: $1" >&2; exit 2; }; }
need bash; need git; need curl; need python3; need pip3

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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-api-key) PROJECT_API_KEY="${2:-null}"; shift 2 ;;
    --openai-api-key)  OPENAI_API_KEY="${2:-null}";  shift 2 ;;
    --ai-chat-id)      AI_CHAT_ID="${2:-null}";      shift 2 ;;
    --repo-path)       REPO_PATH="${2:-null}";       shift 2 ;;
    *) echo "Ignoring unknown arg: $1"; shift ;;
  esac
done

cd "$REPO_NAME"
python3 -m swagger_generation_cli "$REPO_PATH" "$OPENAI_API_KEY" "$PROJECT_API_KEY" "$AI_CHAT_ID" true

exit 0

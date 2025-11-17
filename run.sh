#!/bin/bash

VENV_DIR="qodexai-virtual-env"
REPO_URL="https://github.com/qodex-ai/apimesh.git"
REPO_NAME="apimesh"
CURRENT_DIR="$(pwd)"
DEFAULT_REPO_PATH="$CURRENT_DIR"
PARENT_DIR="$(dirname "$CURRENT_DIR")"
CLONE_DIR="$CURRENT_DIR/$REPO_NAME"
VENV_PATH="$CURRENT_DIR/$VENV_DIR"
SCRIPT_SOURCE="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd -P)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "$SCRIPT_SOURCE")"

cleanup() {
    local exit_code=$?
    trap - EXIT
    if command -v deactivate >/dev/null 2>&1; then
        deactivate >/dev/null 2>&1 || true
    fi
    if [[ -d "$CLONE_DIR" ]]; then
        if [[ -n "$APIMESH_PARENT_DIR" && "$APIMESH_PARENT_DIR" == "$CLONE_DIR" ]]; then
            echo "Skipping removal of '$CLONE_DIR' because it is configured as the output directory."
        else
            echo "Removing cloned repository at '$CLONE_DIR'"
            rm -rf "$CLONE_DIR"
        fi
    fi
    if [[ -d "$VENV_PATH" ]]; then
        echo "Removing virtual environment at '$VENV_PATH'"
        rm -rf "$VENV_PATH"
    fi
    exit "$exit_code"
}
trap cleanup EXIT

if [[ ! -d "$DEFAULT_REPO_PATH/.git" && "$PARENT_DIR" != "$CURRENT_DIR" && -d "$PARENT_DIR/.git" ]]; then
    DEFAULT_REPO_PATH="$PARENT_DIR"
fi

REPO_PATH="$DEFAULT_REPO_PATH"

# Check if the virtual environment directory exists
if [[ -d "$VENV_DIR" ]]; then
    echo "Virtual environment '$VENV_DIR' already exists. Skipping creation."
else
    echo "Creating a Python3 virtual environment"
    python3 -m venv $VENV_DIR
    echo "Virtual environment created at '$VENV_DIR'"
fi

source $VENV_DIR/bin/activate
echo "activated a python3 virtual environment"
echo ""

echo "Installing the requirements..."
pip3 install langchain==0.3.16
pip3 install langchain-community==0.3.16
pip3 install langchain-openai==0.3.5
pip3 install openai==1.76.0
pip3 install tiktoken==0.8.0
pip3 install faiss-cpu==1.9.0.post1
pip3 install langchain-text-splitters==0.3.4
pip3 install pyyaml==6.0.2
pip3 install tree-sitter==0.25.1
pip3 install tree-sitter-python==0.23.6
pip3 install tree-sitter-javascript==0.23.1
pip3 install tree-sitter-ruby==0.23.1
pip3 install tree-sitter-go==0.25.0
pip3 install esprima==4.0.1
echo "Installed the requirements"
echo ""

echo "Checking for existing repo..."

if [ -d "$REPO_NAME" ]; then
    echo "Repo already exists. Pulling latest changes..."
    cd "$REPO_NAME"
    git pull
    cd ..
else
    echo "Repo not found. Cloning the repo..."
    git clone "$REPO_URL"
fi


REPO_DIR="apimesh"

# Check if the directory exists
if [ -d "$REPO_DIR" ]; then
  # Check if it's a git repository
  if [ -d "$REPO_DIR/.git" ]; then
    echo "The repository '$REPO_DIR' exists and is a valid Git repository."
  else
    echo "The directory '$REPO_DIR' exists, but it is not a Git repository."
    exit 1
  fi
else
  echo "The repository '$REPO_DIR' does not exist locally."
  exit 1
fi

PROJECT_API_KEY=null
OPENAI_API_KEY=null
AI_CHAT_ID=null

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-api-key)
      PROJECT_API_KEY="$2"
      shift 2 # Shift past the flag and its value
      ;;
    --openai-api-key)
      OPENAI_API_KEY="$2"
      shift 2
      ;;
    --ai-chat-id)
      AI_CHAT_ID="$2"
      shift 2
      ;;
    --repo-path)
      REPO_PATH="$2"
      shift 2
      ;;
    *)
      shift # Skip unknown options
      ;;
  esac
done

if [[ -z "$REPO_PATH" ]]; then
  REPO_PATH="$DEFAULT_REPO_PATH"
fi

if [[ ! -d "$REPO_PATH" ]]; then
  echo "The repository path '$REPO_PATH' does not exist."
  exit 1
fi

REPO_PATH="$(cd "$REPO_PATH" && pwd -P)"
WORKSPACE_DIR="$REPO_PATH/apimesh"
mkdir -p "$WORKSPACE_DIR"
APIMESH_PARENT_DIR="$(cd "$WORKSPACE_DIR" && pwd -P)"
export APIMESH_PARENT_DIR

TARGET_RUN_SCRIPT="$(cd "$APIMESH_PARENT_DIR" && pwd -P)/run.sh"
if [[ "$SCRIPT_PATH" != "$TARGET_RUN_SCRIPT" ]]; then
  cp "$SCRIPT_PATH" "$TARGET_RUN_SCRIPT"
  chmod +x "$TARGET_RUN_SCRIPT"
  echo "Ensured workspace bootstrap script is up to date at '$TARGET_RUN_SCRIPT'."
fi


echo "Running the Python script..."
(
  cd "$REPO_NAME"/ || exit 1
  python3 -m swagger_generation_cli "$REPO_PATH" "$OPENAI_API_KEY" "$PROJECT_API_KEY" "$AI_CHAT_ID"
)
CLI_EXIT_CODE=$?

echo "Swagger generation finished with status $CLI_EXIT_CODE."
exit "$CLI_EXIT_CODE"

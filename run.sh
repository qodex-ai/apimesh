#!/bin/bash

VENV_DIR="qodexai-virtual-env"
REPO_URL="https://github.com/qodex-ai/apimesh.git"
REPO_NAME="apimesh"
CURRENT_DIR="$(pwd)"
CLONE_DIR="$CURRENT_DIR/$REPO_NAME"
VENV_PATH="$CURRENT_DIR/$VENV_DIR"
SCRIPT_SOURCE="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd -P)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "$SCRIPT_SOURCE")"
CONFIG_FILE_NAME="config.json"

resolve_target_repo_path() {
    local dir="$1"
    while [[ -n "$dir" && "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" ]]; then
            local remote
            remote=$(git -C "$dir" config --get remote.origin.url 2>/dev/null || echo "")
            if [[ -z "$remote" || "$remote" != "$REPO_URL" ]]; then
                echo "$dir"
                return 0
            fi
        fi
        local parent
        parent="$(dirname "$dir")"
        if [[ "$parent" == "$dir" ]]; then
            break
        fi
        dir="$parent"
    done
    echo "$1"
    return 0
}

load_config_value() {
    local key="$1"
    local file="$APIMESH_PARENT_DIR/$CONFIG_FILE_NAME"
    if [[ ! -f "$file" ]]; then
        return
    fi
    python3 - "$file" "$key" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    value = data.get(key, "")
    if isinstance(value, str):
        print(value.strip())
    else:
        print(value)
except Exception:
    pass
PY
}

sync_workspace_config() {
    local expected="$APIMESH_PARENT_DIR/$CONFIG_FILE_NAME"
    mkdir -p "$(dirname "$expected")"

    if [[ -s "$expected" ]]; then
        echo "Configuration saved at '$expected'."
        return 0
    fi

    local fallback=""
    if [[ -d "$APIMESH_PARENT_DIR" ]]; then
        fallback=$(find "$APIMESH_PARENT_DIR" -type f -name "config.json" ! -path "$expected" -print -quit 2>/dev/null || true)
    fi
    if [[ -z "$fallback" && -n "$CLONE_DIR" && -d "$CLONE_DIR" ]]; then
        fallback=$(find "$CLONE_DIR" -type f -name "config.json" -print -quit 2>/dev/null || true)
    fi

    if [[ -n "$fallback" && -s "$fallback" ]]; then
        cp "$fallback" "$expected"
        echo "Relocated config.json from '$fallback' to '$expected'."
    else
        echo "No existing config found for '$expected'."
    fi
}

inject_clone_config() {
    local source="$APIMESH_PARENT_DIR/$CONFIG_FILE_NAME"
    if [[ ! -s "$source" || -z "$CLONE_DIR" || ! -d "$CLONE_DIR" ]]; then
        return
    fi
    local legacy_target="$CLONE_DIR/apimesh/.qodexai"
    mkdir -p "$legacy_target"
    cp "$source" "$legacy_target/$CONFIG_FILE_NAME"
}

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

DEFAULT_REPO_PATH="$(resolve_target_repo_path "$CURRENT_DIR")"
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
sync_workspace_config

TARGET_RUN_SCRIPT="$(cd "$APIMESH_PARENT_DIR" && pwd -P)/run.sh"
if [[ "$SCRIPT_PATH" != "$TARGET_RUN_SCRIPT" ]]; then
  cp "$SCRIPT_PATH" "$TARGET_RUN_SCRIPT"
  chmod +x "$TARGET_RUN_SCRIPT"
  echo "Ensured workspace bootstrap script is up to date at '$TARGET_RUN_SCRIPT'."
fi
inject_clone_config

if [[ -z "$OPENAI_API_KEY" || "$OPENAI_API_KEY" == "null" ]]; then
  OPENAI_API_KEY="$(load_config_value "openai_api_key" | tr -d '\r\n')"
fi


echo "Running the Python script..."
(
  cd "$REPO_NAME"/ || exit 1
  python3 -m swagger_generation_cli "$REPO_PATH" "$OPENAI_API_KEY" "$PROJECT_API_KEY" "$AI_CHAT_ID"
)
CLI_EXIT_CODE=$?

sync_workspace_config

echo "Swagger generation finished with status $CLI_EXIT_CODE."
exit "$CLI_EXIT_CODE"

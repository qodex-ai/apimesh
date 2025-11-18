#!/bin/bash

set -e

# Configuration
APIMESH_DIR="apimesh"
VENV_DIR="$APIMESH_DIR/qodexai-virtual-env"
REPO_URL="https://github.com/qodex-ai/apimesh.git"
CLONE_DIR="$APIMESH_DIR/apimesh"
CURRENT_DIR="$(pwd)"

# Default values for optional parameters
OPENAI_API_KEY=""
PROJECT_API_KEY=""
AI_CHAT_ID=""

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --openai-api-key)
      OPENAI_API_KEY="$2"
      shift 2
      ;;
    --project-api-key)
      PROJECT_API_KEY="$2"
      shift 2
      ;;
    --ai-chat-id)
      AI_CHAT_ID="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      echo "Usage: $0 [--openai-api-key KEY] [--project-api-key KEY] [--ai-chat-id ID]"
      exit 1
      ;;
  esac
done

# Cleanup function
cleanup() {
    local exit_code=$?
    trap - EXIT
    
    # Deactivate virtual environment if active
    if command -v deactivate >/dev/null 2>&1; then
        deactivate >/dev/null 2>&1 || true
    fi
    
    # Remove cloned repository
    if [[ -d "$CLONE_DIR" ]]; then
        echo "Removing cloned repository at '$CLONE_DIR'"
        rm -rf "$CLONE_DIR"
    fi
    
    # Remove virtual environment
    if [[ -d "$VENV_DIR" ]]; then
        echo "Removing virtual environment at '$VENV_DIR'"
        rm -rf "$VENV_DIR"
    fi
    
    exit "$exit_code"
}

# Set trap for cleanup on exit
trap cleanup EXIT

# Step 1: Create apimesh folder
echo "Creating apimesh folder..."
mkdir -p "$APIMESH_DIR"
echo "Created folder: $APIMESH_DIR"
echo ""

# Step 2: Create Python virtual environment
echo "Creating Python virtual environment..."
if [[ -d "$VENV_DIR" ]]; then
    echo "Virtual environment already exists at '$VENV_DIR'. Removing it..."
    rm -rf "$VENV_DIR"
fi

python3 -m venv "$VENV_DIR"
echo "Virtual environment created at '$VENV_DIR'"
echo ""

# Step 3: Activate virtual environment and install dependencies
echo "Activating virtual environment..."
source "$VENV_DIR/bin/activate"
echo "Virtual environment activated"
echo ""

echo "Installing Python dependencies..."
pip3 install --quiet --upgrade pip
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
echo "Dependencies installed"
echo ""

# Step 4: Clone the repository
echo "Cloning repository from $REPO_URL..."
if [[ -d "$CLONE_DIR" ]]; then
    echo "Repository already exists at '$CLONE_DIR'. Removing it..."
    rm -rf "$CLONE_DIR"
fi

git clone "$REPO_URL" "$CLONE_DIR"
echo "Repository cloned to '$CLONE_DIR'"
echo ""

# Step 5: Run the swagger generation CLI
echo "Running swagger generation CLI..."
echo "REPO_PATH: $CURRENT_DIR"
echo "OPENAI_API_KEY: ${OPENAI_API_KEY:+***}"
echo "PROJECT_API_KEY: ${PROJECT_API_KEY:+***}"
echo "AI_CHAT_ID: ${AI_CHAT_ID:+***}"
echo ""

# Add current directory and cloned directory to PYTHONPATH so Python can find modules
export PYTHONPATH="$CURRENT_DIR:$CLONE_DIR:$PYTHONPATH"

# Set config paths
export APIMESH_CONFIG_PATH="$CLONE_DIR/config.yml"
export APIMESH_USER_CONFIG_PATH="$CURRENT_DIR/apimesh/config.json"
export APIMESH_USER_REPO_PATH="$CURRENT_DIR"
export APIMESH_OUTPUT_FILEPATH="$CURRENT_DIR/apimesh/swagger.json"

python3 -m apimesh.apimesh.swagger_generation_cli "$OPENAI_API_KEY" "$PROJECT_API_KEY" "$AI_CHAT_ID"

CLI_EXIT_CODE=$?

echo ""
echo "Swagger generation finished with status $CLI_EXIT_CODE."

# Cleanup will happen automatically via trap
exit "$CLI_EXIT_CODE"

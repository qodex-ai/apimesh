#!/bin/bash

VENV_DIR="qodexai-virtual-env"
REPO_URL="${REPO_URL:-https://github.com/qodex-ai/swagger-generator.git}"
REPO_NAME="swagger-generator"
REPO_BRANCH="${REPO_BRANCH:-changes_1}"

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
pip3 install esprima==4.0.1
echo "Installed the requirements"
echo ""

echo "Checking for existing repo..."

if [ -d "$REPO_NAME/.git" ]; then
    echo "Repo already exists. Switching to branch '$REPO_BRANCH' and pulling latest changes..."
    git -C "$REPO_NAME" fetch --prune origin
    git -C "$REPO_NAME" checkout -B "$REPO_BRANCH" "origin/$REPO_BRANCH"
    git -C "$REPO_NAME" pull --ff-only origin "$REPO_BRANCH"
else
    echo "Repo not found. Cloning branch '$REPO_BRANCH'..."
    git clone --branch "$REPO_BRANCH" --single-branch "$REPO_URL" "$REPO_NAME"
fi


REPO_DIR="swagger-generator"

# Check if the directory exists
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "The repository '$REPO_DIR' either does not exist or is not a valid Git repository."
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

echo "Running the Python script..."
source qodexai-virtual-env/bin/activate
cd swagger-generator/
python3 -m run_swagger $REPO_PATH $OPENAI_API_KEY $PROJECT_API_KEY $AI_CHAT_ID
exit 1

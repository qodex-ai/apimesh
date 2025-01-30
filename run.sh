#!/bin/bash

VENV_DIR="qodexai-virtual-env"

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
pip3 install langchain
pip3 install langchain-community
pip3 install langchain-openai
pip3 install openai
pip3 install tiktoken
pip3 install faiss-cpu
pip3 install langchain-text-splitters
pip3 install pyyaml
echo "Installed the requirements"
echo ""

# Download the Github repo
echo "Downloading the repo"
git clone https://github.com/qodex-ai/swagger-bot.git

REPO_DIR="swagger-bot"

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

echo "Running the Python script..."
source qodexai-virtual-env/bin/activate
cd swagger-bot/
python3 -m repo_to_swagger.run_swagger
exit 1

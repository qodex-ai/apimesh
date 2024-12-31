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
echo "Installed the requirements"
echo ""

# Define the URL for the Python file
PYTHON_FILE_URL="https://raw.githubusercontent.com/qodex-ai/swagger-bot/refs/heads/main/script.py"

# Define the name for the downloaded file
PYTHON_FILE_NAME="script.py"

# Download the Python file
echo "Downloading the Python file from $PYTHON_FILE_URL..."
curl -sSL $PYTHON_FILE_URL -o $VENV_DIR/$PYTHON_FILE_NAME

# Check if the file was downloaded successfully
if [ -f "$VENV_DIR/$PYTHON_FILE_NAME" ]; then
    echo "File downloaded successfully: $VENV_DIR/$PYTHON_FILE_NAME"

    # Run the Python file
    echo "Running the Python script..."
    $VENV_DIR/bin/python3 $VENV_DIR/$PYTHON_FILE_NAME
else
    echo "Failed to download the Python file."
    exit 1
fi
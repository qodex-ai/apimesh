#!/bin/bash
set -e

# Default values
REPO_PATH="${REPO_PATH:-/workspace}"
PROJECT_API_KEY="${PROJECT_API_KEY:-null}"
OPENAI_API_KEY="${OPENAI_API_KEY:-null}"
AI_CHAT_ID="${AI_CHAT_ID:-null}"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-path)
      REPO_PATH="$2"
      REPO_PATH_SET=1
      shift 2
      ;;
    --project-api-key)
      PROJECT_API_KEY="$2"
      shift 2
      ;;
    --openai-api-key)
      OPENAI_API_KEY="$2"
      shift 2
      ;;
    --ai-chat-id)
      AI_CHAT_ID="$2"
      shift 2
      ;;
    --help)
      echo "Swagger Generator Docker Image"
      echo ""
      echo "Usage (run from your repository directory):"
      echo ""
      echo "  # Interactive mode - prompts for missing inputs:"
      echo "  cd /path/to/your/repo"
      echo "  docker run -it --rm -v \$(pwd):/workspace qodexai/apimesh"
      echo ""
      echo "  # With environment variables:"
      echo "  cd /path/to/your/repo"
      echo "  docker run --rm -v \$(pwd):/workspace \\"
      echo "    -e OPENAI_API_KEY=your_key \\"
      echo "    -e PROJECT_API_KEY=your_key \\"
      echo "    -e AI_CHAT_ID=your_chat_id \\"
      echo "    qodexai/apimesh"
      echo ""
      echo "  # With command-line arguments:"
      echo "  cd /path/to/your/repo"
      echo "  docker run --rm -v \$(pwd):/workspace \\"
      echo "    qodexai/apimesh \\"
      echo "    --repo-path /workspace \\"
      echo "    --openai-api-key your_key"
      echo ""
      echo "Environment Variables (all optional - will prompt if not provided):"
      echo "  OPENAI_API_KEY      - Your OpenAI API key"
      echo "  PROJECT_API_KEY     - Your project API key"
      echo "  AI_CHAT_ID          - Target AI chat ID"
      echo "  REPO_PATH           - Path to repository (default: /workspace)"
      echo ""
      echo "Arguments (all optional - will prompt if not provided):"
      echo "  --repo-path         - Path to the repository inside container (default: /workspace)"
      echo "  --project-api-key   - Override PROJECT_API_KEY env var"
      echo "  --openai-api-key    - Override OPENAI_API_KEY env var"
      echo "  --ai-chat-id        - Override AI_CHAT_ID env var"
      echo ""
      echo "Note: Always run docker commands from your repository directory. Use -it flags for interactive mode."
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      echo "Use --help for usage information"
      exit 1
      ;;
  esac
done

# Normalize values - pass empty string if null so Python script can prompt
if [ "$PROJECT_API_KEY" == "null" ] || [ -z "$PROJECT_API_KEY" ]; then
  PROJECT_API_KEY=""
fi

if [ "$OPENAI_API_KEY" == "null" ] || [ -z "$OPENAI_API_KEY" ]; then
  OPENAI_API_KEY=""
fi

if [ "$AI_CHAT_ID" == "null" ] || [ -z "$AI_CHAT_ID" ]; then
  AI_CHAT_ID=""
fi

# Run the swagger generation
# The Python script will prompt for any missing values
cd /app
export PYTHONPATH=/app:$PYTHONPATH

# Set config paths if not already set
if [ -z "${APIMESH_CONFIG_PATH:-}" ]; then
  export APIMESH_CONFIG_PATH="/app/config.yml"
fi

if [ -z "${APIMESH_USER_CONFIG_PATH:-}" ]; then
  export APIMESH_USER_CONFIG_PATH="/workspace/apimesh/config.json"
fi

if [ -z "${APIMESH_DEFAULT_REPO_PATH:-}" ]; then
  export APIMESH_DEFAULT_REPO_PATH="/workspace"
fi

# If repo path doesn't exist and wasn't explicitly provided, check if /workspace exists
if [ ! -d "$REPO_PATH" ] && [ "$REPO_PATH" == "/workspace" ] && [ -z "${REPO_PATH_SET:-}" ]; then
  if [ -d "/workspace" ]; then
    # /workspace exists (mounted), use it
    REPO_PATH="/workspace"
  else
    # /workspace doesn't exist, pass empty string to let Python prompt
    REPO_PATH=""
  fi
fi

# Set default output path environment variable for Python script to use
# This ensures output goes to the mounted volume when running in Docker
if [ -d "/workspace" ] && [ -z "${OUTPUT_FILEPATH:-}" ]; then
  export OUTPUT_FILEPATH="/workspace/apimesh/swagger.json"
fi

python3 swagger_generation_cli.py "$REPO_PATH" "$OPENAI_API_KEY" "$PROJECT_API_KEY" "$AI_CHAT_ID"

# Function to open HTML viewer in browser
open_html_viewer() {
  # Determine the HTML file path
  HTML_FILE=""
  
  # Try multiple locations to find the HTML file
  # 1. Check OUTPUT_FILEPATH environment variable
  if [ -n "${OUTPUT_FILEPATH:-}" ]; then
    SWAGGER_DIR=$(dirname "$OUTPUT_FILEPATH")
    HTML_FILE="$SWAGGER_DIR/apimesh-docs.html"
  fi
  
  # 2. Check default Docker location
  if [ -z "$HTML_FILE" ] || [ ! -f "$HTML_FILE" ]; then
    if [ -d "/workspace" ]; then
      HTML_FILE="/workspace/apimesh/apimesh-docs.html"
    fi
  fi
  
  # 3. Search for apimesh-docs.html in common locations
  if [ -z "$HTML_FILE" ] || [ ! -f "$HTML_FILE" ]; then
    if [ -d "/workspace" ]; then
      FOUND_FILE=$(find /workspace -name "apimesh-docs.html" -type f 2>/dev/null | head -n 1)
      if [ -n "$FOUND_FILE" ]; then
        HTML_FILE="$FOUND_FILE"
      fi
    fi
  fi
  
  # Check if HTML file exists
  if [ -n "$HTML_FILE" ] && [ -f "$HTML_FILE" ]; then
    echo ""
    echo "=========================================="
    echo "Swagger HTML Viewer Generated Successfully"
    echo "=========================================="
    
    # Get relative path for display
    RELATIVE_PATH="$HTML_FILE"
    if [[ "$HTML_FILE" == /workspace/* ]]; then
      RELATIVE_PATH="${HTML_FILE#/workspace/}"
      RELATIVE_PATH="./$RELATIVE_PATH"
    fi
    
    # Check if we're running in Docker
    if [ -f "/.dockerenv" ]; then
      # Running in Docker - print instructions
      echo ""
      echo "The HTML viewer has been generated at:"
      echo "  Relative path:  $RELATIVE_PATH (in your mounted volume)"
      echo ""
      echo "To view it:"
      echo "  1. The file is in your mounted volume directory"
      echo "  2. Open it directly in your browser from your host machine:"
      if [[ "$HTML_FILE" == /workspace/* ]]; then
        echo "     Navigate to your repository directory and open:"
        echo "     $RELATIVE_PATH"
      else
        echo "     $HTML_FILE"
      fi
      echo ""
    else
      # Not in Docker - start HTTP server and open in browser
      echo ""
      echo "Starting local HTTP server to serve HTML viewer..."
      echo ""
      
      # Get the directory containing the HTML file
      HTML_DIR=$(dirname "$HTML_FILE")
      HTML_FILENAME=$(basename "$HTML_FILE")
      
      # Find an available port
      PORT=8000
      # Try to find an available port (check up to 8100)
      while [ $PORT -lt 8100 ]; do
        # Check if port is in use (works on macOS and Linux)
        if command -v lsof > /dev/null; then
          if ! lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
            break
          fi
        else
          # If lsof not available, just try the port
          break
        fi
        PORT=$((PORT + 1))
      done
      
      # Start HTTP server in background
      cd "$HTML_DIR"
      if command -v python3 > /dev/null; then
        python3 -m http.server $PORT > /dev/null 2>&1 &
        SERVER_PID=$!
      elif command -v python > /dev/null; then
        python -m http.server $PORT > /dev/null 2>&1 &
        SERVER_PID=$!
      else
        echo "Error: Python is required to serve the HTML file via HTTP."
        echo "Please install Python or open the file manually: $HTML_FILE"
        echo ""
        echo "To avoid CORS errors, serve it with:"
        echo "  cd $HTML_DIR"
        echo "  python3 -m http.server 8000"
        exit 1
      fi
      
      # Wait a moment for server to start
      sleep 1
      
      # Construct the HTTP URL
      HTTP_URL="http://localhost:$PORT/$HTML_FILENAME"
      
      echo "✓ HTTP server started on port $PORT"
      echo "✓ Opening browser at: $HTTP_URL"
      echo ""
      echo "Note: The server will continue running in the background."
      echo "Press Ctrl+C to stop the server when done."
      echo ""
      
      # Open browser
      if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        open "$HTTP_URL" 2>/dev/null || echo "Could not open browser automatically. Please open: $HTTP_URL"
      elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
        # Linux
        if command -v xdg-open > /dev/null; then
          xdg-open "$HTTP_URL" 2>/dev/null || echo "Could not open browser automatically. Please open: $HTTP_URL"
        elif command -v gnome-open > /dev/null; then
          gnome-open "$HTTP_URL" 2>/dev/null || echo "Could not open browser automatically. Please open: $HTTP_URL"
        else
          echo "Please open in your browser: $HTTP_URL"
        fi
      elif [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "cygwin" ]]; then
        # Windows
        start "$HTTP_URL" 2>/dev/null || echo "Could not open browser automatically. Please open: $HTTP_URL"
      else
        echo "Please open in your browser: $HTTP_URL"
      fi
    fi
    echo "=========================================="
    echo ""
  else
    echo ""
    echo "Warning: HTML viewer file not found at expected location: $HTML_FILE"
    echo "Swagger JSON may have been saved to a different location."
    echo ""
  fi
}

# Open HTML viewer after swagger generation
open_html_viewer


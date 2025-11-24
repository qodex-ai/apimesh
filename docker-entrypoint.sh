#!/bin/bash
set -e

# Default values
PROJECT_API_KEY="${PROJECT_API_KEY:-null}"
OPENAI_API_KEY="${OPENAI_API_KEY:-null}"
AI_CHAT_ID="${AI_CHAT_ID:-null}"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
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
      echo "  docker run --pull always -it --rm -v \$(pwd):/workspace qodexai/apimesh"
      echo ""
      echo "  # With environment variables:"
      echo "  cd /path/to/your/repo"
      echo "  docker run --pull always--rm -v \$(pwd):/workspace \\"
      echo "    -e OPENAI_API_KEY=your_key \\"
      echo "    -e PROJECT_API_KEY=your_key \\"
      echo "    -e AI_CHAT_ID=your_chat_id \\"
      echo "    qodexai/apimesh"
      echo ""
      echo "  # With command-line arguments:"
      echo "  cd /path/to/your/repo"
      echo "  docker run --pull always --rm -v \$(pwd):/workspace \\"
      echo "    qodexai/apimesh \\"
      echo "    --openai-api-key your_key"
      echo ""
      echo "Environment Variables (all optional - will prompt if not provided):"
      echo "  OPENAI_API_KEY      - Your OpenAI API key"
      echo "  PROJECT_API_KEY     - Your project API key"
      echo "  AI_CHAT_ID          - Target AI chat ID"
      echo ""
      echo "Arguments (all optional - will prompt if not provided):"
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

python3 swagger_generation_cli.py "$OPENAI_API_KEY" "$PROJECT_API_KEY" "$AI_CHAT_ID"
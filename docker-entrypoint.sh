#!/bin/bash
set -e

# Default values
PROJECT_API_KEY="${PROJECT_API_KEY:-null}"
OPENAI_API_KEY="${OPENAI_API_KEY:-null}"
AI_CHAT_ID="${AI_CHAT_ID:-null}"

# Flags forwarded to the Python CLI as-is. APIMESH_API_HOST and APIMESH_OPENAI_MODEL
# need no forwarding, Python reads them straight from the environment.
EXTRA_ARGS=()

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
    --no-html)
      export APIMESH_SKIP_HTML=1
      shift
      ;;
    --api-host)
      EXTRA_ARGS+=(--api-host "$2")
      shift 2
      ;;
    --model)
      EXTRA_ARGS+=(--model "$2")
      shift 2
      ;;
    --redetect-framework)
      EXTRA_ARGS+=(--redetect-framework)
      shift
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
      echo "  docker run --pull always --rm -v \$(pwd):/workspace \\"
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
      echo "Environment Variables (all optional):"
      echo "  OPENAI_API_KEY        - Your OpenAI API key"
      echo "  PROJECT_API_KEY       - Your project API key"
      echo "  AI_CHAT_ID            - Target AI chat ID"
      echo "  APIMESH_API_HOST      - Base URL written to servers[0].url of the spec"
      echo "  APIMESH_OPENAI_MODEL  - OpenAI model to use (default gpt-5.6-terra)"
      echo ""
      echo "Arguments (all optional):"
      echo "  --project-api-key     - Override PROJECT_API_KEY env var"
      echo "  --openai-api-key      - Override OPENAI_API_KEY env var"
      echo "  --ai-chat-id          - Override AI_CHAT_ID env var"
      echo "  --api-host URL        - Override APIMESH_API_HOST env var and the stored value"
      echo "  --model NAME          - Override APIMESH_OPENAI_MODEL env var and the stored value"
      echo "  --redetect-framework  - Forget the cached framework and detect it again"
      echo "  --no-html             - Write swagger.json only, skip the HTML viewer"
      echo ""
      echo "Without an API host the spec is still generated, with a placeholder servers[0].url."
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

python3 swagger_generation_cli.py "$OPENAI_API_KEY" "$PROJECT_API_KEY" "$AI_CHAT_ID" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
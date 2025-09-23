# Swagger Bot --- Quick Start

## Approach A --- Run the MCP server directly

1)  **Download the MCP server file**

``` bash
wget https://github.com/qodex-ai/swagger-bot/blob/ankit/swagger_mcp.py -O swagger_mcp.py
```

2)  **Add this to your MCP settings**

``` json
{
  "mcpServers": {
    "swagger-bot": {
      "command": "uv",
      "args": ["run", "/Users/ankits/Downloads/testing_mcp/swagger_mcp.py"]
    }
  }
}
```

> Replace the path with wherever you saved `swagger_mcp.py`.

------------------------------------------------------------------------

## Approach B --- One-liner install & run (curl)

``` bash
curl -sSL https://raw.githubusercontent.com/qodex-ai/swagger-bot/refs/heads/main/run.sh -o script.sh   && chmod +x script.sh   && ./script.sh --repo-path {repo_path} --project-api-key {project_api_key} --ai-chat-id {ai_chat_id}
```

**Flags**

-   `--repo-path` --- local path where the repo should be cloned / used
-   `--project-api-key` --- your project API key
-   `--ai-chat-id` --- target AI chat ID

------------------------------------------------------------------------

That's it---pick the approach that suits your setup and you're ready to
go.

# Swagger Generator 🚀

Open-source tool to auto-analyze your code and generate accurate Swagger/OpenAPI documentation. Save time, improve API visibility, and keep docs in sync.


[![LinkedIn](https://img.shields.io/badge/LinkedIn-0077B5?style=for-the-badge&logo=linkedin&logoColor=white)](https://www.linkedin.com/company/qodexai)
[![X](https://img.shields.io/badge/Follow%20on%20X-000000?style=for-the-badge&logo=twitter&logoColor=white)](https://x.com/qodex_ai)


------------------------------------------------------------------------

## OpenAPI/Swagger Generator – Open Source

This open-source project scans your codebase, detects API endpoints, and instantly generates Swagger/OpenAPI specifications. No more manual documentation — keep your API docs up-to-date, accurate, and developer-friendly.

### ✨ Features

Code Analysis → Auto-discovers REST APIs from your code.

Swagger/OpenAPI Docs → Generates spec files instantly.

Language Agnostic → Works across popular frameworks.

Developer Friendly → Easy setup, extensible, open-source.

SEO Benefit → Great for teams publishing public API docs.

### 🚀 Why Use It?

Eliminate manual documentation.

Keep API docs always in sync with your code.

Improve onboarding for new devs and external users.

Integrate Swagger UI for interactive docs.

Perfect for developers, startups, and open-source projects who want reliable, always-up-to-date API documentation without extra effort.

------------------------------------------------------------------------

## 🚀 Quick Start

You can set up Swagger Generator in **two ways**:

------------------------------------------------------------------------

### Approach A --- One-liner install & run (curl) ✅ *Quickest setup*

``` bash
curl -sSL https://raw.githubusercontent.com/qodex-ai/swagger-generator/refs/heads/main/run.sh -o script.sh   && chmod +x script.sh   && ./script.sh --repo-path {repo_path} --project-api-key {project_api_key} --ai-chat-id {ai_chat_id}
```

**Flags**\
- `--repo-path` --- local path where the repo should be cloned / used\
- `--project-api-key` --- your project API key\
- `--ai-chat-id` --- target AI chat ID

------------------------------------------------------------------------

### Approach B --- Run the MCP server directly

1.  **Download the MCP server file**

``` bash
wget https://github.com/qodex-ai/swagger-generator/blob/ankit/swagger_mcp.py -O swagger_mcp.py
```

2.  **Add this to your MCP settings**

``` json
{
  "mcpServers": {
    "swagger-generator": {
      "command": "uv",
      "args": ["run", "/path/to/swagger_mcp/swagger_mcp.py"]
    }
  }
}
```

> Replace the path with wherever you saved `swagger_mcp.py`.

------------------------------------------------------------------------

Once complete, you'll find a generated **`swagger.json`** in your repo
path, ready to use with any Swagger UI or OpenAPI tooling.

------------------------------------------------------------------------

🎉 That's it---pick the approach that suits your setup and you're ready
to go!

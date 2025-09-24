# Swagger Generator 🚀

Generate **OpenAPI (Swagger) specs** for your codebase in seconds.  
Just provide a repo path and API key, and Swagger Generator will analyze your project and produce a Swagger JSON automatically.

---

## ✨ Features

- 🔍 **Automatic OpenAPI Spec Generation**  
  Analyze any repository and generate an OpenAPI-compliant Swagger JSON with minimal setup.

- ⚡ **Quick Setup**  
  Run directly as an MCP server or via a one-liner install script.

- 🔑 **Secure API Integration**  
  Uses your project API key and AI chat ID for uploading it to the Qodex project.

- 🛠️ **Flexible Execution**  
  - Integrates with MCP settings  
  - Or run standalone with a shell script  

- 📦 **Repository Aware**  
  Works with any local repo path you provide.

---

## 🚀 Quick Start

You can set up Swagger Generator in **two ways**:  

---

### Approach A — Run the MCP server directly

1. **Download the MCP server file**

```bash
wget https://github.com/qodex-ai/swagger-generator/blob/ankit/swagger_mcp.py -O swagger_mcp.py
```

2. **Add this to your MCP settings**

```json
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

---

### Approach B — One-liner install & run (curl)

```bash
curl -sSL https://raw.githubusercontent.com/qodex-ai/swagger-generator/refs/heads/main/run.sh -o script.sh \
  && chmod +x script.sh \
  && ./script.sh --repo-path {repo_path} --project-api-key {project_api_key} --ai-chat-id {ai_chat_id}
```

**Flags**  
- `--repo-path` — local path where the repo should be cloned / used  
- `--project-api-key` — your project API key  
- `--ai-chat-id` — target AI chat ID  

---

Once complete, you’ll find a generated **`swagger.json`** in your repo path, ready to use with any Swagger UI or OpenAPI tooling.

---

That’s it—pick the approach that suits your setup and you’re ready to go! 🎉

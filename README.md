# Swagger Generator 🚀  

**Open-source Swagger/OpenAPI Generator** – Automatically analyze your codebase and generate accurate, always up-to-date API documentation.  
Save time, improve API visibility, and keep docs in sync with your source code.  

[![LinkedIn](https://img.shields.io/badge/LinkedIn-0077B5?style=for-the-badge&logo=linkedin&logoColor=white)](https://www.linkedin.com/company/qodexai)  
[![X](https://img.shields.io/badge/Follow%20on%20X-000000?style=for-the-badge&logo=twitter&logoColor=white)](https://x.com/qodex_ai)  

---

## 📖 What is Swagger Generator?  

Swagger Generator is an **open-source tool** that:  
- Scans your codebase automatically.  
- Detects REST API endpoints.  
- Generates **Swagger/OpenAPI specifications** (`swagger.json`).  

It removes the need for **manual API documentation** and ensures your API docs stay **accurate, searchable, and developer-friendly**.  

---

## ✨ Key Features  

- 🔍 **Code Analysis** → Auto-discovers REST APIs from your code.  
- 📄 **Swagger/OpenAPI Docs** → Instantly generates spec files (`swagger.json`).  
- 🌍 **Language Agnostic** → Works with popular frameworks & languages.  
- ⚡ **Developer Friendly** → Lightweight, fast setup, fully open-source.  
- 📈 **SEO & Visibility** → Great for publishing public API docs with **Swagger UI**.  

---

## 🚀 Why Use Swagger Generator?  

- ⏱️ **Eliminate manual documentation** → No more writing Swagger files by hand.  
- 🔄 **Keep docs always in sync** → Docs auto-update with your codebase.  
- 👨‍💻 **Improve onboarding** → Easier for developers, clients, and external users.  
- 🛠️ **Integrate with Swagger UI** → Interactive API documentation out of the box.  
- ✅ **Ideal for** → Startups, open-source projects, and enterprise teams managing APIs.  

---

## ⚡ Quick Start Guide  

You can set up **Swagger Generator** in two ways:  

---

### Approach A — One-liner install & run (curl) ✅ *Quickest setup*  

```bash
curl -sSL https://raw.githubusercontent.com/qodex-ai/swagger-generator/refs/heads/main/run.sh -o script.sh \
  && chmod +x script.sh \
  && ./script.sh --repo-path {repo_path} --project-api-key {project_api_key} --ai-chat-id {ai_chat_id}
```

Flags

--repo-path → Local path where the repo should be cloned / used

--project-api-key → Your project API key

--ai-chat-id → Target AI chat ID

### Approach B — Run the MCP server directly

Download the MCP server file

```bash
wget https://github.com/qodex-ai/swagger-generator/blob/ankit/swagger_mcp.py -O swagger_mcp.py
```

Add this to your MCP settings
```bash
{
  "mcpServers": {
    "swagger-generator": {
      "command": "uv",
      "args": ["run", "/path/to/swagger_mcp/swagger_mcp.py"]
    }
  }
}
```

Replace /path/to/swagger_mcp/swagger_mcp.py with the actual file path.

📄 Once complete, you’ll find a generated swagger.json in your repo path — ready to use with Swagger UI, OpenAPI tools, or API gateways.

## 🛠️ Installation

Requires Python 3.9+ and uv.

Works on Linux, macOS, and Windows (via WSL).

Lightweight, no heavy dependencies.

## 🤝 Contributing

Contributions are welcome!

Open an issue for bugs, feature requests, or improvements.

Submit PRs to enhance language/framework coverage.

Help us make API documentation automatic and effortless 🚀

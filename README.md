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

## 🧠 How It Works  

Swagger Generator follows a deterministic workflow so you can trust every emitted spec:

1. **Repository Scan**  
   `FileScanner` walks your repo (respecting `config.yml` ignores) and collects relevant source files across Python, Node/TypeScript, Ruby on Rails, and Go.
2. **Framework Detection**  
   `FrameworkIdentifier` uses routing heuristics + LLM verification to determine the dominant framework (Express, Django/FastAPI/Flask, Rails, etc.).
3. **Endpoint Harvesting**  
   - *Language pipelines* (`python_openapi_pipeline`, `nodejs_openapi_pipeline`, `rails_openapi_pipeline`) parse route files and controllers directly whenever possible.  
   - If native parsing is insufficient, `EndpointsExtractor` asks the LLM to interpret tricky files (custom routers, decorators, DSLs).
4. **Context + Intelligence**  
   `GenerateFaissIndex` chunks your codebase into vector embeddings so the LLM can pull nearby authentication logic, schemas, and helpers per endpoint.
5. **Spec Generation**  
   `SwaggerGeneration` prompts OpenAI with the gathered context, stitches every response into a valid `swagger.json`, and saves it to the configured output path.
6. **Optional Upload**  
   If you provide a Project API key, the tool can push the resulting spec to Qodex AI for further automation (test generation, mocking, etc.).

---

## 🌐 Supported Frameworks & Languages  

| Ecosystem | Detection Signals | Pipeline | Notes |
|-----------|------------------|----------|-------|
| **Python** | `urls.py`, `@app.route`, FastAPI decorators | `python_openapi_pipeline` | Works with Django, Flask, FastAPI, DRF-style routes |
| **Node.js / TypeScript** | `express.Router`, `app.get`, decorators | `nodejs_openapi_pipeline` | Supports Express and similar router abstractions |
| **Ruby on Rails** | `config/routes.rb`, controller naming | `rails_openapi_pipeline` | Parses controllers + routes via Tree-sitter |
| **Other stacks** | Golang routers, generic REST hints | fallback LLM extraction | Still improves coverage even without a native pipeline |

You can extend `config.yml` to tweak ignored folders, routing regexes, or add additional language heuristics. Feel free to open a PR with your custom pipeline!

---

## 📦 Output & Customization  

- **Artifacts**  
  - `swagger.json` (default path: repo root)  
  - Optional upload payload for Qodex AI collections  
- **Configuration**  
  - `.qodexai/config.json` stores API keys, repo path, framework overrides, and desired host URL.  
  - `config.yml` lets you refine ignored directories and routing hints.  
  - You can supply CLI flags (`--project-api-key`, `--openai-api-key`, `--ai-chat-id`, `--repo-path`) to avoid prompts.  
- **Usage Tips**  
  - Commit the generated `swagger.json` if you want versioned docs, or add it to `.gitignore` for on-demand generation.  
  - Pair with Swagger UI / Redoc to publish a live portal in minutes.  

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
curl -sSL https://raw.githubusercontent.com/qodex-ai/swagger-generator/refs/heads/main/bootstrap_swagger_generator.sh -o swagger_bootstrap.sh \
  && chmod +x swagger_bootstrap.sh \
  && ./swagger_bootstrap.sh --repo-path {repo_path} --project-api-key {project_api_key} --ai-chat-id {ai_chat_id}
```

Flags

--repo-path → Local path where the repo should be cloned / used

--project-api-key → Your project API key

--ai-chat-id → Target AI chat ID

### Approach B — Run the MCP server directly

Download the MCP server file

```bash
wget https://github.com/qodex-ai/swagger-generator/blob/main/swagger_mcp.py -O swagger_mcp.py
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

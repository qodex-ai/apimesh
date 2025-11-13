# Swagger Generator 🚀  

**Open-source Swagger/OpenAPI Generator** – Automatically analyze your codebase and generate accurate, always up-to-date **OpenAPI 3.0** documentation.  
Save time, improve API visibility, and keep docs in sync with your source code.  

[![LinkedIn](https://img.shields.io/badge/LinkedIn-0077B5?style=for-the-badge&logo=linkedin&logoColor=white)](https://www.linkedin.com/company/qodexai)  
[![X](https://img.shields.io/badge/Follow%20on%20X-000000?style=for-the-badge&logo=twitter&logoColor=white)](https://x.com/qodex_ai)  

---

## 📖 What is Swagger Generator?  

Swagger Generator is an **open-source tool** that:  
- Scans your codebase automatically.  
- Detects REST API endpoints.  
- Generates **OpenAPI 3.0 specifications** (`swagger.json`).  

It removes the need for **manual API documentation** and ensures your API docs stay **accurate, searchable, and developer-friendly**.  

---

## ✨ Key Features  

- 🔍 **Code Analysis** → Auto-discovers REST APIs from your code.  
- 📄 **OpenAPI 3.0 Docs** → Instantly generates spec files (`swagger.json`).  
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
   `SwaggerGeneration` prompts OpenAI with the gathered context, stitches every response into a valid OpenAPI 3.0 `swagger.json`, and saves it to the configured output path.
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
  - OpenAPI 3.0 `swagger.json` (default path: `{repo_path}/apimesh/swagger.json`)  
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

### Approach 1 — Run the MCP server directly

Download the MCP server file

```bash
wget https://github.com/qodex-ai/apimesh/blob/main/swagger_mcp.py -O swagger_mcp.py
```

Add this to your MCP settings
```bash
{
  "mcpServers": {
    "apimesh": {
      "command": "uv",
      "args": ["run", "/path/to/swagger_mcp/swagger_mcp.py"]
    }
  }
}
```

Replace /path/to/swagger_mcp/swagger_mcp.py with the actual file path.

### Approach 2 — One-liner install & run (curl) ✅ *Quickest setup*  

```bash
curl -sSL https://raw.githubusercontent.com/qodex-ai/apimesh/refs/heads/main/bootstrap_swagger_generator.sh -o swagger_bootstrap.sh \
  && chmod +x swagger_bootstrap.sh \
  && ./swagger_bootstrap.sh
```

Flags

--repo-path → Local path of which repo should be used

--project-api-key → Your project API key

--ai-chat-id → Target AI chat ID

📄 Once complete, you'll find a generated OpenAPI 3.0 `swagger.json` in `{repo_path}/apimesh/swagger.json` by default — ready to use with Swagger UI, OpenAPI tools, or API gateways.

### Approach 3 — Docker 🐳 *Containerized setup*

#### Building the Docker Image

```bash
# Build the Docker image
docker build -t qodexai/apimesh:latest .

# Push to Docker Hub
docker push qodexai/apimesh:latest
```

#### Using the Docker Image

**Important:** Always run Docker commands from your repository directory.

**Why the volume mount (`-v $(pwd):/workspace`) is required:**
- The container needs to **read** your repository files to analyze them
- The container needs to **write** the generated `swagger.json` file back to your repository
- Docker containers are isolated from your host filesystem, so the volume mount shares files between your computer and the container

**Step 1: Pull the image**
```bash
docker pull qodexai/apimesh:latest
```

**Step 2: Navigate to your repository and run**

**Option 1: Interactive Mode (Prompts for missing inputs)**

```bash
# Navigate to your repository
cd /path/to/your/repo

# Run interactively - will prompt for any missing inputs
docker run -it --rm -v $(pwd):/workspace qodexai/apimesh:latest
```

**Option 2: With Environment Variables**

```bash
# Navigate to your repository
cd /path/to/your/repo

# Run with all parameters as environment variables
docker run --rm \
  -v $(pwd):/workspace \
  -e OPENAI_API_KEY=your_openai_api_key \
  -e PROJECT_API_KEY=your_project_api_key \
  -e AI_CHAT_ID=your_chat_id \
  qodexai/apimesh:latest
```

**Option 3: With Command-Line Arguments**

```bash
# Navigate to your repository
cd /path/to/your/repo

# Run with command-line arguments
docker run --rm \
  -v $(pwd):/workspace \
  qodexai/apimesh:latest \
  --repo-path /workspace \
  --openai-api-key your_key \
  --project-api-key your_key \
  --ai-chat-id your_chat_id
```

**Environment Variables (all optional - will prompt if not provided):**
- `OPENAI_API_KEY` - Your OpenAI API key
- `PROJECT_API_KEY` - Your project API key
- `AI_CHAT_ID` - Target AI chat ID
- `REPO_PATH` - Path to repository (default: `/workspace`)

**Arguments (all optional - will prompt if not provided):**
- `--repo-path` - Path to the repository inside container (default: `/workspace`)
- `--openai-api-key` - Override OPENAI_API_KEY env var
- `--project-api-key` - Override PROJECT_API_KEY env var
- `--ai-chat-id` - Override AI_CHAT_ID env var

**Quick Examples:**

```bash
# From your repo directory - minimal (will prompt for all inputs)
cd /path/to/your/repo
docker run -it --rm -v $(pwd):/workspace qodexai/apimesh:latest

# From your repo directory - with OpenAI API key (will prompt for others)
cd /path/to/your/repo
docker run -it --rm \
  -v $(pwd):/workspace \
  -e OPENAI_API_KEY=sk-... \
  qodexai/apimesh:latest

# From your repo directory - fully automated (no prompts)
cd /path/to/your/repo
docker run --rm \
  -v $(pwd):/workspace \
  -e OPENAI_API_KEY=sk-... \
  -e PROJECT_API_KEY=your_project_key \
  qodexai/apimesh:latest
```

**Note:** 
- Always run `docker run` commands from your repository directory
- Use `-it` flags for interactive mode when running without parameters
- The generated `swagger.json` will be created in `{repo_path}/apimesh/swagger.json` by default

## 🛠️ Installation

Requires Python 3.10+ and uv.

Works on Linux, macOS, and Windows (via WSL).

Lightweight, no heavy dependencies.

## 🤝 Contributing

Contributions are welcome!

Open an issue for bugs, feature requests, or improvements.

Submit PRs to enhance language/framework coverage.

Help us make API documentation automatic and effortless 🚀

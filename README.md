# ApiMesh: Code to OpenAPI Docs, Instantly

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Docker Build](https://img.shields.io/badge/docker%20build-passing-22c55e?logo=docker&logoColor=white)](https://github.com/qodex-ai/apimesh/actions/workflows/docker-build.yml)
[![Discord](https://img.shields.io/badge/Discord-Join%20Community-5865f2?logo=discord&logoColor=white)](https://discord.gg/MHDayrP7)
[![Twitter](https://img.shields.io/badge/Twitter-Follow%20Updates-1da1f2?logo=x&logoColor=white)](https://x.com/qodex_ai)

**Open-source OpenAPI generator.** Point it at a repository and it detects the web framework (one LLM call, cached), extracts REST endpoints, and produces a valid **OpenAPI 3.0 `swagger.json`** plus a **self-contained HTML endpoint catalog** you can open in any browser. For the supported frameworks below, routes, methods, and prefixes come from static analysis and the LLM only writes schemas and descriptions; for anything else, a generic LLM extraction fallback takes over, which can miss or misread routes.

Built to be driven by humans or by AI agents: one non-interactive command in, one machine-readable spec out, with honest exit codes and a coverage report inside the spec.

## Quick start

### Docker (recommended)

```bash
cd /path/to/your/repo
docker run --pull always -it --rm -v $(pwd):/workspace \
  -e OPENAI_API_KEY=your_key \
  qodexai/apimesh:latest --api-host https://api.yourservice.com
```

Interactive prompts only appear on a real terminal; every value can be supplied by flag or environment variable instead. On Linux, if your repo is not owned by UID 1000, add `--user "$(id -u):$(id -g)"`.

### Shell script

```bash
cd /path/to/your/repo
mkdir -p apimesh && \
  curl -fsSL https://raw.githubusercontent.com/qodex-ai/apimesh/refs/heads/main/run.sh -o apimesh/run.sh && \
  chmod +x apimesh/run.sh && \
  apimesh/run.sh --openai-api-key your_key --api-host https://api.yourservice.com
```

### MCP server

```bash
curl -f https://raw.githubusercontent.com/qodex-ai/apimesh/main/swagger_mcp.py -o swagger_mcp.py
```

```json
{
  "mcpServers": {
    "apimesh": {
      "command": "uv",
      "args": ["run", "/absolute/path/to/swagger_mcp.py"]
    }
  }
}
```

The file carries inline dependency metadata, so `uv run` works as-is. The tool takes `openai_api_key`, `repo_path`, and an optional `api_host`, and raises a tool error when generation fails rather than returning a success payload.

## For AI agents

Everything is settable without a TTY. A complete non-interactive run:

```bash
docker run --pull always --rm -v $(pwd):/workspace \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  qodexai/apimesh:latest --api-host https://api.yourservice.com --no-html
```

**Flags** (docker image, `run.sh`, and the Python CLI all accept them):

| Flag | Effect |
| --- | --- |
| `--api-host <url>` | `servers[0].url` in the spec. Without it, a placeholder is used and a warning printed. |
| `--model <name>` | OpenAI model for generation. Default: `gpt-5.6-terra`. |
| `--no-html` | Write `swagger.json` only, skip the HTML catalog. |
| `--redetect-framework` | Forget the cached framework and detect again. |
| `--openai-api-key <key>` | The key (script and docker wrappers; the env var works everywhere). |

**Environment variables:** `OPENAI_API_KEY`, `APIMESH_API_HOST`, `APIMESH_OPENAI_MODEL`, `APIMESH_SKIP_HTML=1`, `APIMESH_TELEMETRY=0` (opt out of usage telemetry). Flags beat environment variables beat stored config.

**Exit codes:**

| Code | Meaning |
| --- | --- |
| 0 | Spec written (and HTML, unless skipped). |
| 1 | Fatal failure, nothing written. Common causes: no endpoints found, no OpenAI key, framework detection or generation errors. The message says which. |
| 2 | Spec written, but the requested HTML catalog failed to render. |

**Outputs**, all inside the `apimesh/` folder of the scanned repo (docker) or next to it:

| File | What it is |
| --- | --- |
| `swagger.json` | The OpenAPI 3.0 spec. |
| `apimesh-docs.html` | Self-contained, offline endpoint catalog (search, filters, sort). No server needed. |
| `api_index.json` | Per-endpoint state for incremental runs (dependencies, content hashes). |
| `config.json` | Stored key (mode 0600, auto-gitignored), model, host, framework. |
| `metadata_cache/` | Content-addressed parse cache; makes reruns cheap. Safe to delete. |

**Trust, but verify:** every spec carries `info.x-apimesh-coverage` with `endpoints_extracted`, `generated`, `skipped_unchanged`, and `failed` counts. If `failed` is nonzero, treat the spec as incomplete: new endpoints that failed are absent, and a changed endpoint that failed may still show its previous operation until a rerun succeeds. For the supported frameworks, rerunning retries just the failed endpoints; the generic fallback reruns its whole extraction. Custom metadata lives in `x-` extension fields (`x-authorization-tag`, `x-module-tag`, `x-sensitive-information`), so strict OpenAPI validators accept the output.

## Supported frameworks

| Language | Frameworks | How |
| --- | --- | --- |
| Python | Flask, FastAPI, Django (URLconf), Django REST Framework | AST analysis |
| Node.js / TypeScript | Express (incl. mounted routers), NestJS | tree-sitter |
| Ruby on Rails | resources/resource, namespaces, scopes, member/collection, concerns, shallow nesting, engines, split route files | tree-sitter |
| Go | gin, echo, chi, fiber, gorilla/mux, net/http (incl. Go 1.22 patterns) | tree-sitter |
| Anything else | Generic LLM extraction fallback | LLM |

## How it works

1. **Detect** the framework (cached after the first run).
2. **Extract** endpoints: parsers own routes, methods, and prefixes for the supported frameworks; the generic fallback asks the model instead.
3. **Generate** schemas and descriptions with the LLM, batched per source file under a hard token budget, with per-endpoint failure isolation.
4. **Rerun cheaply**: unchanged endpoints are skipped via content hashes, edits to shared helpers invalidate their dependents, and failed endpoints retry automatically.

Only two things leave your machine: source context sent to the OpenAI API for schema generation, and anonymous usage telemetry (an install UUID and run timings, disable with `APIMESH_TELEMETRY=0`). Nothing is created inside your repository except the `apimesh/` output folder.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev setup, test suite, and PR checklist. Issues and PRs welcome, especially framework coverage.

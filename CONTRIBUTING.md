# Contributing to Swagger Generator

First off, thanks for taking the time to contribute! 🎉  
This document explains how to propose changes, report issues, and help improve the project.

> **Project summary:** Swagger Generator analyzes a codebase and produces an OpenAPI (Swagger) JSON. You can run it via a one-liner shell script or as an MCP server. See the [README](./README.md) for setup and usage details.

---

## 📜 Code of Conduct

By participating, you agree to uphold our [Code of Conduct](./CODE_OF_CONDUCT.md).  
If you witness or experience unacceptable behavior, please report it per that document.

## 🔒 Security

Please **do not** open public issues for security vulnerabilities.  
Follow the responsible disclosure process in our [Security Policy](./security.md).

## 🪪 License

By contributing, you agree that your contributions will be licensed under the
[MIT License](./LICENSE).

---

## 🧭 How to Contribute

### 1) Report bugs & request features
- Search existing [Issues](https://github.com/qodex-ai/apimesh/issues) first.
- If none exist, open a new issue with:
  - **What happened** and **what you expected**
  - **Steps to reproduce** (repo, command, flags, logs)
  - Environment details (OS, Python version, shell)

### 2) Propose improvements
- For larger changes, open an issue first to discuss design/approach.
- Small fixes (typos, docs, comments) can go straight to a PR.

---

## 🛠️ Development Setup

### Prerequisites
- Python 3.10 or newer (CI and the dev venv use 3.11)
- Git + a shell (bash/zsh)

### Get the code and install
```bash
git clone https://github.com/qodex-ai/apimesh.git
cd apimesh
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

### Environment variables

The CLI reads four paths from the environment. `run.sh` and the Docker image set them for you; set them yourself when running the Python entry point directly.

| Variable | What it points at |
| --- | --- |
| `APIMESH_CONFIG_PATH` | This repo's `config.yml` (ignored dirs and framework routing patterns) |
| `APIMESH_USER_REPO_PATH` | The repository being analyzed |
| `APIMESH_USER_CONFIG_PATH` | Per-run `config.json` in the target repo's `apimesh/` workspace |
| `APIMESH_OUTPUT_FILEPATH` | Where `swagger.json` is written |

Optional: `APIMESH_API_HOST` and `APIMESH_OPENAI_MODEL` (also available as `--api-host` and `--model`), `APIMESH_SKIP_HTML=1` to skip the HTML viewer, and `APIMESH_TELEMETRY=0` to opt out of usage telemetry.

### Running the generator (two common paths)

**A) One-liner script (quickest)**  
Create a dedicated `apimesh` workspace folder inside the repo you want to analyze.
```bash
# Fetch and run the helper script (see README for the latest command/flags)
mkdir -p apimesh
cd apimesh
curl -sSL https://raw.githubusercontent.com/qodex-ai/apimesh/refs/heads/main/run.sh -o run.sh
chmod +x run.sh
./run.sh --openai-api-key {openai_api_key}
```

> After completion you should always see `config.json`, `swagger.json`, `apimesh-docs.html`, and `run.sh` inside your repo's `apimesh/` workspace.

> The bootstrap helper removes its temporary clone and virtual environment after it finishes generating docs, so rerun the snippet whenever you need to refresh the output.

**B) Run as an MCP server**
```bash
# Fetch the MCP server file if needed
# (If you already have it locally from the clone, point to that path instead)
wget https://raw.githubusercontent.com/qodex-ai/apimesh/main/swagger_mcp.py -O swagger_mcp.py

# Example MCP client config snippet (adjust path/command to your setup)
# {
#   "mcpServers": {
#     "apimesh": {
#       "command": "uv",
#       "args": ["run", "/absolute/path/to/swagger_mcp.py"]
#     }
#   }
# }
```

> After running, you should see a `swagger.json` emitted in the target repo path.

---

## 🧹 Style, Linting & Commit Messages

We aim for clear, readable Python and tidy shell scripts.

- **Python**
  - Prefer small, focused functions.
  - Add docstrings and inline comments where logic is non-obvious.
  - Keep imports organized and avoid unused imports.
- **Shell**
  - Use `set -euo pipefail` for robustness when appropriate.
  - Quote variables; avoid bashisms if not needed.

**Commit messages**
- Use present tense and be descriptive:  
  `feat: add repository path validation`, `fix: handle empty swagger output`, `docs: clarify MCP setup`
- Reference issues when applicable: `Fixes #123`

---

## ✅ Pull Request Checklist

Before you open a PR:

- [ ] `pytest tests/` passes.
- [ ] The change is documented (README or inline comments as needed).
- [ ] Scripts still work (`run.sh`, `bootstrap_mcp_runner.sh` if applicable).
- [ ] Any new flags or behavior are reflected in the README examples.
- [ ] Code is reasonably linted/typed (if you added type hints).
- [ ] No secrets or API keys committed.

Open your PR against the `main` branch and fill out the template (or describe):
- **What** the change does
- **Why** it’s needed
- **How** you validated it

---

## 🧪 Testing Changes

Run the suite from the repo root:

```bash
pytest tests/
```

CI runs the same command before it builds the Docker image, so a red suite blocks the release. Keep tests hermetic: no network access and no OpenAI key, and place them under `tests/`.

For changes that alter generated output, also do one end-to-end run against a small repo with a few HTTP endpoints, and confirm the resulting `swagger.json` has the paths and schemas you expect.

---

## 🧱 Project Structure (high level)

- `swagger_generation_cli.py`: CLI entry point and run orchestration.
- `swagger_mcp.py`: MCP server entry point.
- `python_pipeline/`, `nodejs_pipeline/`, `rails_pipeline/`, `golang_pipeline/`: per-language static analysis that extracts endpoints without an LLM.
- `file_scanner.py`, `framework_identifier.py`, `endpoints_extractor.py`, `faiss_index_generator.py`, `swagger_generator.py`: the LLM fallback path used when a pipeline cannot handle the repo.
- `prompts.py`: every LLM prompt used above.
- `config.py` + `config.yml`: ignored directories and per-framework routing patterns.
- `user_config.py`, `utils.py`, `llm_client.py`, `telemetry_posthog.py`: per-run config, path helpers, OpenAI client, usage telemetry.
- `run.sh`, `bootstrap_mcp_runner.sh`, `Dockerfile`, `docker-entrypoint.sh`: runners and packaging.
- `tests/`: pytest suite.
- `README.md`, `CODE_OF_CONDUCT.md`, `security.md`, `LICENSE`: docs & policies.

(Filenames can evolve; check the tree for the latest layout.)

---

## 🗣️ Communication

- Use GitHub Issues for bugs and feature requests.
- Use PR comments for code review discussions.
- Be respectful, constructive, and kind (see [Code of Conduct](./CODE_OF_CONDUCT.md)).

---

## 🙏 Acknowledgements

Thanks for improving Swagger Generator! Every issue, PR, and suggestion helps make the tool better for everyone.

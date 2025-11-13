# Contributing to Swagger Generator

First off — thanks for taking the time to contribute! 🎉  
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
[AGPL-3.0 License](./LICENSE.md).

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

> The repo primarily contains Python and a couple of shell scripts. You can run the tool either via the helper script or directly as an MCP server.

### Prerequisites
- A recent Python 3.x
- Git + a shell (bash/zsh)
- (Optional) [uv](https://docs.astral.sh/uv/) or a virtual environment tool

### Get the code
```bash
git clone https://github.com/qodex-ai/apimesh.git
cd apimesh
```

### Running the generator (two common paths)

**A) One-liner script (quickest)**
```bash
# Fetch and run the helper script (see README for the latest command/flags)
curl -sSL https://raw.githubusercontent.com/qodex-ai/apimesh/refs/heads/main/bootstrap_swagger_generator.sh -o swagger_bootstrap.sh
chmod +x swagger_bootstrap.sh
./swagger_bootstrap.sh --repo-path {repo_path} --project-api-key {project_api_key} --ai-chat-id {ai_chat_id}
```

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

- [ ] The change is documented (README or inline comments as needed).
- [ ] Scripts still work (`bootstrap_swagger_generator.sh`, `bootstrap_mcp_runner.sh` if applicable).
- [ ] Any new flags or behavior are reflected in the README examples.
- [ ] Code is reasonably linted/typed (if you added type hints).
- [ ] Tests added or manual test steps documented (see below).
- [ ] No secrets or API keys committed.

Open your PR against the `main` branch and fill out the template (or describe):
- **What** the change does
- **Why** it’s needed
- **How** you validated it

---

## 🧪 Testing Changes

This project currently relies primarily on **manual validation**. Please include a short note in your PR describing how you tested:

**Suggested manual test flow**
1. Choose a small public repo with a few HTTP endpoints (or a simple local sample).
2. Run the generator using your change (script or MCP path).
3. Verify a `swagger.json` was generated.
4. Open it in Swagger UI / an OpenAPI viewer to confirm endpoints, paths, and schemas look correct.
5. Try edge cases your change might affect (e.g., unusual file layout, multiple languages, missing dependencies).

If you add unit tests:
- Place them under a `tests/` folder.
- Keep tests hermetic; avoid requiring network access whenever possible.

---

## 🧱 Project Structure (high level)

- `swagger_mcp.py` — MCP server entry and core orchestration.
- `legacy_swagger_pipeline.py`, `bootstrap_swagger_generator.sh`, `bootstrap_mcp_runner.sh` — runner/helper scripts.
- `ruby_dependencies.py` — language-specific helpers (example).
- `README.md`, `CODE_OF_CONDUCT.md`, `security.md`, `LICENSE.md` — docs & policies.

(Filenames can evolve; check the tree for the latest layout.)

---

## 🗣️ Communication

- Use GitHub Issues for bugs and feature requests.
- Use PR comments for code review discussions.
- Be respectful, constructive, and kind (see [Code of Conduct](./CODE_OF_CONDUCT.md)).

---

## 🙏 Acknowledgements

Thanks for improving Swagger Generator! Every issue, PR, and suggestion helps make the tool better for everyone.

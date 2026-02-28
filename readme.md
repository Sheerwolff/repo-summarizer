# GitHub Repository Summarizer

A FastAPI service that accepts a GitHub repository URL and returns a human-readable summary of the project — what it does, what technologies it uses, and how it's structured.

---

## Setup & Run

**Requirements:** Python 3.10+

```bash
# 1. Clone or unzip the project
cd repo-summarizer

# 2. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set your Nebius API key
export NEBIUS_API_KEY=your_key_here     # Windows: $env:NEBIUS_API_KEY="your-key-here"

# 5. Start the server
uvicorn main:app --host 0.0.0.0 --port 8000
```

The server will be available at `http://localhost:8000`.

---

## Usage

```bash
curl -X POST http://localhost:8000/summarize \
  -H "Content-Type: application/json" \
  -d '{"github_url": "https://github.com/psf/requests"}'
```

**Response:**
```json
{
  "summary": "**Requests** is a widely-used Python HTTP library...",
  "technologies": ["Python", "urllib3", "certifi", "charset-normalizer"],
  "structure": "Standard Python package layout with source code in src/requests/..."
}
```

**Error response:**
```json
{
  "status": "error",
  "message": "Repository not found or is private."
}
```

---

## Model Choice

**Model:** `gpt-oss-20b` (OpenAI)

I chose GPT-OSS-20B for this task because it offers a strong balance between performance and flexibility, which is important when analyzing and summarizing structured technical content like GitHub repositories. With 20 billion parameters, it has enough capacity to understand code structure, detect technologies from configuration files, and generate coherent, human-readable summaries. At the same time, being open-source allows for greater transparency and control, which is useful when integrating it into an API service. Its ability to handle longer context windows also makes it suitable for processing multiple repository files and synthesizing them into a clear, structured explanation.

---

## Repository Processing Strategy

The core challenge is extracting *understanding* from a codebase without exceeding the LLM's context window. I treat this as an **information hierarchy problem**.

### What is included (in priority order)

| Tier | Files | Rationale |
|------|-------|-----------|
| 1 | `README`, `ARCHITECTURE`, `CONTRIBUTING`, `CHANGELOG` | Written specifically to explain the project — highest signal per token |
| 2 | Dependency manifests (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, etc.), `Dockerfile`, `docker-compose.yml` | A single manifest file reveals language, framework, all major dependencies, and deployment model |
| 3 | Entry points (`main.py`, `app.py`, `index.js`, `server.go`, `__main__.py`, etc.) | Shows how the project wires together at startup |
| 4 | CI/infrastructure configs (`.github/workflows/`, `Makefile`, `nginx.conf`) | Reveals deployment context, test strategy, build system |
| 5 | Remaining source files | Included shallowest-first: files closer to the root tend to be more architectural |

### What is skipped

- **Lock files** (`package-lock.json`, `yarn.lock`, `poetry.lock`, etc.) — fully redundant given manifests, but expensive in tokens
- **Binary and media files** — images, fonts, compiled artifacts, archives
- **Generated code** — protobuf outputs, migration files, minified bundles, source maps
- **Vendor/dependency directories** — `node_modules/`, `vendor/`, `dist/`, `build/`, `__pycache__/`
- **Test fixtures and data files** — large JSON/YAML fixtures, snapshots

### Budget management

I maintain a **70,000 character budget** (~18k tokens), leaving headroom for the prompt scaffolding and the model's response.

Within each tier, files are sorted by size **ascending** — I prefer including *more smaller files* over *fewer large ones*. Breadth of coverage across the codebase gives the LLM better overall understanding than reading one large file in full.

Tier 1 and 2 files are always included regardless of budget, since they're almost always small and carry the highest value.

### Smart truncation

Files exceeding 6,000 characters are truncated from the **bottom**, not arbitrarily in the middle. The top of a source file (imports, class definitions, function signatures) is far more informative than implementation bodies. A truncation notice is appended so the LLM knows the file was cut.

### Directory tree

The full filtered directory tree is always included as plain text before file contents. It costs very few tokens but gives the LLM the overall shape of the project — module boundaries, naming conventions, monorepo vs. single package, test layout — none of which requires reading individual file contents.

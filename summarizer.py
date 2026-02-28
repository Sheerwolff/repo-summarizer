import os
import json
import logging
import httpx
from processor import build_file_context

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM config — uses Anthropic by default, swap base URL for Nebius
# ---------------------------------------------------------------------------

API_KEY = os.getenv("ANTHROPIC_API_KEY") or os.getenv("NEBIUS_API_KEY")
API_BASE = "https://api.studio.nebius.com/v1/chat/completions"
MODEL = "openai/gpt-oss-20b"
MAX_TOKENS = 1024
TIMEOUT = 60.0

SYSTEM_PROMPT = """You are an expert software engineer performing code repository analysis.
You will be given a directory tree and selected file contents from a GitHub repository.
Your task is to produce a structured analysis.

Respond ONLY with a valid JSON object — no markdown fences, no explanation, no preamble.
The JSON must have exactly these three fields:

{
  "summary": "<A clear, human-readable paragraph describing what the project does, its purpose, and who would use it>",
  "technologies": ["<language or framework>", "..."],
  "structure": "<A concise description of how the project is organized: key directories, main modules, and overall architecture pattern>"
}

Rules:
- summary: 2–4 sentences. Be specific. Mention the project name.
- technologies: List languages, frameworks, major libraries, databases, and infrastructure tools. No version numbers. No duplicates.
- structure: 2–3 sentences describing the layout and architecture (e.g. monorepo, MVC, microservices, library package, CLI tool, etc.)
"""


def _build_user_prompt(repo_data: dict) -> str:
    owner = repo_data["owner"]
    repo = repo_data["repo"]
    tree = repo_data["directory_tree"]
    file_context = build_file_context(repo_data["files"])

    return f"""Repository: {owner}/{repo}

## Directory Structure
```
{tree}
```

## File Contents
{file_context}

Analyze the above repository and return the JSON summary."""


async def summarize_repo(repo_data: dict) -> dict:
    """Call the LLM and return parsed {summary, technologies, structure}."""
    if not API_KEY:
        raise RuntimeError(
            "No LLM API key found. Set ANTHROPIC_API_KEY or NEBIUS_API_KEY environment variable."
        )

    prompt = _build_user_prompt(repo_data)
    logger.info(f"Sending prompt of ~{len(prompt)} chars to LLM")

    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(API_BASE, json=payload, headers=headers, timeout=TIMEOUT)
        except httpx.TimeoutException:
            raise TimeoutError("LLM API request timed out.")
        except httpx.RequestError as e:
            raise RuntimeError(f"Network error calling LLM API: {e}")

        if r.status_code == 401:
            raise RuntimeError("Invalid LLM API key.")
        if r.status_code == 429:
            raise RuntimeError("LLM API rate limit exceeded.")
        if not r.is_success:
            raise RuntimeError(f"LLM API error {r.status_code}: {r.text[:200]}")

    data = r.json()

    # Extract text from response
    raw = data["choices"][0]["message"]["content"].strip()

    # Strip markdown fences if the model added them anyway
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM response as JSON: {raw[:300]}")
        raise RuntimeError(f"LLM returned non-JSON response: {e}")

    # Validate required fields
    for field in ("summary", "technologies", "structure"):
        if field not in result:
            raise RuntimeError(f"LLM response missing required field: '{field}'")

    if not isinstance(result["technologies"], list):
        result["technologies"] = [result["technologies"]]

    return {
        "summary": str(result["summary"]),
        "technologies": [str(t) for t in result["technologies"]],
        "structure": str(result["structure"]),
    }

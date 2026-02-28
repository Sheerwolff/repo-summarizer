import os
import asyncio
import logging
import httpx

from processor import filter_and_prioritize, build_directory_tree

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
TIMEOUT = 20.0
MAX_CONCURRENT_FETCHES = 10


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _get(client: httpx.AsyncClient, url: str) -> dict | list:
    r = await client.get(url, headers=_headers(), timeout=TIMEOUT)
    if r.status_code == 404:
        raise ValueError("Repository not found or is private.")
    if r.status_code == 403:
        msg = r.json().get("message", "")
        if "rate limit" in msg.lower():
            raise RuntimeError("GitHub API rate limit exceeded. Try again later or set GITHUB_TOKEN.")
        raise PermissionError("Access denied. The repository may be private.")
    if r.status_code == 451:
        raise PermissionError("Repository unavailable for legal reasons.")
    r.raise_for_status()
    return r.json()


async def _get_default_branch(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    data = await _get(client, f"{GITHUB_API}/repos/{owner}/{repo}")
    return data["default_branch"]


async def _get_tree(client: httpx.AsyncClient, owner: str, repo: str, branch: str) -> list[dict]:
    data = await _get(client, f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
    if data.get("truncated"):
        logger.warning("GitHub tree response was truncated (very large repo). Results may be incomplete.")
    return [item for item in data.get("tree", []) if item["type"] == "blob"]


async def _fetch_file(client: httpx.AsyncClient, owner: str, repo: str, path: str) -> str | None:
    """Fetch raw file content. Returns None on failure (skip silently)."""
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}"
    try:
        r = await client.get(url, headers=_headers(), timeout=TIMEOUT)
        if r.status_code == 200:
            # Detect binary content heuristically
            try:
                text = r.content.decode("utf-8")
                return text
            except UnicodeDecodeError:
                logger.debug(f"Skipping binary file: {path}")
                return None
        return None
    except Exception as e:
        logger.debug(f"Failed to fetch {path}: {e}")
        return None


async def fetch_repo_contents(owner: str, repo: str) -> dict:
    """
    Returns a dict with:
      - owner, repo
      - tree: list of {path, size} for all blobs
      - directory_tree: rendered ASCII tree string
      - files: dict of {path: content} for selected files
    """
    async with httpx.AsyncClient() as client:
        try:
            branch = await _get_default_branch(client, owner, repo)
        except httpx.TimeoutException:
            raise TimeoutError("GitHub API request timed out.")
        except httpx.RequestError as e:
            raise RuntimeError(f"Network error contacting GitHub: {e}")

        try:
            tree = await _get_tree(client, owner, repo, branch)
        except httpx.TimeoutException:
            raise TimeoutError("GitHub API request timed out fetching tree.")
        except httpx.RequestError as e:
            raise RuntimeError(f"Network error fetching repository tree: {e}")

        if not tree:
            raise ValueError("Repository appears to be empty.")

        directory_tree = build_directory_tree(tree)
        selected_paths = filter_and_prioritize(tree)

        if not selected_paths:
            raise ValueError("No readable files found in repository.")

        logger.info(f"Fetching {len(selected_paths)} files from {owner}/{repo}")

        # Fetch files concurrently with a semaphore to avoid hammering GitHub
        sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

        async def bounded_fetch(path: str) -> tuple[str, str | None]:
            async with sem:
                content = await _fetch_file(client, owner, repo, path)
                return path, content

        results = await asyncio.gather(*[bounded_fetch(p) for p in selected_paths])

        files = {path: content for path, content in results if content is not None}
        logger.info(f"Successfully fetched {len(files)} files")

        return {
            "owner": owner,
            "repo": repo,
            "directory_tree": directory_tree,
            "files": files,
        }

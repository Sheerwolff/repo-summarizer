"""
Repository content filtering, prioritization, and truncation.

Strategy:
  1. Filter out noise (binaries, lock files, generated code, vendor dirs)
  2. Rank remaining files into tiers by information value
  3. Fill a character budget tier-by-tier, preferring more small files over fewer large ones
  4. Truncate oversized files smartly (keep top of file — imports + signatures)
"""

import re
from pathlib import PurePosixPath

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CHAR_BUDGET = 70_000          # ~18–20k tokens, leaving room for prompt + response
MAX_FILE_CHARS = 6_000        # hard cap per individual file before truncation
TREE_MAX_ENTRIES = 500        # cap tree display for huge repos

# ---------------------------------------------------------------------------
# Skip rules
# ---------------------------------------------------------------------------

SKIP_DIRS = {
    "node_modules", "vendor", "dist", "build", ".git", "__pycache__",
    ".next", ".nuxt", "venv", ".venv", "env", ".env", "target",
    "coverage", ".nyc_output", "eggs", ".eggs", "*.egg-info",
    "htmlcov", ".tox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "site-packages", "bower_components", "jspm_packages",
}

SKIP_EXTENSIONS = {
    # Binaries & media
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp", ".tiff",
    ".mp4", ".mp3", ".wav", ".ogg", ".mov", ".avi",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz",
    ".exe", ".dll", ".so", ".dylib", ".a", ".lib",
    ".pyc", ".pyo", ".class", ".jar", ".war",
    ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
    # Lock files (covered below too, but ext catches some)
    ".lock",
}

SKIP_FILENAMES = {
    "package-lock.json", "yarn.lock", "poetry.lock", "Pipfile.lock",
    "composer.lock", "Gemfile.lock", "cargo.lock", "pnpm-lock.yaml",
    "shrinkwrap.json", "npm-shrinkwrap.json",
    ".DS_Store", "Thumbs.db", ".gitkeep",
}

SKIP_PATTERNS = [
    re.compile(r"\.min\.(js|css)$"),          # minified
    re.compile(r"\.bundle\.js$"),              # bundled
    re.compile(r"\.(pb|pb2)\.go$"),            # protobuf generated
    re.compile(r"\.generated\.\w+$"),          # explicitly generated
    re.compile(r"_pb2\.py$"),                  # protobuf python
    re.compile(r"migrations?/\d+.*\.py$"),     # django/alembic migrations
    re.compile(r"\.snap$"),                    # jest snapshots
    re.compile(r"\.map$"),                     # source maps
    re.compile(r"test[_-]?fixtures?/"),        # test fixtures
    re.compile(r"fixtures?/.*\.(json|yaml|yml)$"),
]

# ---------------------------------------------------------------------------
# Priority tiers
# ---------------------------------------------------------------------------

TIER1_NAMES = re.compile(
    r"^(README|ARCHITECTURE|OVERVIEW|CONTRIBUTING|CHANGELOG|DESIGN)(\.md|\.rst|\.txt)?$",
    re.IGNORECASE,
)

TIER2_NAMES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "Gemfile", "composer.json", "mix.exs", "pubspec.yaml",
    "requirements.txt", "Pipfile",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env.example", ".env.sample",
}

TIER3_ENTRY_PATTERNS = re.compile(
    r"(^|/)(main|app|server|index|cli|__main__|__init__)\.(py|go|js|ts|rb|rs|java|cs|cpp|c)$",
    re.IGNORECASE,
)

TIER4_INFRA_PATTERNS = re.compile(
    r"(\.github/workflows/|\.gitlab-ci|Makefile|Taskfile|nginx\.conf|supervisord|gunicorn|uwsgi)",
    re.IGNORECASE,
)


def _should_skip(path: str) -> bool:
    p = PurePosixPath(path)
    parts = p.parts
    name = p.name
    ext = p.suffix.lower()

    # Skip if any path component is a known skip dir
    for part in parts[:-1]:
        if part.lower() in SKIP_DIRS or part.endswith(".egg-info"):
            return True

    if name in SKIP_FILENAMES:
        return True
    if ext in SKIP_EXTENSIONS:
        return True

    for pattern in SKIP_PATTERNS:
        if pattern.search(path):
            return True

    return False


def _tier(path: str) -> int:
    name = PurePosixPath(path).name
    if TIER1_NAMES.match(name):
        return 1
    if name in TIER2_NAMES:
        return 2
    if TIER3_ENTRY_PATTERNS.search(path):
        return 3
    if TIER4_INFRA_PATTERNS.search(path):
        return 4
    return 5


def filter_and_prioritize(tree: list[dict]) -> list[str]:
    """
    Given the raw GitHub tree (list of {path, size, type}),
    return an ordered list of file paths to fetch, respecting the budget.

    Within each tier, we prefer more smaller files over fewer large ones
    (breadth of coverage > depth on one file).
    """
    candidates = []
    for item in tree:
        path = item["path"]
        size = item.get("size", 0)
        if not _should_skip(path):
            candidates.append((path, size, _tier(path)))

    # Sort: tier ASC, then size ASC (smaller files first within tier)
    candidates.sort(key=lambda x: (x[2], x[1]))

    selected = []
    budget = CHAR_BUDGET

    for path, size, tier in candidates:
        if budget <= 0:
            break
        # Estimate: size in bytes ≈ chars for UTF-8 text
        # We'll over-select slightly and truncate later during content building
        allotment = min(size, MAX_FILE_CHARS)
        if allotment <= budget or tier <= 2:  # always include tier 1+2
            selected.append(path)
            budget -= allotment

    return selected


# ---------------------------------------------------------------------------
# Content truncation
# ---------------------------------------------------------------------------

def truncate_file(path: str, content: str, char_limit: int = MAX_FILE_CHARS) -> str:
    """
    Truncate file content intelligently:
    - For source files, keep the top (imports + signatures have most signal)
    - Append a note if truncated
    """
    if len(content) <= char_limit:
        return content

    truncated = content[:char_limit]
    # Try to cut at a clean line boundary
    last_newline = truncated.rfind("\n")
    if last_newline > char_limit * 0.8:
        truncated = truncated[:last_newline]

    lines_total = content.count("\n") + 1
    lines_kept = truncated.count("\n") + 1
    return truncated + f"\n\n... [truncated: showing {lines_kept}/{lines_total} lines]"


def build_file_context(files: dict[str, str]) -> str:
    """
    Render the fetched file contents into a single string for the LLM prompt.
    Applies per-file truncation and tracks total char budget.
    """
    parts = []
    total = 0

    for path, content in files.items():
        content = truncate_file(path, content)
        block = f"### {path}\n```\n{content}\n```"
        parts.append(block)
        total += len(block)
        if total >= CHAR_BUDGET:
            parts.append("... [additional files omitted due to context budget]")
            break

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Directory tree renderer
# ---------------------------------------------------------------------------

def build_directory_tree(tree: list[dict]) -> str:
    """
    Render a filtered directory tree as an ASCII string.
    Binary/noise files are excluded so the tree reflects meaningful structure.
    """
    paths = sorted(
        item["path"] for item in tree
        if item["type"] == "blob" and not _should_skip(item["path"])
    )

    if not paths:
        return "(empty)"

    # Cap for huge repos
    truncated = False
    if len(paths) > TREE_MAX_ENTRIES:
        paths = paths[:TREE_MAX_ENTRIES]
        truncated = True

    lines = []
    for path in paths:
        depth = path.count("/")
        name = PurePosixPath(path).name
        indent = "  " * depth
        lines.append(f"{indent}{'└── ' if depth > 0 else ''}{name}")

    if truncated:
        lines.append(f"  ... [tree truncated at {TREE_MAX_ENTRIES} entries]")

    return "\n".join(lines)

# SPDX-License-Identifier: Apache-2.0
"""GitHub REST API provider for the Archeologist agent.

Wraps GitHub API v3 with httpx. PAT injected via GITHUB_PERSONAL_ACCESS_TOKEN
env var (accessed through sandbox provider config, same pattern as Tavily).
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Files to auto-read when indexing a repository
KEY_FILES = [
    "README.md", "README.rst", "README.txt",
    "package.json", "requirements.txt", "Pipfile", "pyproject.toml",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
    "docker-compose.yml", "docker-compose.yaml", "Dockerfile",
    "tsconfig.json", ".eslintrc.json", ".eslintrc.js",
    "Makefile", "CMakeLists.txt",
    ".github/workflows/ci.yml", ".github/workflows/ci.yaml",
]

# Directories/patterns to skip when listing files
SKIP_PATTERNS = {
    "node_modules/", ".git/", "vendor/", "__pycache__/", ".next/",
    "dist/", "build/", ".cache/", ".tox/", ".mypy_cache/",
    "coverage/", ".nyc_output/", ".pytest_cache/",
}

# Binary extensions to skip
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2",
    ".ttf", ".eot", ".mp3", ".mp4", ".zip", ".tar", ".gz", ".bz2",
    ".pdf", ".exe", ".dll", ".so", ".dylib", ".pyc", ".pyo",
    ".lock", ".min.js", ".min.css",
}

# Source file patterns to auto-read (up to MAX_EXTRA_FILES)
SOURCE_PATTERNS = [
    r".*/routes/.*\.(ts|js|py)$",
    r".*/controllers/.*\.(ts|js|py)$",
    r".*/models/.*\.(ts|js|py)$",
    r".*/schemas/.*\.(ts|js|py)$",
    r".*/middleware/.*\.(ts|js|py)$",
    r".*/api/.*\.(ts|js|py)$",
    r".*/services/.*\.(ts|js|py)$",
    r".*/(app|main|server|index)\.(ts|js|py)$",
]
MAX_EXTRA_FILES = 10
MAX_TREE_ENTRIES = 500
MAX_FILE_SIZE = 50_000  # chars


class GitHubProvider:
    """Async GitHub REST API client for repository analysis."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=headers,
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # -- Repo metadata --------------------------------------------------------

    async def get_repo_info(self, owner: str, repo: str) -> dict[str, Any]:
        """Get basic repository metadata."""
        resp = await self._client.get(f"/repos/{owner}/{repo}")
        resp.raise_for_status()
        data = resp.json()
        return {
            "name": data.get("full_name", f"{owner}/{repo}"),
            "description": data.get("description", ""),
            "language": data.get("language", ""),
            "default_branch": data.get("default_branch", "main"),
            "stars": data.get("stargazers_count", 0),
            "topics": data.get("topics", []),
            "size_kb": data.get("size", 0),
            "created_at": data.get("created_at", ""),
            "updated_at": data.get("pushed_at", ""),
        }

    # -- File tree ------------------------------------------------------------

    async def get_tree(
        self, owner: str, repo: str, branch: str = "main",
    ) -> list[dict[str, Any]]:
        """Get repository file tree (recursive). Filtered and capped."""
        resp = await self._client.get(
            f"/repos/{owner}/{repo}/git/trees/{branch}",
            params={"recursive": "1"},
        )
        resp.raise_for_status()
        tree = resp.json().get("tree", [])

        filtered = []
        for item in tree:
            path = item.get("path", "")
            # Skip directories we don't care about
            if any(path.startswith(skip) or f"/{skip}" in f"/{path}" for skip in SKIP_PATTERNS):
                continue
            # Skip binary files
            ext = os.path.splitext(path)[1].lower()
            if ext in BINARY_EXTENSIONS:
                continue
            filtered.append({
                "path": path,
                "type": item.get("type", "blob"),
                "size": item.get("size", 0),
            })
            if len(filtered) >= MAX_TREE_ENTRIES:
                break

        return filtered

    # -- File content ---------------------------------------------------------

    async def get_file_content(
        self, owner: str, repo: str, path: str, branch: str = "main",
    ) -> str:
        """Read a single file. Returns content string (base64-decoded), capped."""
        resp = await self._client.get(
            f"/repos/{owner}/{repo}/contents/{path}",
            params={"ref": branch},
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("encoding") == "base64" and data.get("content"):
            content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            return content[:MAX_FILE_SIZE]

        return data.get("content", "")[:MAX_FILE_SIZE]

    # -- Dependencies ---------------------------------------------------------

    async def get_dependencies(
        self, owner: str, repo: str, branch: str = "main",
    ) -> dict[str, str]:
        """Detect and read dependency manifests."""
        dep_files = [
            "package.json", "requirements.txt", "Pipfile", "pyproject.toml",
            "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
        ]
        results: dict[str, str] = {}
        for f in dep_files:
            try:
                content = await self.get_file_content(owner, repo, f, branch)
                if content:
                    results[f] = content
            except httpx.HTTPStatusError:
                continue  # File doesn't exist
            except Exception as e:
                logger.debug("Failed to read %s: %s", f, e)
        return results

    # -- Commits --------------------------------------------------------------

    async def get_recent_commits(
        self, owner: str, repo: str, branch: str = "main", count: int = 20,
    ) -> list[dict[str, str]]:
        """Get recent commit messages."""
        resp = await self._client.get(
            f"/repos/{owner}/{repo}/commits",
            params={"sha": branch, "per_page": count},
        )
        resp.raise_for_status()
        commits = []
        for c in resp.json():
            cm = c.get("commit", {})
            author = cm.get("author", {})
            commits.append({
                "sha": c.get("sha", "")[:8],
                "message": cm.get("message", "").split("\n")[0][:200],
                "author": author.get("name", ""),
                "date": author.get("date", ""),
            })
        return commits

    # -- Issues ---------------------------------------------------------------

    async def get_issues(
        self, owner: str, repo: str, state: str = "open", count: int = 20,
    ) -> list[dict[str, Any]]:
        """Get repository issues."""
        resp = await self._client.get(
            f"/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": count},
        )
        resp.raise_for_status()
        issues = []
        for iss in resp.json():
            # Skip pull requests (GitHub returns them in issues endpoint)
            if iss.get("pull_request"):
                continue
            issues.append({
                "number": iss.get("number"),
                "title": iss.get("title", ""),
                "body_preview": (iss.get("body") or "")[:300],
                "labels": [lb.get("name", "") for lb in iss.get("labels", [])],
                "state": iss.get("state", ""),
            })
        return issues

    # -- Key files reader -----------------------------------------------------

    async def read_key_files(
        self, owner: str, repo: str, branch: str, tree: list[dict[str, Any]],
    ) -> dict[str, str]:
        """Read KEY_FILES + auto-detected source files from the tree."""
        tree_paths = {item["path"] for item in tree}
        files_to_read: list[str] = []

        # Key files
        for kf in KEY_FILES:
            if kf in tree_paths:
                files_to_read.append(kf)

        # Pattern-matched source files
        compiled = [re.compile(p) for p in SOURCE_PATTERNS]
        extra = []
        for item in tree:
            if item["type"] != "blob":
                continue
            path = item["path"]
            if path in files_to_read:
                continue
            if any(pat.match(path) for pat in compiled):
                extra.append(path)
                if len(extra) >= MAX_EXTRA_FILES:
                    break
        files_to_read.extend(extra)

        # Read all in parallel
        results: dict[str, str] = {}
        for path in files_to_read:
            try:
                content = await self.get_file_content(owner, repo, path, branch)
                results[path] = content
            except Exception as e:
                logger.debug("Failed to read %s: %s", path, e)
        return results

    # -- URL parsing ----------------------------------------------------------

    @staticmethod
    def parse_repo_url(url: str) -> tuple[str, str]:
        """Extract (owner, repo) from a GitHub URL or shorthand.

        Accepts:
          https://github.com/owner/repo
          https://github.com/owner/repo.git
          github.com/owner/repo
          owner/repo
        """
        url = url.strip().rstrip("/")
        # Strip .git suffix
        if url.endswith(".git"):
            url = url[:-4]
        # Full URL
        match = re.match(r"(?:https?://)?github\.com/([^/]+)/([^/]+)", url)
        if match:
            return match.group(1), match.group(2)
        # Shorthand: owner/repo
        parts = url.split("/")
        if len(parts) == 2 and all(parts):
            return parts[0], parts[1]
        raise ValueError(f"Cannot parse GitHub repo from: {url}")

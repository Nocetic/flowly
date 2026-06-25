"""Hub client for fetching skills from registry."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from flowly.profile import get_flowly_home


# Default registry URL (override via FLOWLY_HUB_REGISTRY env var or config)
import os
DEFAULT_REGISTRY = os.environ.get("FLOWLY_HUB_REGISTRY", "https://useflowlyapp.com")

# GitHub raw content base URL for fallback
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"


@dataclass
class SkillInfo:
    """Information about a skill from the registry."""

    name: str
    slug: str
    description: str
    version: str
    author: str
    homepage: str | None
    repository: str | None
    download_url: str
    hash: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict) -> "SkillInfo":
        return cls(
            name=data.get("name", ""),
            slug=data.get("slug", ""),
            description=data.get("description", ""),
            version=data.get("version", "1.0.0"),
            author=data.get("author", {}).get("displayName", "unknown") if isinstance(data.get("author"), dict) else data.get("author", "unknown"),
            homepage=data.get("homepage"),
            repository=data.get("repository"),
            download_url=data.get("download_url", ""),
            hash=data.get("hash"),
            metadata=data.get("metadata", {}),
        )


class HubClient:
    """
    Client for interacting with the Flowly Hub registry.

    Supports:
    - Central registry (hub.flowly.ai)
    - GitHub-based skills (github:owner/repo/path)
    - Local .skill files
    """

    def __init__(self, registry_url: str | None = None):
        """
        Initialize hub client.

        Args:
            registry_url: Custom registry URL (default: hub.flowly.ai)
        """
        self.registry_url = (registry_url or DEFAULT_REGISTRY).rstrip("/")
        self._client = httpx.Client(timeout=30.0)

    def search(self, query: str, limit: int = 20) -> list[SkillInfo]:
        """
        Search for skills in the registry.

        Args:
            query: Search query.
            limit: Maximum results.

        Returns:
            List of matching skills.
        """
        try:
            resp = self._client.get(
                f"{self.registry_url}/api/skills",
                params={"q": query, "limit": limit}
            )
            resp.raise_for_status()
            data = resp.json()
            skills = data.get("skills", [])
            # Add download_url if missing
            for s in skills:
                if not s.get("download_url"):
                    s["download_url"] = f"{self.registry_url}/api/skills/{s.get('slug', '')}/download"
            return [SkillInfo.from_dict(s) for s in skills]
        except httpx.HTTPError as e:
            logger.warning(f"Registry search failed: {e}")
            return []

    def get_skill(self, slug: str, version: str | None = None) -> SkillInfo | None:
        """
        Get skill info from registry.

        Args:
            slug: Skill slug (e.g., "github", "weather")
            version: Specific version (default: latest)

        Returns:
            Skill info or None if not found.
        """
        try:
            url = f"{self.registry_url}/api/skills/{slug}"
            if version:
                url += f"?version={version}"

            resp = self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            # Web app wraps skill in { skill: {...}, readme: "..." }
            skill_data = data.get("skill", data)
            # Construct download URL if not provided
            if not skill_data.get("download_url"):
                skill_data["download_url"] = f"{self.registry_url}/api/skills/{slug}/download"
            return SkillInfo.from_dict(skill_data)
        except httpx.HTTPError as e:
            logger.warning(f"Failed to get skill {slug}: {e}")
            return None

    def download_skill(self, skill: SkillInfo, target_dir: Path) -> Path | None:
        """
        Download a skill to the target directory.

        Args:
            skill: Skill info from registry.
            target_dir: Directory to download to.

        Returns:
            Path to downloaded skill directory, or None on failure.
        """
        skill_dir = target_dir / skill.slug
        skill_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Download skill content
            resp = self._client.get(skill.download_url)
            resp.raise_for_status()

            # Verify hash if provided
            if skill.hash:
                content_hash = hashlib.sha256(resp.content).hexdigest()
                if content_hash != skill.hash:
                    logger.error(f"Hash mismatch for {skill.slug}")
                    return None

            # Check if it's a tarball or single file
            content_type = resp.headers.get("content-type", "")

            if "application/zip" in content_type or skill.download_url.endswith(".zip"):
                # Extract ZIP with path traversal protection
                import zipfile
                import io

                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    for member in zf.namelist():
                        member_path = (skill_dir / member).resolve()
                        if not str(member_path).startswith(str(skill_dir.resolve())):
                            raise ValueError(f"ZIP member {member} escapes target dir")
                    zf.extractall(skill_dir)

            elif "application/gzip" in content_type or skill.download_url.endswith(".tar.gz"):
                # Extract tarball with path traversal protection
                import sys
                import tarfile
                import io

                with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
                    if sys.version_info >= (3, 12):
                        tar.extractall(skill_dir, filter="data")
                    else:
                        for member in tar.getmembers():
                            member_path = (skill_dir / member.name).resolve()
                            if not str(member_path).startswith(str(skill_dir.resolve())):
                                raise ValueError(f"Tar member {member.name} escapes target dir")
                        tar.extractall(skill_dir)
            else:
                # Single SKILL.md file or unknown — try ZIP first, fallback to raw
                try:
                    import zipfile
                    import io
                    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                        zf.extractall(skill_dir)
                except zipfile.BadZipFile:
                    (skill_dir / "SKILL.md").write_bytes(resp.content)

            # Write metadata
            meta_path = skill_dir / ".flowly-skill.json"
            meta_path.write_text(json.dumps({
                "slug": skill.slug,
                "version": skill.version,
                "installed_from": skill.download_url,
                "hash": skill.hash,
            }, indent=2), encoding="utf-8")

            logger.info(f"Downloaded skill: {skill.slug} v{skill.version}")
            return skill_dir

        except Exception as e:
            logger.error(f"Failed to download {skill.slug}: {e}")
            return None

    def download_from_github(
        self,
        repo: str,
        path: str = "skills",
        skill_name: str | None = None,
        target_dir: Path | None = None
    ) -> Path | None:
        """
        Download skill(s) from a GitHub repository.

        Args:
            repo: GitHub repo (e.g., "owner/repo")
            path: Path within repo (default: "skills")
            skill_name: Specific skill to download (or all if None)
            target_dir: Target directory for download.

        Returns:
            Path to downloaded skill(s).
        """
        if not target_dir:
            target_dir = get_flowly_home() / "skills"

        target_dir.mkdir(parents=True, exist_ok=True)

        # Parse repo format: owner/repo[@branch]
        branch = "main"
        if "@" in repo:
            repo, branch = repo.rsplit("@", 1)

        try:
            if skill_name:
                # Download single skill
                skill_url = f"{GITHUB_RAW_BASE}/{repo}/{branch}/{path}/{skill_name}/SKILL.md"
                resp = self._client.get(skill_url)
                resp.raise_for_status()

                skill_dir = target_dir / skill_name
                skill_dir.mkdir(parents=True, exist_ok=True)
                (skill_dir / "SKILL.md").write_text(resp.text, encoding="utf-8")

                # Try to download additional files (scripts/, references/)
                self._download_github_extras(repo, branch, path, skill_name, skill_dir)

                logger.info(f"Downloaded {skill_name} from github:{repo}")
                return skill_dir
            else:
                # List and download all skills
                # This requires GitHub API
                logger.warning("Downloading all skills from repo not yet supported")
                return None

        except httpx.HTTPError as e:
            logger.error(f"Failed to download from GitHub: {e}")
            return None

    def _download_github_extras(
        self,
        repo: str,
        branch: str,
        path: str,
        skill_name: str,
        skill_dir: Path
    ) -> None:
        """Try to download extra files from GitHub skill."""
        extras = ["scripts", "references", "assets"]

        for extra in extras:
            try:
                # Try to get directory listing via GitHub API
                # For now, just try common file names
                pass
            except Exception:
                pass

    def parse_skill_source(self, source: str) -> tuple[str, dict]:
        """
        Parse a skill source string.

        Formats:
        - "skill-name" -> registry skill
        - "github:owner/repo/skill-name" -> GitHub (assumes skills/ path)
        - "github:owner/repo/path/to/skill-name" -> GitHub with custom path
        - "./path/to/skill" -> local
        - "https://..." -> URL

        Returns:
            Tuple of (source_type, params)
        """
        if source.startswith("github:"):
            # github:owner/repo/skill-name[@branch]
            # or github:owner/repo/custom/path/skill-name[@branch]
            raw = source[7:]

            # Handle branch suffix
            branch = "main"
            if "@" in raw:
                raw, branch = raw.rsplit("@", 1)

            parts = raw.split("/")
            if len(parts) < 3:
                raise ValueError(f"Invalid GitHub source: {source} (need owner/repo/skill)")

            owner, repo = parts[0], parts[1]
            skill_name = parts[-1]

            # If more than 3 parts, use intermediate parts as path
            if len(parts) > 3:
                path = "/".join(parts[2:-1])
            else:
                path = "skills"  # Default path

            return "github", {
                "repo": f"{owner}/{repo}@{branch}",
                "path": path,
                "skill_name": skill_name,
            }

        elif source.startswith(("http://", "https://")):
            return "url", {"url": source}

        elif source.startswith(("./", "/", "~")):
            return "local", {"path": source}

        else:
            # Registry skill
            # Handle version: skill-name@1.2.3
            version = None
            if "@" in source and not source.startswith("@"):
                source, version = source.rsplit("@", 1)

            return "registry", {"slug": source, "version": version}

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

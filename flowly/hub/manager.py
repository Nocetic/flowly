"""Skill manager for installing, updating, and managing skills."""

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.hub.client import HubClient, SkillInfo
from flowly.profile import get_flowly_home


@dataclass
class InstalledSkill:
    """Information about an installed skill."""

    name: str
    slug: str
    version: str
    source: str
    installed_at: str
    hash: str | None = None
    local_hash: str | None = None
    description: str = ""
    path: Path = field(default_factory=Path)

    @property
    def is_modified(self) -> bool:
        """Check if skill was locally modified."""
        if not self.hash or not self.local_hash:
            return False
        return self.hash != self.local_hash

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "slug": self.slug,
            "version": self.version,
            "source": self.source,
            "installed_at": self.installed_at,
            "hash": self.hash,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict, path: Path) -> "InstalledSkill":
        return cls(
            name=data.get("name", ""),
            slug=data.get("slug", ""),
            version=data.get("version", "1.0.0"),
            source=data.get("source", "unknown"),
            installed_at=data.get("installed_at", ""),
            hash=data.get("hash"),
            description=data.get("description", ""),
            path=path,
        )


class SkillManager:
    """
    Manages skill installation, updates, and removal.

    Skills are installed to:
    - ~/.flowly/skills/ (managed skills - installed via hub)
    - <workspace>/skills/ (workspace skills - highest priority)
    """

    MANAGED_DIR = get_flowly_home() / "skills"
    META_FILE = ".flowly-skill.json"

    def __init__(
        self,
        managed_dir: Path | None = None,
        workspace_dir: Path | None = None,
        registry_url: str | None = None,
    ):
        """
        Initialize skill manager.

        Args:
            managed_dir: Directory for managed skills.
            workspace_dir: Workspace directory (for workspace skills).
            registry_url: Custom registry URL.
        """
        self.managed_dir = managed_dir or self.MANAGED_DIR
        self.workspace_dir = workspace_dir
        self.managed_dir.mkdir(parents=True, exist_ok=True)

        self._client = HubClient(registry_url)

    def list_installed(self, include_workspace: bool = True) -> list[InstalledSkill]:
        """
        List all installed skills.

        Args:
            include_workspace: Include workspace skills.

        Returns:
            List of installed skills.
        """
        skills = []

        # Managed skills
        for skill_dir in self.managed_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                skill = self._load_installed_skill(skill_dir)
                if skill:
                    skills.append(skill)

        # Workspace skills
        if include_workspace and self.workspace_dir:
            ws_skills = self.workspace_dir / "skills"
            if ws_skills.exists():
                for skill_dir in ws_skills.iterdir():
                    if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                        skill = self._load_installed_skill(skill_dir, source="workspace")
                        if skill:
                            skills.append(skill)

        return skills

    def get_installed(self, slug: str) -> InstalledSkill | None:
        """Get an installed skill by slug."""
        # Check managed first
        skill_dir = self.managed_dir / slug
        if skill_dir.exists() and (skill_dir / "SKILL.md").exists():
            return self._load_installed_skill(skill_dir)

        # Check workspace
        if self.workspace_dir:
            ws_dir = self.workspace_dir / "skills" / slug
            if ws_dir.exists() and (ws_dir / "SKILL.md").exists():
                return self._load_installed_skill(ws_dir, source="workspace")

        return None

    def install(
        self,
        source: str,
        force: bool = False,
        to_workspace: bool = False,
    ) -> InstalledSkill | None:
        """
        Install a skill from various sources.

        Args:
            source: Skill source (registry slug, github:..., URL, or local path)
            force: Force reinstall if already exists.
            to_workspace: Install to workspace instead of managed dir.

        Returns:
            Installed skill info, or None on failure.
        """
        source_type, params = self._client.parse_skill_source(source)
        target_dir = (
            (self.workspace_dir / "skills") if to_workspace and self.workspace_dir
            else self.managed_dir
        )
        target_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Installing skill from {source_type}: {source}")

        if source_type == "registry":
            return self._install_from_registry(
                params["slug"], params.get("version"), target_dir, force
            )
        elif source_type == "github":
            return self._install_from_github(
                params["repo"], params.get("path", "skills"),
                params.get("skill_name"), target_dir, force
            )
        elif source_type == "url":
            return self._install_from_url(params["url"], target_dir, force)
        elif source_type == "local":
            return self._install_from_local(params["path"], target_dir, force)
        else:
            logger.error(f"Unknown source type: {source_type}")
            return None

    def _install_from_registry(
        self,
        slug: str,
        version: str | None,
        target_dir: Path,
        force: bool,
    ) -> InstalledSkill | None:
        """Install from the central registry."""
        # Check if already installed
        existing = self.get_installed(slug)
        if existing and not force:
            logger.warning(f"Skill {slug} already installed (use --force to reinstall)")
            return existing

        # Get skill info from registry
        skill_info = self._client.get_skill(slug, version)
        if not skill_info:
            logger.error(f"Skill {slug} not found in registry")
            return None

        # Download
        skill_dir = self._client.download_skill(skill_info, target_dir)
        if not skill_dir:
            return None

        return self._load_installed_skill(skill_dir)

    def _install_from_github(
        self,
        repo: str,
        path: str,
        skill_name: str | None,
        target_dir: Path,
        force: bool,
    ) -> InstalledSkill | None:
        """Install from GitHub repository."""
        if not skill_name:
            logger.error("Skill name required for GitHub install")
            return None

        # Check if already installed
        existing = self.get_installed(skill_name)
        if existing and not force:
            logger.warning(f"Skill {skill_name} already installed (use --force to reinstall)")
            return existing

        # Download from GitHub
        skill_dir = self._client.download_from_github(
            repo, path, skill_name, target_dir
        )
        if not skill_dir:
            return None

        # Create metadata
        self._write_meta(skill_dir, {
            "slug": skill_name,
            "version": "github",
            "source": f"github:{repo}/{path}/{skill_name}",
            "installed_at": datetime.now().isoformat(),
        })

        return self._load_installed_skill(skill_dir)

    def _install_from_url(
        self,
        url: str,
        target_dir: Path,
        force: bool,
    ) -> InstalledSkill | None:
        """Install from direct URL."""
        import httpx

        # Extract skill name from URL
        slug = url.rstrip("/").split("/")[-1]
        if slug.endswith(".md"):
            slug = slug[:-3]
        slug = re.sub(r"[^a-z0-9-]", "-", slug.lower())

        # Check if already installed
        existing = self.get_installed(slug)
        if existing and not force:
            logger.warning(f"Skill {slug} already installed (use --force to reinstall)")
            return existing

        try:
            resp = httpx.get(url, timeout=30.0)
            resp.raise_for_status()

            skill_dir = target_dir / slug
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(resp.text, encoding="utf-8")

            # Create metadata
            self._write_meta(skill_dir, {
                "slug": slug,
                "version": "url",
                "source": url,
                "installed_at": datetime.now().isoformat(),
                "hash": hashlib.sha256(resp.content).hexdigest(),
            })

            logger.info(f"Installed skill from URL: {slug}")
            return self._load_installed_skill(skill_dir)

        except Exception as e:
            logger.error(f"Failed to install from URL: {e}")
            return None

    def _install_from_local(
        self,
        path: str,
        target_dir: Path,
        force: bool,
    ) -> InstalledSkill | None:
        """Install from local path."""
        source_path = Path(path).expanduser().resolve()

        if not source_path.exists():
            logger.error(f"Path not found: {source_path}")
            return None

        # Determine skill name
        if source_path.is_file() and source_path.name == "SKILL.md":
            slug = source_path.parent.name
            source_dir = source_path.parent
        elif source_path.is_dir() and (source_path / "SKILL.md").exists():
            slug = source_path.name
            source_dir = source_path
        else:
            logger.error(f"Invalid skill path: {source_path}")
            return None

        # Check if already installed
        existing = self.get_installed(slug)
        if existing and not force:
            logger.warning(f"Skill {slug} already installed (use --force to reinstall)")
            return existing

        # Copy to target
        target_skill = target_dir / slug
        if target_skill.exists():
            shutil.rmtree(target_skill)

        shutil.copytree(source_dir, target_skill)

        # Create metadata
        self._write_meta(target_skill, {
            "slug": slug,
            "version": "local",
            "source": str(source_path),
            "installed_at": datetime.now().isoformat(),
        })

        logger.info(f"Installed skill from local path: {slug}")
        return self._load_installed_skill(target_skill)

    def update(
        self,
        slug: str | None = None,
        force: bool = False,
    ) -> list[InstalledSkill]:
        """
        Update installed skill(s).

        Args:
            slug: Specific skill to update (or all if None).
            force: Force update even if locally modified.

        Returns:
            List of updated skills.
        """
        updated = []

        if slug:
            skills = [self.get_installed(slug)] if self.get_installed(slug) else []
        else:
            skills = self.list_installed(include_workspace=False)

        for skill in skills:
            if not skill:
                continue

            # Skip workspace skills
            if "workspace" in str(skill.path):
                continue

            # Check for local modifications
            if skill.is_modified and not force:
                logger.warning(
                    f"Skill {skill.slug} has local modifications (use --force to overwrite)"
                )
                continue

            # Re-install from source
            result = self.install(skill.source, force=True)
            if result:
                updated.append(result)

        return updated

    def remove(self, slug: str, from_workspace: bool = False) -> bool:
        """
        Remove an installed skill.

        Args:
            slug: Skill slug to remove.
            from_workspace: Remove from workspace instead of managed.

        Returns:
            True if removed, False otherwise.
        """
        if from_workspace and self.workspace_dir:
            skill_dir = self.workspace_dir / "skills" / slug
        else:
            skill_dir = self.managed_dir / slug

        if not skill_dir.exists():
            logger.warning(f"Skill {slug} not found")
            return False

        try:
            shutil.rmtree(skill_dir)
            logger.info(f"Removed skill: {slug}")
            return True
        except Exception as e:
            logger.error(f"Failed to remove {slug}: {e}")
            return False

    def search(self, query: str) -> list[SkillInfo]:
        """Search for skills in the registry."""
        return self._client.search(query)

    def info(self, slug: str) -> dict[str, Any] | None:
        """
        Get detailed info about a skill.

        Checks both installed and registry.
        """
        # Check installed
        installed = self.get_installed(slug)

        # Check registry
        registry_info = self._client.get_skill(slug)

        if not installed and not registry_info:
            return None

        result: dict[str, Any] = {"slug": slug}

        if installed:
            result["installed"] = True
            result["version"] = installed.version
            result["source"] = installed.source
            result["path"] = str(installed.path)
            result["modified"] = installed.is_modified

        if registry_info:
            result["registry"] = {
                "name": registry_info.name,
                "description": registry_info.description,
                "version": registry_info.version,
                "author": registry_info.author,
                "homepage": registry_info.homepage,
            }

            if installed and installed.version != registry_info.version:
                result["update_available"] = registry_info.version

        return result

    def _load_installed_skill(
        self,
        skill_dir: Path,
        source: str = "managed",
    ) -> InstalledSkill | None:
        """Load installed skill info from directory."""
        skill_file = skill_dir / "SKILL.md"
        meta_file = skill_dir / self.META_FILE

        if not skill_file.exists():
            return None

        # Read metadata if exists
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

        # Parse frontmatter from SKILL.md
        content = skill_file.read_text(encoding="utf-8")
        frontmatter = self._parse_frontmatter(content)

        # Calculate current hash
        local_hash = hashlib.sha256(content.encode()).hexdigest()

        return InstalledSkill(
            name=frontmatter.get("name", skill_dir.name),
            slug=meta.get("slug", skill_dir.name),
            version=meta.get("version", "unknown"),
            source=meta.get("source", source),
            installed_at=meta.get("installed_at", ""),
            hash=meta.get("hash"),
            local_hash=local_hash,
            description=frontmatter.get("description", ""),
            path=skill_dir,
        )

    def _parse_frontmatter(self, content: str) -> dict:
        """Parse YAML frontmatter from SKILL.md content."""
        if not content.startswith("---"):
            return {}

        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            return {}

        result = {}
        for line in match.group(1).split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                result[key.strip()] = value.strip().strip("\"'")

        return result

    def _write_meta(self, skill_dir: Path, meta: dict) -> None:
        """Write skill metadata file."""
        meta_file = skill_dir / self.META_FILE
        meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def close(self):
        """Close the hub client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

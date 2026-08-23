from __future__ import annotations

from dataclasses import dataclass

import pytest

from flowly.channels import feature_rpc


@dataclass
class _Installed:
    slug: str

    def to_dict(self):
        return {"slug": self.slug, "name": self.slug, "version": "test"}


class _Manager:
    created: list[tuple] = []
    removed: list[str] = []

    def __init__(self, *, managed_dir, workspace_dir):
        self.created.append((managed_dir, workspace_dir))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def install(self, source, force=False):
        return _Installed(source) if source != "missing" else None

    def remove(self, slug):
        self.removed.append(slug)
        return slug == "managed-skill"


@pytest.fixture(autouse=True)
def _skill_manager(monkeypatch, tmp_path):
    from flowly.agent import skills
    from flowly.hub import manager

    _Manager.created.clear()
    _Manager.removed.clear()
    monkeypatch.setattr(manager, "SkillManager", _Manager)
    monkeypatch.setattr(feature_rpc, "get_flowly_home", lambda: tmp_path)
    monkeypatch.setattr(skills, "clear_skills_snapshot", lambda: None)


async def test_skills_install_is_scoped_to_the_active_profile_home(tmp_path):
    result = await feature_rpc.skills_install({"source": "research", "restart": False})

    assert result["skill"]["slug"] == "research"
    assert result["willRestart"] is False
    assert _Manager.created == [(tmp_path / "skills", tmp_path / "workspace")]


async def test_skills_remove_only_reports_a_real_managed_removal():
    result = await feature_rpc.skills_remove({"slug": "managed-skill", "restart": False})
    assert result == {"ok": True, "willRestart": False}

    with pytest.raises(feature_rpc.FeatureRpcError, match="managed skill not found"):
        await feature_rpc.skills_remove({"slug": "bundled-skill", "restart": False})

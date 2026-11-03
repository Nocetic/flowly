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

    def __init__(self, *, managed_dir, workspace_dir):
        self.created.append((managed_dir, workspace_dir))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def install(self, source, force=False):
        return _Installed(source) if source != "missing" else None



@pytest.fixture(autouse=True)
def _skill_manager(monkeypatch, tmp_path):
    from flowly.agent import skills
    from flowly.hub import manager

    _Manager.created.clear()
    monkeypatch.setattr(manager, "SkillManager", _Manager)
    monkeypatch.setattr(feature_rpc, "get_flowly_home", lambda: tmp_path)
    monkeypatch.setattr(skills, "clear_skills_snapshot", lambda: None)


async def test_skills_install_is_scoped_to_the_active_profile_home(tmp_path):
    result = await feature_rpc.skills_install({"source": "research", "restart": False})

    assert result["skill"]["slug"] == "research"
    assert result["willRestart"] is False
    assert _Manager.created == [(tmp_path / "skills", tmp_path / "workspace")]


async def test_a_blocked_skill_is_reported_with_its_reason(monkeypatch):
    from flowly.hub import guard

    def _blocked(self, source, force=False):
        raise guard.SkillBlocked(guard.ScanResult(source, guard.TRUST_REGISTRY, guard.DANGEROUS, []),
                                 "Blocked: trusted source, dangerous (0 findings).")

    monkeypatch.setattr(_Manager, "install", _blocked)
    with pytest.raises(feature_rpc.FeatureRpcError) as raised:
        await feature_rpc.skills_install({"source": "evil", "restart": False})
    assert raised.value.code == "SKILL_BLOCKED"
    assert "dangerous" in raised.value.message

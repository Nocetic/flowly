"""Tests for the pet display config + pet constants."""

import json
from pathlib import Path

from flowly.config.loader import load_config, save_config
from flowly.config.schema import Config, PetDisplayConfig
from flowly.pet import constants


class TestPetConstants:
    def test_clamp_scale_within_range(self):
        assert constants.clamp_scale(0.5) == 0.5

    def test_clamp_scale_below_min(self):
        assert constants.clamp_scale(0.0) == constants.SCALE_MIN

    def test_clamp_scale_above_max(self):
        assert constants.clamp_scale(99.0) == constants.SCALE_MAX

    def test_default_state_is_known(self):
        assert constants.DEFAULT_STATE in constants.PET_STATES


class TestPetDisplayConfig:
    def test_defaults_off(self):
        cfg = Config()
        assert cfg.display.pet.enabled is False
        assert cfg.display.pet.slug == ""
        assert cfg.display.pet.scale == 0.33

    def test_scale_clamped_on_construct(self):
        assert PetDisplayConfig(scale=99.0).scale == 3.0
        assert PetDisplayConfig(scale=0.0).scale == 0.1

    def test_roundtrip(self, tmp_path: Path):
        original = Config()
        original.display.pet.enabled = True
        original.display.pet.slug = "otter"
        original.display.pet.scale = 0.5

        config_file = tmp_path / "config.json"
        save_config(original, config_file)
        loaded = load_config(config_file)

        assert loaded.display.pet.enabled is True
        assert loaded.display.pet.slug == "otter"
        assert loaded.display.pet.scale == 0.5

    def test_save_preserves_unknown_fields(self, tmp_path: Path):
        """Writing display.pet must not drop unrelated/unknown config keys —
        the whole pet plan relies on save_config's read-modify-write merge."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "providers": {"openrouter": {"apiKey": "sk-precious"}},
            "someFutureSection": {"keepMe": 1},
        }))

        cfg = load_config(config_file)
        cfg.display.pet.enabled = True
        cfg.display.pet.slug = "cat"
        save_config(cfg, config_file)

        data = json.loads(config_file.read_text())
        assert data["display"]["pet"]["enabled"] is True
        assert data["display"]["pet"]["slug"] == "cat"
        # unknown section + unrelated provider key survive the write
        assert data["someFutureSection"] == {"keepMe": 1}
        assert data["providers"]["openrouter"]["apiKey"] == "sk-precious"

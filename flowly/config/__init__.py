"""Configuration module for flowly."""

from flowly.config.loader import load_config, get_config_path
from flowly.config.schema import Config

__all__ = ["Config", "load_config", "get_config_path"]

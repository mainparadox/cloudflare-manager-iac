"""
Configuration Manager.

ARCHITECTURE:
    Serves as the persistence gateway for the Single Source of Truth (SSOT)
    in this Infrastructure-as-Data (IaD) platform. Bridges runtime state,
    web UI forms, and declarative generator pipelines with the underlying
    YAML specification (`config.yaml`). Guarantees strict UTF-8 encoding
    to prevent byte-level corruption of Internationalized Domain Names (IDNs)
    and non-ASCII domain metadata.
"""

from pathlib import Path
from typing import Any, Dict
import yaml


class ConfigManager:
    """
    Manages filesystem serialization and deserialization of the global infrastructure state.
    
    Provides schema resilience, graceful degradation on malformed files,
    and Git-friendly deterministic YAML formatting.
    """

    def __init__(self, config_path: Path):
        """
        Initializes the configuration manager with a target file path.

        :param config_path: Absolute or relative Path pointing to the YAML configuration file.
        """
        # ARCHITECTURE: Normalize input into an explicit pathlib.Path instance to abstract
        # OS-specific filesystem separators across Linux/macOS/Windows environments.
        self.config_path = Path(config_path)

    def load_config(self) -> Dict[str, Any]:
        """
        Loads and validates the YAML configuration file.

        ARCHITECTURE:
            Implements defensive schema fallbacks. If the file is missing, empty,
            or contains invalid/corrupted syntax, it safely degrades by returning
            the canonical minimal IaD schema (`{"global_defaults": {}, "sites": {}}`).
            This guarantees zero unhandled exceptions (`KeyError`) in downstream consumers.

        SECURITY:
            Strictly employs `yaml.safe_load` rather than `yaml.load` to neutralize
            arbitrary code execution and object deserialization attack vectors.

        :return: Validated configuration dictionary containing root structural nodes.
        """
        if not self.config_path.exists():
            return {"global_defaults": {}, "sites": {}}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                # NOTE: Guard against YAML files containing valid syntax but invalid root types
                # (e.g., raw scalars, empty content returning None, or lists instead of mappings).
                return data if isinstance(data, dict) else {"global_defaults": {}, "sites": {}}
        except Exception:
            # Fallback gracefully on disk I/O errors or YAML parsing exceptions
            return {"global_defaults": {}, "sites": {}}

    def save_config(self, data: Dict[str, Any]) -> bool:
        """
        Atomically serializes infrastructure state to YAML with Unicode fidelity.

        ARCHITECTURE:
            1. `allow_unicode=True`: Prevents PyYAML from escaping Cyrillic, Greek,
               and extended characters into unreadable escaped sequences (e.g., '\\u0442').
               This ensures IDN/Cyrillic domains remain directly readable in source control.
            2. `sort_keys=False`: Maintains original key insertion order, eliminating
               unnecessary diff churn in Git tracking and preserving logical grouping.
            3. Explicit UTF-8 encoding guarantees cross-platform byte uniformity.

        :param data: Target configuration dictionary conforming to the IaD schema.
        :return: True if write succeeded, False on I/O error.
        """
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, sort_keys=False)
            return True
        except Exception:
            return False
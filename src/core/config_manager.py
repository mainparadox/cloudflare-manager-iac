"""
Configuration Manager.

ARCHITECTURE:
    Serves as the persistence gateway for the Single Source of Truth (SSOT).
    Guarantees thread-safe in-memory operations and ACID-like atomic filesystem writes
    via temporary file substitution (os.replace). This prevents file corruption 
    (0-byte truncation) in case of unexpected process termination or OS crash during I/O.
"""

import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Dict
import yaml


class ConfigManager:
    """
    Manages filesystem serialization and deserialization of the global infrastructure state.
    
    Provides schema resilience, thread-safety, and crash-proof atomic persistence.
    """

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        # ARCHITECTURE: Thread lock prevents race conditions within Streamlit's multi-threaded runtime
        self._lock = threading.Lock()

    def load_config(self) -> Dict[str, Any]:
        """Loads and validates the YAML configuration file."""
        with self._lock:
            if not self.config_path.exists():
                return {"global_defaults": {}, "sites": {}}
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    return data if isinstance(data, dict) else {"global_defaults": {}, "sites": {}}
            except Exception:
                return {"global_defaults": {}, "sites": {}}

    def save_config(self, data: Dict[str, Any]) -> bool:
        """
        Atomically serializes infrastructure state to YAML.

        ARCHITECTURE (Crash-Proof Atomic Write):
            Instead of writing directly to `config.yaml` (which truncates the file to 0 bytes 
            on start), data is written to a temporary sibling file and flushed to disk (`fsync`). 
            Then, `os.replace` executes an atomic filesystem pointer swap. If the process is killed 
            mid-write, the existing `config.yaml` remains 100% intact and uncorrupted.
        """
        with self._lock:
            try:
                parent_dir = self.config_path.parent
                parent_dir.mkdir(parents=True, exist_ok=True)

                # Create temp file in the same directory to ensure it resides on the same filesystem/mount
                with tempfile.NamedTemporaryFile("w", dir=parent_dir, delete=False, encoding="utf-8") as tf:
                    temp_name = tf.name
                    yaml.dump(data, tf, allow_unicode=True, sort_keys=False)
                    tf.flush()
                    os.fsync(tf.fileno())  # Force OS to write buffers directly to physical disk

                # Atomic swap: replaces target file instantaneously
                os.replace(temp_name, self.config_path)
                return True
            except Exception:
                # Cleanup orphaned temp file if dump or fsync failed
                if 'temp_name' in locals() and os.path.exists(temp_name):
                    try:
                        os.unlink(temp_name)
                    except OSError:
                        pass
                return False
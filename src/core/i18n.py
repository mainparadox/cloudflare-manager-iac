"""
Localization (i18n) Engine.

ARCHITECTURE:
    Provides a decoupled, lightweight internationalization subsystem tailored for
    declarative UI platforms (e.g., Streamlit). Avoids the operational overhead,
    compilation steps, and binary dependencies of GNU gettext/Babel (`.po`/`.mo` files)
    by managing human-readable YAML translation dictionaries directly in memory.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml


class I18n:
    """
    In-memory translation registry and dot-notation string resolver.
    
    Guarantees resilient rendering with graceful fallbacks, ensuring UI availability
    even when dictionary keys are partially missing across different language packs.
    """

    def __init__(self, locales_dir: Path, default_locale: str = "en"):
        """
        Initializes the translation registry and triggers immediate catalog loading.

        :param locales_dir: Directory path containing `{locale}.yaml` translation files.
        :param default_locale: Canonical fallback ISO language code (defaults to 'en').
        """
        self.locales_dir = Path(locales_dir)
        self.default_locale = default_locale
        self.locale = default_locale
        # ARCHITECTURE: Translation dictionary schema: {locale_code: {nested_key: value}}
        self.translations: Dict[str, Dict[str, Any]] = {}
        self.reload()

    def reload(self):
        """
        Discovers and reloads all locale definitions from the locales directory.

        ARCHITECTURE:
            Supports zero-restart catalog updates (hot reloading). Iterates over
            available YAML manifests using globbing. Implements isolated fault tolerance:
            if a single language manifest is corrupt, it degrades to an empty mapping
            without failing the initialization of other languages or crashing the app.
        """
        if not self.locales_dir.exists():
            return
        for file_path in self.locales_dir.glob("*.yaml"):
            lang = file_path.stem
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    # SECURITY: Enforce safe YAML parsing to prevent code execution
                    # if locale files are sourced dynamically or modified externally.
                    self.translations[lang] = yaml.safe_load(f) or {}
            except Exception:
                # Isolate translation load failure to the specific corrupted language file
                self.translations[lang] = {}

    def set_locale(self, locale: str):
        """
        Switches the active rendering locale at runtime.

        :param locale: Target language code to activate.
        """
        # NOTE: State-guard preventing the UI from switching to an unloaded or invalid locale
        if locale in self.translations:
            self.locale = locale

    def _resolve(self, locale: str, keys: List[str]) -> Optional[Any]:
        """
        Traverses a nested dictionary tree for a given locale hierarchy.

        :param locale: Target language catalog identifier.
        :param keys: Ordered list of path components.
        :return: Extracted value or None if resolution path is severed.
        """
        current = self.translations.get(locale, {})
        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return None
        return current

    def t(self, key_path: str, default: str = "") -> str:
        """
        Resolves a localized string using dot-separated hierarchical notation.

        ARCHITECTURE (Never-Crash Cascading Fallback):
            Traverses localization catalogs through a deterministic priority chain:
            1. Target value in active runtime locale (`self.locale`).
            2. Upstream fallback in base canonical locale (`self.default_locale`),
               preventing unrendered placeholder keys when secondary translation catalogs lag behind.
            3. Explicit `default` string passed by caller.
            4. Raw `key_path` literal, ensuring deterministic operational visibility.

        :param key_path: Dot-delimited path into the YAML tree (e.g., 'actions.purge_cache').
        :param default: Fallback text if the key path is unresolved across catalogs.
        :return: Resolved translated string or fallback identifier.
        """
        keys = key_path.split(".")

        val = self._resolve(self.locale, keys)
        if val is not None:
            return str(val)

        if self.locale != self.default_locale:
            val = self._resolve(self.default_locale, keys)
            if val is not None:
                return str(val)

        return default if default else key_path
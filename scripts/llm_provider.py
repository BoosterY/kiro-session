"""LLM Provider — abstraction layer with auto-detect and fallback."""
import subprocess
import shutil
import json
from config import load_config, get


class LLMProvider:
    def query(self, prompt: str, timeout: int = 60) -> str | None:
        raise NotImplementedError

    def is_available(self) -> bool:
        raise NotImplementedError

    @property
    def name(self) -> str:
        return self.__class__.__name__


class KiroProvider(LLMProvider):
    """Use kiro-cli headless mode."""

    def is_available(self) -> bool:
        return shutil.which("kiro-cli") is not None

    def query(self, prompt: str, timeout: int = 60) -> str | None:
        import uuid
        marker = f"__ks_{uuid.uuid4().hex[:12]}__"
        tagged_prompt = f"{prompt}\n\n[internal-marker: {marker}]"
        try:
            result = subprocess.run(
                ["kiro-cli", "chat", "--no-interactive", tagged_prompt],
                capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                return None
            import re
            text = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
            self._cleanup_by_marker(marker)
            return text.strip() or None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    @staticmethod
    def _cleanup_by_marker(marker: str):
        """Delete the session containing our unique marker."""
        try:
            import sqlite3
            from pathlib import Path
            db = Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3"
            conn = sqlite3.connect(str(db), timeout=5)
            rows = conn.execute(
                "SELECT conversation_id, value FROM conversations_v2 WHERE value LIKE ?",
                (f"%{marker}%",)
            ).fetchall()
            conn.close()
            for cid, _ in rows:
                subprocess.run(
                    ["kiro-cli", "chat", "--delete-session", cid],
                    capture_output=True, timeout=10,
                )
        except Exception:
            pass


class OllamaProvider(LLMProvider):
    """Use local ollama instance."""

    def __init__(self, model: str = "llama3.2"):
        self.model = model

    def is_available(self) -> bool:
        if not shutil.which("ollama"):
            return False
        try:
            r = subprocess.run(["ollama", "list"], capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    def query(self, prompt: str, timeout: int = 60) -> str | None:
        try:
            result = subprocess.run(
                ["ollama", "run", self.model, prompt],
                capture_output=True, text=True, timeout=timeout,
            )
            return result.stdout.strip() or None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None


class NoneProvider(LLMProvider):
    """Degraded mode — no LLM available."""

    def is_available(self) -> bool:
        return True

    def query(self, prompt: str, timeout: int = 60) -> str | None:
        return None


# Provider registry (priority order)
_PROVIDERS = {
    "kiro": KiroProvider,
    "ollama": OllamaProvider,
    "none": NoneProvider,
}


def get_provider() -> LLMProvider:
    """Get LLM provider based on config, with auto-detect fallback."""
    cfg = load_config()
    choice = get(cfg, "llm.provider") or "auto"

    if choice != "auto" and choice in _PROVIDERS:
        p = _PROVIDERS[choice]()
        if p.is_available():
            return p

    # Auto-detect: try in priority order
    if choice == "auto":
        for name in ("kiro", "ollama"):
            p = _PROVIDERS[name]()
            if p.is_available():
                return p

    return NoneProvider()

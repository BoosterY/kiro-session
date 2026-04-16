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
        try:
            result = subprocess.run(
                ["kiro-cli", "chat", "--no-interactive", prompt],
                capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                return None
            # Strip ANSI codes
            import re
            text = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
            # Clean up the garbage session kiro-cli just created
            self._cleanup_garbage(prompt)
            return text.strip() or None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    @staticmethod
    def _cleanup_garbage(prompt: str):
        """Delete the session kiro-cli created for this headless call."""
        marker = prompt[:80]
        try:
            # Check SQLite
            import sqlite3
            from pathlib import Path
            db = Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3"
            conn = sqlite3.connect(str(db), timeout=5)
            rows = conn.execute(
                "SELECT conversation_id, value FROM conversations_v2 ORDER BY updated_at DESC LIMIT 5"
            ).fetchall()
            for cid, val in rows:
                if marker in val:
                    subprocess.run(
                        ["kiro-cli", "chat", "--delete-session", cid],
                        capture_output=True, timeout=10,
                    )
                    return
            conn.close()
        except Exception:
            pass
        try:
            # Check JSONL files
            from pathlib import Path
            sessions_dir = Path.home() / ".kiro" / "sessions" / "cli"
            if not sessions_dir.exists():
                return
            import json as _json
            # Check most recent JSONL files
            jsonl_files = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
            for jf in jsonl_files:
                with open(jf) as f:
                    first_line = f.readline()
                if marker in first_line:
                    sid = jf.stem
                    subprocess.run(
                        ["kiro-cli", "chat", "--delete-session", sid],
                        capture_output=True, timeout=10,
                    )
                    return
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

"""LLM Provider — abstraction layer with auto-detect and fallback."""
import subprocess
import shutil
import json
from pathlib import Path
from config import load_config, get


class LLMProvider:
    def query(self, prompt: str, timeout: int = 60) -> str | None:
        raise NotImplementedError

    def query_resume(self, prompt: str, timeout: int = 60) -> str | None:
        """Continue in the same session. Default: falls back to query()."""
        return self.query(prompt, timeout)

    def cleanup(self):
        """Clean up any resources. Default: no-op."""
        pass

    def is_available(self) -> bool:
        raise NotImplementedError

    @property
    def name(self) -> str:
        return self.__class__.__name__


class KiroProvider(LLMProvider):
    """Use kiro-cli headless mode with isolated cwd for reliable cleanup."""

    _SANDBOX = Path.home() / ".kiro" / "skills" / "session-manager" / "llm-sandbox"

    def is_available(self) -> bool:
        return shutil.which("kiro-cli") is not None

    def query(self, prompt: str, timeout: int = 60) -> str | None:
        import re

        self._SANDBOX.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["kiro-cli", "chat", "--no-interactive", prompt],
                capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL, cwd=str(self._SANDBOX),
            )
            if result.returncode != 0:
                return None
            text = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
            return text.strip() or None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        finally:
            self.cleanup()

    def query_resume(self, prompt: str, timeout: int = 60) -> str | None:
        """Continue in the sandbox session using --resume. No cleanup."""
        import re

        self._SANDBOX.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["kiro-cli", "chat", "--no-interactive", "--resume", prompt],
                capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL, cwd=str(self._SANDBOX),
            )
            if result.returncode != 0:
                return None
            text = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
            return text.strip() or None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    def cleanup(self):
        """Delete all sessions created under the sandbox dir."""
        sandbox = str(self._SANDBOX)
        try:
            import sqlite3
            from pathlib import Path
            db = Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3"
            conn = sqlite3.connect(str(db), timeout=5)
            # Match exact path or parent (kiro-cli may resolve/shorten the cwd)
            rows = conn.execute(
                "SELECT conversation_id FROM conversations_v2 WHERE key = ? OR key LIKE ?",
                (sandbox, sandbox + "%")
            ).fetchall()
            conn.close()
            for (cid,) in rows:
                subprocess.run(
                    ["kiro-cli", "chat", "--delete-session", cid],
                    capture_output=True, timeout=10,
                )
        except Exception:
            pass
        # Also clean v2 JSONL sessions
        try:
            from pathlib import Path
            import json as _json
            sessions_dir = Path.home() / ".kiro" / "sessions" / "cli"
            if sessions_dir.exists():
                for meta_file in sessions_dir.glob("*.json"):
                    try:
                        with open(meta_file) as f:
                            meta = _json.load(f)
                        cwd = meta.get("cwd", "")
                        if cwd == sandbox or cwd.startswith(sandbox):
                            sid = meta.get("session_id", meta_file.stem)
                            subprocess.run(
                                ["kiro-cli", "chat", "--delete-session", sid],
                                capture_output=True, timeout=10,
                            )
                    except Exception:
                        continue
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
